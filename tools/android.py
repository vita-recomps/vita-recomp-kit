"""Put a game's folder on an Android device, for the game's APK (ss-delta docs/09-android.md): over adb,
from the computer that built it (install-game.py --android: <folder>/fs and <folder>/android/).

usage:
  python -I android.py [--project DIR] push <folder> [--apk FILE] [--driver ZIP] [--no-fs]
                       [--serial S] [--adb FILE] [--package ID]

  push       the APK (--apk) installed; the Vita file system (<folder>/fs) and the modules for Android
             (<folder>/android/ssd_aot_*.so) into the app's folder, Android/data/<package>/files/.
             Saves already on the device (fs/ux0/user) and its settings file stay as they are.
  --driver   a Vulkan driver package (libadrenotools zip, such as Turnip's <name>.adpkg.zip) into
             files/drivers/, and [debug] gpu_driver = <name> in the settings file: the game unpacks it
             into its own storage at the next start
  --no-fs    the modules (and the driver) only, when the device has the game's files already
  --serial   the device, when more than one is connected (adb devices)
  --adb      adb itself (default: on PATH, else ANDROID_HOME's or the default SDK's platform-tools)
  --package  the APK's package (default: game.json android_package, else io.github.vitarecomps.<id>)

The device needs USB or wireless debugging on (Settings > System > Developer options). Copying the
game's files takes a few minutes over Wi-Fi (3.2 GB for Soul Sacrifice Delta: about 4 minutes).
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gameproject import Project  # noqa: E402

ACTIVITY = 'io.github.vitarecomps.frontend.GameActivity'


def find_adb(given):
    if given:
        return given
    found = shutil.which('adb')
    if found:
        return found
    exe = 'adb.exe' if sys.platform == 'win32' else 'adb'
    roots = [os.environ.get('ANDROID_HOME'), os.environ.get('ANDROID_SDK_ROOT')]
    if sys.platform == 'win32':
        roots += [os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Android', 'Sdk'), r'C:\Android\Sdk']
    elif sys.platform == 'darwin':
        roots += [os.path.expanduser('~/Library/Android/sdk')]
    else:
        roots += [os.path.expanduser('~/Android/Sdk')]
    for root in roots:
        if root and os.path.exists(os.path.join(root, 'platform-tools', exe)):
            return os.path.join(root, 'platform-tools', exe)
    sys.exit('adb not found: install Android\'s platform-tools, or pass --adb')


class Device:
    def __init__(self, adb, serial):
        self.adb = [adb] + (['-s', serial] if serial else [])

    def run(self, *args, check=True):
        r = subprocess.run(self.adb + list(args), capture_output=True, text=True, errors='replace')
        if check and r.returncode:
            sys.exit(f'adb {" ".join(args[:2])} failed:\n{(r.stdout + r.stderr).strip()}')
        return r.stdout.replace('\r', '')

    def shell(self, command, check=True):
        return self.run('shell', command, check=check)

    def exists(self, path):
        return self.shell(f'[ -e {path} ] && echo yes || true').strip() == 'yes'

    def push(self, local, remote):
        """adb push, its progress on one line"""
        r = subprocess.run(self.adb + ['push', local, remote], capture_output=True, text=True, errors='replace')
        lines = [ln for ln in (r.stdout + r.stderr).replace('\r', '\n').splitlines() if ln and not ln.startswith('[')]
        if r.returncode:
            sys.exit(f'adb push {local} failed:\n' + '\n'.join(lines[-5:]))
        print('  ' + (lines[-1] if lines else os.path.basename(local)))


def set_ini(device, ini, section, key, value):
    """section.key = value in the device's settings file (pulled, changed, pushed back writable for the app)."""
    with tempfile.TemporaryDirectory() as tmp:
        local = os.path.join(tmp, 'settings.ini')
        device.run('pull', ini, local)
        with open(local, encoding='utf-8') as f:
            lines = f.read().split('\n')
        current, at, done = None, None, False
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith('[') and s.endswith(']'):
                if current == section and at is None:
                    at = i
                current = s[1:-1]
            elif current == section and re.match(rf'^{re.escape(key)}\s*=', s):
                lines[i] = f'{key} = {value}'
                done = True
        if not done:
            if current == section and at is None:
                at = len(lines)
            if at is None:
                lines += ['', f'[{section}]']
                at = len(lines)
            lines.insert(at, f'{key} = {value}')
        with open(local, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(lines))
        device.push(local, ini)
    # a file the shell user writes is the shell's, readable only by others: the app must write it too
    device.shell(f'chmod 660 {ini}', check=False)


def push(a, project):
    folder = os.path.abspath(a.folder)
    fs = os.path.join(folder, 'fs')
    modules = sorted(glob.glob(os.path.join(folder, 'android', 'ssd_aot_*.so')))
    if not a.no_fs and not os.path.isdir(fs):
        sys.exit(f'{fs} does not exist: the game\'s folder from install-game.py (or --no-fs)')
    if not modules:
        print(f'note: no {os.path.join(folder, "android", "ssd_aot_*.so")}: the game runs on the JIT '
              '(install-game.py --android builds them)')

    device = Device(find_adb(a.adb), a.serial)
    serials = [ln.split()[0] for ln in device.run('devices').splitlines()[1:] if ln.strip().endswith('device')]
    if not a.serial and len(serials) != 1:
        sys.exit('no device connected (adb devices)' if not serials else
                 f'{len(serials)} devices connected: pass --serial ({", ".join(serials)})')
    package = a.package or project.android_package
    files = f'/sdcard/Android/data/{package}/files'
    ini = f'{files}/{project.id}.ini'

    if a.apk:
        print(f'installing {os.path.basename(a.apk)}')
        out = device.run('install', '-r', os.path.abspath(a.apk), check=False)
        if 'Success' not in out:
            sys.exit(f'installing the APK failed:\n{out.strip()}')
    if f'package:{package}' not in device.shell(f'pm list packages {package}'):
        sys.exit(f'{package} is not installed on the device: pass --apk')

    # the first start makes Android/data/<package>/files and the settings file
    if not device.exists(ini):
        print(f'starting {package} once')
        device.shell(f'am start -n {package}/{ACTIVITY}')
        for _ in range(40):
            if device.exists(ini):
                break
            time.sleep(0.5)
        device.shell(f'am force-stop {package}')
        if not device.exists(ini):
            sys.exit(f'{ini} did not appear: start the game on the device once, then run this again')

    if not a.no_fs:
        # adb can't make nested folders inside another app's Android/data (secure_mkdirs): the files go
        # to the shell's /data/local/tmp first, then the device's shell copies them in
        stage = f'/data/local/tmp/{package}-fs'
        device.shell(f'rm -rf {stage} && mkdir -p {stage}')
        print(f'copying {fs} to the device (minutes over Wi-Fi)')
        for entry in sorted(os.listdir(fs)):
            device.push(os.path.join(fs, entry), f'{stage}/')
        # the game's first start makes empty user folders: saves are files there
        if device.shell(f'find {files}/fs/ux0/user -type f 2>/dev/null | head -1', check=False).strip():
            print('  the saves on the device stay (fs/ux0/user)')
            device.shell(f'rm -rf {stage}/ux0/user')
        device.shell(f'mkdir -p {files}/fs && cp -R {stage}/. {files}/fs/ && rm -rf {stage}')
        # the shell's files, readable by others only: the app must write them too (saves, its time records)
        device.shell(f'chmod -R g+rwX {files}/fs 2>/dev/null; true', check=False)

    if modules:
        print(f'copying {len(modules)} modules')
        device.shell(f'rm -f {files}/ssd_aot_*.so', check=False)
        for so in modules:
            device.push(so, f'{files}/')

    if a.driver:
        name = os.path.basename(a.driver)
        stem = re.sub(r'(\.adpkg)?\.zip$', '', name)
        print(f'driver {stem}')
        device.shell(f'mkdir -p {files}/drivers')
        device.push(os.path.abspath(a.driver), f'{files}/drivers/')
        set_ini(device, ini, 'debug', 'gpu_driver', stem)
    print(f'done: start {project.id} on the device')


def main():
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument('--project', default=os.getcwd())
    ap.add_argument('command', choices=['push'])
    ap.add_argument('folder')
    ap.add_argument('--apk')
    ap.add_argument('--driver')
    ap.add_argument('--no-fs', action='store_true')
    ap.add_argument('--serial')
    ap.add_argument('--adb')
    ap.add_argument('--package')
    a = ap.parse_args()
    push(a, Project(a.project))


if __name__ == '__main__':
    main()
