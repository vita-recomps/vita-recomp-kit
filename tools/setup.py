"""The game's importer: from the player's own dump of the game to the game, built in its own folder.
A release is that folder (make-setup.py lays it out): the game's executable (the one thing the
player starts), its files, and this setup in setup/. The game starts the importer itself when it
isn't installed yet, or when asked (--install). The importer downloads the PS Vita firmware from Sony
and a compiler for the recompiled modules (Windows: from LLVM's and Microsoft's servers; Linux and
macOS: zig, from ziglang.org), installs the game, recompiles its code and compiles it on this
computer, then deletes the downloads and its work files: everything stays in the game's folder.
Nothing of the game is ever uploaded or shared.

usage (no parts: a window asks, setupgui.py; --console: questions in the console). The game passes
its own --dump parts on as --install:
  python -I setup/kit/tools/setup.py [--console]
  python -I setup/kit/tools/setup.py [--install <pkg|folder|zip|vpk> [--license <work.bin|rif> | --zrif Z]] ...
            [--cache DIR] [--accept-microsoft-license] [--jobs N] [--shortcut] [--steam] [--progress] [--exe FILE]
  python -I setup/kit/tools/setup.py --add-to-steam
  python -I setup/kit/tools/setup.py --android [--console] [--push-only] [--accept-android-sdk-license]
            [--serial S | --pair HOST:PORT --code CODE | --connect HOST:PORT] [--apk FILE] [--cache DIR]

  --install   repeat for each part, in order: the game, then its update, then add-ons
  --cache     keep the downloads in DIR, shared by every game's setup and kept for the next time
              (default: inside setup/, deleted after a successful build)
  --accept-microsoft-license   Windows only: the compiler's C++ runtime and SDK
  --shortcut  Windows: a shortcut on the desktop. Linux: an entry in the applications menu (which
              Steam's "Add a Non-Steam Game" lists) and on the desktop. macOS: the game's app in
              ~/Applications too (Launchpad and Spotlight list it)
  --steam     Linux: add the game to Steam as a non-Steam game (Steam must be running), as SteamOS's
              "Add to Steam" does; on a Steam Deck it then starts from Game Mode
  --add-to-steam  only that (Linux), then exit
  --progress  with --install: also lines "progress <0-1> <text>" for a launcher's progress bar
              (RecompLauncher's setup step, recomplauncher.json "prepare")
  --exe       the game's executable (default: the one in the game's folder; macOS: inside its app)
  --android   the game on an Android device too (the release's APK, setup/android/<id>.apk): its
              modules built for Android (zig, as above; from the game already built here, or with
              --install from the dump in the same run), then the APK, the game's files and the modules
              copied to the device over adb, which is downloaded from Google (Android SDK Platform-
              Tools, under Google's licence: --accept-android-sdk-license). The device needs USB
              debugging (and the cable), or wireless debugging (--pair with the address and code it
              shows; adb then finds it). Saves already on the device stay. Without --console and
              device options, a window asks
  --push-only  with --android: copy what an earlier --android built (<game folder>/android/) again

Exit codes: 0 built (the game then starts), 2 built but the window was closed without Play, 1 failed
or cancelled. The output of a build (from the window too) is also in <game folder>/<id>/setup.log,
next to the game's own log, so a launcher's crash report carries it.
"""
import glob
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import dumpinfo  # noqa: E402
import toolchain  # noqa: E402
from gameproject import EXE_SUFFIX, MACOS, WINDOWS  # noqa: E402

# the minimum macOS of the fork's build (CMAKE_OSX_DEPLOYMENT_TARGET) and of the modules (toolchain.py)
MACOS_MINIMUM = '13.3'

SETUP = os.path.dirname(os.path.dirname(TOOLS))  # <game folder>/setup
OUT = os.path.dirname(SETUP)  # the game's folder: the release, built in place
GAME = os.path.join(SETUP, 'kit', 'game')  # game.json and the game folder's README


def game_exe(game):
    """The game's executable in its folder (macOS: the app's own, inside the bundle)."""
    if MACOS:
        return os.path.join(OUT, app_name(game) + '.app', 'Contents', 'MacOS', game['id'])
    return os.path.join(OUT, game['id'] + EXE_SUFFIX)


def desktop_shortcut(exe, name):
    """A shortcut to exe on the player's desktop (OneDrive's too); its path."""
    env = dict(os.environ, SHORTCUT_TARGET=exe, SHORTCUT_NAME=name)
    script = ("$d = [Environment]::GetFolderPath('Desktop'); "
              "$l = Join-Path $d ($env:SHORTCUT_NAME + '.lnk'); "
              "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($l); "
              "$s.TargetPath = $env:SHORTCUT_TARGET; "
              "$s.WorkingDirectory = Split-Path $env:SHORTCUT_TARGET; $s.Save(); $l")
    r = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], env=env,
                       capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return r.stdout.strip() if r.returncode == 0 else ''


def desktop_entry(exe, game, out, menu):
    """Linux: <id>.desktop in the playable folder, and with menu in the applications menu and on the
    desktop too; the folder's file and the menu entry's path ('' when not made)."""
    icon = os.path.join(out, game['id'] + '.png')
    text = ('[Desktop Entry]\nType=Application\n'
            f'Name={game["name"]}\nExec="{exe}"\nPath={out}\nTerminal=false\nCategories=Game;\n'
            + (f'Icon={icon}\n' if os.path.exists(icon) else ''))
    home = os.path.expanduser('~')
    data = os.environ.get('XDG_DATA_HOME') or os.path.join(home, '.local', 'share')
    folders = [out] + ([os.path.join(data, 'applications'), os.path.join(home, 'Desktop')] if menu else [])
    made = []
    for folder in folders:
        if not os.path.isdir(os.path.dirname(folder)):
            continue
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, game['id'] + '.desktop')
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        os.chmod(path, 0o755)  # KDE starts a desktop file from the desktop only when it is executable
        made.append(path)
    return made[0], (made[1] if len(made) > 1 else '')


def add_to_steam(desktop):
    """Linux: hand a .desktop file to the running Steam client as a non-Steam game, as SteamOS's "Add
    to Steam" (holo-add-to-steam) does. Steam takes steam://addnonsteamgame/<path> only while
    /tmp/addnonsteamgamefile exists, so that a link elsewhere can't add games. Returns what happened."""
    steam = shutil.which('steam')
    running = subprocess.run(['pgrep', '-x', 'steam'], capture_output=True).returncode == 0
    if not steam or not running:
        return (f'Steam is not running: start it, then add {desktop} with "Add a Non-Steam Game" '
                '(or run the setup again with --steam)')
    open('/tmp/addnonsteamgamefile', 'a').close()
    url = 'steam://addnonsteamgame/' + urllib.parse.quote(desktop, safe='')
    try:
        # the steam script passes the link to the running client and returns
        subprocess.run([steam, url], capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        pass
    return f'added to Steam as a non-Steam game ({desktop})'


def app_name(game):
    """The game's name as a file name: Finder shows ':' as '/', so the subtitle follows a dash."""
    return game['name'].replace(': ', ' - ').replace(':', '-').replace('/', '-')


# Apple's app icon grid: a 1024-point canvas, the artwork an 824-point rounded square in its middle with a
# slight shadow below. macOS doesn't shape an app's icon itself: a square picture fills the whole canvas
# and looks bigger than the other icons in the Dock, with sharp corners. Drawn with AppKit through
# osascript's JavaScript bridge, so the setup still needs nothing but macOS's own python3.
MAC_ICON_SCRIPT = '''
ObjC.import('AppKit');
function run(argv) {
    const src = $.NSImage.alloc.initWithContentsOfFile(argv[0]);
    if (src.isNil()) return 'unreadable';
    const size = 1024, side = 824, radius = 185.4, inset = (size - side) / 2;
    const rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
        null, size, size, 8, 4, true, false, $.NSDeviceRGBColorSpace, 0, 0);
    // (setters called as methods: assigning a property in JXA doesn't reach Objective-C)
    const context = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep);
    $.NSGraphicsContext.saveGraphicsState;
    $.NSGraphicsContext.setCurrentContext(context);
    context.setImageInterpolation($.NSImageInterpolationHigh);
    const rect = $.NSMakeRect(inset, inset + 6, side, side);
    const shape = $.NSBezierPath.bezierPathWithRoundedRectXRadiusYRadius(rect, radius, radius);
    const shadow = $.NSShadow.alloc.init;
    shadow.setShadowOffset($.NSMakeSize(0, -10));
    shadow.setShadowBlurRadius(24);
    shadow.setShadowColor($.NSColor.colorWithCalibratedWhiteAlpha(0, 0.3));
    $.NSGraphicsContext.saveGraphicsState;
    shadow.set;
    $.NSColor.blackColor.setFill;
    shape.fill;
    $.NSGraphicsContext.restoreGraphicsState;
    shape.addClip;
    // only the opaque part: a game icon may have its own transparent margin and corners (the middle
    // row and column give the margins; colorAtXY counts y from the top, the image from the bottom)
    const pixels = $.NSBitmapImageRep.imageRepWithData(src.TIFFRepresentation);
    const w = pixels.pixelsWide, h = pixels.pixelsHigh;
    const opaque = (x, y) => pixels.colorAtXY(x, y).alphaComponent > 0.5;
    const edge = (n, test) => { let i = 0; while (i < n / 2 && !test(i)) i++; return i < n / 2 ? i : 0; };
    const left = edge(w, x => opaque(x, h >> 1)), right = edge(w, x => opaque(w - 1 - x, h >> 1));
    const top = edge(h, y => opaque(w >> 1, y)), bottom = edge(h, y => opaque(w >> 1, h - 1 - y));
    const sx = src.size.width / w, sy = src.size.height / h;
    const from = $.NSMakeRect(left * sx, bottom * sy, (w - left - right) * sx, (h - top - bottom) * sy);
    src.drawInRectFromRectOperationFraction(rect, from, $.NSCompositingOperationSourceOver, 1.0);
    $.NSGraphicsContext.restoreGraphicsState;
    const data = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $({}));
    return data.writeToFileAtomically(argv[1], true) ? 'ok' : 'unwritable';
}
'''


def mac_icon_shape(png, out):
    """png drawn into the macOS icon shape (see MAC_ICON_SCRIPT) as out; False if that failed."""
    r = subprocess.run(['osascript', '-l', 'JavaScript', '-e', MAC_ICON_SCRIPT, png, out],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == 'ok' and os.path.exists(out)


def mac_icns(png, icns):
    """png as icns, shaped as a macOS icon, every size from 16 to 1024 (512@2x) through an iconset
    (sips and iconutil, part of macOS; sips alone writes an .icns only up to 512). Falls back to sips
    on the unshaped picture."""
    with tempfile.TemporaryDirectory() as tmp:
        shaped = os.path.join(tmp, 'icon.png')
        source = shaped if mac_icon_shape(png, shaped) else png
        iconset = os.path.join(tmp, 'icon.iconset')
        os.makedirs(iconset)
        ok = True
        for size in (16, 32, 128, 256, 512):
            for scale, suffix in ((1, ''), (2, '@2x')):
                name = os.path.join(iconset, f'icon_{size}x{size}{suffix}.png')
                ok = ok and subprocess.run(['sips', '-z', str(size * scale), str(size * scale), source,
                                            '--out', name], capture_output=True).returncode == 0
        if ok and subprocess.run(['iconutil', '-c', 'icns', iconset, '-o', icns],
                                 capture_output=True).returncode == 0 and os.path.exists(icns):
            return True
    return subprocess.run(['sips', '-s', 'format', 'icns', png, '--out', icns],
                          capture_output=True).returncode == 0


def mac_bundle(exe, game, app, png, game_dir=None):
    """macOS: the game's app bundle at app ('<name>.app'), made by make-setup.py for a release. It runs
    exe itself, a copy in Contents/MacOS with MoltenVK beside it (exe's rpath): macOS gives Game Mode
    only to a bundle's own executable. The game's folder is the one holding the app
    (SDL_FILESYSTEM_BASE_DIR_TYPE = parent), or game_dir (LSEnvironment VITA_RECOMPS_GAME_DIR, for a
    copy elsewhere). Its icon is png in a macOS icon's shape; the bundle is signed ad hoc as a whole."""
    shutil.rmtree(app, ignore_errors=True)
    macos = os.path.join(app, 'Contents', 'MacOS')
    os.makedirs(macos)
    os.makedirs(os.path.join(app, 'Contents', 'Resources'))
    shutil.copy2(exe, os.path.join(macos, game['id']))
    moltenvk = os.path.join(os.path.dirname(exe), 'libMoltenVK.dylib')
    if os.path.exists(moltenvk):
        shutil.copy2(moltenvk, macos)
    info = {
        'CFBundleName': game['name'], 'CFBundleDisplayName': game['name'],
        'CFBundleIdentifier': f'io.github.vita-recomps.{game["id"]}', 'CFBundleExecutable': game['id'],
        'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '1.0', 'CFBundleVersion': '1',
        'CFBundleInfoDictionaryVersion': '6.0', 'LSMinimumSystemVersion': MACOS_MINIMUM,
        'LSApplicationCategoryType': 'public.app-category.games', 'NSHighResolutionCapable': True,
        # Game Mode in full screen (macOS 26 reads LSSupportsGameMode, 14 and 15 GCSupportsGameMode): the
        # game gets the CPU and GPU first, background work less, Bluetooth controllers a faster rate
        'LSSupportsGameMode': True, 'GCSupportsGameMode': True,
        'SDL_FILESYSTEM_BASE_DIR_TYPE': 'parent',
    }
    if game_dir:
        info['LSEnvironment'] = {'VITA_RECOMPS_GAME_DIR': game_dir}
    icns = os.path.join(app, 'Contents', 'Resources', game['id'] + '.icns')
    if png and os.path.exists(png) and mac_icns(png, icns):
        info['CFBundleIconFile'] = game['id']
    with open(os.path.join(app, 'Contents', 'Info.plist'), 'wb') as f:
        plistlib.dump(info, f)
    # one ad hoc signature over the bundle (its Info.plist bound), as an app built by Xcode has
    subprocess.run(['codesign', '-s', '-', '--force', app], capture_output=True, check=True)


def mac_applications_copy(app, game_dir):
    """macOS: a copy of the game's app in ~/Applications (Launchpad, Spotlight) that runs the game in
    game_dir (LSEnvironment), signed again. Its path."""
    folder = os.path.join(os.path.expanduser('~'), 'Applications')
    os.makedirs(folder, exist_ok=True)
    copy = os.path.join(folder, os.path.basename(app))
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(app, copy, symlinks=True)
    plist = os.path.join(copy, 'Contents', 'Info.plist')
    with open(plist, 'rb') as f:
        info = plistlib.load(f)
    info['LSEnvironment'] = {'VITA_RECOMPS_GAME_DIR': game_dir}
    with open(plist, 'wb') as f:
        plistlib.dump(info, f)
    subprocess.run(['codesign', '-s', '-', '--force', copy], capture_output=True)
    lsregister = ('/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework'
                  '/Support/lsregister')
    if os.path.exists(lsregister):  # LaunchServices reads LSEnvironment when it registers the app
        subprocess.run([lsregister, '-f', copy], capture_output=True)
    return copy


def ask(prompt, default=''):
    answer = input(f'{prompt}{f" [{default}]" if default else ""}: ').strip().strip('"')
    return answer or default


def ask_parts(game):
    """The dump's parts, asked one by one: the game, then its update and add-ons."""
    parts = []
    print(f'\nWhere is your dump of {game["name"]} ({" or ".join([game["title_id"]] + game.get("other_title_ids", []))})? A pkg, a folder, a zip or a vpk.')
    labels = ['the game', 'its update (1.0x patch)', 'an add-on (DLC)']
    step = 0  # the next part asked for: 0 the game, 1 its update, 2 add-ons
    while True:
        label = labels[step]
        path = ask(f'Path to {label}' + (' (empty: none)' if parts else ''))
        if not path:
            if parts:
                return parts
            continue
        if not os.path.exists(path):
            print(f'  {path} does not exist')
            continue
        parts += ['--install', os.path.abspath(path)]
        licence_inside = False
        if step == 0 and not path.lower().endswith('.pkg'):
            # a zip or folder may hold the update and the licence too (NoNpDrm dumps): not asked again
            found = dumpinfo.inspect(path)
            info = dumpinfo.summary(found, [game['title_id']] + game.get('other_title_ids', [])) if found else None
            need, have = dumpinfo.supported_version(game), info['version'] if info else None
            licence_inside = bool(info and info['game'] and info['game']['licence'])
            if have and need and have >= need:
                print(f'  the update is in this dump ({dumpinfo.label(have)})')
                step = 1
            if info and info['game'] and not licence_inside:
                lic = ask('  no licence inside it: work.bin or .rif path, or a zRIF (empty: none)')
                if lic:
                    parts += (['--license', os.path.abspath(lic)] if os.path.exists(lic) else ['--zrif', lic])
        step = min(step + 1, 2)
        if path.lower().endswith('.pkg'):
            lic = ask('  its licence: work.bin or .rif path, or a zRIF (empty: covered by the game\'s)')
            if lic:
                parts += (['--license', os.path.abspath(lic)] if os.path.exists(lic) else ['--zrif', lic])


def remove(path):
    """A file or folder, if there."""
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.exists(path):
        os.remove(path)


class Tee:
    """sys.stdout or sys.stderr, also written to the setup's log file."""

    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, text):
        self.log.write(text)
        self.log.flush()
        try:
            return self.stream.write(text)
        except UnicodeEncodeError:  # a console code page without the character
            return self.stream.write(text.encode(self.stream.encoding or 'ascii', 'replace').decode(
                self.stream.encoding or 'ascii'))

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def start_log(path):
    """The setup's output, also in path (the game's log folder, which a launcher sends with a report).
    Returns the log file, or None if it can't be written."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        log = open(path, 'w', encoding='utf-8', newline='')
    except OSError:
        return None
    sys.stdout, sys.stderr = Tee(sys.stdout, log), Tee(sys.stderr, log)
    return log


def run_logged(cmd, on_line=None):
    """Run a tool with its output passed on (and so into the log), each line also to on_line."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for raw in proc.stdout:
        line = raw.decode('utf-8', 'replace').replace('\r\n', '\n')
        sys.stdout.write(line)
        sys.stdout.flush()
        if on_line:
            on_line(line)
    return proc.wait()


def main():
    argv = sys.argv[1:]
    with open(os.path.join(GAME, 'game.json'), encoding='utf-8') as f:
        game = json.load(f)
    platform = 'windows' if WINDOWS else 'macos' if MACOS else 'linux'
    lock_path = os.path.join(TOOLS, f'toolchain-{platform}.json')
    with open(lock_path, encoding='utf-8') as f:
        lock = json.load(f)
    parts, rest, i = [], [], 0
    while i < len(argv):
        if argv[i] in ('--install', '--license', '--zrif') and i + 1 < len(argv):
            parts += [argv[i], argv[i + 1]]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    opts = {}
    j = 0
    while j < len(rest):
        if rest[j] in ('--cache', '--jobs', '--exe', '--serial', '--pair', '--code', '--connect', '--apk') \
                and j + 1 < len(rest):
            opts[rest[j]] = rest[j + 1]
            j += 2
        elif rest[j] in ('--accept-microsoft-license', '--shortcut', '--steam', '--add-to-steam', '--console',
                         '--progress', '--android', '--accept-android-sdk-license', '--push-only'):
            opts[rest[j]] = True
            j += 1
        else:
            sys.exit(__doc__)
    exe = os.path.abspath(opts['--exe']) if opts.get('--exe') else game_exe(game)

    if opts.get('--add-to-steam'):
        if WINDOWS or MACOS:
            sys.exit('--add-to-steam: Linux (and the Steam Deck) only for now; add the game to Steam with '
                     '"Add a Non-Steam Game"')
        print(add_to_steam(desktop_entry(exe, game, OUT, False)[0]))
        return
    device_opts = [k for k in ('--serial', '--pair', '--connect') if opts.get(k)]
    if opts.get('--android') and not parts and not opts.get('--console') and not device_opts:
        import setupgui
        sys.exit(setupgui.AndroidWindow(game, os.path.abspath(__file__), OUT, exe).run())
    if not parts and not opts.get('--console') and not opts.get('--android'):
        import setupgui
        sys.exit(setupgui.Window(game, os.path.abspath(__file__), OUT, exe, lock.get('microsoft_license')).run())
    if opts.get('--android') and not parts:
        # the game on a device, from what this folder holds: no firmware, no PC compiler, no dump
        return android(game, opts, exe, platform, interactive=bool(opts.get('--console')))
    interactive = not parts
    # the window runs this same setup in a hidden console, so the log has its output too
    log_path = os.path.join(OUT, game['id'], 'setup.log')
    if start_log(log_path):
        print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {game["name"]} setup on {platform} '
              f'({sys.platform}, Python {sys.version.split()[0]}); log: {log_path}')

    def step(fraction, text):
        """--progress: a launcher's progress line, the fraction of the whole setup done."""
        if opts.get('--progress'):
            print(f'progress {fraction:.2f} {text}', flush=True)

    print(f'{game["name"]}: setup')
    if interactive:
        parts = ask_parts(game)
    out = OUT
    # the downloads: inside setup/ and gone after the build, unless --cache keeps them elsewhere
    cache = os.path.abspath(opts['--cache']) if opts.get('--cache') else os.path.join(SETUP, 'downloads')
    work = os.path.join(SETUP, 'work')
    jobs = opts.get('--jobs') or str(max(1, min(os.cpu_count() or 4, 6)))

    # nothing to accept on Linux and macOS: zig is MIT, its LLVM parts Apache
    accepted = bool(opts.get('--accept-microsoft-license')) or 'microsoft_license' not in lock
    if not accepted:
        print('\nThe game\'s code is compiled on this PC with LLVM\'s clang and Microsoft\'s C++ runtime and\n'
              'Windows SDK, downloaded from Microsoft under Microsoft\'s licence (Visual Studio Build Tools):\n'
              f'  {lock["microsoft_license"]}')
        if not interactive or ask('Do you accept it? (yes/no)').lower() not in ('y', 'yes'):
            sys.exit('Not accepted: nothing was downloaded.')

    print(f'\n1/3 firmware (from Sony) and compiler ({lock["download_bytes"] / 2**20:,.0f} MB), into {cache}')
    py = [sys.executable, '-I', '-u']
    step(0.02, 'Downloading the PS Vita firmware from Sony')
    if run_logged(py + [os.path.join(TOOLS, 'fetch-firmware.py'), os.path.join(cache, 'firmware')]):
        sys.exit('downloading the firmware failed')
    tc_dir = os.path.join(cache, 'toolchain' if WINDOWS else f'toolchain-{platform}')
    pinned = [k for k in ('msvc', 'llvm_mingw', 'zig') if k in lock]  # the versions the lock file pins
    have = {}
    if os.path.exists(os.path.join(tc_dir, 'toolchain.json')):
        with open(os.path.join(tc_dir, 'toolchain.json'), encoding='utf-8') as f:
            have = json.load(f)
    if any(have.get(k) != lock[k] for k in pinned):
        step(0.10, 'Downloading the compiler')
        toolchain.fetch(lock_path, tc_dir, True)
    android_tc = android_toolchain(cache) if opts.get('--android') else ''

    print(f'\n2/3 installing the game, recompiling and compiling its code in {out} (several minutes)')
    firmware = [os.path.join(cache, 'firmware', 'PSVUPDAT.PUP'), os.path.join(cache, 'firmware', 'PSP2UPDAT.PUP')]
    install = py + [os.path.join(TOOLS, 'install-game.py'), '--project', GAME, '--bin', out,
                    '--exe', exe, '--work', work, '--toolchain', tc_dir, '--out', out, '--jobs', jobs,
                    '--firmware', *firmware] + parts + (['--android', android_tc] if android_tc else [])
    # install-game.py's output passed on (and into the log), with --progress a progress line at each of its steps
    steps = [('installing', 0.30, 'Installing the game'),
             ('running the game', 0.40, 'Running the game once to read its code'),
             ('recompiling', 0.50, 'Recompiling the game\'s code'),
             ('compiling', 0.60, 'Compiling the game\'s code (this takes a while)')]

    def install_step(line):
        for i, (prefix, fraction, text) in enumerate(steps):
            if line.startswith(prefix):
                step(fraction, text)
                del steps[:i + 1]
                break

    if run_logged(install, install_step):
        # the work files stay, for a look at what went wrong; the next build replaces them
        sys.exit(f'the setup failed (see above, also in {log_path}; its work files are in {work})')

    # nothing of the build is left outside what the game needs: the work files (generated code), the
    # modules as the game loaded them, and the downloads (unless --cache keeps them)
    step(0.95, 'Cleaning up')
    remove(work)
    remove(os.path.join(out, game['id'], 'elfdumps'))
    if not opts.get('--cache'):
        remove(cache)

    if opts.get('--shortcut') and WINDOWS:
        lnk = desktop_shortcut(exe, game['name'])
        print(f'desktop shortcut: {lnk}' if lnk else 'the desktop shortcut could not be made')
    elif MACOS and opts.get('--shortcut'):
        app = exe.split('.app' + os.sep)[0] + '.app'
        print(f'in Applications: {mac_applications_copy(app, out)}')
    elif not WINDOWS and not MACOS:
        desktop, entry = desktop_entry(exe, game, out, bool(opts.get('--shortcut')))
        if opts.get('--shortcut'):
            print(f'applications menu entry: {entry}' if entry else 'the menu entry could not be made')
        if opts.get('--steam'):
            print(add_to_steam(desktop))
    print(f'\n3/3 done: the game is built in {out}')
    step(1.0, 'Done')
    if opts.get('--android'):
        android(game, opts, exe, platform, interactive, built=True)


def android_toolchain(cache):
    """zig for the Android modules: Windows' own lock file (the PC compiler there is clang-cl), the
    PC's zig on Linux and macOS; fetched into the downloads when not there yet."""
    if WINDOWS:
        lock_path, dest = os.path.join(TOOLS, 'toolchain-android-windows.json'), os.path.join(cache, 'toolchain-android')
    else:
        lock_path = os.path.join(TOOLS, f'toolchain-{"macos" if MACOS else "linux"}.json')
        dest = os.path.join(cache, f'toolchain-{"macos" if MACOS else "linux"}')
    with open(lock_path, encoding='utf-8') as f:
        lock = json.load(f)
    have = {}
    if os.path.exists(os.path.join(dest, 'toolchain.json')):
        with open(os.path.join(dest, 'toolchain.json'), encoding='utf-8') as f:
            have = json.load(f)
    if have.get('zig') != lock['zig']:
        print(f'downloading zig {lock["zig"]} for the Android modules ({lock["download_bytes"] / 2**20:,.0f} MB)')
        toolchain.fetch(lock_path, dest, True)
    return dest


def android(game, opts, exe, platform, interactive, built=False):
    """--android: the modules for Android (unless built just now with the PC's, or --push-only), then the
    APK, the game's files and the modules onto the device (android.py push)."""
    import android as droid
    if not built:
        log_path = os.path.join(OUT, game['id'], 'setup.log')
        if start_log(log_path):
            print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {game["name"]} setup for Android on {platform} '
                  f'({sys.platform}, Python {sys.version.split()[0]}); log: {log_path}')
    apk = os.path.abspath(opts['--apk']) if opts.get('--apk') else os.path.join(SETUP, 'android', game['id'] + '.apk')
    if not os.path.exists(apk):
        sys.exit(f'{apk} does not exist: this release has no APK for Android (download it, then pass --apk)')
    cache = os.path.abspath(opts['--cache']) if opts.get('--cache') else os.path.join(SETUP, 'downloads')
    work = os.path.join(SETUP, 'work')
    modules = os.path.join(OUT, 'android')
    py = [sys.executable, '-I', '-u']

    accepted = bool(opts.get('--accept-android-sdk-license'))
    if not accepted:
        with open(droid.LOCK, encoding='utf-8') as f:
            url = json.load(f)['license_url']
        print('\nThe game is copied to the device with adb, part of Google\'s Android SDK Platform-Tools, downloaded\n'
              f'from Google under the Android SDK License Agreement:\n  {url}')
        if not interactive or ask('Do you accept it? (yes/no)').lower() not in ('y', 'yes'):
            sys.exit('Not accepted: nothing was downloaded.')

    if built or opts.get('--push-only'):
        if not glob.glob(os.path.join(modules, '*.so')):
            sys.exit(f'{modules} holds no modules: run the setup with --android (without --push-only) first')
    else:
        if not any(os.path.exists(os.path.join(OUT, 'fs', 'ux0', 'app', t, 'eboot.bin'))
                   for t in [game['title_id']] + game.get('other_title_ids', [])):
            sys.exit('The game isn\'t built on this computer yet: start it once and build it from your dump '
                     '(or run the setup with --install <dump> --android)')
        print(f'\nandroid 1/3 the modules for Android, from the game in {OUT} (several minutes)')
        tc = android_toolchain(cache)
        jobs = opts.get('--jobs') or str(max(1, min(os.cpu_count() or 4, 6)))
        cmd = py + [os.path.join(TOOLS, 'install-game.py'), '--project', GAME, '--bin', OUT, '--exe', exe,
                    '--work', work, '--out', OUT, '--jobs', jobs, '--android', tc, '--android-only']
        if run_logged(cmd):
            sys.exit(f'building the modules for Android failed (see above; its work files are in {work})')
        remove(work)
        remove(os.path.join(OUT, game['id'], 'elfdumps'))

    print('\nandroid 2/3 adb')
    adb = droid.fetch_adb(cache, True)
    print(f'\nandroid 3/3 copying the game to the device (minutes over Wi-Fi)')
    cmd = py + [os.path.join(TOOLS, 'android.py'), '--project', GAME, 'push', OUT, '--apk', apk, '--adb', adb]
    for k in ('--serial', '--pair', '--code', '--connect'):
        if opts.get(k):
            cmd += [k, opts[k]]
    code = run_logged(cmd)
    # adb's server keeps running from the downloads: stopped, so they can go
    subprocess.run([adb, 'kill-server'], capture_output=True)
    time.sleep(1)
    if not opts.get('--cache'):
        remove(cache)
    if code:
        sys.exit('copying to the device failed (see above); the modules stay in '
                 f'{modules}: --android --push-only tries again without building')
    print(f'\nandroid done: start {game["name"]} on the device')


if __name__ == '__main__':
    main()
