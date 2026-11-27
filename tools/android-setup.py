"""The Python side of a game's setup on an Android device (ss-delta D27), run by the APK with the tools
android-build-tools.py packs (python.org's Python, capstone, zig): the device builds the game's native
modules from the player's own dump, as install-game.py does on a desktop. The APK's own native code does
the rest (installing the firmware and the dump, dumping the modules: the front end's --install-firmware,
--install-content and --dump-elfs).

usage (the tools' Python; <game> is the game's folder, the app's Android/data/<package>/files):
  android-setup.py firmware <work>     download the firmware from Sony into <work>/firmware/
  android-setup.py check <game>        the installed game's eboot.bin must be a build the port supports
  android-setup.py build <game> <work> <toolchain> [--jobs N] [--stamp TEXT]
                                       recompile and compile the dumped modules (<game>/<id>/elfdumps/)
                                       into <game>/ssd_aot_*.so; <game>/android-build.json records
                                       --stamp (what built them); <work> is removed afterwards
  [--project DIR]                      the folder with game.json (default: the one holding this kit)

Lines starting with "@ " are for the app's screen: "@ step <text>", "@ error <text>", "@ done".
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
import toolchain  # noqa: E402
from gameproject import Project  # noqa: E402

# a build's peak: one compiler process takes up to 1.8 GB (eboot's largest files), the work files 4.4 GB
GB = 1 << 30
JOB_MEMORY = 2 * GB
WORK_SPACE = 5 * GB


def say(kind, text=''):
    print(f'@ {kind} {text}'.rstrip(), flush=True)


def fail(text):
    say('error', text)
    sys.exit(1)


def mem_available():
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def firmware(a):
    import importlib.util
    spec = importlib.util.spec_from_file_location('fetch_firmware', os.path.join(TOOLS, 'fetch-firmware.py'))
    fetch_firmware = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch_firmware)
    say('step', 'Downloading the firmware from Sony')
    for path in fetch_firmware.fetch(os.path.join(a.work, 'firmware')):
        print(path, flush=True)
    say('done')


def check(a, p):
    fs = os.path.join(a.game, 'fs')
    title = p.installed_title(fs)
    if not title:
        fail(f'{p.name} is not installed ({" or ".join(p.title_ids)}): the dump holds another game, or none')
    eboot = os.path.join(fs, 'ux0', 'app', title, 'eboot.bin')
    h = hashlib.sha256()
    with open(eboot, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    build = p.eboot_sha256.get(h.hexdigest())
    if p.eboot_sha256 and not build:
        fail(f'the installed game ({title}): ' + p.unsupported_eboot(h.hexdigest(), 'the setup log'))
    print(f'{title}: {build or "eboot.bin"}', flush=True)
    say('done')


def build_on_device(a, p):
    fs = os.path.join(a.game, 'fs')
    title = p.installed_title(fs)
    if not title:
        fail(f'{p.name} is not installed')
    dumps = os.path.join(a.game, p.id, 'elfdumps', title)
    work = os.path.abspath(a.work)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    free = shutil.disk_usage(work).free
    if free < WORK_SPACE:
        fail(f'the build needs about {WORK_SPACE // GB} GB of free space on the device ({free / GB:.1f} GB free)')
    elf_dir = os.path.join(work, 'elf')
    os.makedirs(elf_dir)
    for m in p.modules:
        found = glob.glob(os.path.join(dumps, f'*_{m}.elf'))
        if not found:
            fail(f'the game did not load {m} while it ran: dump the modules again')
        shutil.copy2(found[0], os.path.join(elf_dir, m + '.elf'))

    start = time.time()
    gen = os.path.join(work, 'generated')
    for i, (m, target) in enumerate(p.modules.items()):
        say('step', f'Recompiling {m} ({i + 1}/{len(p.modules)})')
        base = p.recompile_args(m)
        r = subprocess.run([sys.executable, os.path.join(KIT, 'recomp', 'recompile.py'), os.path.join(elf_dir, m + '.elf'),
                            os.path.join(gen, target)] + base, capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if 'recompiled' in line or 'code hash' in line:
                print(' ', line, flush=True)
        if r.returncode:
            print(r.stderr, flush=True)
            fail(f'recompiling {m} failed')

    # one job per core, as far as memory allows (a phone with 8 GB gets fewer)
    jobs = a.jobs or max(1, min(os.cpu_count() or 4, mem_available() // JOB_MEMORY))
    say('step', f'Compiling the native code ({jobs} at a time; this takes a while)')
    out = os.path.join(work, 'build')
    toolchain.build_modules(gen, list(p.modules.values()), out, a.toolchain, jobs, android=True)

    say('step', 'Putting the modules in place')
    for old in glob.glob(os.path.join(a.game, toolchain.DLL_PREFIX + '*.so')):
        os.remove(old)
    for so in sorted(glob.glob(os.path.join(out, toolchain.DLL_PREFIX + '*.so'))):
        shutil.copy2(so, a.game)
    record = os.path.join(a.game, 'android-build.json')
    with open(record, 'w', encoding='utf-8') as f:
        json.dump({'stamp': a.stamp, 'title': title, 'minutes': round((time.time() - start) / 60, 1)}, f, indent=1)
    shutil.rmtree(work, ignore_errors=True)
    print(f'built in {(time.time() - start) / 60:.1f} min', flush=True)
    say('done')


def user_import_root():

    return os.path.abspath(os.path.join(KIT, '..', '..'))

def install_imported_modules(a, p):

    import zipfile

    check(a, p)

    archive = os.path.join(user_import_root(), 'arm64-import.zip')

    if not os.path.isfile(archive):

        fail('No ARM64 modules imported. Open Offline imports, select the modules ZIP/folder, or choose Compile on this device.')

    expected = {toolchain.DLL_PREFIX + m + '.so' for m in p.modules.values()}

    stage = os.path.join(os.path.abspath(a.work), 'validated-arm64')

    os.makedirs(stage, exist_ok=True)

    try:

        with zipfile.ZipFile(archive) as package:

            names = package.namelist()

            if len(names) != len(set(names)) or set(names) != expected | {'modules.json'}:

                fail('The imported ARM64 module set is incomplete or contains duplicate/unexpected entries')

            info = package.getinfo('modules.json')

            if info.file_size > 65536:

                fail('Imported module manifest too large')

            records = json.loads(package.read(info))

            if set(records) != expected:

                fail('Imported manifest does not match this project')

            total = 0

            for name in sorted(expected):

                info = package.getinfo(name)

                total += info.file_size

                if not 64 <= info.file_size <= 256 * 2**20 or total > 512 * 2**20:

                    fail('Invalid imported module size: ' + name)

                if records[name]['bytes'] != info.file_size:

                    fail('Imported manifest size mismatch: ' + name)

                digest = hashlib.sha256()

                target = os.path.join(stage, name)

                with package.open(info) as source, open(target, 'wb') as out:

                    header = source.read(64)

                    if (header[:7] != b'\x7fELF\x02\x01\x01' or

                            int.from_bytes(header[16:18], 'little') != 3 or

                            int.from_bytes(header[18:20], 'little') != 183):

                        fail('Not an ARM64 shared library: ' + name)

                    digest.update(header)

                    out.write(header)

                    size = len(header)

                    for chunk in iter(lambda: source.read(65536), b''):

                        size += len(chunk)

                        if size > info.file_size:

                            fail('Imported module exceeds declared size: ' + name)

                        digest.update(chunk)

                        out.write(chunk)

                if size != info.file_size or digest.hexdigest() != records[name]['sha256']:

                    fail('Imported module integrity mismatch: ' + name)

        # All modules pass before any installed library is replaced.

        say('step', 'Installing imported ARM64 modules (no phone compilation)')

        for name in sorted(expected):

            destination = os.path.join(a.game, name)

            shutil.copyfile(os.path.join(stage, name), destination + '.arm-new')

        for name in sorted(expected):

            destination = os.path.join(a.game, name)

            os.replace(destination + '.arm-new', destination)

        record = os.path.join(a.game, 'android-build.json')

        with open(record + '.new', 'w', encoding='utf-8') as out:

            json.dump({'stamp': a.stamp, 'title': p.installed_title(os.path.join(a.game, 'fs')),

                       'source': 'User-imported ARM64', 'modules': records}, out, indent=1)

        os.replace(record + '.new', record)

        print('Imported ARM64 modules installed; the runtime also verifies its code hash and ABI at load time', flush=True)

        say('done')

    finally:

        shutil.rmtree(stage, ignore_errors=True)

def build(a, p):

    archive = os.path.join(user_import_root(), 'arm64-import.zip')

    if os.path.isfile(archive):

        return install_imported_modules(a, p)

    say('step', 'No ARM64 modules imported: compiling on this device')

    return build_on_device(a, p)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--project', default=os.path.dirname(KIT))
    sub = ap.add_subparsers(dest='command', required=True)
    f = sub.add_parser('firmware')
    f.add_argument('work')
    c = sub.add_parser('check')
    c.add_argument('game')
    b = sub.add_parser('build')
    b.add_argument('game')
    b.add_argument('work')
    b.add_argument('toolchain')
    b.add_argument('--jobs', type=int, default=0)
    b.add_argument('--stamp', default='')
    a = ap.parse_args()
    if a.command == 'firmware':
        return firmware(a)
    p = Project(a.project)
    return check(a, p) if a.command == 'check' else build(a, p)


if __name__ == '__main__':
    main()
