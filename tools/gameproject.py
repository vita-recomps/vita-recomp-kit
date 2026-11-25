"""A game project's description (game.json at its root), for the install and packaging scripts.

game.json:
  {
    "id": "shinobido2",                       front end: <id>.exe (Linux: <id>.x86_64, macOS: <id>.arm64),
                                              <id>.ini, <id>/
    "name": "Shinobido 2: Revenge of Zen",
    "title_id": "PCSE00015",
    "other_title_ids": ["PCSB00123"],         the same game in other regions (optional): the
                                              installed one is used; title_id names the game
    "eboot_sha256": {"<sha256>": "1.02"},     the eboot.bin builds the port supports
    "eboot_sha256_other": {"<sha256>": "1.00"},   other builds, named when the setup refuses one (a game
                                              installed without its update)
    "modules": {"eboot": "eboot", "libc": "libc", "sysmodule": "fw_sysmodule"},
                                              module as dumped -> generated/<name> (recompiled)
    "load_bases": {"adhoc_matching": "80757000"},   modules recompiled for another address
    "recomp_overrides": {"eboot": "recomp/eboot-overrides.json"}   fixes to a module's code, passed
                                              to recompile.py --overrides (project paths; a release
                                              carries them, make-setup.py)
    "dump_seconds": 45,                       how long --dump-elfs runs the game
    "dist_readme": "docs/dist-readme.txt",    README.txt of the packaged folder (project path)
    "dist_readme_linux": "docs/dist-readme-linux.txt",   the same on Linux (default: dist_readme)
    "dist_files": {"run-player2.bat": "..."}  more files written into the packaged folder (Windows)
    "dist_files_linux": {"run-player2.sh": "..."}   the same on Linux (written executable)
    "dist_readme_macos", "dist_files_macos"   the same on macOS (default: none of the Linux files)
    "release_files": ["recomplauncher.json"]  project files copied as they are to the top of a
                                              release's folder (make-setup.py), on every platform
    "android_package": "io.github.vitarecomps.shinobido2"   the game's APK (android.py; default
                                              io.github.vitarecomps.<id without other characters>)
  }
"""
import json
import os
import struct
import sys

WINDOWS = sys.platform == 'win32'
MACOS = sys.platform == 'darwin'  # Apple Silicon only (arm64)
# the fork's add_game names it <id>.x86_64 on Linux, <id>.arm64 on macOS
EXE_SUFFIX = '.exe' if WINDOWS else '.arm64' if MACOS else '.x86_64'
MODULE_SUFFIX = '.dll' if WINDOWS else '.dylib' if MACOS else '.so'  # ssd_aot_<module>.dll / .dylib / .so


def ico_to_png(ico, png):
    """The largest PNG-compressed image of a .ico file (Linux desktop entries show PNGs); False if it
    has none."""
    with open(ico, 'rb') as f:
        d = f.read()
    entries = []
    for i in range(struct.unpack_from('<H', d, 4)[0]):
        w, _, _, _, _, _, size, offset = struct.unpack_from('<BBBBHHII', d, 6 + 16 * i)
        if d[offset:offset + 4] == b'\x89PNG':
            entries.append((w or 256, offset, size))
    if not entries:
        return False
    _, offset, size = max(entries)
    with open(png, 'wb') as f:
        f.write(d[offset:offset + size])
    return True


class Project:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        path = os.path.join(self.root, 'game.json')
        if not os.path.exists(path):
            sys.exit(f'no game.json in {self.root} (pass --project)')
        with open(path, encoding='utf-8') as f:
            g = json.load(f)
        self.id = g['id']
        self.name = g['name']
        self.title_id = g['title_id']
        self.title_ids = [self.title_id] + [t for t in g.get('other_title_ids', []) if t != self.title_id]
        self.eboot_sha256 = {k.lower(): v for k, v in g.get('eboot_sha256', {}).items()}
        self.eboot_sha256_other = {k.lower(): v for k, v in g.get('eboot_sha256_other', {}).items()}
        self.modules = g.get('modules', {})
        self.load_bases = g.get('load_bases', {})
        self.recomp_overrides = g.get('recomp_overrides', {})
        self.dump_seconds = g.get('dump_seconds', 45)
        if WINDOWS:
            self.dist_readme, self.dist_files = g.get('dist_readme'), g.get('dist_files', {})
        elif MACOS:
            self.dist_readme, self.dist_files = g.get('dist_readme_macos', g.get('dist_readme')), g.get('dist_files_macos', {})
        else:
            self.dist_readme, self.dist_files = g.get('dist_readme_linux', g.get('dist_readme')), g.get('dist_files_linux', {})
        self.release_files = g.get('release_files', [])
        self.exe_name = self.id + EXE_SUFFIX
        # the game's APK, as game-frontend/android/build.gradle names it by default
        self.android_package = g.get('android_package') or 'io.github.vitarecomps.' + ''.join(
            c for c in self.id.lower() if c.isalnum())

    def build_files(self):
        """The project files a build of the game's code reads besides game.json (recompile_args), as
        project paths. Every package that builds the game carries them at the same paths: a release's
        setup/kit/game (make-setup.py) and the APK's build tools (android-build-tools.py)."""
        return sorted(set(self.recomp_overrides.values()))

    def check_build_files(self):
        """Stops with a message if a build file is missing or its overrides would keep a module built for
        Android from loading (check-overrides.py): a release or an APK is never packed with them."""
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location('check_overrides', os.path.join(here, 'check-overrides.py'))
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        problems = check.check_project(self.root)
        if problems:
            sys.exit('\n'.join(problems))

    def recompile_args(self, module):
        """recompile.py's options for a module (as dumped): its load base and the project's overrides."""
        args = ['--load-base', self.load_bases[module]] if module in self.load_bases else []
        if module in self.recomp_overrides:
            path = os.path.join(self.root, self.recomp_overrides[module])
            if not os.path.isfile(path):
                sys.exit(f'{self.recomp_overrides[module]} (game.json recomp_overrides) is missing from '
                         f'{self.root}: this package of the game is incomplete')
            args += ['--overrides', path]
        return args

    def unsupported_eboot(self, digest, log):
        """Why the installed eboot.bin (its SHA-256) can't be used, for the setup's message."""
        supported = ', '.join(sorted(set(self.eboot_sha256.values())))
        other = self.eboot_sha256_other.get(digest)
        if other:
            return (f'eboot.bin is the game\'s version {other}, not one this port supports ({supported}): its update '
                    f'wasn\'t installed over it. Give the update with the game (a dump holding both app/ and patch/, '
                    f'or the update as a part of its own); {log} says why it wasn\'t installed')
        return (f'eboot.bin is not a build this port supports ({supported}; SHA-256 {digest}): '
                'install the game\'s update with it')

    def installed_title(self, fs):
        """The title ID of the game installed in a Vita file system (ux0/app/<id>/eboot.bin), or None."""
        for t in self.title_ids:
            if os.path.exists(os.path.join(fs, 'ux0', 'app', t, 'eboot.bin')):
                return t
        return None

    def bin_dir(self, no_jit=False, config='RelWithDebInfo'):
        return os.path.join(self.root, 'build', 'vita3k-nojit' if no_jit else 'vita3k', 'bin', config)

    def exe(self, no_jit=False, bin_dir=None):
        """The front end: built in the project (build-vita3k.py), or in bin_dir (a prebuilt release)."""
        path = os.path.join(bin_dir or self.bin_dir(no_jit), self.exe_name)
        if not os.path.exists(path):
            sys.exit(f'no {path}: build {self.exe_name} first (tools/build-vita3k.py --project {self.root})')
        return os.path.abspath(path)
