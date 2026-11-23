"""A compiler for the recompiled modules that needs no Visual Studio, Xcode or SDK: zig, a self-contained
clang with libc++ and each system's link stubs, under MIT and LLVM's Apache licence (nothing to accept).
Every file is pinned by size and SHA-256 in a lock file, one a platform.

usage:
  python -I toolchain.py fetch <lock.json> <dir>   download, check and unpack zig into <dir>/zig

Windows (toolchain-windows.json): the modules come out as ssd_aot_<module>.dll for x86_64-windows-gnu
(MinGW-w64's headers and import libraries, which zig carries; the DLLs call Windows' own UCRT). The
front end, built with MSVC, loads them through aot_abi.h's C interface, and its C++ exceptions (the
escapes) unwind through them on Windows' SEH tables (ss-delta docs/03-plan.md, installer
future-proofing E). Until 2026-11 this was clang-cl from llvm-mingw with Microsoft's C++ runtime and
Windows SDK, downloaded from Microsoft under Microsoft's licence (447 MB): git history has it.

Linux (toolchain-linux.json) and macOS (toolchain-macos.json, Apple Silicon): ssd_aot_<module>.so
(.dylib on macOS, ad-hoc signed by zig's linker, as Apple Silicon requires), against glibc's stubs
and macOS's libSystem (no Xcode or Command Line Tools).

Android (arm64-v8a), from any of the three: the PC's zig, with musl's headers and no C library linked:
the modules bind to bionic's libc.so and libm.so when the game's APK loads them.
recomp/runtime/aot_android.cpp brings the little a module needs from the C++ runtime (ss-delta
docs/09-android.md).
  python -I build-recomp.py [--android] --toolchain <dir>

zig keeps what it builds on first use (libc++) in <dir>/cache/, removed after each build.
"""
import concurrent.futures, glob, hashlib, json, os, shutil, subprocess, sys, tarfile, urllib.error, urllib.request, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import offline  # noqa: E402

MACOS = sys.platform == 'darwin'
WINDOWS = sys.platform == 'win32'

RECOMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'recomp')
RUNTIME = os.path.join(RECOMP, 'runtime')
DLL_PREFIX = 'ssd_aot_'  # the name the Vita3K fork loads (RECOMP_DLL_PREFIX in recomp/CMakeLists.txt)
# Linux: zig's clang, which brings libc++ and the glibc stubs to link against (no sysroot). The target
# glibc is the oldest the front end needs too; the baseline CPU (no FMA, no AVX) keeps the maths
# what the front end's own build assumes. Windows: MinGW-w64's ABI, the same baseline CPU. macOS: the
# fork's deployment target (CMakeLists.txt, CMAKE_OSX_DEPLOYMENT_TARGET) and the first Apple Silicon CPU
ZIG_TARGET = (['-target', 'aarch64-macos.13.3', '-mcpu=apple_m1'] if MACOS else
              ['-target', 'x86_64-windows-gnu', '-mcpu=baseline'] if WINDOWS else
              ['-target', 'x86_64-linux-gnu.2.30', '-mcpu=baseline'])
ZIG_CXXFLAGS = ['-std=c++20', '-O2', *([] if WINDOWS else ['-fPIC']), '-fvisibility=hidden', '-fvisibility-inlines-hidden', '-DNDEBUG']
# no contraction, as in recomp/CMakeLists.txt: ARMv7's vmla/vmls round twice (it matters on arm64 hosts)
ZIG_GEN_FLAGS = ['-O1', '-w', '-ffp-contract=off', '-DRECOMP_COUNT_INSNS']
# a module exports its entry points (armrecomp_aot_init, armrecomp_aot_features) and nothing else
# (libc++'s operator new/delete stay inside). zig's Mach-O linker takes no export list: there, hidden
# visibility leaves the entry points and libc++'s operator new/delete, which two-level namespaces keep
# from binding anything outside the module. Windows: the entry points' __declspec(dllexport)
# (aot_dll.cpp), with which lld exports nothing else
ZIG_EXPORTS = '{\n  global: armrecomp_aot_init; armrecomp_aot_features;\n  local: *;\n};\n'
# Android: arm64 with musl's headers (the functions a module calls have the same ABI in bionic), every
# ARMv8 (LL/SC atomics inline: no outline atomics, which would want compiler-rt and getauxval), unwind
# tables for the escapes the host throws through generated frames, no stack protector (bionic's guard
# is not musl's). Linked without a C library: libc.so and libm.so stubs give the DT_NEEDED entries,
# --eh-frame-hdr the PT_GNU_EH_FRAME the host's unwinder looks for, 16 KiB alignment the 16 KiB-page
# devices since Android 15
ANDROID_TARGET = ['-target', 'aarch64-linux-musl', '-mcpu=generic', '-mno-outline-atomics']
ANDROID_CXXFLAGS = ['-fno-stack-protector', '-funwind-tables']
ANDROID_LDFLAGS = ['-shared', '-nostdlib', '-Wl,--eh-frame-hdr', '-Wl,-z,max-page-size=16384', '-Wl,--gc-sections', '-s']
USER_AGENT = 'vita-recomp-kit toolchain.py'


def windows_curl():
    """Windows' own curl.exe (System32, Windows 10 1803 and later), which uses Windows' TLS; '' if missing."""
    curl = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'curl.exe')
    return curl if sys.platform == 'win32' and os.path.exists(curl) else ''


def get(url, path=None):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            if path is None:
                return r.read()
            with open(path, 'wb') as f:
                shutil.copyfileobj(r, f, 1 << 20)
    except urllib.error.URLError as e:
        # On Windows, Python trusts only the root certificates already in Windows' store, and Windows
        # adds a newer root only when its own TLS first meets it. GitHub's release-asset servers chain
        # to Let's Encrypt's Root YR, which a PC may not have yet: certificate verification then fails
        # here while browsers download the file. Windows' curl.exe uses Windows' TLS, which fetches
        # the root; the SHA-256 of every payload is still checked against the lock by the caller.
        curl = windows_curl()
        if not (curl and 'CERTIFICATE_VERIFY_FAILED' in str(e.reason)):
            raise
        args = [curl, '-sSfL', '--retry', '3', '--connect-timeout', '60', '-A', USER_AGENT, url]
        quiet = {'creationflags': subprocess.CREATE_NO_WINDOW}  # no console flashing up over the setup's window
        if path is None:
            return subprocess.run(args, check=True, capture_output=True, **quiet).stdout
        subprocess.run(args + ['-o', path], check=True, **quiet)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def obtain(p, local):
    """One pinned payload ({file, url, size, sha256}) at local: kept if already there, else from the
    offline folders (setup.py --offline), else downloaded; exits naming the file when neither works."""
    if os.path.exists(local) and os.path.getsize(local) == p['size'] and sha256(local) == p['sha256']:
        return
    if offline.provide(local, p['size'], p['sha256']):
        return
    # the lock file's URL, then its mirrors (copies of the same file; the SHA-256 is checked)
    urls = [p['url']] + p.get('mirrors', [])
    errors = []
    for n, url in enumerate(urls):
        if n:
            print(f'  {p["file"]}: from a copy at {url.split("/")[2]}', flush=True)
        try:
            get(url, local)
            if os.path.getsize(local) == p['size'] and sha256(local) == p['sha256']:
                return
            errors.append(f'{url.split("/")[2]}: wrong SHA-256 (damaged, or another file)')
        except (OSError, ValueError, subprocess.CalledProcessError) as e:  # URLError, timeouts, curl.exe
            errors.append(f'{url.split("/")[2]}: {e}')
        if os.path.exists(local):
            os.remove(local)
    sys.exit(offline.missing(p['file'], p['size'], p['sha256'], urls, '; '.join(errors)))


def download(lock, cache):
    """Every payload of a lock file into cache/, checked against its SHA-256 (kept if already there)."""
    os.makedirs(cache, exist_ok=True)
    total = sum(p['size'] for p in lock['payloads'])
    done = 0
    for p in lock['payloads']:
        local = os.path.join(cache, p['file'])
        if not (os.path.exists(local) and os.path.getsize(local) == p['size'] and sha256(local) == p['sha256']):
            obtain(p, local)
            if sha256(local) != p['sha256']:
                os.remove(local)
                sys.exit(f'{p["file"]}: wrong SHA-256 (the download changed or was damaged)')
        done += p['size']
        print(f'  {done / 2**20:,.0f} / {total / 2**20:,.0f} MB  {p["file"]}', flush=True)


# what the clang-cl toolchain (until 2026-11) unpacked into the same folder: removed when zig replaces it
OLD_WINDOWS_DIRS = ('llvm', 'msvc', 'sdk', 'redist')


def fetch(lock_path, dest):
    """zig (MIT; LLVM's clang and libc++ inside, under their Apache licence) into dest/zig, its
    archive kept in dest/downloads."""
    lock = json.load(open(lock_path, encoding='utf-8'))
    dest = os.path.abspath(dest)
    download(lock, os.path.join(dest, 'downloads'))
    payload = next(p for p in lock['payloads'] if p['kind'] == 'zig')
    zig_dir = os.path.join(dest, 'zig')
    shutil.rmtree(zig_dir, ignore_errors=True)
    for old in OLD_WINDOWS_DIRS:
        shutil.rmtree(os.path.join(dest, old), ignore_errors=True)
    os.makedirs(zig_dir)
    archive = os.path.join(dest, 'downloads', payload['file'])
    if archive.endswith('.zip'):
        # Windows' zig: a zip, its files under zig-<arch>-windows-<version>/
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                parts = info.filename.split('/', 1)
                if len(parts) == 2 and parts[1] and not info.is_dir():
                    target = os.path.join(zig_dir, *parts[1].split('/'))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(info) as src, open(target, 'wb') as out:
                        shutil.copyfileobj(src, out)
    else:
        with tarfile.open(archive) as t:
            members = []
            for m in t.getmembers():
                parts = m.name.split('/', 1)
                if len(parts) == 2 and parts[1]:
                    m.name = parts[1]  # without the top folder (zig-<arch>-<os>-<version>/)
                    members.append(m)
            t.extractall(zig_dir, members, filter='tar')
    with open(os.path.join(dest, 'toolchain.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump({k: lock[k] for k in ('schema', 'zig')}, f, indent=1)
    print(f'{dest}: zig {lock["zig"]}')


def run(cmd, env, rsp=None):
    """Run a tool, its arguments in a response file when they are many; return its output if it failed."""
    if rsp:
        with open(rsp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(f'"{a}"' if ' ' in a else a for a in cmd[1:]))
        cmd = [cmd[0], '@' + rsp]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, errors='replace')
    return (r.stdout + r.stderr).strip() if r.returncode else None


def compile_all(work, env, jobs):
    """Run the compile commands in work, jobs at a time; stop at the first failure."""
    print(f'{len(work)} files to compile')
    done = 0
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        for cmd, err in zip(work, pool.map(lambda c: run(c, env), work)):
            done += 1
            if err is not None:
                pool.shutdown(cancel_futures=True)
                sys.exit(f'{cmd[-3]}: compile failed\n{err}')
            if done % 25 == 0 or done == len(work):
                print(f'  {done}/{len(work)}', flush=True)


def zig_executable(toolchain_dir):
    """The zig that fetch unpacked into toolchain_dir, or None."""
    for name in ('zig', 'zig.exe'):
        path = os.path.join(os.path.abspath(toolchain_dir), 'zig', name)
        if os.path.exists(path):
            return path
    return None


def build_modules_zig(gen, modules, build, toolchain_dir, jobs=4, defines=(), android=False):
    """Linux and macOS: generated/<module>/ into <build>/ssd_aot_<module>.so (.dylib) with zig's clang.
    android: ssd_aot_<module>.so for Android arm64 instead, from any host."""
    zig = zig_executable(toolchain_dir)
    if not zig:
        sys.exit(f'no zig in {toolchain_dir}: python -I toolchain.py fetch <lock file> {toolchain_dir}')
    # zig keeps the libc++ it builds on first use in its cache: the toolchain's own folder
    cache = os.path.join(os.path.abspath(toolchain_dir), 'cache')
    env = dict(os.environ, ZIG_GLOBAL_CACHE_DIR=cache, ZIG_LOCAL_CACHE_DIR=cache)
    headers = max(os.path.getmtime(h) for h in glob.glob(os.path.join(RUNTIME, '*.h')))
    target = ANDROID_TARGET if android else ZIG_TARGET
    cxx = [zig, 'c++', *target, *ZIG_CXXFLAGS, *(ANDROID_CXXFLAGS if android else [])]

    work = []
    for m in modules:
        obj_dir = os.path.join(build, 'obj', m)
        os.makedirs(obj_dir, exist_ok=True)
        newest = max(headers, *(os.path.getmtime(h) for h in glob.glob(os.path.join(gen, m, '*.h'))))
        for src in sorted(glob.glob(os.path.join(gen, m, '*.cpp'))):
            obj = os.path.join(obj_dir, os.path.basename(src)[:-4] + '.o')
            if not os.path.exists(obj) or os.path.getmtime(obj) < max(newest, os.path.getmtime(src)):
                work.append([*cxx, *ZIG_GEN_FLAGS, *(f'-D{d}' for d in defines), '-I', RUNTIME,
                             '-I', os.path.join(gen, m), '-c', src, '-o', obj])
    entry = os.path.join(build, 'obj', 'aot_dll.o')
    if not os.path.exists(entry) or os.path.getmtime(entry) < max(headers, os.path.getmtime(os.path.join(RUNTIME, 'aot_dll.cpp'))):
        work.append([*cxx, '-I', RUNTIME, '-c', os.path.join(RUNTIME, 'aot_dll.cpp'), '-o', entry])
    extra = []  # Android: the C++ runtime's few pieces, and the stubs naming bionic's libraries
    if android:
        runtime = os.path.join(build, 'obj', 'aot_android.o')
        source = os.path.join(RUNTIME, 'aot_android.cpp')
        if not os.path.exists(runtime) or os.path.getmtime(runtime) < os.path.getmtime(source):
            work.append([*cxx, '-c', source, '-o', runtime])
        extra.append(runtime)
    compile_all(work, env, jobs)
    if android:
        stubs = os.path.join(build, 'stubs')
        os.makedirs(stubs, exist_ok=True)
        stub_c = os.path.join(stubs, 'stub.c')
        with open(stub_c, 'w', encoding='utf-8', newline='\n') as f:
            f.write('void vita_recomp_kit_stub(void) {}\n')
        stub_libs = []
        for name in ('libc.so', 'libm.so'):
            stub = os.path.join(stubs, name)
            err = run([zig, 'cc', *ANDROID_TARGET, '-shared', '-nostdlib', f'-Wl,-soname,{name}', '-o', stub, stub_c], env)
            if err is not None:
                sys.exit(f'{name} stub: linking failed\n{err}')
            stub_libs.append(stub)
        extra += ['-Wl,--no-as-needed', *stub_libs]

    exports = os.path.join(build, 'exports.map')
    with open(exports, 'w', encoding='utf-8', newline='\n') as f:
        f.write(ZIG_EXPORTS)
    for m in modules:
        objs = sorted(glob.glob(os.path.join(build, 'obj', m, '*.o')))
        lib = os.path.join(build, f'libgen_{m}.a')
        so = os.path.join(build, f'{DLL_PREFIX}{m}' + ('.so' if android else '.dylib' if MACOS else '.dll' if WINDOWS else '.so'))
        if os.path.exists(so) and os.path.getmtime(so) >= max(os.path.getmtime(o) for o in objs + [entry]):
            continue
        if os.path.exists(lib):
            os.remove(lib)  # ar adds to an existing archive
        err = run([zig, 'ar', 'rcs', lib, *objs], env)
        if err is None:
            if android:
                err = run([zig, 'c++', *ANDROID_TARGET, *ANDROID_LDFLAGS, f'-Wl,--version-script,{exports}',
                           '-o', so, entry, lib, *extra], env)
            else:
                unused = (['-Wl,-dead_strip'] if MACOS else ['-Wl,--gc-sections'] if WINDOWS else
                          [f'-Wl,--version-script,{exports}', '-Wl,--gc-sections'])
                err = run([zig, 'c++', *ZIG_TARGET, '-shared', *unused, '-s', '-o', so, entry, lib], env)
        if err is not None:
            sys.exit(f'{m}: linking failed\n{err}')
    # zig's cache holds a copy of every object (1.5 GB for Soul Sacrifice Delta) besides libc++:
    # the objects are in <build>/obj already, and libc++ is rebuilt in a minute when needed again
    shutil.rmtree(cache, ignore_errors=True)


def build_modules(gen, modules, build, toolchain_dir, jobs=4, defines=(), android=False):
    """Compile generated/<module>/ into <build>/ssd_aot_<module>.dll (.so on Linux, .dylib on macOS;
    android: .so for Android arm64, from any host) with the zig in toolchain_dir. defines: extra
    preprocessor definitions for the generated code (RECOMP_BISECT)."""
    return build_modules_zig(gen, modules, build, toolchain_dir, jobs, defines, android)


def main():
    a = sys.argv[1:]
    if len(a) >= 3 and a[0] == 'fetch':
        fetch(a[1], a[2])  # a --accept-microsoft-license after them, from older scripts: nothing to accept now
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
