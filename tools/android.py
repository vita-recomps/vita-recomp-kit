"""Put a game's folder on an Android device, for the game's APK (ss-delta docs/09-android.md): over adb,
from the computer that built it (install-game.py --android: <folder>/fs and <folder>/android/).

usage:
  python -I android.py [--project DIR] push <folder> [--apk FILE] [--driver ZIP] [--no-fs]
                       [--serial S | --pair HOST:PORT --code CODE | --connect HOST:PORT]
                       [--adb FILE] [--package ID]
  python -I android.py fetch-adb <dir> --accept-android-sdk-license

  push       the APK (--apk) installed; the Vita file system (<folder>/fs) and the modules for Android
             (<folder>/android/ssd_aot_*.so) into the app's folder, Android/data/<package>/files/.
             Saves already on the device (fs/ux0/user) and its settings file stay as they are.
  --driver   a Vulkan driver package (libadrenotools zip, such as Turnip's <name>.adpkg.zip) into
             files/drivers/, and [debug] gpu_driver = <name> in the settings file: the game unpacks it
             into its own storage at the next start (without one, the APK offers Turnip on Adreno 7xx)
  --no-fs    the modules (and the driver) only, when the device has the game's files already
  --serial   the device, when more than one is connected (adb devices)
  --pair     wireless debugging: pair with the device first (Developer options > Wireless debugging >
             Pair device with pairing code: the address and port it shows; --code the six digits)
  --connect  wireless debugging: the address and port on the Wireless debugging screen itself (adb
             usually finds a paired device by itself; this is for when it doesn't)
  --adb      adb itself (default: on PATH, else ANDROID_HOME's or the default SDK's platform-tools)
  --package  the APK's package (default: game.json android_package, else io.github.vitarecomps.<id>)
  fetch-adb  Google's platform-tools (adb), pinned in android-tools.json, into <dir>/platform-tools;
             under the Android SDK License Agreement (android-tools.json license_url)

The device needs USB or wireless debugging on (Settings > System > Developer options). Copying the
game's files takes a few minutes over Wi-Fi (3.2 GB for Soul Sacrifice Delta: about 4 minutes).
"""
import argparse
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import toolchain  # noqa: E402
from gameproject import Project  # noqa: E402

ACTIVITY = 'io.github.vitarecomps.frontend.GameActivity'
LOCK = os.path.join(TOOLS, 'android-tools.json')


def host():
    return 'windows' if sys.platform == 'win32' else 'macos' if sys.platform == 'darwin' else 'linux'


def adb_in(folder):
    """adb inside a folder fetch_adb made, or ''."""
    exe = os.path.join(folder, 'platform-tools', 'adb.exe' if sys.platform == 'win32' else 'adb')
    return exe if os.path.exists(exe) else ''


def fetch_adb(dest, accepted):
    """Google's platform-tools for this computer (android-tools.json) into dest/platform-tools: adb's path."""
    with open(LOCK, encoding='utf-8') as f:
        lock = json.load(f)
    if not accepted:
        sys.exit('adb is part of Google\'s Android SDK Platform-Tools, under the Android SDK License Agreement:\n'
                 f'  {lock["license_url"]}\nRead it, then run again with --accept-android-sdk-license.')
    p = lock['payloads'][host()]
    dest = os.path.abspath(dest)
    os.makedirs(os.path.join(dest, 'downloads'), exist_ok=True)
    local = os.path.join(dest, 'downloads', p['file'])
    if not (os.path.exists(local) and os.path.getsize(local) == p['size'] and toolchain.sha256(local) == p['sha256']):
        if not toolchain.offline.find(p['size'], p['sha256']):
            print(f'downloading adb (Android SDK Platform-Tools {lock["platform_tools"]}, '
                  f'{p["size"] / 2**20:.0f} MB, from Google)', flush=True)
        toolchain.obtain(p, local)
        if toolchain.sha256(local) != p['sha256']:
            os.remove(local)
            sys.exit(f'{p["file"]}: wrong SHA-256 (the download changed or was damaged)')
    shutil.rmtree(os.path.join(dest, 'platform-tools'), ignore_errors=True)
    with zipfile.ZipFile(local) as z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.startswith('platform-tools/'):
                continue
            target = os.path.join(dest, *info.filename.split('/'))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out)
            mode = info.external_attr >> 16
            if mode and sys.platform != 'win32':
                os.chmod(target, mode & 0o777)
    exe = adb_in(dest)
    if not exe:
        sys.exit(f'{p["file"]} holds no adb')
    return exe


def find_adb(given=None, *, required=True):
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
    if required:
        sys.exit('adb not found: install Android\'s platform-tools, or pass --adb')
    return ''


def adb_server_in_use():
    """Check before running an adb client, which can start or replace a server."""
    if any(os.environ.get(name) for name in
           ('ADB_SERVER_SOCKET', 'ANDROID_ADB_SERVER_ADDRESS', 'ANDROID_ADB_SERVER_PORT')):
        return True  # an explicitly configured endpoint belongs to the caller
    for address in ('127.0.0.1', '::1'):
        try:
            with socket.create_connection((address, 5037), timeout=0.5):
                return True
        except OSError:
            continue
    return False


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


def choose_device(device, a):
    """The one device to use: paired and connected first when asked (wireless debugging). The same
    device can be listed twice (its address and its mDNS name): those count once."""
    if a.pair:
        if not a.code:
            sys.exit('--pair needs --code: the six digits the device shows beside the address')
        print(f'pairing with {a.pair}', flush=True)
        out = device.run('pair', a.pair, a.code, check=False)
        if 'Successfully paired' not in out:
            sys.exit(f'pairing failed: {out.strip()}\n(the code and port change each time the device shows '
                     'the pairing dialog: open it again and use the new ones)')
    if a.connect:
        out = device.run('connect', a.connect, check=False)
        if 'connected to' not in out:
            sys.exit(f'connecting to {a.connect} failed: {out.strip()}')
    # after pairing, adb finds the device by itself (mDNS) within a few seconds
    deadline = time.time() + (20 if a.pair else 0)
    while True:
        found = {}
        for ln in device.run('devices').splitlines()[1:]:
            parts = ln.split()
            if len(parts) >= 2 and parts[1] == 'device':
                serialno = device.run('-s', parts[0], 'shell', 'getprop ro.serialno', check=False).strip()
                found.setdefault(serialno or parts[0], parts[0])
        if len(found) == 1:
            return next(iter(found.values()))
        if len(found) > 1:
            sys.exit(f'{len(found)} devices connected: pass --serial ({", ".join(found.values())})')
        if time.time() >= deadline:
            hint = (' The device was paired, but adb didn\'t find it on the network: pass --connect with the '
                    'address and port on the Wireless debugging screen.' if a.pair else
                    ' Turn on USB debugging and connect the cable (allow this computer on the device), or pair '
                    'it over Wi-Fi with --pair and --code.')
            sys.exit('no device connected.' + hint)
        time.sleep(1)


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
    if not a.serial:
        device = Device(device.adb[0], choose_device(device, a))
    package = a.package or project.android_package
    files = f'/sdcard/Android/data/{package}/files'
    ini = f'{files}/{project.id}.ini'

    if a.apk:
        print(f'installing {os.path.basename(a.apk)}')
        out = device.run('install', '-r', os.path.abspath(a.apk), check=False)
        if 'INSTALL_FAILED_UPDATE_INCOMPATIBLE' in out:
            sys.exit(f'{package} on the device was signed by someone else (a build of your own, or another '
                     'copy): Android won\'t replace it. Uninstalling it deletes its saves too: copy '
                     f'{files}/fs/ux0/user off the device first, then uninstall it and run this again.')
        if 'Success' not in out:
            sys.exit(f'installing the APK failed:\n{out.strip()}')
    if f'package:{package}' not in device.shell(f'pm list packages {package}'):
        sys.exit(f'{package} is not installed on the device: pass --apk')

    # the app's folder: the device's shell may make it (adb push may not, see below); the game writes
    # its settings file there at its first start
    device.shell(f'mkdir -p {files} && chmod g+rwX {files}', check=False)
    if not device.exists(files):
        sys.exit(f'{files} could not be made: start the game on the device once, then run this again')
    if a.driver and not device.exists(ini):
        # --driver sets a key in the settings file: the game's first start writes it. The game starts
        # only with the screen on (it waits for its surface)
        print(f'starting {package} once')
        device.shell('input keyevent KEYCODE_WAKEUP', check=False)
        device.shell(f'am start -n {package}/{ACTIVITY}')
        for _ in range(60):
            if device.exists(ini):
                break
            time.sleep(0.5)
        device.shell(f'am force-stop {package}')
        if not device.exists(ini):
            sys.exit(f'{ini} did not appear: unlock the device, start the game on it once, then run this again')

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
    ap.add_argument('command', choices=['push', 'fetch-adb'])
    ap.add_argument('folder')
    ap.add_argument('--apk')
    ap.add_argument('--driver')
    ap.add_argument('--no-fs', action='store_true')
    ap.add_argument('--serial')
    ap.add_argument('--pair')
    ap.add_argument('--code')
    ap.add_argument('--connect')
    ap.add_argument('--adb')
    ap.add_argument('--package')
    ap.add_argument('--accept-android-sdk-license', action='store_true')
    a = ap.parse_args()
    if a.command == 'fetch-adb':
        print(fetch_adb(a.folder, a.accept_android_sdk_license))
        return
    push(a, Project(a.project))


if __name__ == '__main__':
    main()
