"""A compiler for the recompiled modules that needs no Visual Studio: clang-cl and lld-link from
llvm-mingw, with Microsoft's C++ runtime and Windows SDK packages downloaded from Microsoft's own
Visual Studio release manifest (the way Nebula's setup does it). Every file is pinned by SHA-256 in a
lock file; Microsoft's files come under Microsoft's licence, which the player accepts, and are never
redistributed by this project.

usage:
  python -I toolchain.py resolve <lock.json>       pin the current llvm-mingw release, MSVC runtime
                                                   and Windows SDK (needs the network; for maintainers)
  python -I toolchain.py fetch <lock.json> <dir> --accept-microsoft-license
                                                   download, check and unpack the toolchain into <dir>
  python -I toolchain.py env <dir>                 the INCLUDE, LIB and tool paths for a build, as JSON

<dir> then holds llvm/bin/{clang-cl,lld-link,llvm-lib}.exe, msvc/{include,lib/x64},
sdk/{Include,Lib}/<version>/ and redist/ (the VC++ runtime DLLs a game's folder carries, as Microsoft
allows for an application's own folder). build-recomp.py --toolchain <dir> builds the recompiled modules with it.

Linux (toolchain-linux.json) and macOS (toolchain-macos.json, Apple Silicon): the toolchain is zig
alone, a self-contained clang with libc++ and the system's link stubs (glibc's; macOS's libSystem),
under MIT and LLVM's Apache licence (nothing to accept, no Xcode or Command Line Tools). fetch unpacks
it into <dir>/zig/, and the modules come out as ssd_aot_<module>.so (.dylib on macOS, ad-hoc signed by
zig's linker, as Apple Silicon requires); zig keeps what it builds on first use (libc++) in <dir>/cache/.
  python -I toolchain.py fetch toolchain-linux.json|toolchain-macos.json <dir>

Android (arm64-v8a), from any of the three: zig as well (toolchain-android-windows.json on Windows; the
Linux and macOS lock files' zig serves there), with musl's headers and no C library linked: the modules
bind to bionic's libc.so and libm.so when the game's APK loads them. recomp/runtime/aot_android.cpp
brings the little a module needs from the C++ runtime (ss-delta docs/09-android.md).
  python -I build-recomp.py --android --toolchain <dir>
"""
import concurrent.futures, glob, hashlib, json, os, re, shutil, subprocess, sys, tarfile, tempfile, urllib.error, urllib.request, zipfile

MACOS = sys.platform == 'darwin'

CHANNEL = 'https://aka.ms/vs/17/release/channel'
MICROSOFT_LICENSE = 'https://go.microsoft.com/fwlink/?LinkId=2179911'  # Visual Studio Build Tools
LLVM_MINGW_API = 'https://api.github.com/repos/mstorsjo/llvm-mingw/releases/latest'
# the MSVC runtime packages (VSIX) and the Windows SDK installers (MSI, with their cabinets)
CRT_PACKAGES = ['CRT.Headers.base', 'CRT.x64.Desktop.base', 'CRT.x64.Store.base', 'CRT.Redist.X64.base']
SDK_MSIS = [
    'Windows SDK for Windows Store Apps Headers-x86_en-us.msi',
    'Windows SDK for Windows Store Apps Headers OnecoreUap-x86_en-us.msi',
    'Windows SDK for Windows Store Apps Libs-x86_en-us.msi',
    'Windows SDK Desktop Headers x86-x86_en-us.msi',
    'Windows SDK Desktop Headers x64-x86_en-us.msi',
    'Windows SDK Desktop Libs x64-x86_en-us.msi',
    'Universal CRT Headers Libraries and Sources-x86_en-us.msi',
]
RECOMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'recomp')
RUNTIME = os.path.join(RECOMP, 'runtime')
DLL_PREFIX = 'ssd_aot_'  # the name the Vita3K fork loads (RECOMP_DLL_PREFIX in recomp/CMakeLists.txt)
# recomp/CMakeLists.txt's Release flags, as clang-cl takes them
CXXFLAGS = ['/nologo', '/TP', '/DWIN32', '/D_WINDOWS', '/EHsc', '/O2', '/Ob2', '/DNDEBUG', '/std:c++20', '/MD',
            '--target=x86_64-pc-windows-msvc']
GEN_FLAGS = ['/bigobj', '/O1', '/w', '/DRECOMP_COUNT_INSNS']
SYSTEM_LIBS = ['kernel32.lib', 'user32.lib', 'gdi32.lib', 'winspool.lib', 'shell32.lib', 'ole32.lib',
               'oleaut32.lib', 'uuid.lib', 'comdlg32.lib', 'advapi32.lib']
# Linux: zig's clang, which brings libc++ and the glibc stubs to link against (no sysroot). The target
# glibc is the oldest the front end needs too; the baseline CPU keeps clang to what the Windows build
# and GCC assume (no FMA contraction, no AVX). macOS: the fork's deployment target (CMakeLists.txt,
# CMAKE_OSX_DEPLOYMENT_TARGET) and the first Apple Silicon CPU
ZIG_TARGET = (['-target', 'aarch64-macos.13.3', '-mcpu=apple_m1'] if MACOS else
              ['-target', 'x86_64-linux-gnu.2.30', '-mcpu=baseline'])
ZIG_CXXFLAGS = ['-std=c++20', '-O2', '-fPIC', '-fvisibility=hidden', '-fvisibility-inlines-hidden', '-DNDEBUG']
# no contraction, as in recomp/CMakeLists.txt: ARMv7's vmla/vmls round twice (it matters on arm64 hosts)
ZIG_GEN_FLAGS = ['-O1', '-w', '-ffp-contract=off', '-DRECOMP_COUNT_INSNS']
# a module exports its entry points (armrecomp_aot_init, armrecomp_aot_features) and nothing else
# (libc++'s operator new/delete stay inside). zig's Mach-O linker takes no export list: there, hidden
# visibility leaves the entry points and libc++'s
# operator new/delete, which two-level namespaces keep from binding anything outside the module
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
# what a build needs from llvm-mingw: the real compiler (bin/clang.exe there is a wrapper that reads a
# MinGW target from its name), the linker, the librarian, the DLLs they load and clang's headers
LLVM_KEEP = re.compile(r'^[^/]+/(bin/(clang-\d+|ld\.lld|llvm-ar)\.exe|bin/[^/]+\.dll|lib/clang/\d+/include/.+)$')


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


def msi_admin_install(msi, target, *options):
    """Unpack an MSI into target with an administrative install (no elevation); msiexec's exit code.
    msiexec reads PROPERTY="value" and nothing else: Python's quoting of a list turns a TARGETDIR
    with a space (a temp folder under a user name with a space) into "TARGETDIR=...", which msiexec
    rejects with 1639 and its usage window (ss-delta issue #9). Hence a command line written by hand."""
    cmd = subprocess.list2cmdline(['msiexec.exe', '/a', os.path.abspath(msi), *options]) + f' TARGETDIR="{os.path.abspath(target)}"'
    return subprocess.run(cmd).returncode


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def resolve(lock_path):
    channel = json.loads(get(CHANNEL))
    item = next(i for i in channel['channelItems'] if i['id'] == 'Microsoft.VisualStudio.Manifests.VisualStudio')
    manifest = json.loads(get(item['payloads'][0]['url']))
    packages = manifest['packages']
    by_id = {}
    for p in packages:
        by_id.setdefault(p['id'], p)
    crt_versions = sorted((m.group(1) for p in packages
                           if (m := re.match(r'Microsoft\.VC\.(\d+\.\d+\.\d+\.\d+)\.CRT\.Headers\.base$', p['id']))),
                          key=lambda v: tuple(map(int, v.split('.'))))
    msvc = crt_versions[-1]
    sdk_id = sorted((p['id'] for p in packages if re.match(r'Win11SDK_10\.0\.\d+$', p['id'])),
                    key=lambda s: int(s.rsplit('.', 1)[1]))[-1]
    payloads = []
    for name in CRT_PACKAGES:
        for pl in by_id[f'Microsoft.VC.{msvc}.{name}']['payloads']:
            payloads.append({'kind': 'vsix', 'file': pl['fileName'], 'url': pl['url'], 'sha256': pl['sha256'].lower(), 'size': pl['size']})
    sdk = {pl['fileName'].replace('\\', '/').split('/')[-1]: pl for pl in by_id[sdk_id]['payloads']}
    with tempfile.TemporaryDirectory() as tmp:
        for msi in SDK_MSIS:
            pl = sdk[msi]
            payloads.append({'kind': 'msi', 'file': msi, 'url': pl['url'], 'sha256': pl['sha256'].lower(), 'size': pl['size']})
            # an MSI names its cabinets in its Media table: they are separate payloads of the SDK
            local = os.path.join(tmp, 'x.msi')
            get(pl['url'], local)
            for cab in sorted(set(re.findall(rb'[0-9a-f]{32}\.cab', open(local, 'rb').read()))):
                c = sdk[cab.decode()]
                payloads.append({'kind': 'cab', 'file': cab.decode(), 'url': c['url'], 'sha256': c['sha256'].lower(), 'size': c['size']})
    release = json.loads(get(LLVM_MINGW_API))
    asset = next(a for a in release['assets'] if re.match(r'llvm-mingw-[\d.]+-ucrt-x86_64\.zip$', a['name']))
    digest = (asset.get('digest') or '').removeprefix('sha256:')
    if not digest:
        with tempfile.TemporaryDirectory() as tmp:
            get(asset['browser_download_url'], os.path.join(tmp, 'z'))
            digest = sha256(os.path.join(tmp, 'z'))
    payloads.append({'kind': 'llvm', 'file': asset['name'], 'url': asset['browser_download_url'], 'sha256': digest, 'size': asset['size']})
    lock = {
        'schema': 'vita-recomp-kit.toolchain.v1',
        'llvm_mingw': release['tag_name'],
        'msvc': msvc,
        'sdk': sdk_id,
        'visual_studio': channel['info'].get('productDisplayVersion', ''),
        'microsoft_license': MICROSOFT_LICENSE,
        'download_bytes': sum(p['size'] for p in payloads),
        'payloads': payloads,
    }
    with open(lock_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(lock, f, indent=1)
    print(f'{lock_path}: llvm-mingw {lock["llvm_mingw"]}, MSVC {msvc}, {sdk_id}, '
          f'{len(payloads)} files, {lock["download_bytes"] / 2**20:,.0f} MB')


def download(lock, cache):
    """Every payload of a lock file into cache/, checked against its SHA-256 (kept if already there)."""
    os.makedirs(cache, exist_ok=True)
    total = sum(p['size'] for p in lock['payloads'])
    done = 0
    for p in lock['payloads']:
        local = os.path.join(cache, p['file'])
        if not (os.path.exists(local) and os.path.getsize(local) == p['size'] and sha256(local) == p['sha256']):
            get(p['url'], local)
            if sha256(local) != p['sha256']:
                os.remove(local)
                sys.exit(f'{p["file"]}: wrong SHA-256 (the download changed or was damaged)')
        done += p['size']
        print(f'  {done / 2**20:,.0f} / {total / 2**20:,.0f} MB  {p["file"]}', flush=True)


def fetch_zig(lock, dest):
    """Linux and macOS (and Android's modules from Windows): zig (MIT; LLVM's clang and libc++ inside,
    under their Apache licence) into dest/zig."""
    download(lock, os.path.join(dest, 'downloads'))
    payload = next(p for p in lock['payloads'] if p['kind'] == 'zig')
    zig_dir = os.path.join(dest, 'zig')
    shutil.rmtree(zig_dir, ignore_errors=True)
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


def fetch(lock_path, dest, accepted):
    lock = json.load(open(lock_path, encoding='utf-8'))
    if 'zig' in lock:
        return fetch_zig(lock, os.path.abspath(dest))
    if not accepted:
        sys.exit('The toolchain includes Microsoft\'s C++ runtime and Windows SDK, under Microsoft\'s licence:\n'
                 f'  {lock["microsoft_license"]}\nRead it, then run again with --accept-microsoft-license.')
    dest = os.path.abspath(dest)
    cache = os.path.join(dest, 'downloads')
    download(lock, cache)

    # the MSVC runtime: VSIX archives (zip), Contents/VC/Tools/MSVC/<version>/{include,lib/x64}
    msvc_dir = os.path.join(dest, 'msvc')
    redist_dir = os.path.join(dest, 'redist')
    shutil.rmtree(msvc_dir, ignore_errors=True)
    shutil.rmtree(redist_dir, ignore_errors=True)
    for p in lock['payloads']:
        if p['kind'] != 'vsix':
            continue
        with zipfile.ZipFile(os.path.join(cache, p['file'])) as z:
            for name in z.namelist():
                r = re.match(r'Contents/VC/Redist/MSVC/[^/]+/x64/Microsoft\.VC\d+\.CRT/([^/]+\.dll)$', name)
                if r:
                    os.makedirs(redist_dir, exist_ok=True)
                    with z.open(name) as src, open(os.path.join(redist_dir, r.group(1)), 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                    continue
                m = re.match(r'Contents/VC/Tools/MSVC/[^/]+/((include|lib/x64)/.+)$', name.replace('%20', ' '))
                # x64 desktop only: no store/uwp/enclave/onecore variants, no debug symbols
                if m and not name.endswith('/') and not name.endswith('.pdb')                         and not re.match(r'lib/x64/[^/]+/', m.group(1)):
                    out = os.path.join(msvc_dir, *m.group(1).split('/'))
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with z.open(name) as src, open(out, 'wb') as dst:
                        shutil.copyfileobj(src, dst)

    # the Windows SDK: an administrative install of each MSI (no elevation), its cabinets beside it
    sdk_dir = os.path.join(dest, 'sdk')
    shutil.rmtree(sdk_dir, ignore_errors=True)
    with tempfile.TemporaryDirectory() as tmp:
        for p in lock['payloads']:
            if p['kind'] not in ('msi', 'cab'):
                continue
            shutil.copy2(os.path.join(cache, p['file']), os.path.join(tmp, p['file']))
        target = os.path.join(tmp, 'out')
        for p in lock['payloads']:
            if p['kind'] == 'msi':
                code = msi_admin_install(os.path.join(tmp, p['file']), target, '/quiet', '/qn')
                if code:
                    sys.exit(f'{p["file"]}: msiexec failed ({code})')
        kits = os.path.join(target, 'Windows Kits', '10')
        shutil.copytree(os.path.join(kits, 'Include'), os.path.join(sdk_dir, 'Include'))
        for version in os.listdir(os.path.join(kits, 'Lib')):
            for part in ('ucrt', 'um'):  # x64 only
                shutil.copytree(os.path.join(kits, 'Lib', version, part, 'x64'),
                                os.path.join(sdk_dir, 'Lib', version, part, 'x64'))

    # llvm-mingw: only what a build needs, and the names that select clang's and lld's MSVC modes
    llvm_dir = os.path.join(dest, 'llvm')
    shutil.rmtree(llvm_dir, ignore_errors=True)
    llvm = next(p for p in lock['payloads'] if p['kind'] == 'llvm')
    with zipfile.ZipFile(os.path.join(cache, llvm['file'])) as z:
        for name in z.namelist():
            if LLVM_KEEP.match(name) and not name.endswith('/'):
                out = os.path.join(llvm_dir, *name.split('/')[1:])
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with z.open(name) as src, open(out, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
    bin_dir = os.path.join(llvm_dir, 'bin')
    clang = next(f for f in os.listdir(bin_dir) if re.match(r'clang-\d+\.exe$', f))
    for src, alias in ((clang, 'clang-cl.exe'), ('ld.lld.exe', 'lld-link.exe'), ('llvm-ar.exe', 'llvm-lib.exe')):
        shutil.copy2(os.path.join(bin_dir, src), os.path.join(bin_dir, alias))
    with open(os.path.join(dest, 'toolchain.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump({k: lock[k] for k in ('schema', 'llvm_mingw', 'msvc', 'sdk')}, f, indent=1)
    print(f'{dest}: llvm-mingw {lock["llvm_mingw"]}, MSVC {lock["msvc"]}, {lock["sdk"]}')


def environment(dest):
    """INCLUDE and LIB for clang-cl and lld-link, and the tools, for a toolchain fetch() unpacked."""
    dest = os.path.abspath(dest)
    sdk_inc = os.path.join(dest, 'sdk', 'Include')
    version = sorted(os.listdir(sdk_inc), key=lambda v: tuple(int(x) for x in v.split('.') if x.isdigit()))[-1]
    inc = [os.path.join(dest, 'msvc', 'include')] + [os.path.join(sdk_inc, version, d) for d in ('ucrt', 'shared', 'um', 'winrt', 'cppwinrt')]
    lib = [os.path.join(dest, 'msvc', 'lib', 'x64')] + [os.path.join(dest, 'sdk', 'Lib', version, d, 'x64') for d in ('ucrt', 'um')]
    bin_dir = os.path.join(dest, 'llvm', 'bin')
    return {
        'INCLUDE': os.pathsep.join(inc),
        'LIB': os.pathsep.join(lib),
        'cl': os.path.join(bin_dir, 'clang-cl.exe'),
        'link': os.path.join(bin_dir, 'lld-link.exe'),
        'lib': os.path.join(bin_dir, 'llvm-lib.exe'),
        'path': bin_dir,
    }


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
        so = os.path.join(build, f'{DLL_PREFIX}{m}' + ('.dylib' if MACOS and not android else '.so'))
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
                unused = ['-Wl,-dead_strip'] if MACOS else [f'-Wl,--version-script,{exports}', '-Wl,--gc-sections']
                err = run([zig, 'c++', *ZIG_TARGET, '-shared', *unused, '-s', '-o', so, entry, lib], env)
        if err is not None:
            sys.exit(f'{m}: linking failed\n{err}')
    # zig's cache holds a copy of every object (1.5 GB for Soul Sacrifice Delta) besides libc++:
    # the objects are in <build>/obj already, and libc++ is rebuilt in a minute when needed again
    shutil.rmtree(cache, ignore_errors=True)


def build_modules(gen, modules, build, toolchain_dir, jobs=4, defines=(), android=False):
    """Compile generated/<module>/ into <build>/ssd_aot_<module>.dll (Windows; .so or .dylib with zig on
    Linux and macOS; android: .so for Android arm64, with zig from any host)
    with the toolchain in toolchain_dir. defines: extra preprocessor definitions for the generated
    code (RECOMP_BISECT)."""
    if android or zig_executable(toolchain_dir):
        return build_modules_zig(gen, modules, build, toolchain_dir, jobs, defines, android)
    tc = environment(toolchain_dir)
    env = dict(os.environ, INCLUDE=tc['INCLUDE'], LIB=tc['LIB'])
    env['PATH'] = tc['path'] + os.pathsep + env.get('PATH', '')
    headers = max(os.path.getmtime(h) for h in glob.glob(os.path.join(RUNTIME, '*.h')))

    # compile: every generated file whose object is older than it or than the headers
    work = []
    for m in modules:
        obj_dir = os.path.join(build, 'obj', m)
        os.makedirs(obj_dir, exist_ok=True)
        newest = max(headers, *(os.path.getmtime(h) for h in glob.glob(os.path.join(gen, m, '*.h'))))
        for src in sorted(glob.glob(os.path.join(gen, m, '*.cpp'))):
            obj = os.path.join(obj_dir, os.path.basename(src)[:-4] + '.obj')
            if not os.path.exists(obj) or os.path.getmtime(obj) < max(newest, os.path.getmtime(src)):
                work.append([tc['cl'], *CXXFLAGS, *GEN_FLAGS, *(f'/D{d}' for d in defines), '/I', RUNTIME, '/I', os.path.join(gen, m),
                             '/c', src, '/Fo' + obj])
    entry = os.path.join(build, 'obj', 'aot_dll.obj')
    if not os.path.exists(entry) or os.path.getmtime(entry) < max(headers, os.path.getmtime(os.path.join(RUNTIME, 'aot_dll.cpp'))):
        work.append([tc['cl'], *CXXFLAGS, '/I', RUNTIME, '/c', os.path.join(RUNTIME, 'aot_dll.cpp'), '/Fo' + entry])
    print(f'{len(work)} files to compile')
    done = 0
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        for cmd, err in zip(work, pool.map(lambda c: run(c, env), work)):
            done += 1
            if err is not None:
                pool.shutdown(cancel_futures=True)
                sys.exit(f'{cmd[-2]}: compile failed\n{err}')
            if done % 25 == 0 or done == len(work):
                print(f'  {done}/{len(work)}', flush=True)

    # archive and link, as the CMake build does: gen_<module>.lib, then the DLL around aot_dll.obj
    for m in modules:
        objs = sorted(glob.glob(os.path.join(build, 'obj', m, '*.obj')))
        lib = os.path.join(build, f'gen_{m}.lib')
        dll = os.path.join(build, f'{DLL_PREFIX}{m}.dll')
        if os.path.exists(dll) and os.path.getmtime(dll) >= max(os.path.getmtime(o) for o in objs + [entry]):
            continue
        err = run([tc['lib'], '/nologo', '/out:' + lib, *objs], env, os.path.join(build, f'gen_{m}.rsp'))
        if err is None:
            err = run([tc['link'], '/nologo', '/dll', '/machine:x64', '/INCREMENTAL:NO', '/out:' + dll,
                       '/implib:' + os.path.join(build, f'{DLL_PREFIX}{m}.lib'), entry, lib, *SYSTEM_LIBS], env)
        if err is not None:
            sys.exit(f'{m}: linking failed\n{err}')


def main():
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == 'resolve':
        resolve(a[1])
    elif len(a) >= 3 and a[0] == 'fetch':
        fetch(a[1], a[2], '--accept-microsoft-license' in a[3:])
    elif len(a) >= 2 and a[0] == 'env':
        print(json.dumps(environment(a[1]), indent=1))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
