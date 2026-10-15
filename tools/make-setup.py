"""Assemble a game's setup for players: a folder (and zip) that turns the player's own dump into a
playable port without Git, Visual Studio or a Python install (docs D9 of the game projects). It holds:

  setup.bat               runs python\\python.exe -I -u kit\\tools\\setup.py
  python\\                 Python's embeddable distribution with capstone and PyYAML (versions pinned:
                          the recompiler's output depends on capstone's)
  kit\\recomp, kit\\tools   this kit's recompiler and tools
  game\\                   the game's front end (a VITA3K_GAMES_ONLY build), Vita3K's data, game.json
                          and the README of the playable folder

Nothing derived from a game or the firmware goes in: release checks refuse such files.

usage:
  python -I make-setup.py resolve <lock.json>           pin the embeddable Python and the wheels
  python -I make-setup.py build <lock.json> --project DIR --bin DIR --out DIR [--zip]
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gameproject import Project  # noqa: E402

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = '3.12.10'
WHEELS = {'capstone': '5.0.7', 'PyYAML': '6.0.3'}
# what the front end's folder may hold; anything else (logs, dumps, saves, recompiled modules) stays out
BIN_FILES = re.compile(r'^(\w[\w-]*\.exe|discord_game_sdk\.dll|lib(ssl|crypto)-\d+-x64\.dll|COPYING\.txt)$')
BIN_DIRS = ('data', 'shaders-builtin', 'icons')
# files a setup must never contain
FORBIDDEN = re.compile(r'(?i)(\.(elf|self|suprx|pkg|pup|rif|vpk|bin|psarc|log)$|work\.bin|ssd_aot_|elfdumps|savedata)')


def get(url, path=None):
    req = urllib.request.Request(url, headers={'User-Agent': 'vita-recomp-kit make-setup.py'})
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


def resolve(lock_path, cache):
    os.makedirs(cache, exist_ok=True)
    files = []
    url = f'https://www.python.org/ftp/python/{PYTHON}/python-{PYTHON}-embed-amd64.zip'
    local = os.path.join(cache, os.path.basename(url))
    get(url, local)
    files.append({'kind': 'python', 'url': url, 'sha256': sha256(local), 'size': os.path.getsize(local)})
    tag = 'cp' + ''.join(PYTHON.split('.')[:2])
    for name, version in WHEELS.items():
        release = json.loads(get(f'https://pypi.org/pypi/{name}/{version}/json'))
        wheels = [u for u in release['urls'] if u['filename'].endswith('.whl') and 'win_amd64' in u['filename']
                  and (f'-{tag}-' in u['filename'] or '-py3-none-' in u['filename'])]
        if not wheels:
            sys.exit(f'{name} {version}: no Windows x64 wheel for {tag}')
        w = wheels[0]
        files.append({'kind': 'wheel', 'url': w['url'], 'sha256': w['digests']['sha256'], 'size': w['size']})
    lock = {'schema': 'vita-recomp-kit.setup.v1', 'python': PYTHON, 'wheels': WHEELS, 'files': files}
    with open(lock_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(lock, f, indent=1)
    print(f'{lock_path}: Python {PYTHON}, ' + ', '.join(f'{k} {v}' for k, v in WHEELS.items()))


def fetch(lock, cache):
    os.makedirs(cache, exist_ok=True)
    paths = {}
    for f in lock['files']:
        local = os.path.join(cache, os.path.basename(f['url']))
        if not (os.path.exists(local) and sha256(local) == f['sha256']):
            get(f['url'], local)
            if sha256(local) != f['sha256']:
                sys.exit(f'{local}: wrong SHA-256')
        paths.setdefault(f['kind'], []).append(local)
    return paths


def copy_tree(src, dst, skip=('__pycache__', 'tests')):
    for d, dirs, files in os.walk(src):
        dirs[:] = [x for x in dirs if x not in skip and not x.startswith('.')]
        rel = os.path.relpath(d, src)
        for f in files:
            if f.endswith(('.pyc', '.ps1')):
                continue
            os.makedirs(os.path.join(dst, rel), exist_ok=True)
            shutil.copy2(os.path.join(d, f), os.path.join(dst, rel, f))


def build(a):
    lock = json.load(open(a.lock, encoding='utf-8'))
    p = Project(a.project)
    out = os.path.abspath(a.out)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    paths = fetch(lock, os.path.join(p.root, 'build', 'setup-downloads'))

    # Python, embeddable, with the wheels in Lib/site-packages; its ._pth file is the whole sys.path
    py = os.path.join(out, 'python')
    with zipfile.ZipFile(paths['python'][0]) as z:
        z.extractall(py)
    site = os.path.join(py, 'Lib', 'site-packages')
    for w in paths['wheel']:
        with zipfile.ZipFile(w) as z:
            z.extractall(site)
    pth = glob.glob(os.path.join(py, 'python*._pth'))[0]
    zipname = os.path.basename(pth)[:-5] + '.zip'
    with open(pth, 'w', encoding='ascii', newline='\r\n') as f:
        f.write(f'{zipname}\n.\nLib\\site-packages\n')

    # the kit: recompiler and tools
    copy_tree(os.path.join(KIT, 'recomp'), os.path.join(out, 'kit', 'recomp'))
    os.makedirs(os.path.join(out, 'kit', 'tools'))
    for f in glob.glob(os.path.join(KIT, 'tools', '*.py')) + glob.glob(os.path.join(KIT, 'tools', '*.json')):
        shutil.copy2(f, os.path.join(out, 'kit', 'tools'))
    shutil.copy2(os.path.join(KIT, 'LICENSE'), os.path.join(out, 'kit', 'LICENSE'))

    # the game's front end and its description
    game = os.path.join(out, 'game')
    os.makedirs(game)
    bin_dir = os.path.abspath(a.bin)
    if not os.path.exists(os.path.join(bin_dir, p.id + '.exe')):
        sys.exit(f'no {p.id}.exe in {bin_dir}')
    for f in os.listdir(bin_dir):
        if os.path.isfile(os.path.join(bin_dir, f)) and BIN_FILES.match(f) \
                and (not f.endswith('.exe') or f == p.id + '.exe'):
            shutil.copy2(os.path.join(bin_dir, f), game)
    for d in BIN_DIRS:
        if os.path.isdir(os.path.join(bin_dir, d)):
            shutil.copytree(os.path.join(bin_dir, d), os.path.join(game, d))
    shutil.copy2(os.path.join(p.root, 'game.json'), game)
    if p.dist_readme:
        os.makedirs(os.path.dirname(os.path.join(game, p.dist_readme)), exist_ok=True)
        shutil.copy2(os.path.join(p.root, p.dist_readme), os.path.join(game, p.dist_readme))

    with open(os.path.join(out, 'setup.bat'), 'w', encoding='ascii', newline='\r\n') as f:
        f.write('@echo off\n"%~dp0python\\python.exe" -I -u "%~dp0kit\\tools\\setup.py" %*\n'
                'if not "%1"=="" exit /b %errorlevel%\npause\n')

    # release check: nothing that looks like game or firmware data
    bad = [os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs
           if FORBIDDEN.search(os.path.relpath(os.path.join(d, f), out).replace('\\', '/'))
           and not os.path.relpath(d, out).startswith('python')]
    if bad:
        shutil.rmtree(out)
        sys.exit('refused, files that look like game data: ' + ', '.join(bad[:10]))

    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
    print(f'{out}: {size / 2**20:,.0f} MB')
    if a.zip:
        archive = shutil.make_archive(out, 'zip', os.path.dirname(out), os.path.basename(out))
        print(f'{archive}: {os.path.getsize(archive) / 2**20:,.0f} MB, SHA-256 {sha256(archive)}')


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == 'resolve':
        resolve(sys.argv[2], os.path.join(os.getcwd(), 'build', 'setup-downloads'))
        return
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument('command', choices=['build'])
    ap.add_argument('lock')
    ap.add_argument('--project', default=os.getcwd())
    ap.add_argument('--bin', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--zip', action='store_true')
    build(ap.parse_args())


if __name__ == '__main__':
    main()
