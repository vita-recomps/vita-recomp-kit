"""From the player's own dump to a playable folder of a game's port (ss-delta D11's install flow):
  1. install the firmware update(s) and the game (app, patch, add-ons) into a fresh Vita file
     system, through the game's front end;
  2. check that eboot.bin is a build the port supports (game.json eboot_sha256);
  3. run the game briefly to write every module's ELF as loaded (decrypted, relocated);
  4. recompile the modules game.json lists and build them;
  5. lay out the folder (make-dist.py).
Everything it writes is derived from the player's dump and firmware: it stays local.

usage:
  python -I install-game.py [--project DIR] --firmware PUP [PUP ...]
                            --install <zip|vpk|folder|pkg> [--license <work.bin|rif> | --zrif Z]
                            [--install ...] [--name NAME | --out DIR] [--dump-seconds N] [--no-recompile]
                            [--no-jit] [--bin DIR] [--toolchain DIR] [--jobs N]

  --install   repeat for each part, in order (the game, then its patch, then add-ons); a pkg's
              --license or --zrif follows it. A patch's or add-on's licence can be omitted when
              the game's licence covers it (installed before it).
  --no-recompile  stop after step 2: the folder runs everything on the JIT
  --no-jit        package the front end built without dynarmic (build-vita3k.py --no-jit): native
                  code only. The install and the ELF dump still use the JIT build.
  --bin           the front end's folder from a prebuilt release instead of the project's build
                  (it installs, dumps and is packaged)
  --toolchain     compile with the toolchain toolchain.py unpacked there (no Visual Studio, CMake or
                  Ninja); otherwise MSVC, CMake and Ninja
  --out           the playable folder (default dist/<name>); the work files go to <out>-work
"""
import argparse
import glob
import hashlib
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import msvc  # noqa: E402
import toolchain  # noqa: E402
from gameproject import Project  # noqa: E402

RECOMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'recomp')


def main():
    # --install/--license/--zrif keep their order, so they are taken apart by hand
    argv = sys.argv[1:]
    content = []
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] in ('--install', '--license', '--zrif') and i + 1 < len(argv):
            content += [argv[i], os.path.abspath(argv[i + 1]) if argv[i] != '--zrif' else argv[i + 1]]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument('--project', default=os.getcwd())
    ap.add_argument('--firmware', nargs='+', required=True)
    ap.add_argument('--name', default='')
    ap.add_argument('--dump-seconds', type=int, default=0)
    ap.add_argument('--no-recompile', action='store_true')
    ap.add_argument('--no-jit', action='store_true')
    ap.add_argument('--bin', default='')
    ap.add_argument('--toolchain', default='')
    ap.add_argument('--out', default='')
    ap.add_argument('--jobs', type=int, default=4)
    a = ap.parse_args(rest)
    if '--install' not in content:
        sys.exit('nothing to install: --install <game dump>')

    p = Project(a.project)
    exe = p.exe(bin_dir=a.bin)
    bin_dir = os.path.dirname(exe)
    name = a.name or p.id
    if a.out:
        out = os.path.abspath(a.out)
        work = out + '-work'
    else:
        out = os.path.join(p.root, 'dist', name)
        work = os.path.join(p.root, 'build', 'install-' + name)
    fs = os.path.join(out, 'fs')
    os.makedirs(fs, exist_ok=True)
    os.makedirs(work, exist_ok=True)

    def front_end(args, extra_env=None):
        env = dict(os.environ, **(extra_env or {}))
        return subprocess.run([exe, '--vita-fs', fs] + args, cwd=bin_dir, env=env).returncode

    # 1. firmware and game
    args = []
    for pup in a.firmware:
        args += ['--install-firmware', os.path.abspath(pup)]
    print('installing', ' '.join(os.path.basename(x) for x in a.firmware + content[1::2] if os.path.exists(x)))
    if front_end(args + content):
        sys.exit(f'installation failed: see {os.path.join(bin_dir, p.id, "vita3k.log")}')

    # 2. a supported build
    eboot = os.path.join(fs, 'ux0', 'app', p.title_id, 'eboot.bin')
    if not os.path.exists(eboot):
        sys.exit(f'no {p.title_id} in {fs} after installing')
    with open(eboot, 'rb') as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    if p.eboot_sha256 and digest not in p.eboot_sha256:
        supported = ', '.join(sorted(set(p.eboot_sha256.values())))
        sys.exit(f'eboot.bin is not a build this port supports ({supported}; SHA-256 {digest}): '
                 'install the game\'s update with it')
    print('eboot.bin:', p.eboot_sha256.get(digest, digest))

    recompiled_dir = os.path.join(work, 'build')
    if not a.no_recompile and p.modules:
        # 3. the modules' ELFs, written as the game loads them (on the JIT: nothing recompiled yet)
        dumps = os.path.join(bin_dir, p.id, 'elfdumps', p.title_id)
        for old in glob.glob(os.path.join(dumps, '*.elf')):
            os.remove(old)
        seconds = a.dump_seconds or p.dump_seconds
        print(f'running the game for {seconds} s to write the modules')
        front_end(['--dump-elfs', str(seconds)], {'SSD_AOT': 'none', 'SSD_AOT_NATIVE': '0'})
        elf_dir = os.path.join(work, 'elf')
        os.makedirs(elf_dir, exist_ok=True)
        for m in p.modules:
            found = glob.glob(os.path.join(dumps, f'*_{m}.elf'))
            if not found:
                sys.exit(f'the game did not load {m} within {seconds} s: try a longer --dump-seconds')
            shutil.copy2(found[0], os.path.join(elf_dir, m + '.elf'))

        # 4. recompile and build
        gen = os.path.join(work, 'generated')
        for m, target in p.modules.items():
            print('recompiling', m)
            base = ['--load-base', p.load_bases[m]] if m in p.load_bases else []
            # recompile.py needs its own folder on the path, so it runs without -I
            r = subprocess.run([sys.executable, os.path.join(RECOMP, 'recompile.py'), os.path.join(elf_dir, m + '.elf'),
                                os.path.join(gen, target)] + base, capture_output=True, text=True)
            for line in r.stdout.splitlines():
                if 'recompiled' in line or 'code hash' in line:
                    print(' ', line)
            if r.returncode:
                sys.exit(f'recompiling {m} failed:\n{r.stderr}')
        print('compiling (this takes a while)')
        if a.toolchain:
            toolchain.build_modules(gen, list(p.modules.values()), recompiled_dir, a.toolchain, a.jobs)
        else:
            env = msvc.environment()
            if subprocess.run(['cmake', '-S', RECOMP, '-B', recompiled_dir, '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release',
                               f'-DGENERATED_DIR={gen}', '-DRECOMP_MODULES=' + ';'.join(p.modules.values()),
                               '-DRECOMP_TESTS=OFF', f'-DRECOMP_JOBS={a.jobs}'], env=env,
                              capture_output=True).returncode:
                sys.exit('configuring the recompiled modules failed')
            if subprocess.run(['cmake', '--build', recompiled_dir], env=env).returncode:
                sys.exit('compiling the recompiled modules failed')

    # 5. the folder; the Vita file system is its own fs/, the ini's default
    dist = [sys.executable, '-I', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'make-dist.py'),
            '--project', p.root, '--out', out]
    dist += ['--bin', bin_dir] if a.bin else []
    dist += ['--redist', os.path.join(a.toolchain, 'redist')] if a.toolchain else []
    dist += ['--recompiled-dir', recompiled_dir] if not a.no_recompile and p.modules else ['--no-recompiled']
    dist += ['--no-jit'] if a.no_jit else []
    sys.exit(subprocess.run(dist).returncode)


if __name__ == '__main__':
    main()
