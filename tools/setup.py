"""The game's importer: from the player's own dump of the game to the game, built in its own folder.
A release is that folder (make-setup.py lays it out): the game's executable (the one thing the
player starts), its files, and this setup in setup/. The game starts the importer itself when it
isn't installed yet, or when asked (--install). The importer downloads the PS Vita firmware from Sony
and uses a compiler for the recompiled modules (zig: the release's own in setup/toolchain, else
downloaded from ziglang.org), installs the game, recompiles its code and compiles it on this
computer, then deletes the downloads and its work files: everything stays in the game's folder.
Nothing of the game is ever uploaded or shared.

usage (no parts: a window asks, setupgui.py; --console: questions in the console). The game passes
its own --dump parts on as --install:
  python -I setup/kit/tools/setup.py [--console]
  python -I setup/kit/tools/setup.py [--install <pkg|folder|zip|vpk> [--license <work.bin|rif> | --zrif Z]] ...
            [--offline PATH ...] [--cache DIR] [--jobs N] [--shortcut] [--steam] [--progress] [--rebuild]
            [--exe FILE]
  python -I setup/kit/tools/setup.py --download-only DIR [--for windows,linux,macos,android|all]
            [--offline PATH ...] [--accept-android-sdk-license]
  python -I setup/kit/tools/setup.py --add-to-steam
  python -I setup/kit/tools/setup.py --android [--console] [--push-only] [--accept-android-sdk-license]
            [--serial S | --pair HOST:PORT --code CODE | --connect HOST:PORT] [--apk FILE] [--cache DIR]

  --install   repeat for each part, in order: the game, then its update, then add-ons and rePatch mods
              (a folder or zip with rePatch/<title ID>/, as for the Vita's rePatch plugin). A pkg
              without --license or --zrif takes the licence beside it (work.bin, or a single .rif).
              When the game is built here by this release and the parts add only add-ons and mods
              (the game's own parts already installed, or left out), only those are installed: no
              download, no build. The same parts as the build here by this release (the same files,
              unchanged), its modules all there: nothing to do ("progress 1.00 Already built" with
              --progress), as a launcher that runs the setup again for every release expects
  --rebuild   with --install: build again even when the game is built here from the same parts
  --offline  a folder (searched with its subfolders, four levels deep) or a file the downloads are
              taken from first, matched by size and SHA-256 whatever their names (offline.py): a folder
              --download-only made, or the firmware PUPs saved earlier. Repeat for more. The game's
              folder's offline/ is searched unasked. A file found nowhere is downloaded as usual
  --download-only  everything the setup downloads (the firmware, the compiler unless the release
              carries it; with android, adb), for this computer's platform or --for's, into DIR with a
              list of it (offline.json); nothing is installed or built. Run again, it fetches only what
              is missing. With that folder the setup needs no network (--offline DIR)
  --cache    keep the downloads in DIR, shared by every game's setup and kept for the next time
              (default: inside setup/, deleted after a successful build)
  --accept-microsoft-license   ignored (nothing of Microsoft's is downloaded any more; older launchers
              still pass it)
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
from pathlib import Path

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import dumpinfo  # noqa: E402
import offline  # noqa: E402
import toolchain  # noqa: E402
from gameproject import EXE_SUFFIX, MACOS, MODULE_SUFFIX, WINDOWS  # noqa: E402

# the minimum macOS of the fork's build (CMAKE_OSX_DEPLOYMENT_TARGET) and of the modules (toolchain.py)
MACOS_MINIMUM = '13.3'

SETUP = os.path.dirname(os.path.dirname(TOOLS))  # <game folder>/setup
OUT = os.path.dirname(SETUP)  # the game's folder: the release, built in place
GAME = os.path.join(SETUP, 'kit', 'game')  # game.json and the game folder's README
OFFLINE = os.path.join(OUT, 'offline')  # searched for the downloads without --offline (offline.py)
BUNDLED_TOOLCHAIN = os.path.join(SETUP, 'toolchain')  # zig, when the release carries it (make-setup.py)


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
    labels = ['the game', 'its update (1.0x patch)', 'an add-on (DLC) or a rePatch mod']
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


def part_groups(parts):
    """The --install parts as [[path, its --license/--zrif args ...], ...]."""
    groups = []
    for k in range(0, len(parts) - 1, 2):
        if parts[k] == '--install':
            groups.append([parts[k + 1]])
        elif groups:
            groups[-1] += [parts[k], parts[k + 1]]
    return groups


def with_pkg_licences(parts):
    """A pkg given without its licence takes the one beside it, as dumps keep it (work.bin, a .rif)."""
    out = []
    for g in part_groups(parts):
        out += ['--install'] + g
        if g[0].lower().endswith('.pkg') and len(g) == 1:
            lic = dumpinfo.find_licence(g[0])
            if lic:
                print(f'{os.path.basename(g[0])}: its licence {lic}')
                out += ['--license', lic]
    return out


def stamp_path(game, out):
    """What built the game in this folder: the executable's SHA-256 (a new release brings a new one)."""
    return os.path.join(out, game['id'], 'build.json')


def sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def read_stamp(game, out):
    try:
        with open(stamp_path(game, out), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_stamp(game, out, exe, parts):
    os.makedirs(os.path.dirname(stamp_path(game, out)), exist_ok=True)
    with open(stamp_path(game, out), 'w', encoding='utf-8') as f:
        json.dump({'exe_sha256': sha256(exe), 'built': time.strftime('%Y-%m-%d %H:%M:%S'),
                   'copy': copy_signature(parts)}, f, indent=1)


def built_by_this_release(game, out, exe):
    return read_stamp(game, out).get('exe_sha256') == sha256(exe)


def copy_signature(parts):
    """What the parts were, to tell the same copy from a changed one: each file's path, size and time;
    a folder's path, file count, total size and newest time; a zRIF only as its SHA-256."""
    import hashlib
    sig = []
    for k in range(0, len(parts) - 1, 2):
        key, value = parts[k], parts[k + 1]
        if key == '--zrif':
            sig.append([key, hashlib.sha256(value.encode()).hexdigest()])
            continue
        path = os.path.abspath(value)
        if os.path.isdir(path):
            n = size = newest = 0
            for root, _, files in os.walk(path):
                for name in files:
                    try:
                        st = os.stat(os.path.join(root, name))
                    except OSError:
                        continue
                    n, size, newest = n + 1, size + st.st_size, max(newest, st.st_mtime_ns)
            sig.append([key, path, 'folder', n, size, newest])
        else:
            try:
                st = os.stat(path)
                sig.append([key, path, st.st_size, st.st_mtime_ns])
            except OSError:
                sig.append([key, path])
    return sig


def module_files(game, out):
    """The recompiled modules the build leaves in the game's folder."""
    return [os.path.join(out, f'ssd_aot_{name}{MODULE_SUFFIX}') for name in game['modules'].values()]


def already_built(game, out, exe, parts):
    """Built here by this release from the same copy (the same parts), its modules all there: a
    launcher's setup step run again (RecompLauncher runs it for every new release and when the player
    changes their copy) has nothing to do."""
    stamp = read_stamp(game, out)
    return (stamp.get('exe_sha256') == sha256(exe) and stamp.get('copy') == copy_signature(parts)
            and all(os.path.exists(m) for m in module_files(game, out)))


def installed_version(out, title_id):
    """The newest version of title_id installed in the folder's Vita file system (the game or its
    update), as (1, 30); None when it isn't installed."""
    versions = []
    for sub in ('app', 'patch'):
        sfo = os.path.join(out, 'fs', 'ux0', sub, title_id, 'sce_sys', 'param.sfo')
        if os.path.exists(sfo):
            with open(sfo, 'rb') as f:
                versions.append(dumpinfo.version(str(dumpinfo.parse_sfo(f.read(1 << 16)).get('APP_VER', ''))))
    versions = [v for v in versions if v]
    return max(versions) if versions else None


def addon_parts(parts, out):
    """The parts to install when the game is built: those holding add-ons or rePatch mods, if every
    game or update in the parts is installed already (at that version or newer). None when something
    else is new (a full build) or no part holds an add-on or mod (the player asked to build again)."""
    addons = []
    for g in part_groups(parts):
        found = dumpinfo.inspect(g[0])
        if not found:
            return None
        for e in found:
            if e['kind'] in ('game', 'update'):
                have, want = installed_version(out, e['title_id']), dumpinfo.version(e['version'])
                if not have or not want or want > have:
                    return None
        if any(e['kind'] in ('add-on', 'mod') for e in found):
            addons.append(g)
    return addons or None


def note_other_region_addons(parts, out, ids):
    """An add-on for another title than the game's (the other region's pack) installs for that title,
    which the game never reads: said, not refused."""
    games = {t for t in ids if installed_version(out, t)}
    entries = [e for g in part_groups(parts) for e in (dumpinfo.inspect(g[0]) or [])]
    games |= {e['title_id'] for e in entries if e['kind'] == 'game'}
    for e in entries:
        if e['kind'] == 'add-on' and games and e['title_id'] not in games:
            print(f'Note: an add-on for {e["title_id"]}, but the game is {" or ".join(sorted(games))}: it is '
                  f'installed for {e["title_id"]}, and this game won\'t use it (each region\'s game reads only '
                  f'its own add-ons)')


def warn_encrypted_addons(out):
    """Add-ons installed as dumped, still encrypted (no licence found): the game can't read them."""
    for files_db in glob.glob(os.path.join(out, 'fs', 'ux0', 'addcont', '*', '*', 'sce_pfs', 'files.db')):
        content = os.path.dirname(os.path.dirname(files_db))
        print(f'Warning: the add-on in {content} is still encrypted: its licence wasn\'t found (work.bin in '
              f'its sce_sys/package, a .rif in the dump\'s license/addcont/..., or a pkg\'s licence beside '
              f'it). Install it again with its licence.')


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


def step_error(folder):
    """The front end's step-error.txt in the game's log folder: a line per part a failed install step
    could not install, in words for the player; '' when it left none."""
    try:
        with open(os.path.join(folder, 'step-error.txt'), encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


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
    parts, rest, offline_given, i = [], [], [], 0
    while i < len(argv):
        if argv[i] in ('--install', '--license', '--zrif') and i + 1 < len(argv):
            parts += [argv[i], argv[i + 1]]
            i += 2
        elif argv[i] == '--offline' and i + 1 < len(argv):
            offline_given.append(os.path.abspath(argv[i + 1]))
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    opts = {}
    j = 0
    while j < len(rest):
        if rest[j] in ('--cache', '--jobs', '--exe', '--serial', '--pair', '--code', '--connect', '--apk',
                       '--download-only', '--for') and j + 1 < len(rest):
            opts[rest[j]] = rest[j + 1]
            j += 2
        elif rest[j] in ('--accept-microsoft-license', '--shortcut', '--steam', '--add-to-steam', '--console',
                         '--progress', '--android', '--accept-android-sdk-license', '--push-only', '--rebuild'):
            opts[rest[j]] = True
            j += 1
        else:
            sys.exit(__doc__)
    exe = os.path.abspath(opts['--exe']) if opts.get('--exe') else game_exe(game)
    # files the downloads are taken from first: --offline, and the game folder's offline/ unasked
    offline_paths = offline_given + ([OFFLINE] if os.path.isdir(OFFLINE) and OFFLINE not in offline_given else [])
    offline.use(offline_paths)

    if opts.get('--download-only'):
        return download_only(os.path.abspath(opts['--download-only']), opts, platform, offline_paths)
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
        w = setupgui.Window(game, os.path.abspath(__file__), OUT, exe, offline_given)
        sys.exit(w.run())
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
    parts = with_pkg_licences(parts)
    out = OUT
    # a dump without the game itself (only its update) fails at the install: said before the downloads.
    # An update alone is fine when the game is installed in the folder already.
    ids = [game['title_id']] + game.get('other_title_ids', [])
    if parts and not any(os.path.exists(os.path.join(out, 'fs', 'ux0', 'app', t, 'eboot.bin')) for t in ids):
        missing = dumpinfo.missing_game([parts[k + 1] for k in range(0, len(parts), 2) if parts[k] == '--install'],
                                        ids)
        if missing:
            sys.exit(f'Not installed: {missing}')
    # a folder copied straight off a Vita: its licence is the account's, unusable here (ss-delta #13)
    unusable = dumpinfo.account_licences(part_groups(parts), ids) if parts else None
    if unusable:
        sys.exit(f'Not installed: {unusable}')
    note_other_region_addons(parts, out, ids)

    # built here by this release from these same parts: nothing to do, unless --rebuild
    if parts and not opts.get('--android') and not opts.get('--rebuild') and already_built(game, out, exe, parts):
        print('\nThe game is built here already, by this release and from this copy: nothing to do '
              '(--rebuild builds it again)')
        step(1.0, 'Already built')
        return

    # the game built here by this release, and the parts bring only add-ons or mods (a launcher's
    # setup run again for a new add-on): those installed, nothing downloaded or built again
    addons = addon_parts(parts, out) if parts and not opts.get('--android') and \
        built_by_this_release(game, out, exe) else None
    if addons:
        print('\nThe game is built here already: installing only the add-ons and mods')
        for n, g in enumerate(addons):
            step(0.10 + 0.85 * n / len(addons), 'Installing the add-ons and mods')
            print(f'installing {os.path.basename(g[0])}')
            cmd = [exe, '--game', out, '--vita-fs', os.path.join(out, 'fs'), '--install-content'] + g
            (Path(out) / game['id'] / 'step-error.txt').unlink(missing_ok=True)
            if run_logged(cmd):
                # the front end's own words for what failed (a licence for other content ...)
                log = os.path.join(out, game['id'], 'vita3k.log')
                why = step_error(os.path.join(out, game['id']))
                sys.exit(f'installing {g[0]} failed: {why}\nMore in {log}' if why
                         else f'installing {g[0]} failed (the game\'s log: {log})')
        warn_encrypted_addons(out)
        write_stamp(game, out, exe, parts)  # these parts now, so the same run again has nothing to do
        print('\ndone: installed')
        step(1.0, 'Done')
        return
    # a build cut short must never look finished: the stamp goes before anything is written
    remove(stamp_path(game, out))
    # the downloads: inside setup/ and gone after the build, unless --cache keeps them elsewhere
    cache = os.path.abspath(opts['--cache']) if opts.get('--cache') else os.path.join(SETUP, 'downloads')
    work = os.path.join(SETUP, 'work')
    jobs = opts.get('--jobs') or str(max(1, min(os.cpu_count() or 4, 6)))

    # nothing to accept for the compiler: zig is MIT, its LLVM parts Apache
    bundled = zig_version(BUNDLED_TOOLCHAIN) == lock['zig']  # the release carries the compiler
    print(f'\n1/3 firmware (from Sony), into {cache}' if bundled else
          f'\n1/3 firmware (from Sony) and compiler ({lock["download_bytes"] / 2**20:,.0f} MB), into {cache}')
    py = [sys.executable, '-I', '-u']
    if offline_paths:
        print('files found in ' + ', '.join(offline_paths) + ' are used instead of downloads')
    step(0.02, 'Downloading the PS Vita firmware from Sony')
    if run_logged(py + [os.path.join(TOOLS, 'fetch-firmware.py'), os.path.join(cache, 'firmware')]
                  + [a for p in offline_paths for a in ('--offline', p)]):
        sys.exit('downloading the firmware failed')
    if not bundled:
        step(0.10, 'Downloading the compiler')
    tc_dir = pc_toolchain(cache, platform)
    android_tc = tc_dir if opts.get('--android') else ''  # the same zig builds Android's modules

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
    warn_encrypted_addons(out)
    # what built it, from what: a later run with the same parts has nothing to do, one with only add-ons
    # or mods installs them without building again
    write_stamp(game, out, exe, parts)

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


PLATFORMS = ('windows', 'linux', 'macos', 'android')


def download_only(dest, opts, platform, offline_paths):
    """--download-only: every file the setup downloads, for this platform or --for's, into dest with
    offline.json; nothing installed or built. dest has the downloads folder's layout, but the setup
    matches the files by size and SHA-256 alone (--offline dest, or dest as the game folder's offline/).
    The player downloads Sony's and Google's files themselves, under their licences, as a build
    would."""
    import importlib.util
    targets = [t.strip() for t in (opts.get('--for') or platform).lower().split(',') if t.strip()]
    if 'all' in targets:
        targets = list(PLATFORMS)
    bad = [t for t in targets if t not in PLATFORMS]
    if bad:
        sys.exit(f'--for: {", ".join(bad)}? (windows, linux, macos, android, all; comma-separated)')
    # the computers the setup runs on; android: for Android from those (this one when none is named)
    hosts = [t for t in PLATFORMS[:3] if t in targets] or [platform]

    def lock_of(name):
        with open(os.path.join(TOOLS, name), encoding='utf-8') as f:
            return json.load(f)

    # (folder in dest, as setup.py's downloads lay it out; payload: file, url, size, sha256; what)
    items = []
    for h in hosts:  # the PC's zig builds Android's modules too
        lock = lock_of(f'toolchain-{h}.json')
        if h == platform and zig_version(BUNDLED_TOOLCHAIN) == lock['zig']:
            print(f'the compiler for {h}: this release carries it ({BUNDLED_TOOLCHAIN})')
            continue
        folder = os.path.join(toolchain_folder(h), 'downloads')
        items += [(folder, p, f'compiler for {h}') for p in lock['payloads']]
    if 'android' in targets:
        adb = lock_of('android-tools.json')
        items += [('downloads', adb['payloads'][h], f'adb for {h}') for h in hosts]

    licences = []
    if 'android' in targets and not opts.get('--accept-android-sdk-license'):
        licences.append(('--accept-android-sdk-license', 'Google\'s Android SDK Platform-Tools (adb), the Android '
                         'SDK License Agreement', lock_of('android-tools.json')['license_url']))
    for flag, what, url in licences:
        print(f'\nThis downloads {what}:\n  {url}')
        if not (sys.stdin and sys.stdin.isatty()) or ask('Do you accept it? (yes/no)').lower() not in ('y', 'yes'):
            sys.exit(f'Not accepted: nothing was downloaded (or pass {flag}).')

    spec = importlib.util.spec_from_file_location('fetch_firmware', os.path.join(TOOLS, 'fetch-firmware.py'))
    fw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fw)
    total = sum(s for _, _, s, _ in fw.FIRMWARE) + sum(p['size'] for _, p, _ in items)
    print(f'{len(fw.FIRMWARE) + len(items)} files ({total / 2**20:,.0f} MB) for {", ".join(targets)} into {dest}')
    print('\nthe PS Vita firmware (from Sony)')
    fw.fetch(os.path.join(dest, 'firmware'), offline_paths)
    done = 0
    for folder, p, what in items:
        local = os.path.join(dest, folder, p['file'])
        os.makedirs(os.path.dirname(local), exist_ok=True)
        toolchain.obtain(p, local)
        if toolchain.sha256(local) != p['sha256']:
            os.remove(local)
            sys.exit(f'{p["file"]}: wrong SHA-256 (the download changed or was damaged)')
        done += 1
        print(f'  {done}/{len(items)}  {p["size"] / 2**20:7,.1f} MB  {what}: {p["file"]}', flush=True)

    files = [{'path': f'firmware/{name}', 'size': size, 'sha256': digest, 'url': url, 'what': 'PS Vita firmware'}
             for name, url, size, digest in fw.FIRMWARE]
    files += [{'path': f'{folder}/{p["file"]}'.replace(os.sep, '/'), 'size': p['size'], 'sha256': p['sha256'],
               'url': p['url'], 'what': what} for folder, p, what in items]
    manifest = {'schema': 'vita-recomp-kit.offline.v1', 'made': time.strftime('%Y-%m-%d'), 'for': targets,
                'note': 'Files the vita-recomp-kit setup downloads, for setup.py --offline (or a game folder\'s '
                        'offline/). Matched by size and SHA-256. Sony\'s and Google\'s files are '
                        'under their own licences: keep them for yourself, don\'t share them.',
                'files': files}
    with open(os.path.join(dest, 'offline.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(manifest, f, indent=1)
    print(f'\ndone: {dest}\nWith this folder the setup needs no network: put it in the game\'s folder as offline/, '
          f'or pass --offline "{dest}" (the game\'s executable takes it too).')


def toolchain_folder(platform):
    """The compiler's folder in the downloads (its archive in <folder>/downloads)."""
    return 'toolchain' if platform == 'windows' else f'toolchain-{platform}'


def zig_version(folder):
    """The zig version toolchain.json in folder names, when its zig is there; None otherwise."""
    try:
        with open(os.path.join(folder, 'toolchain.json'), encoding='utf-8') as f:
            have = json.load(f).get('zig')
    except (OSError, ValueError):
        return None
    return have if toolchain.zig_executable(folder) else None


def pc_toolchain(cache, platform):
    """zig for this computer's modules and for Android's: the release's own (setup/toolchain,
    make-setup.py), else fetched into the downloads when not there yet (or another version is)."""
    lock_path = os.path.join(TOOLS, f'toolchain-{platform}.json')
    with open(lock_path, encoding='utf-8') as f:
        lock = json.load(f)
    if zig_version(BUNDLED_TOOLCHAIN) == lock['zig']:
        return BUNDLED_TOOLCHAIN
    dest = os.path.join(cache, toolchain_folder(platform))
    if zig_version(dest) != lock['zig']:
        print(f'downloading zig {lock["zig"]} ({lock["download_bytes"] / 2**20:,.0f} MB)')
        toolchain.fetch(lock_path, dest)
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

    installed_adb = droid.find_adb(required=False)
    if not installed_adb and not opts.get('--accept-android-sdk-license'):
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
        tc = pc_toolchain(cache, platform)
        jobs = opts.get('--jobs') or str(max(1, min(os.cpu_count() or 4, 6)))
        cmd = py + [os.path.join(TOOLS, 'install-game.py'), '--project', GAME, '--bin', OUT, '--exe', exe,
                    '--work', work, '--out', OUT, '--jobs', jobs, '--android', tc, '--android-only']
        if run_logged(cmd):
            sys.exit(f'building the modules for Android failed (see above; its work files are in {work})')
        remove(work)
        remove(os.path.join(OUT, game['id'], 'elfdumps'))

    print('\nandroid 2/3 adb')
    server_in_use = droid.adb_server_in_use()
    if installed_adb:
        adb = installed_adb
        print(f'using installed adb: {adb}')
    else:
        adb = droid.adb_in(cache) if server_in_use else ''
        if not adb:
            adb = droid.fetch_adb(cache, True)
        server_in_use = server_in_use or droid.adb_server_in_use()
        if server_in_use:
            print('An adb server is already running. The downloaded adb may replace it if the versions differ. '
                  "If the wireless device disappears, reconnect it or put the running server's adb on PATH and retry.")
    print(f'\nandroid 3/3 copying the game to the device (minutes over Wi-Fi)')
    cmd = py + [os.path.join(TOOLS, 'android.py'), '--project', GAME, 'push', OUT, '--apk', apk, '--adb', adb]
    for k in ('--serial', '--pair', '--code', '--connect'):
        if opts.get(k):
            cmd += [k, opts[k]]
    code = run_logged(cmd)
    # A server started from the downloaded client keeps its files open on Windows.
    if not installed_adb and not server_in_use:
        subprocess.run([adb, 'kill-server'], capture_output=True)
        time.sleep(1)
    if not opts.get('--cache') and (installed_adb or not server_in_use):
        remove(cache)
    if code:
        sys.exit('copying to the device failed (see above); the modules stay in '
                 f'{modules}: --android --push-only tries again without building')
    print(f'\nandroid done: start {game["name"]} on the device')


if __name__ == '__main__':
    main()
