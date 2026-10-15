"""The player's setup (make-setup.py lays it out; setup.bat runs it): from the player's own dump of
the game to a playable folder. It downloads the PS Vita firmware from Sony and a compiler for the
recompiled modules from LLVM's and Microsoft's servers, installs the game, recompiles its code and
compiles it on this PC. Nothing of the game is ever uploaded or shared.

usage (no arguments: it asks):
  setup.bat [--install <pkg|folder|zip|vpk> [--license <work.bin|rif> | --zrif Z]] ...
            [--out DIR] [--cache DIR] [--accept-microsoft-license] [--jobs N]

  --install   repeat for each part, in order: the game, then its update, then add-ons
  --out       the playable folder (default: <game id> next to the setup)
  --cache     the downloads, shared by every game's setup (default %LOCALAPPDATA%\\vita-recomps)
"""
import json
import os
import subprocess
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import toolchain  # noqa: E402

SETUP = os.path.dirname(os.path.dirname(TOOLS))
GAME = os.path.join(SETUP, 'game')


def ask(prompt, default=''):
    answer = input(f'{prompt}{f" [{default}]" if default else ""}: ').strip().strip('"')
    return answer or default


def ask_parts(game):
    """The dump's parts, asked one by one: the game, then its update and add-ons."""
    parts = []
    print(f'\nWhere is your dump of {game["name"]} ({game["title_id"]})? A pkg, a folder, a zip or a vpk.')
    labels = ['the game', 'its update (1.0x patch)', 'an add-on (DLC)']
    while True:
        label = labels[min(len([p for p in parts if p == '--install']), 2)]
        path = ask(f'Path to {label}' + (' (empty: none)' if parts else ''))
        if not path:
            if parts:
                return parts
            continue
        if not os.path.exists(path):
            print(f'  {path} does not exist')
            continue
        parts += ['--install', os.path.abspath(path)]
        if path.lower().endswith('.pkg'):
            lic = ask('  its licence: work.bin or .rif path, or a zRIF (empty: covered by the game\'s)')
            if lic:
                parts += (['--license', os.path.abspath(lic)] if os.path.exists(lic) else ['--zrif', lic])


def main():
    argv = sys.argv[1:]
    with open(os.path.join(GAME, 'game.json'), encoding='utf-8') as f:
        game = json.load(f)
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
        if rest[j] in ('--out', '--cache', '--jobs') and j + 1 < len(rest):
            opts[rest[j]] = rest[j + 1]
            j += 2
        elif rest[j] == '--accept-microsoft-license':
            opts[rest[j]] = True
            j += 1
        else:
            sys.exit(__doc__)
    interactive = not parts

    print(f'{game["name"]}: setup')
    if interactive:
        parts = ask_parts(game)
    out = opts.get('--out') or (ask('\nPlayable folder', os.path.join(os.path.dirname(SETUP), game['id']))
                                if interactive else os.path.join(os.path.dirname(SETUP), game['id']))
    cache = opts.get('--cache') or os.path.join(os.environ.get('LOCALAPPDATA', SETUP), 'vita-recomps')
    jobs = opts.get('--jobs') or str(max(1, min(os.cpu_count() or 4, 6)))

    lock_path = os.path.join(TOOLS, 'toolchain-windows.json')
    with open(lock_path, encoding='utf-8') as f:
        lock = json.load(f)
    accepted = bool(opts.get('--accept-microsoft-license'))
    if not accepted:
        print('\nThe game\'s code is compiled on this PC with LLVM\'s clang and Microsoft\'s C++ runtime and\n'
              'Windows SDK, downloaded from Microsoft under Microsoft\'s licence (Visual Studio Build Tools):\n'
              f'  {lock["microsoft_license"]}')
        if not interactive or ask('Do you accept it? (yes/no)').lower() not in ('y', 'yes'):
            sys.exit('Not accepted: nothing was downloaded.')

    print(f'\n1/3 firmware (from Sony) and compiler ({lock["download_bytes"] / 2**20:,.0f} MB), into {cache}')
    py = [sys.executable, '-I', '-u']
    if subprocess.run(py + [os.path.join(TOOLS, 'fetch-firmware.py'), os.path.join(cache, 'firmware')]).returncode:
        sys.exit('downloading the firmware failed')
    tc_dir = os.path.join(cache, 'toolchain')
    if not os.path.exists(os.path.join(tc_dir, 'toolchain.json')) or \
            json.load(open(os.path.join(tc_dir, 'toolchain.json'), encoding='utf-8')).get('msvc') != lock['msvc'] or \
            json.load(open(os.path.join(tc_dir, 'toolchain.json'), encoding='utf-8')).get('llvm_mingw') != lock['llvm_mingw']:
        toolchain.fetch(lock_path, tc_dir, True)

    print(f'\n2/3 installing the game, recompiling and compiling its code into {out} (several minutes)')
    firmware = [os.path.join(cache, 'firmware', 'PSVUPDAT.PUP'), os.path.join(cache, 'firmware', 'PSP2UPDAT.PUP')]
    r = subprocess.run(py + [os.path.join(TOOLS, 'install-game.py'), '--project', GAME, '--bin', GAME,
                             '--toolchain', tc_dir, '--out', out, '--jobs', jobs, '--firmware', *firmware] + parts)
    if r.returncode:
        sys.exit('the setup failed (see above)')

    print(f'\n3/3 done: start {os.path.join(out, game["id"] + ".exe")}')


if __name__ == '__main__':
    main()
