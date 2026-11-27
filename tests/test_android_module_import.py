"""Host checks of the actual Android module-import wrapper (no game modules executed)."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile
import types
import unittest
import zipfile


class AndroidModuleImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aot-import-test-")
        self.root = Path(self.temp.name)
        kit = self.root / "buildtools" / "kit"
        kit.mkdir(parents=True)
        source = (Path(__file__).resolve().parents[1] / "tools" / "android-setup.py").read_text(encoding="utf-8")
        def fail(message):
            raise RuntimeError(message)
        self.checked = []
        self.env = dict(KIT=str(kit), os=os, shutil=shutil, json=json, hashlib=hashlib,
                        toolchain=types.SimpleNamespace(DLL_PREFIX="ssd_aot_"),
                        check=lambda *args: self.checked.append(True), say=lambda *args: None,
                        fail=fail, build_on_device=lambda *args: "official compiler")
        code = source[source.index("def user_import_root():"):source.index("def main():")]
        exec(compile(code, "android-import-wrapper", "exec"), self.env)
        self.project = types.SimpleNamespace(modules={"0": "eboot", "1": "libc"}, installed_title=lambda fs: "TEST")
        self.args = types.SimpleNamespace(game=str(self.root / "game"), work=str(self.root / "work"), stamp="test")
        Path(self.args.game).mkdir()
        Path(self.args.work).mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, bad=False):
        header = bytearray(64)
        header[:7] = b"\x7fELF\x02\x01\x01"
        header[16] = 3
        header[18] = 183
        data = bytes(header)
        records = {"ssd_aot_" + name + ".so": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                   for name in self.project.modules.values()}
        if bad:
            next(iter(records.values()))["sha256"] = "0" * 64
        with zipfile.ZipFile(self.root / "arm64-import.zip", "w") as archive:
            archive.writestr("modules.json", json.dumps(records))
            for name in records:
                archive.writestr(name, data)

    def test_without_import_uses_official_compiler(self):
        self.assertEqual(self.env["build"](self.args, self.project), "official compiler")

    def test_import_checks_game_and_installs_complete_set(self):
        self.archive()
        self.env["build"](self.args, self.project)
        self.assertTrue(self.checked)
        self.assertEqual(len(list(Path(self.args.game).glob("*.so"))), 2)
        record = json.loads((Path(self.args.game) / "android-build.json").read_text())
        self.assertEqual(record["source"], "User-imported ARM64")

    def test_bad_digest_preserves_installed_set(self):
        self.archive()
        self.env["build"](self.args, self.project)
        before = {p.name: p.read_bytes() for p in Path(self.args.game).glob("*.so")}
        self.archive(bad=True)
        with self.assertRaises(RuntimeError):
            self.env["build"](self.args, self.project)
        self.assertEqual(before, {p.name: p.read_bytes() for p in Path(self.args.game).glob("*.so")})


if __name__ == "__main__":
    unittest.main()
