"""Offline unit tests for 0.6.1 kit fixes: toolchain fallback, ARM table bounds, Mach-O export pruning, adb check."""
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

TOOLS = Path(__file__).resolve().parents[1]
RECOMP_TESTS = TOOLS.parent / 'recomp' / 'tests'
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(RECOMP_TESTS))

import android  # noqa: E402
import inventory  # noqa: E402
import macho_exports  # noqa: E402
import smoke_module  # noqa: E402
import toolchain  # noqa: E402
import vitaelf  # noqa: E402


class ToolchainObtainTests(unittest.TestCase):
    def test_slow_primary_retried_without_floor_after_mirror_fails(self):
        payload_bytes = b'hello zig archive'
        payload = {
            'file': 'zig.zip',
            'url': 'https://primary.example/zig.zip',
            'mirrors': ['https://mirror.example/zig.zip'],
            'size': len(payload_bytes),
            'sha256': hashlib.sha256(payload_bytes).hexdigest(),
        }
        calls = []

        def fake_get(url, path, min_speed=0):
            calls.append((url, min_speed))
            if url == payload['url'] and min_speed > 0:
                Path(path).write_bytes(b'partial')
                raise toolchain.SlowDownload('slower than 256 KB/s')
            if url == payload['mirrors'][0]:
                raise urllib.error.URLError('mirror down')
            Path(path).write_bytes(payload_bytes)

        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'zig.zip')
            with mock.patch.object(toolchain.offline, 'provide', return_value=False), \
                 mock.patch.object(toolchain, 'get', side_effect=fake_get):
                toolchain.obtain(payload, dest)
            self.assertEqual(Path(dest).read_bytes(), payload_bytes)
        self.assertEqual(calls, [
            ('https://primary.example/zig.zip', toolchain.MIN_SPEED),
            ('https://mirror.example/zig.zip', 0),
            ('https://primary.example/zig.zip', 0),
        ])

    def test_windows_curl_omits_retry_when_speed_limited(self):
        cert_err = urllib.error.URLError('CERTIFICATE_VERIFY_FAILED')
        seen_cmds = []

        def fake_run(cmd, **kwargs):
            seen_cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 28, '', 'Operation too slow')

        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'file.bin')
            with mock.patch.object(toolchain.urllib.request, 'urlopen', side_effect=cert_err), \
                 mock.patch.object(toolchain, 'windows_curl', return_value='curl.exe'), \
                 mock.patch.object(toolchain.subprocess, 'run', side_effect=fake_run):
                with self.assertRaises(toolchain.SlowDownload):
                    toolchain.get('https://primary.example/file.bin', dest, min_speed=toolchain.MIN_SPEED)
                with self.assertRaises(OSError):
                    toolchain.get('https://primary.example/file.bin', dest, min_speed=0)

        self.assertIn('--speed-limit', seen_cmds[0])
        self.assertNotIn('--retry', seen_cmds[0])
        self.assertIn('--retry', seen_cmds[1])
        self.assertNotIn('--speed-limit', seen_cmds[1])


class InventoryArmTableTests(unittest.TestCase):
    def test_decode_arm_table_checks_register_and_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            elf_path = Path(tmp) / 'smoke.elf'
            smoke_module.write_fixture(elf_path)
            inv = inventory.Inventory(vitaelf.load(elf_path))
            work = []
            self.assertFalse(inv.decode_arm_table(
                smoke_module.BASE, smoke_module.BASE, smoke_module.BASE + 64,
                'addls', 'pc, pc, r2, lsl #2', ('r1', 1), work))
            self.assertFalse(inv.decode_arm_table(
                smoke_module.BASE, smoke_module.BASE, smoke_module.BASE + 64,
                'addls', 'pc, pc, r2, lsl #2', ('r2', -1), work))
            self.assertFalse(inv.decode_arm_table(
                smoke_module.BASE, smoke_module.BASE, smoke_module.BASE + 64,
                'addls', 'pc, pc, r2, lsl #2', ('r2', 5000), work))
            self.assertEqual(work, [])
            self.assertEqual(inv.jumptables, [])

            self.assertTrue(inv.decode_arm_table(
                smoke_module.BASE, smoke_module.BASE, smoke_module.BASE + 64,
                'addls', 'pc, pc, r2, lsl #2', ('r2', 1), work))
            self.assertEqual(inv.jumptables[0]['count'], 2)
            self.assertEqual({entry[0] for entry in work}, {smoke_module.BASE + 8, smoke_module.BASE + 12})


class MachoExportsTests(unittest.TestCase):
    def test_restrict_exports_keeps_only_requested_aot_symbols(self):
        # Synthetic trie with three exports: _armrecomp_aot_features, _armrecomp_aot_init, _extra
        leaves = [b'\x02\x00\x10\x00', b'\x02\x00\x20\x00', b'\x02\x00\x30\x00']
        names = [b'_armrecomp_aot_features', b'_armrecomp_aot_init', b'_extra']
        root = bytearray(b'\x00\x03')
        offset = len(root) + sum(len(n) + 2 for n in names)
        for name, leaf in zip(names, leaves):
            root += name + bytes((0, offset))
            offset += len(leaf)
        orig_trie = bytes(root + b''.join(leaves))
        header = struct.pack('<8I', 0xfeedfacf, 0x0100000c, 0, 6, 1, 16, 0, 0)
        load_cmd = struct.pack('<4I', 0x80000033, 16, 48, len(orig_trie))
        macho = header + load_cmd + orig_trie

        with tempfile.TemporaryDirectory() as tmp:
            dylib = Path(tmp) / 'test.dylib'
            dylib.write_bytes(macho)
            macho_exports.restrict_exports(dylib, toolchain.MACHO_EXPORTS)
            updated = dylib.read_bytes()
            new_trie = updated[48:48 + len(orig_trie)]
            self.assertEqual(macho_exports._find_export(new_trie, b'_armrecomp_aot_features'), leaves[0])
            self.assertEqual(macho_exports._find_export(new_trie, b'_armrecomp_aot_init'), leaves[1])
            with self.assertRaises(ValueError):
                macho_exports._find_export(new_trie, b'_extra')


class AdbServerCheckTests(unittest.TestCase):
    def test_env_var_marks_adb_server_in_use(self):
        with mock.patch.dict(os.environ, {'ADB_SERVER_SOCKET': 'tcp:127.0.0.1:5037'}, clear=False):
            self.assertTrue(android.adb_server_in_use())


if __name__ == '__main__':
    unittest.main()
