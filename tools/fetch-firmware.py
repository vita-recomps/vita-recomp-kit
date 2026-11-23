"""Download the PS Vita firmware Vita3K needs from Sony's own update servers, as Vita3K's guide does:
the 3.74 system update and the font package, or, when Sony's server fails, the Wayback Machine's
copies of the same files (MIRRORS). Each file is checked against its SHA-256. Nothing of the firmware
is in this repository, and nothing downloaded here may be committed.

A copy the player already has is used instead of a download (offline.py): --offline takes a
folder (searched with its subfolders) or a file, matched by size and SHA-256 whatever its name.

usage:
  python -I fetch-firmware.py <dir> [--offline PATH ...]   -> <dir>/PSVUPDAT.PUP, <dir>/PSP2UPDAT.PUP
"""
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import offline  # noqa: E402

FIRMWARE = [
    # (file, URL, size, SHA-256)
    ('PSVUPDAT.PUP', 'http://dus01.psv.update.playstation.net/update/psv/image/2022_0209/'
     'rel_f2c7b12fe85496ec88a0391b514d6e3b/PSVUPDAT.PUP', 133834240,
     '6ef6dc8da6db026f28647713e473486d770087a605c52a8d751bfca7478386cf'),
    ('PSP2UPDAT.PUP', 'http://dus01.psp2.update.playstation.net/update/psp2/image/2019_0924/'
     'sd_8b5f60b56c3da8365b973dba570c53a5/PSP2UPDAT.PUP', 56768512,
     'f372d306a7f2711125bea29aa75cbcb1eb19724e9648478fba07b8d655b37344'),
]
# Tried in order when Sony's URL fails: the Wayback Machine's copies of it (the raw bytes: id_), at
# snapshots checked to be the very file (its SHA-1 in Wayback's index, 2026-11-23; PSP2UPDAT's
# 2025-07-15 copy downloaded and its SHA-256 checked). Never the newest snapshot by itself: PSP2UPDAT's
# of 2026-02-27 is another file. The SHA-256 is checked anyway.
WAYBACK = 'https://web.archive.org/web/{}id_/{}'
MIRRORS = {
    'PSVUPDAT.PUP': [WAYBACK.format(t, FIRMWARE[0][1]) for t in ('20250319110354', '20230212113342')],
    'PSP2UPDAT.PUP': [WAYBACK.format(t, FIRMWARE[1][1]) for t in ('20250715044721', '20220707235317')],
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def download(url, part):
    req = urllib.request.Request(url, headers={'User-Agent': 'vita-recomp-kit fetch-firmware.py'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(part, 'wb') as f:
            shutil.copyfileobj(r, f, 1 << 20)
    except urllib.error.URLError as e:
        # Windows: Python checks certificates against the local store only (Windows fetches a
        # missing intermediate certificate on demand for its own programs): Windows' curl.exe
        curl = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'curl.exe')
        if not (sys.platform == 'win32' and 'CERTIFICATE_VERIFY_FAILED' in str(e) and os.path.exists(curl)):
            raise
        # (no console flashing up over the setup's window)
        code = subprocess.run([curl, '-sSfL', '--retry', '3', '-A', 'vita-recomp-kit fetch-firmware.py',
                               '-o', part, url], creationflags=subprocess.CREATE_NO_WINDOW).returncode
        if code:
            raise OSError(f'curl.exe exit code {code}') from e


def fetch(dest, offline_paths=()):
    """Take from offline_paths, or download, what is missing or damaged; return the PUP paths."""
    os.makedirs(dest, exist_ok=True)
    if offline_paths:
        offline.use(offline_paths)
    paths = []
    for name, url, size, digest in FIRMWARE:
        path = os.path.join(dest, name)
        have = os.path.exists(path) and os.path.getsize(path) == size and sha256(path) == digest
        if not have and not offline.provide(path, size, digest):
            urls = [url] + MIRRORS.get(name, [])
            errors = []
            for n, u in enumerate(urls):
                print(f'downloading {name} ({size / 2**20:.0f} MB) from '
                      + ('Sony' if n == 0 else f'a copy of Sony\'s file ({u.split("/")[2]})'), flush=True)
                try:
                    download(u, path + '.part')
                except (OSError, ValueError) as e:  # URLError, timeouts, curl.exe's failure
                    errors.append(f'{u.split("/")[2]}: {e}')
                    continue
                if sha256(path + '.part') == digest:
                    os.replace(path + '.part', path)
                    break
                os.remove(path + '.part')
                errors.append(f'{u.split("/")[2]}: wrong SHA-256 (damaged, or another file)')
            else:
                sys.exit(offline.missing(name, size, digest, urls, '; '.join(errors)))
        paths.append(path)
    return paths


if __name__ == '__main__':
    args = sys.argv[1:]
    given = [args[i + 1] for i in range(len(args) - 1) if args[i] == '--offline']
    rest = [a for i, a in enumerate(args) if a != '--offline' and (i == 0 or args[i - 1] != '--offline')]
    if len(rest) != 1:
        sys.exit(__doc__)
    for p in fetch(rest[0], given):
        print(p)
