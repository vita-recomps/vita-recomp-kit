"""The tools a game's APK carries to build its native modules on the device itself (ss-delta D27, 09-android):
python.org's CPython for Android with a small launcher, capstone, PyYAML, zig for aarch64-linux, and this
kit's recompiler and build scripts. Laid out for the APK's Gradle project (its buildToolsDir property):

  <out>/jniLibs/arm64-v8a/   the programs and libraries, as lib*.so: Android extracts these to the app's
                             native library folder, the one place an app may execute programs from
                             (W^X since Android 10: never a file the app wrote)
      libpython3.14.so, lib*_python.so   python.org's interpreter and the OpenSSL/SQLite it links
      libpython-launcher.so              a program: Py_BytesMain, so the kit's scripts run as on a desktop
      libcapstone.so                     capstone (ARM only), which its binding loads with ctypes
      libzig.so                          zig itself (a static program)
  <out>/assets/buildtools.zip   unpacked by the app on first use into its own storage:
      python/lib/python3.14/   the standard library (without its tests, IDLE, Tk and ensurepip)
      site/capstone, site/yaml the Python sides of capstone and PyYAML
      zig/lib/                 zig's lib/, without what an aarch64-linux-musl build never reads
      kit/recomp, kit/tools    this kit's recompiler and scripts
  <out>/assets/buildtools.json  the versions; the app unpacks again when it changes

usage:
  python -I android-build-tools.py <lock: android-build-tools.json> <out> --ndk <NDK folder> [--cache DIR]

The NDK (r29) builds capstone and the launcher; the payloads are checked against the lock's SHA-256.
Nothing here comes from a game: the APK may carry it.
"""
import argparse
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import toolchain  # noqa: E402

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = 28  # the APK's minSdk
ABI = 'arm64-v8a'

LAUNCHER_C = '#include <Python.h>\nint main(int argc, char **argv) { return Py_BytesMain(argc, argv); }\n'
CAPSTONE_SOURCES = ['cs.c', 'Mapping.c', 'MCInst.c', 'MCInstrDesc.c', 'MCRegisterInfo.c', 'SStream.c', 'utils.c',
                    'arch/ARM/ARMDisassembler.c', 'arch/ARM/ARMInstPrinter.c', 'arch/ARM/ARMMapping.c',
                    'arch/ARM/ARMModule.c']
# 16 KiB alignment for the 16 KiB-page devices since Android 15
LDFLAGS = ['-Wl,-z,max-page-size=16384', '-s']

# the standard library's parts no build needs
PYTHON_DROP = {'test', 'idlelib', 'tkinter', 'turtledemo', 'ensurepip', 'pydoc_data', '__pycache__'}
# zig's lib/: an aarch64-linux-musl build reads musl's headers and sources, libc++, libunwind, compiler-rt
# and std (compiler-rt is Zig code); the other C libraries' headers and sources are most of its size
ZIG_DROP = {'libtsan', 'docs', 'fuzzer.zig', 'build-web', 'init', 'lldb'}
ZIG_LIBC_DROP = {'mingw', 'glibc', 'wasi', 'darwin', 'freebsd', 'netbsd', 'openbsd'}
ZIG_LIBC_INCLUDE_KEEP = {'generic-musl', 'aarch64-linux-musl', 'any-linux-any', 'aarch64-linux-any'}


def ndk_clang(ndk):
    hosts = glob.glob(os.path.join(ndk, 'toolchains', 'llvm', 'prebuilt', '*', 'bin'))
    for name in ('clang', 'clang.exe'):
        for h in hosts:
            if os.path.isfile(os.path.join(h, name)):
                return os.path.join(h, name)
    sys.exit(f'no clang in {ndk}/toolchains/llvm/prebuilt/*/bin: --ndk names the NDK (r29)')


def cc(clang, args):
    cmd = [clang, f'--target=aarch64-linux-android{API}', '-O2', '-fPIC'] + args
    if subprocess.run(cmd).returncode:
        sys.exit('compiling failed: ' + ' '.join(cmd))


def members(tar, top):
    """tar's regular files and folders under top/, renamed relative to it (symbolic links skipped)."""
    for m in tar.getmembers():
        parts = m.name.lstrip('./').split('/', 1)
        if parts[0] == top.strip('/') and len(parts) == 2 and (m.isfile() or m.isdir()):
            m.name = parts[1]
            yield m


def zip_tree(z, src, arc, drop=lambda rel: False):
    count = 0
    for root, dirs, files in os.walk(src):
        rel_root = os.path.relpath(root, src).replace(os.sep, '/')
        rel_root = '' if rel_root == '.' else rel_root + '/'
        dirs[:] = sorted(d for d in dirs if not drop(rel_root + d) and d != '__pycache__')
        for f in sorted(files):
            if drop(rel_root + f) or f.endswith('.pyc'):
                continue
            z.write(os.path.join(root, f), arc + '/' + rel_root + f)
            count += 1
    return count


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('lock')
    ap.add_argument('out')
    ap.add_argument('--ndk', required=True)
    ap.add_argument('--cache', default='')
    a = ap.parse_args()
    lock = json.load(open(a.lock, encoding='utf-8'))
    if lock.get('schema') != 'vita-recomp-kit.android-build-tools.v1':
        sys.exit(f'{a.lock}: not an android-build-tools lock file')
    clang = ndk_clang(os.path.abspath(a.ndk))
    out = os.path.abspath(a.out)
    libs = os.path.join(out, 'jniLibs', ABI)
    assets = os.path.join(out, 'assets')
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(libs)
    os.makedirs(assets)

    with tempfile.TemporaryDirectory() as tmp:
        cache = os.path.abspath(a.cache) if a.cache else os.path.join(tmp, 'downloads')
        print('downloading')
        toolchain.download(lock, cache)
        payload = {p['kind']: os.path.join(cache, p['file']) for p in lock['payloads']}
        work = os.path.join(tmp, 'work')

        print('unpacking')
        with tarfile.open(payload['python']) as t:
            t.extractall(os.path.join(work, 'python'), members=members(t, 'prefix'), filter='data')
        with tarfile.open(payload['zig']) as t:
            top = t.getmembers()[0].name.lstrip('./').split('/')[0]
            t.extractall(os.path.join(work, 'zig'), members=members(t, top), filter='data')
        with tarfile.open(payload['capstone']) as t:
            t.extractall(os.path.join(work, 'capstone'), members=members(t, f'capstone-{lock["capstone"]}'),
                         filter='data')
        with tarfile.open(payload['pyyaml']) as t:
            t.extractall(os.path.join(work, 'pyyaml'), members=members(t, f'pyyaml-{lock["pyyaml"]}'), filter='data')

        print('building capstone and the launcher')
        py = os.path.join(work, 'python')
        version = lock['python'].rsplit('.', 1)[0]  # 3.14
        # the interpreter and the copies of OpenSSL and SQLite it and its modules link (lib*_python.so; the
        # package also holds them under their plain names, and libpython3.so, the stable-ABI stub)
        for so in [f'libpython{version}.so', *map(os.path.basename, glob.glob(os.path.join(py, 'lib', 'lib*_python.so')))]:
            shutil.copy2(os.path.join(py, 'lib', so), libs)
        launcher_c = os.path.join(work, 'launcher.c')
        with open(launcher_c, 'w') as f:
            f.write(LAUNCHER_C)
        cc(clang, ['-fPIE', '-pie', '-I', os.path.join(py, 'include', f'python{version}'), launcher_c,
                   '-L', os.path.join(py, 'lib'), f'-lpython{version}', *LDFLAGS,
                   '-o', os.path.join(libs, 'libpython-launcher.so')])
        cs = os.path.join(work, 'capstone', 'src')
        cc(clang, ['-shared', '-DCAPSTONE_HAS_ARM', '-DCAPSTONE_USE_SYS_DYN_MEM', '-I', os.path.join(cs, 'include'),
                   *[os.path.join(cs, s) for s in CAPSTONE_SOURCES], *LDFLAGS, '-Wl,-soname,libcapstone.so',
                   '-o', os.path.join(libs, 'libcapstone.so')])
        zig = os.path.join(work, 'zig')
        shutil.copy2(os.path.join(zig, 'zig'), os.path.join(libs, 'libzig.so'))

        print('packing the assets')
        count = 0
        with zipfile.ZipFile(os.path.join(assets, 'buildtools.zip'), 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            count += zip_tree(z, os.path.join(py, 'lib', f'python{version}'), f'python/lib/python{version}',
                              lambda rel: rel.split('/')[0] in PYTHON_DROP or rel.startswith('config-'))
            count += zip_tree(z, os.path.join(work, 'capstone', 'capstone'), 'site/capstone')
            count += zip_tree(z, os.path.join(work, 'pyyaml', 'lib', 'yaml'), 'site/yaml')

            def zig_drop(rel):
                p = rel.split('/')
                return (p[0] in ZIG_DROP or (p[0] == 'libc' and len(p) > 1 and p[1] in ZIG_LIBC_DROP) or
                        (p[:2] == ['libc', 'include'] and len(p) > 2 and p[2] not in ZIG_LIBC_INCLUDE_KEEP))
            count += zip_tree(z, os.path.join(zig, 'lib'), 'zig/lib', zig_drop)
            count += zip_tree(z, os.path.join(KIT, 'recomp'), 'kit/recomp', lambda rel: rel.split('/')[0] == 'tests')
            count += zip_tree(z, os.path.join(KIT, 'tools'), 'kit/tools', lambda rel: not rel.endswith('.py'))
        stamp = {k: lock[k] for k in ('python', 'zig', 'capstone', 'pyyaml')}
        stamp['files'] = count
        stamp['zip_sha256'] = toolchain.sha256(os.path.join(assets, 'buildtools.zip'))
        with open(os.path.join(assets, 'buildtools.json'), 'w', encoding='utf-8', newline='\n') as f:
            json.dump(stamp, f, indent=1)
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(out) for f in fs)
    print(f'{out}: {count} files in buildtools.zip, {size / 2**20:,.0f} MB in all')


if __name__ == '__main__':
    main()
