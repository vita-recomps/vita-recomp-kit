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

<dir> then holds llvm/bin/{clang-cl,lld-link,llvm-lib}.exe, msvc/{include,lib/x64} and
sdk/{Include,Lib}/<version>/. build-recomp.py --toolchain <dir> builds the recompiled modules with it.
"""
import hashlib, json, os, re, shutil, subprocess, sys, tempfile, urllib.request, zipfile

CHANNEL = 'https://aka.ms/vs/17/release/channel'
MICROSOFT_LICENSE = 'https://go.microsoft.com/fwlink/?LinkId=2179911'  # Visual Studio Build Tools
LLVM_MINGW_API = 'https://api.github.com/repos/mstorsjo/llvm-mingw/releases/latest'
# the MSVC runtime packages (VSIX) and the Windows SDK installers (MSI, with their cabinets)
CRT_PACKAGES = ['CRT.Headers.base', 'CRT.x64.Desktop.base', 'CRT.x64.Store.base']
SDK_MSIS = [
    'Windows SDK for Windows Store Apps Headers-x86_en-us.msi',
    'Windows SDK for Windows Store Apps Headers OnecoreUap-x86_en-us.msi',
    'Windows SDK for Windows Store Apps Libs-x86_en-us.msi',
    'Windows SDK Desktop Headers x86-x86_en-us.msi',
    'Windows SDK Desktop Headers x64-x86_en-us.msi',
    'Windows SDK Desktop Libs x64-x86_en-us.msi',
    'Universal CRT Headers Libraries and Sources-x86_en-us.msi',
]
# what a build needs from llvm-mingw: the real compiler (bin/clang.exe there is a wrapper that reads a
# MinGW target from its name), the linker, the librarian, the DLLs they load and clang's headers
LLVM_KEEP = re.compile(r'^[^/]+/(bin/(clang-\d+|ld\.lld|llvm-ar)\.exe|bin/[^/]+\.dll|lib/clang/\d+/include/.+)$')


def get(url, path=None):
    req = urllib.request.Request(url, headers={'User-Agent': 'vita-recomp-kit toolchain.py'})
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


def fetch(lock_path, dest, accepted):
    lock = json.load(open(lock_path, encoding='utf-8'))
    if not accepted:
        sys.exit('The toolchain includes Microsoft\'s C++ runtime and Windows SDK, under Microsoft\'s licence:\n'
                 f'  {lock["microsoft_license"]}\nRead it, then run again with --accept-microsoft-license.')
    dest = os.path.abspath(dest)
    cache = os.path.join(dest, 'downloads')
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

    # the MSVC runtime: VSIX archives (zip), Contents/VC/Tools/MSVC/<version>/{include,lib/x64}
    msvc_dir = os.path.join(dest, 'msvc')
    shutil.rmtree(msvc_dir, ignore_errors=True)
    for p in lock['payloads']:
        if p['kind'] != 'vsix':
            continue
        with zipfile.ZipFile(os.path.join(cache, p['file'])) as z:
            for name in z.namelist():
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
                r = subprocess.run(['msiexec.exe', '/a', os.path.join(tmp, p['file']), '/quiet', '/qn', f'TARGETDIR={target}'])
                if r.returncode:
                    sys.exit(f'{p["file"]}: msiexec failed ({r.returncode})')
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
