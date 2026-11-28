"""Build a synthetic Vita ELF with the release recompiler and zig, then check its host ABI."""
import argparse
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

RECOMP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECOMP.parent / 'tools'))
import inventory  # noqa: E402
import msvc  # noqa: E402
import toolchain  # noqa: E402
import vitaelf  # noqa: E402

BASE = 0x1000
RETURN = bytes.fromhex('1e ff 2f e1')  # bx lr
FUNCTIONS = (
    ('vuzp.16 q0, q1', '42 01 b6 f3'),
    ('vcvt.s32.f32 s8, s8, #8', 'cc 4a be ee'),
    ('vcvt.f32.s32 s8, s8, #8', 'cc 4a ba ee'),
    ('vcvt.s32.f32 q3, q3, #8', '56 6f b8 f2'),
    ('vcvt.f32.s32 q3, q3, #8', '56 6e b8 f2'),
    ('movw r0, #0; movt r0, #0', '00 00 00 e3 00 00 40 e3'),
)
EXPORTS = {'armrecomp_aot_init', 'armrecomp_aot_features'}


def write_fixture(path):
    code, starts = bytearray(), []
    for _, encoding in FUNCTIONS:
        starts.append(len(code))
        code += bytes.fromhex(encoding) + RETURN
    # One load segment with module info and PREL31 exidx entries, plus a relocation segment.
    # The loader would write the immediate halves at the two ARM MOV sites.
    mod_info = len(code)
    exidx = mod_info + 92
    data = code + struct.pack('<HH28s15I', 0, 1, b'smoke',
                              *([0] * 11), exidx, exidx + 8 * len(starts), 0, 0)
    for i, start in enumerate(starts):
        data += struct.pack('<II', (start - (exidx + 8 * i)) & 0x7fffffff, 1)
    relocations = struct.pack('<III', (2 << 28) | (44 << 20) | (43 << 8), 0, starts[-1])
    offset = 52 + 2 * 32
    ident = b'\x7fELF\x01\x01\x01' + bytes(9)
    header = struct.pack('<16sHHIIIIIHHHHHH', ident, 0xfe04, 40, 1, mod_info, 52, 0, 0x05000000,
                         52, 32, 2, 0, 0, 0)
    load = struct.pack('<8I', 1, offset, BASE, 0, len(data), len(data), 5, 4)
    reloc = struct.pack('<8I', 0x60000000, offset + len(data), 0, 0, len(relocations), 0, 0, 4)
    path.write_bytes(header + load + reloc + data + relocations)


def check_exports(module, android):
    if android or not (toolchain.WINDOWS or toolchain.MACOS):
        readelf = shutil.which('readelf') or shutil.which('llvm-readelf')
        if not readelf:
            raise RuntimeError('readelf (or llvm-readelf) must be on PATH')
        symbols = subprocess.check_output([readelf, '--dyn-syms', '--wide', module], text=True)
        exports = set()
        for line in symbols.splitlines():
            fields = line.split()
            if len(fields) >= 8 and fields[0].endswith(':') and fields[0][:-1].isdigit():
                if fields[4] in ('GLOBAL', 'WEAK') and fields[6] != 'UND':
                    exports.add(fields[7])
        dynamic = subprocess.check_output([readelf, '-dW', module], text=True)
        if 'TEXTREL' in dynamic:
            raise RuntimeError(f'{module} has text relocations')
        if android and (problem := toolchain.check_android_module(module)):
            raise RuntimeError(problem)
    elif toolchain.WINDOWS:
        env = msvc.environment()
        dumpbin = shutil.which('dumpbin', path=env['PATH'])
        if not dumpbin:
            raise RuntimeError('dumpbin not found in the Visual Studio build environment')
        report = subprocess.check_output([dumpbin, '/headers', '/exports', '/relocations', module], env=env, text=True)
        exports = set(re.findall(r'^\s*\d+\s+[0-9A-F]+\s+[0-9A-F]{8}\s+(\w+)', report, re.M))
        if 'Dynamic base' not in report:
            raise RuntimeError(f'{module} does not support relocation at load time')
        text = re.search(r'^\s*\.text name\n\s*([0-9A-F]+) virtual size\n\s*([0-9A-F]+) virtual address', report, re.M)
        if not text:
            raise RuntimeError(f'{module}: cannot locate the PE text section')
        size, start = (int(value, 16) for value in text.groups())
        for block in re.split(r'^\s*([0-9A-F]+) RVA,\s+[0-9A-F]+ SizeOfBlock\n', report, flags=re.M)[1:][::2]:
            if start <= int(block, 16) < start + size:
                raise RuntimeError(f'{module} has text relocations')
    else:
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', module], check=True)
        symbols = subprocess.check_output(['xcrun', 'llvm-objdump', '--macho', '--exports-trie', module], text=True)
        exports = set(re.findall(r'^\s*0x[0-9A-Fa-f]+\s+_(\w+)', symbols, re.M))
        loads = subprocess.check_output(['otool', '-l', module], text=True)
        for section in loads.split('Section\n')[1:]:
            if re.search(r'^\s*segname __TEXT$', section, re.M) and not re.search(r'^\s*nreloc 0$', section, re.M):
                raise RuntimeError(f'{module} has text relocations')
    if exports != EXPORTS:
        raise RuntimeError(f'{module}: expected {sorted(EXPORTS)}, got {sorted(exports)}')


def check_arm_table(elf):
    inv = inventory.Inventory(vitaelf.load(elf))
    work = []
    if inv.decode_arm_table(BASE, BASE, BASE + 64, 'addls', 'pc, pc, r2, lsl #2', ('r1', 1), work):
        raise RuntimeError('ARM table used the bound of a different register')
    if inv.decode_arm_table(BASE, BASE, BASE + 64, 'addls', 'pc, pc, r2, lsl #2', ('r2', -1), work):
        raise RuntimeError('ARM table accepted a non-positive case count')
    if work or inv.jumptables:
        raise RuntimeError('a stale comparison produced ARM jump table entries')
    if not inv.decode_arm_table(BASE, BASE, BASE + 64, 'addls', 'pc, pc, r2, lsl #2', ('r2', 1), work):
        raise RuntimeError('ARM table with a bound on its index was not decoded')
    if inv.jumptables[0]['count'] != 2 or {entry[0] for entry in work} != {BASE + 8, BASE + 12}:
        raise RuntimeError('ARM table with a valid bound has the wrong case targets')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('toolchain', type=Path, help='unpacked release toolchain containing zig/')
    parser.add_argument('--android', action='store_true', help='cross-compile for Android and check the ELF')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='recomp-smoke-') as work:
        work = Path(work)
        elf = work / 'smoke.elf'
        write_fixture(elf)
        check_arm_table(elf)
        generated = work / 'generated'
        subprocess.run([sys.executable, '-I', RECOMP / 'recompile.py', elf, generated / 'smoke'], check=True)
        build = work / 'build'
        toolchain.build_modules(generated, ['smoke'], build, args.toolchain, jobs=2, android=args.android)
        module, = build.glob(toolchain.DLL_PREFIX + 'smoke.*')
        check_exports(module, args.android)
        if not args.android:
            runner = work / ('smoke.exe' if toolchain.WINDOWS else 'smoke')
            env = dict(os.environ, ZIG_GLOBAL_CACHE_DIR=str(work / 'cache'), ZIG_LOCAL_CACHE_DIR=str(work / 'cache'))
            subprocess.run([toolchain.zig_executable(args.toolchain), 'c++', *toolchain.ZIG_TARGET,
                            '-std=c++20', '-I', toolchain.RUNTIME, Path(__file__).with_suffix('.cpp'),
                            *(['-ldl'] if sys.platform.startswith('linux') else []), '-o', runner], env=env, check=True)
            subprocess.run([runner, module], check=True)
        print(f'{module.name}: smoke test passed')


if __name__ == '__main__':
    main()
