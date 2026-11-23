"""Assemble a game's release for players: the game's own folder (and an archive of it), which builds
the game from the player's own dump the first time it starts, without Git, Visual Studio or a
Python install (docs D9, D22 of the game projects). One program per platform, everything in the
folder:

  <id>.exe | <id>.x86_64 | <Name>.app   the game's front end (a VITA3K_GAMES_ONLY build): the one
                          thing the player starts. Not built yet, it opens the importer (setup/)
  (Windows) the DLLs it loads, the VC++ runtime among them (from the build machine's Visual Studio)
  data/ shaders-builtin/ icons/ COPYING.txt   Vita3K's files the front end reads
  README.txt              the game folder's README (game.json's dist_readme for the platform)
  game.json's release_files, e.g. recomplauncher.json (RecompLauncher's setup and settings)
  setup/python/           Python with Tk, capstone and PyYAML (versions pinned: the recompiler's
                          output depends on capstone's): Windows, python.org's embeddable
                          distribution and tcltk.msi; Linux and macOS, python-build-standalone
  setup/kit/recomp, setup/kit/tools   this kit's recompiler and tools (tools/setup.py: the importer)
  setup/kit/game/         game.json, the README's source and the code overrides game.json names
  setup/android/<id>.apk  with --apk: the game's APK, which setup.py --android puts on a device

Nothing derived from a game or the firmware goes in: release checks refuse such files.

macOS: the app bundle runs the front end itself (Game Mode) and finds the folder as the one holding
it; nothing is signed with a developer ID: the player lets the app through Gatekeeper once ("Open
Anyway"), and the front end clears the download's quarantine from the rest of its folder.

usage:
  python -I make-setup.py resolve <lock.json>           pin the embeddable Python and the wheels
  python -I make-setup.py resolve-linux <lock.json> <tag>   pin python-build-standalone release <tag>
  python -I make-setup.py resolve-macos <lock.json> <tag>   the same for Apple Silicon macOS
  python -I make-setup.py build <lock.json> --project DIR --bin DIR --out DIR [--apk FILE]
                           [--zip [--archive PATH]]

  --out       the game's folder to make (its name is the archive's top folder)
  --apk       the game's APK for Android (the release workflow's android job), as setup/android/<id>.apk
  --zip       also an archive of it (Windows .zip, Linux and macOS .tar.gz, which keep the
              executables' modes and Python's symbolic links), at --archive (without extension;
              default: --out)
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import sys
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gameproject import Project, ico_to_png  # noqa: E402
from toolchain import msi_admin_install  # noqa: E402

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = '3.12.10'
WHEELS = {'capstone': '5.0.7', 'PyYAML': '6.0.3'}
# what the front end's folder may hold; anything else (logs, dumps, saves, recompiled modules) stays out
BIN_FILES = re.compile(r'^(\w[\w-]*\.exe|discord_game_sdk\.dll|lib(ssl|crypto)-\d+-x64\.dll|COPYING\.txt)$')
BIN_DIRS = ('data', 'shaders-builtin', 'icons')
# files a release must never contain
FORBIDDEN = re.compile(r'(?i)(\.(elf|self|suprx|pkg|pup|rif|vpk|bin|psarc|log)$|work\.bin|ssd_aot_|elfdumps|savedata)')


# the VC++ runtime ss-delta.exe imports: shipped beside it from the build machine's Visual Studio
# (D22: the exe must start before its importer has downloaded anything)
VC_RUNTIME = re.compile(r'(?i)^(msvcp140.*|vcruntime140.*)\.dll$')

# Linux: Python from python-build-standalone (with Tk), the same wheels as manylinux builds
PBS_REPO = 'astral-sh/python-build-standalone'
LINUX_BIN_FILES = re.compile(r'^(\w[\w-]*\.x86_64|\w[\w-]*\.ico|COPYING\.txt)$')
# macOS: the executable and MoltenVK go into the app bundle; these stay beside it
MACOS_BIN_FILES = re.compile(r'^(COPYING\.txt)$')
LINUX_SYSTEM_LIBS = {'libc.so.6', 'libm.so.6', 'libdl.so.2', 'libpthread.so.0', 'librt.so.1',
                     'ld-linux-x86-64.so.2', 'libz.so.1'}


def pe_imports(path):
    """The DLL names a PE32+ file imports, normal and delay-loaded."""
    with open(path, 'rb') as f:
        d = f.read()
    pe = struct.unpack_from('<I', d, 0x3c)[0]
    nsec, optsz = struct.unpack_from('<H', d, pe + 6)[0], struct.unpack_from('<H', d, pe + 20)[0]
    opt = pe + 24
    secs = [struct.unpack_from('<8sIIII', d, opt + optsz + 40 * i) for i in range(nsec)]

    def off(rva):
        for _, vsz, va, rsz, ptr in secs:
            if va <= rva < va + max(vsz, rsz):
                return rva - va + ptr
        raise ValueError(hex(rva))

    names = []
    for idx, size, name_at in ((1, 20, 12), (13, 32, 4)):
        rva = struct.unpack_from('<I', d, opt + 112 + 8 * idx)[0]
        o = off(rva) if rva else None
        while o is not None:
            name = struct.unpack_from('<I', d, o + name_at)[0]
            if not name:
                break
            n = off(name)
            names.append(d[n:d.index(b'\0', n)].decode())
            o += size
    return names


def missing_dlls(exe):
    """Imported DLLs that are neither beside the exe nor part of Windows (the VC++ runtime counts as
    missing: a player's PC may lack it)."""
    system = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
    here = {f.lower() for f in os.listdir(os.path.dirname(exe))}
    return sorted(n for n in pe_imports(exe)
                  if n.lower() not in here and not n.lower().startswith(('api-ms-', 'ext-ms-'))
                  and (VC_RUNTIME.match(n) or not os.path.exists(os.path.join(system, n))))


def vc_runtime_dlls():
    """The x64 VC++ runtime DLLs of this machine's newest Visual Studio (its Redist folder, which
    Microsoft's licence lets a program carry)."""
    vswhere = os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                           'Microsoft Visual Studio', 'Installer', 'vswhere.exe')
    r = subprocess.run([vswhere, '-latest', '-products', '*', '-requires',
                        'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'],
                       capture_output=True, text=True)
    root = r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else ''
    versions = [d for d in glob.glob(os.path.join(root, 'VC', 'Redist', 'MSVC', '*'))
                if re.match(r'^\d+\.\d+\.\d+$', os.path.basename(d))] if root else []
    for d in sorted(versions, key=lambda d: tuple(int(x) for x in os.path.basename(d).split('.')), reverse=True):
        crt = glob.glob(os.path.join(d, 'x64', 'Microsoft.VC*.CRT'))
        dlls = [f for f in glob.glob(os.path.join(crt[0], '*.dll')) if VC_RUNTIME.match(os.path.basename(f))] if crt else []
        if dlls:
            return dlls
    sys.exit(f'no VC++ runtime (VC/Redist/MSVC/<version>/x64/Microsoft.VC*.CRT) in Visual Studio ({root or "not found"})')


def elf_needed(path):
    """The DT_NEEDED libraries of an x86-64 ELF file."""
    with open(path, 'rb') as f:
        d = f.read()
    if d[:4] != b'\x7fELF' or d[4] != 2:
        raise ValueError(f'{path}: not a 64-bit ELF file')
    phoff, = struct.unpack_from('<Q', d, 0x20)
    phentsize, phnum = struct.unpack_from('<HH', d, 0x36)
    loads, dynamic = [], None
    for i in range(phnum):
        p_type, _, p_offset, p_vaddr, _, p_filesz, _, _ = struct.unpack_from('<IIQQQQQQ', d, phoff + i * phentsize)
        if p_type == 1:  # PT_LOAD
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == 2:  # PT_DYNAMIC
            dynamic = (p_offset, p_filesz)
    if not dynamic:
        return []

    def off(vaddr):
        return next(o + vaddr - v for v, o, sz in loads if v <= vaddr < v + sz)

    needed, strtab = [], None
    for o in range(dynamic[0], dynamic[0] + dynamic[1], 16):
        tag, val = struct.unpack_from('<qQ', d, o)
        if tag == 0:
            break
        if tag == 1:  # DT_NEEDED
            needed.append(val)
        elif tag == 5:  # DT_STRTAB
            strtab = off(val)
    return [d[strtab + n:d.index(b'\0', strtab + n)].decode() for n in needed]


def missing_libs(exe):
    """Linux: libraries the executable needs beyond glibc and zlib, which every distribution has."""
    return sorted(n for n in elf_needed(exe) if n not in LINUX_SYSTEM_LIBS)


def get(url, path=None):
    req = urllib.request.Request(url, headers={'User-Agent': 'vita-recomp-kit make-setup.py'})
    with urllib.request.urlopen(req, timeout=120) as r:
        if path is None:
            return r.read()
        with open(path, 'wb') as f:
            shutil.copyfileobj(r, f, 1 << 20)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def resolve(lock_path, cache):
    os.makedirs(cache, exist_ok=True)
    files = []
    url = f'https://www.python.org/ftp/python/{PYTHON}/python-{PYTHON}-embed-amd64.zip'
    local = os.path.join(cache, os.path.basename(url))
    get(url, local)
    files.append({'kind': 'python', 'url': url, 'sha256': sha256(local), 'size': os.path.getsize(local)})
    # Tk, which the embeddable distribution leaves out: the Windows installer's own component
    url = f'https://www.python.org/ftp/python/{PYTHON}/amd64/tcltk.msi'
    local = os.path.join(cache, os.path.basename(url))
    get(url, local)
    files.append({'kind': 'tcltk', 'url': url, 'sha256': sha256(local), 'size': os.path.getsize(local)})
    tag = 'cp' + ''.join(PYTHON.split('.')[:2])
    for name, version in WHEELS.items():
        release = json.loads(get(f'https://pypi.org/pypi/{name}/{version}/json'))
        wheels = [u for u in release['urls'] if u['filename'].endswith('.whl') and 'win_amd64' in u['filename']
                  and (f'-{tag}-' in u['filename'] or '-py3-none-' in u['filename'])]
        if not wheels:
            sys.exit(f'{name} {version}: no Windows x64 wheel for {tag}')
        w = wheels[0]
        files.append({'kind': 'wheel', 'url': w['url'], 'sha256': w['digests']['sha256'], 'size': w['size']})
    lock = {'schema': 'vita-recomp-kit.setup.v1', 'python': PYTHON, 'wheels': WHEELS, 'files': files}
    with open(lock_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(lock, f, indent=1)
    print(f'{lock_path}: Python {PYTHON}, ' + ', '.join(f'{k} {v}' for k, v in WHEELS.items()))


def resolve_linux(lock_path, cache, tag, platform='linux'):
    """Pin python-build-standalone's CPython 3.12 of release <tag> (Tk included) and the wheels, for
    x86-64 Linux or (platform 'macos') Apple Silicon macOS."""
    os.makedirs(cache, exist_ok=True)
    release = json.loads(get(f'https://api.github.com/repos/{PBS_REPO}/releases/tags/{tag}'))
    triple = 'aarch64-apple-darwin' if platform == 'macos' else 'x86_64-unknown-linux-gnu'
    pattern = re.compile(r'^cpython-(3\.12\.\d+)\+' + re.escape(tag) + '-' + triple + r'-install_only\.tar\.gz$')
    asset = next((x for x in release['assets'] if pattern.match(x['name'])), None)
    if not asset:
        sys.exit(f'{PBS_REPO} {tag}: no CPython 3.12 install_only build for {triple}')
    version = pattern.match(asset['name']).group(1)
    sums = get(f'https://github.com/{PBS_REPO}/releases/download/{tag}/SHA256SUMS').decode()
    digest = next(line.split()[0] for line in sums.splitlines() if line.endswith(' ' + asset['name']))
    files = [{'kind': 'python', 'url': asset['browser_download_url'], 'sha256': digest, 'size': asset['size']}]
    tag_py = 'cp' + ''.join(version.split('.')[:2])

    def fits(fn):
        if platform == 'macos':  # arm64 or universal2 (both architectures) macOS wheels
            return 'macosx' in fn and ('arm64' in fn or 'universal2' in fn)
        return 'manylinux' in fn and 'x86_64' in fn
    for name, wversion in WHEELS.items():
        info = json.loads(get(f'https://pypi.org/pypi/{name}/{wversion}/json'))
        wheels = [u for u in info['urls'] if u['filename'].endswith('.whl') and fits(u['filename'])
                  and (f'-{tag_py}-' in u['filename'] or '-py3-none-' in u['filename'])]
        if not wheels:
            sys.exit(f'{name} {wversion}: no {triple} wheel for {tag_py}')
        w = wheels[0]
        files.append({'kind': 'wheel', 'url': w['url'], 'sha256': w['digests']['sha256'], 'size': w['size']})
    lock = {'schema': 'vita-recomp-kit.setup.v1', 'platform': platform, 'python': version,
            'python_build_standalone': tag, 'wheels': WHEELS, 'files': files}
    with open(lock_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(lock, f, indent=1)
    print(f'{lock_path}: Python {version} ({PBS_REPO} {tag}), ' + ', '.join(f'{k} {v}' for k, v in WHEELS.items()))


def fetch(lock, cache):
    os.makedirs(cache, exist_ok=True)
    paths = {}
    for f in lock['files']:
        local = os.path.join(cache, os.path.basename(f['url']))
        if not (os.path.exists(local) and sha256(local) == f['sha256']):
            get(f['url'], local)
            if sha256(local) != f['sha256']:
                sys.exit(f'{local}: wrong SHA-256')
        paths.setdefault(f['kind'], []).append(local)
    return paths


def copy_tree(src, dst, skip=('__pycache__', 'tests')):
    for d, dirs, files in os.walk(src):
        dirs[:] = [x for x in dirs if x not in skip and not x.startswith('.')]
        rel = os.path.relpath(d, src)
        for f in files:
            if f.endswith(('.pyc', '.ps1')):
                continue
            os.makedirs(os.path.join(dst, rel), exist_ok=True)
            shutil.copy2(os.path.join(d, f), os.path.join(dst, rel, f))


def release_check(out):
    """Refuse a setup holding anything that looks like game or firmware data."""
    bad = [os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs
           if FORBIDDEN.search(os.path.relpath(os.path.join(d, f), out).replace('\\', '/'))
           and not os.path.relpath(d, out).replace('\\', '/').startswith('setup/python')]
    if bad:
        shutil.rmtree(out)
        sys.exit('refused, files that look like game data: ' + ', '.join(bad[:10]))


def copy_kit(out, p):
    """setup/kit: this kit's recompiler and tools; setup/kit/game: game.json, the README's source and the
    recompiler overrides game.json names (recomp_overrides)."""
    kit = os.path.join(out, 'setup', 'kit')
    copy_tree(os.path.join(KIT, 'recomp'), os.path.join(kit, 'recomp'))
    os.makedirs(os.path.join(kit, 'tools'))
    for f in glob.glob(os.path.join(KIT, 'tools', '*.py')) + glob.glob(os.path.join(KIT, 'tools', '*.json')):
        shutil.copy2(f, os.path.join(kit, 'tools'))
    shutil.copy2(os.path.join(KIT, 'LICENSE'), os.path.join(kit, 'LICENSE'))
    game = os.path.join(kit, 'game')
    os.makedirs(game)
    shutil.copy2(os.path.join(p.root, 'game.json'), game)
    for rel in ([p.dist_readme] if p.dist_readme else []) + sorted(set(p.recomp_overrides.values())):
        os.makedirs(os.path.dirname(os.path.join(game, rel)), exist_ok=True)
        shutil.copy2(os.path.join(p.root, rel), os.path.join(game, rel))


def downloads_dir(out):
    """Where the release's own downloads (Python, wheels) are kept between builds: beside --out."""
    return os.path.join(os.path.dirname(out), 'setup-downloads')


def python_windows(lock, out):
    """setup/python: Python's embeddable distribution with the wheels in Lib/site-packages, and Tk."""
    paths = fetch(lock, downloads_dir(out))
    py = os.path.join(out, 'setup', 'python')
    with zipfile.ZipFile(paths['python'][0]) as z:
        z.extractall(py)
    site = os.path.join(py, 'Lib', 'site-packages')
    for w in paths['wheel']:
        with zipfile.ZipFile(w) as z:
            z.extractall(site)
    # its ._pth file is the whole sys.path
    pth = glob.glob(os.path.join(py, 'python*._pth'))[0]
    zipname = os.path.basename(pth)[:-5] + '.zip'
    with open(pth, 'w', encoding='ascii', newline='\r\n') as f:
        f.write(f'{zipname}\n.\nLib\nLib\\site-packages\n')
    # Tk: an administrative install of the MSI (no elevation), then only what tkinter loads
    with tempfile.TemporaryDirectory() as tmp:
        code = msi_admin_install(paths['tcltk'][0], os.path.join(tmp, 'tk'), '/qn')
        if code:
            sys.exit(f'unpacking tcltk.msi failed ({code})')
        tk = os.path.join(tmp, 'tk')
        for f in os.listdir(os.path.join(tk, 'DLLs')):
            shutil.copy2(os.path.join(tk, 'DLLs', f), py)
        shutil.copytree(os.path.join(tk, 'Lib', 'tkinter'), os.path.join(py, 'Lib', 'tkinter'),
                        ignore=shutil.ignore_patterns('test', '__pycache__'))
        for d in ('tcl8', 'tcl8.6', 'tk8.6'):
            shutil.copytree(os.path.join(tk, 'tcl', d), os.path.join(py, 'tcl', d),
                            ignore=shutil.ignore_patterns('demos', 'images', 'tzdata'))


def python_standalone(lock, out):
    """setup/python from python-build-standalone (Linux, macOS), with the wheels in its site-packages."""
    paths = fetch(lock, downloads_dir(out))
    py = os.path.join(out, 'setup', 'python')
    with tarfile.open(paths['python'][0]) as t:
        members = []
        for m in t.getmembers():
            parts = m.name.split('/', 1)
            if parts[0] == 'python' and len(parts) == 2 and parts[1]:
                m.name = parts[1]
                members.append(m)
        t.extractall(py, members, filter='tar')
    site = glob.glob(os.path.join(py, 'lib', 'python3.*', 'site-packages'))[0]
    for w in paths['wheel']:
        with zipfile.ZipFile(w) as z:
            z.extractall(site)
            for info in z.infolist():  # zip keeps no modes: capstone's library must stay loadable
                if info.filename.endswith(('.so', '.dylib')) or '.so.' in info.filename:
                    os.chmod(os.path.join(site, info.filename), 0o755)


def macho_dylibs(path):
    """The libraries an arm64 Mach-O file loads (normal, weak, re-exported and upward), from its load
    commands; a universal file's arm64 slice."""
    with open(path, 'rb') as f:
        d = f.read()
    base = 0
    if struct.unpack_from('>I', d, 0)[0] == 0xcafebabe:  # universal: find the arm64 slice
        for i in range(struct.unpack_from('>I', d, 4)[0]):
            cputype, _, offset, _, _ = struct.unpack_from('>iiIII', d, 8 + 20 * i)
            if cputype == 0x0100000c:
                base = offset
    if struct.unpack_from('<I', d, base)[0] != 0xfeedfacf:
        raise ValueError(f'{path}: not a 64-bit Mach-O file')
    ncmds = struct.unpack_from('<I', d, base + 16)[0]
    names, o = [], base + 32
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', d, o)
        if cmd in (0xc, 0x80000018, 0x8000001f, 0x80000023):  # LC_LOAD_(WEAK_,UPWARD_)DYLIB, LC_REEXPORT_DYLIB
            n = o + struct.unpack_from('<I', d, o + 8)[0]
            names.append(d[n:d.index(b'\0', n)].decode())
        o += size
    return names


def missing_macos_libs(path):
    """macOS: libraries a Mach-O file loads that are neither part of macOS nor beside it."""
    here = os.path.dirname(path)
    return sorted(n for n in macho_dylibs(path)
                  if not n.startswith(('/usr/lib/', '/System/Library/'))
                  and not (n.startswith(('@rpath/', '@executable_path/', '@loader_path/'))
                           and os.path.exists(os.path.join(here, n.split('/', 1)[1]))))


def front_end(p, bin_dir, out, files, exe=None):
    """The game's folder: the front end's files from bin_dir (those files matches; of the
    executables, only exe), Vita3K's data, README.txt and the extra files game.json lists."""
    if exe and not os.path.exists(os.path.join(bin_dir, exe)):
        sys.exit(f'no {exe} in {bin_dir}')
    suffix = os.path.splitext(exe)[1] if exe else None
    for f in os.listdir(bin_dir):
        # a build folder holds every game's executable and icon: only this game's
        if os.path.isfile(os.path.join(bin_dir, f)) and files.match(f) and (not suffix or not f.endswith(suffix) or f == exe) \
                and (not f.endswith('.ico') or f == p.id + '.ico'):
            shutil.copy2(os.path.join(bin_dir, f), out)
    for d in BIN_DIRS:
        if os.path.isdir(os.path.join(bin_dir, d)):
            shutil.copytree(os.path.join(bin_dir, d), os.path.join(out, d))
    windows = exe and exe.endswith('.exe')
    if p.dist_readme:
        with open(os.path.join(p.root, p.dist_readme), encoding='utf-8') as f:
            text = f.read()
        with open(os.path.join(out, 'README.txt'), 'w', encoding='utf-8', newline='\r\n' if windows else '\n') as f:
            f.write(text)
    for fname, text in p.dist_files.items():
        with open(os.path.join(out, fname), 'w', encoding='utf-8', newline='\r\n' if windows else '\n') as f:
            f.write(text)
        if not windows:
            os.chmod(os.path.join(out, fname), 0o755)  # a launcher script
    for rel in p.release_files:
        shutil.copy2(os.path.join(p.root, rel), os.path.join(out, os.path.basename(rel)))


def finish(a, out, fmt):
    """The release checks, its size, and with --zip its archive (fmt: 'zip' or 'gztar')."""
    if a.apk:
        android = os.path.join(out, 'setup', 'android')
        os.makedirs(android, exist_ok=True)
        shutil.copy2(a.apk, os.path.join(android, Project(a.project).id + '.apk'))
    release_check(out)
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
    print(f'{out}: {size / 2**20:,.0f} MB')
    if a.zip:
        base = os.path.abspath(a.archive) if a.archive else out
        archive = shutil.make_archive(base, fmt, os.path.dirname(out), os.path.basename(out))
        print(f'{archive}: {os.path.getsize(archive) / 2**20:,.0f} MB, SHA-256 {sha256(archive)}')


def build_windows(a, lock, p, out):
    """Windows: <id>.exe with its DLLs and the VC++ runtime, setup/python (embeddable, Tk); a .zip."""
    bin_dir = os.path.abspath(a.bin)
    exe = p.id + '.exe'
    front_end(p, bin_dir, out, BIN_FILES, exe)
    for f in vc_runtime_dlls():
        shutil.copy2(f, out)
    # release check: the executable finds every DLL it imports on a player's PC, before anything
    # is downloaded
    missing = missing_dlls(os.path.join(out, exe))
    if missing:
        shutil.rmtree(out)
        sys.exit(f'refused, {exe} imports DLLs the release does not carry: ' + ', '.join(missing))
    python_windows(lock, out)
    copy_kit(out, p)
    finish(a, out, 'zip')


def build_linux(a, lock, p, out):
    """Linux: <id>.x86_64 and the icon a desktop entry shows, setup/python (python-build-standalone);
    a .tar.gz."""
    bin_dir = os.path.abspath(a.bin)
    exe = p.id + '.x86_64'
    front_end(p, bin_dir, out, LINUX_BIN_FILES, exe)
    # without its debug information (most of a RelWithDebInfo build's size)
    objcopy = shutil.which('objcopy')
    if objcopy:
        subprocess.run([objcopy, '--strip-debug', os.path.join(out, exe)], check=True)
    else:
        print(f'warning: no objcopy: {exe} keeps its debug information')
    if os.path.exists(os.path.join(bin_dir, p.id + '.ico')):
        ico_to_png(os.path.join(bin_dir, p.id + '.ico'), os.path.join(out, p.id + '.png'))
    missing = missing_libs(os.path.join(out, exe))
    if missing:
        shutil.rmtree(out)
        sys.exit(f'refused, {exe} needs libraries a player\'s system may lack: ' + ', '.join(missing))
    python_standalone(lock, out)
    copy_kit(out, p)
    finish(a, out, 'gztar')


def build_macos(a, lock, p, out):
    """macOS (Apple Silicon): '<Name>.app', which runs <id>.arm64 itself with MoltenVK beside it,
    setup/python (python-build-standalone); a .tar.gz."""
    import setup as importer  # the app bundle, as the importer would make its copy in ~/Applications
    bin_dir = os.path.abspath(a.bin)
    exe = p.id + '.arm64'
    for f in (exe, 'libMoltenVK.dylib'):
        if not os.path.exists(os.path.join(bin_dir, f)):
            shutil.rmtree(out)
            sys.exit(f'refused, no {f} in {bin_dir}')
    front_end(p, bin_dir, out, MACOS_BIN_FILES)
    with open(os.path.join(p.root, 'game.json'), encoding='utf-8') as f:
        game = json.load(f)
    app = os.path.join(out, importer.app_name(game) + '.app')
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy2(os.path.join(bin_dir, exe), tmp)
        shutil.copy2(os.path.join(bin_dir, 'libMoltenVK.dylib'), tmp)
        # without the debug map (stripping drops the linker's ad-hoc signature; the bundle is signed
        # again as a whole)
        subprocess.run(['strip', '-S', os.path.join(tmp, exe)], check=True)
        png = os.path.join(tmp, p.id + '.png')
        if os.path.exists(os.path.join(bin_dir, p.id + '.ico')):
            ico_to_png(os.path.join(bin_dir, p.id + '.ico'), png)
        importer.mac_bundle(os.path.join(tmp, exe), game, app, png)
    missing = missing_macos_libs(os.path.join(app, 'Contents', 'MacOS', game['id']))
    if missing:
        shutil.rmtree(out)
        sys.exit(f'refused, {exe} needs libraries a player\'s Mac may lack: ' + ', '.join(missing))
    python_standalone(lock, out)
    copy_kit(out, p)
    finish(a, out, 'gztar')


def build(a):
    lock = json.load(open(a.lock, encoding='utf-8'))
    p = Project(a.project)
    out = os.path.abspath(a.out)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    platform = lock.get('platform', 'windows')
    return {'windows': build_windows, 'linux': build_linux, 'macos': build_macos}[platform](a, lock, p, out)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == 'resolve':
        resolve(sys.argv[2], os.path.join(os.getcwd(), 'build', 'setup-downloads'))
        return
    if len(sys.argv) >= 4 and sys.argv[1] in ('resolve-linux', 'resolve-macos'):
        resolve_linux(sys.argv[2], os.path.join(os.getcwd(), 'build', 'setup-downloads'), sys.argv[3],
                      sys.argv[1].split('-')[1])
        return
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument('command', choices=['build'])
    ap.add_argument('lock')
    ap.add_argument('--project', default=os.getcwd())
    ap.add_argument('--bin', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--apk', default='')
    ap.add_argument('--zip', action='store_true')
    ap.add_argument('--archive', default='')
    build(ap.parse_args())


if __name__ == '__main__':
    main()
