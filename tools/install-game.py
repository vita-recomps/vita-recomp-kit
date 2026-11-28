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
                            [--no-jit] [--bin DIR] [--exe FILE] [--work DIR] [--toolchain DIR] [--jobs N]
                            [--android DIR [--android-only]]

  --install   repeat for each part, in order (the game, then its patch, then add-ons); a pkg's
              --license or --zrif follows it. A patch's or add-on's licence can be omitted when
              the game's licence covers it (installed before it).
  --no-recompile  stop after step 2: the folder runs everything on the JIT
  --no-jit        package the front end built without dynarmic (build-vita3k.py --no-jit): native
                  code only. The install and the ELF dump still use the JIT build.
  --bin           the front end's folder from a prebuilt release instead of the project's build
                  (it installs, dumps and is packaged); it may be --out itself (a release folder
                  that builds in place: the front end's files stay as they are)
  --exe           the front end's executable, when it isn't <bin>/<id><suffix> (macOS: inside the
                  release's app bundle)
  --work          the work files (default <out>-work)
  --toolchain     compile with the zig toolchain.py unpacked there (no Visual Studio, CMake or Ninja);
                  otherwise MSVC, CMake and Ninja
  --out           the playable folder (default dist/<name>); the work files go to <out>-work
  --android       also build the modules for the game's Android APK (arm64), with the zig toolchain.py
                  unpacked in DIR (the PC's, --toolchain's), into <out>/android/; android.py push puts
                  them and fs/ on a device
  --android-only  only the modules for Android: no PC modules, the folder itself left as it is. Without
                  --install (and --firmware), the game already installed in <out>/fs is used: a built
                  folder needs no dump again
"""
import argparse
import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dumpinfo  # noqa: E402
import msvc  # noqa: E402
import toolchain  # noqa: E402
from gameproject import Project  # noqa: E402

RECOMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'recomp')


def step_error(folder):
    """The front end's step-error.txt in the game's log folder: a line per part it could not install,
    in words for the player; '' when it left none."""
    try:
        with open(os.path.join(folder, 'step-error.txt'), encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


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
    ap.add_argument('--firmware', nargs='+', default=[])
    ap.add_argument('--name', default='')
    ap.add_argument('--dump-seconds', type=int, default=0)
    ap.add_argument('--no-recompile', action='store_true')
    ap.add_argument('--no-jit', action='store_true')
    ap.add_argument('--bin', default='')
    ap.add_argument('--exe', default='')
    ap.add_argument('--work', default='')
    ap.add_argument('--toolchain', default='')
    ap.add_argument('--out', default='')
    ap.add_argument('--jobs', type=int, default=4)
    ap.add_argument('--android', default='')
    ap.add_argument('--android-only', action='store_true')
    a = ap.parse_args(rest)
    if a.android and not toolchain.zig_executable(a.android):
        sys.exit(f'--android {a.android}: no zig there (python -I toolchain.py fetch <lock file> {a.android})')
    if a.android_only and not a.android:
        sys.exit('--android-only needs --android <toolchain dir>')
    reuse = '--install' not in content and a.android_only
    if '--install' not in content and not reuse:
        sys.exit('nothing to install: --install <game dump>')
    if not reuse and not a.firmware:
        sys.exit('--firmware: the PS Vita firmware update(s) to install (PSVUPDAT.PUP, PSP2UPDAT.PUP)')
    # the front end takes each part as --install-content (its own --install reopens a release's importer)
    content = ['--install-content' if x == '--install' else x for x in content]

    p = Project(a.project)
    exe = os.path.abspath(a.exe) if a.exe else p.exe(bin_dir=a.bin)
    # the folder the front end treats as its own (settings, logs, elfdumps): --bin, else the exe's
    bin_dir = os.path.abspath(a.bin) if a.bin else os.path.dirname(exe)
    name = a.name or p.id
    if a.out:
        out = os.path.abspath(a.out)
        work = out + '-work'
    else:
        out = os.path.join(p.root, 'dist', name)
        work = os.path.join(p.root, 'build', 'install-' + name)
    work = os.path.abspath(a.work) if a.work else work
    fs = os.path.join(out, 'fs')
    os.makedirs(fs, exist_ok=True)
    os.makedirs(work, exist_ok=True)

    def front_end(args, extra_env=None, drop=None):
        """Run the front end; output lines matching drop are left out."""
        env = dict(os.environ, **(extra_env or {}))
        cmd = [exe, '--game', bin_dir, '--vita-fs', fs] + args
        if not drop:
            return subprocess.run(cmd, cwd=bin_dir, env=env).returncode
        proc = subprocess.Popen(cmd, cwd=bin_dir, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors='replace')
        for line in proc.stdout:
            if not drop.search(line):
                sys.stdout.write(line)
        return proc.wait()

    # 1. firmware and game (--android-only without --install: the game installed already)
    if reuse:
        if not p.installed_title(fs):
            sys.exit(f'no {" or ".join(p.title_ids)} in {fs}: build the game first (or pass --install)')
        print(f'the game installed in {fs}')
    else:
        # a dump without the game itself (only its update): said plainly, not as Vita3K's
        # "Install app before patch"; an update alone is fine over an installed game
        if not p.installed_title(fs):
            missing = dumpinfo.missing_game([content[k + 1] for k in range(0, len(content), 2)
                                             if content[k] == '--install-content'], p.title_ids)
            if missing:
                sys.exit(f'not installed: {missing}')
        args = []
        for pup in a.firmware:
            args += ['--install-firmware', os.path.abspath(pup)]
        print('installing', ' '.join(os.path.basename(x) for x in a.firmware + content[1::2] if os.path.exists(x)))
        (Path(bin_dir) / p.id / 'step-error.txt').unlink(missing_ok=True)
        if front_end(args + content):
            # the front end's own words for each part that failed (a licence for other content ...)
            log = os.path.join(bin_dir, p.id, 'vita3k.log')
            why = step_error(os.path.join(bin_dir, p.id))
            sys.exit(f'installation failed: {why}\nMore in {log}' if why else f'installation failed: see {log}')

    # 2. a supported build
    title = p.installed_title(fs)
    if not title:
        sys.exit(f'no {" or ".join(p.title_ids)} in {fs} after installing')
    eboot = os.path.join(fs, 'ux0', 'app', title, 'eboot.bin')
    with open(eboot, 'rb') as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    if p.eboot_sha256 and digest not in p.eboot_sha256:
        sys.exit(p.unsupported_eboot(digest, os.path.join(bin_dir, p.id, 'vita3k.log')))
    print(f'eboot.bin ({title}):', p.eboot_sha256.get(digest, digest))

    recompiled_dir = os.path.join(work, 'build')
    if not a.no_recompile and p.modules:
        # 3. the modules' ELFs, written as the game loads them (on the JIT: nothing recompiled yet)
        dumps = os.path.join(bin_dir, p.id, 'elfdumps', title)
        for old in glob.glob(os.path.join(dumps, '*.elf')):
            os.remove(old)
        seconds = a.dump_seconds or p.dump_seconds
        print(f'running the game for {seconds} s to write the modules')
        # on the JIT; dynarmic notes every memory fault it passes on to Vita3K (Linux: thousands, harmless)
        front_end(['--dump-elfs', str(seconds)], {'SSD_AOT': 'none', 'SSD_AOT_NATIVE': '0'},
                  re.compile(r'^Unhandled SIG(SEGV|BUS) at '))
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
            base = p.recompile_args(m)
            # recompile.py needs its own folder on the path, so it runs without -I
            r = subprocess.run([sys.executable, os.path.join(RECOMP, 'recompile.py'), os.path.join(elf_dir, m + '.elf'),
                                os.path.join(gen, target)] + base, capture_output=True, text=True)
            for line in r.stdout.splitlines():
                if 'recompiled' in line or 'code hash' in line:
                    print(' ', line)
            if r.returncode:
                sys.exit(f'recompiling {m} failed:\n{r.stderr}')
        print('compiling (this takes a while)' if not a.android_only else 'compiling for Android (this takes a while)')
        if a.android_only:
            pass
        elif a.toolchain:
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
        if a.android:
            if not a.android_only:
                print('compiling for Android (this takes a while too)')
            toolchain.build_modules(gen, list(p.modules.values()), os.path.join(work, 'build-android'), a.android,
                                    a.jobs, android=True)

    # 5. the folder; the Vita file system is its own fs/, the ini's default (--android-only: as it is)
    dist = [sys.executable, '-I', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'make-dist.py'),
            '--project', p.root, '--out', out]
    dist += ['--bin', bin_dir] if a.bin else []
    dist += ['--exe', exe] if a.exe else []
    dist += ['--recompiled-dir', recompiled_dir] if not a.no_recompile and p.modules else ['--no-recompiled']
    dist += ['--no-jit'] if a.no_jit else []
    code = 0 if a.android_only else subprocess.run(dist).returncode
    if code == 0 and a.android and not a.no_recompile and p.modules:
        # the Android modules beside the folder's fs/, for android.py push
        android_dir = os.path.join(out, 'android')
        shutil.rmtree(android_dir, ignore_errors=True)
        os.makedirs(android_dir)
        for so in glob.glob(os.path.join(work, 'build-android', toolchain.DLL_PREFIX + '*.so')):
            shutil.copy2(so, android_dir)
        print(f'{android_dir}: the modules for Android (python -I android.py push {out})')
    sys.exit(code)


if __name__ == '__main__':
    main()
