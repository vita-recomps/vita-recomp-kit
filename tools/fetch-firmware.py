"""Download the PS Vita firmware Vita3K needs from Sony's own update servers, as Vita3K's guide does:
the 3.74 system update and the font package. Each file is checked against its SHA-256. Nothing of
the firmware is in this repository, and nothing downloaded here may be committed.

usage:
  python -I fetch-firmware.py <dir>        -> <dir>/PSVUPDAT.PUP, <dir>/PSP2UPDAT.PUP
"""
import hashlib
import os
import shutil
import sys
import urllib.request

FIRMWARE = [
    # (file, URL, size, SHA-256)
    ('PSVUPDAT.PUP', 'http://dus01.psv.update.playstation.net/update/psv/image/2022_0209/'
     'rel_f2c7b12fe85496ec88a0391b514d6e3b/PSVUPDAT.PUP', 133834240,
     '6ef6dc8da6db026f28647713e473486d770087a605c52a8d751bfca7478386cf'),
    ('PSP2UPDAT.PUP', 'http://dus01.psp2.update.playstation.net/update/psp2/image/2019_0924/'
     'sd_8b5f60b56c3da8365b973dba570c53a5/PSP2UPDAT.PUP', 56768512,
     'f372d306a7f2711125bea29aa75cbcb1eb19724e9648478fba07b8d655b37344'),
]


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def fetch(dest):
    """Download what is missing or damaged; return the PUP paths."""
    os.makedirs(dest, exist_ok=True)
    paths = []
    for name, url, size, digest in FIRMWARE:
        path = os.path.join(dest, name)
        if not (os.path.exists(path) and os.path.getsize(path) == size and sha256(path) == digest):
            print(f'downloading {name} ({size / 2**20:.0f} MB) from Sony', flush=True)
            req = urllib.request.Request(url, headers={'User-Agent': 'vita-recomp-kit fetch-firmware.py'})
            with urllib.request.urlopen(req, timeout=120) as r, open(path + '.part', 'wb') as f:
                shutil.copyfileobj(r, f, 1 << 20)
            if sha256(path + '.part') != digest:
                os.remove(path + '.part')
                sys.exit(f'{name}: wrong SHA-256 (the download was damaged or Sony changed the file)')
            os.replace(path + '.part', path)
        paths.append(path)
    return paths


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    for p in fetch(sys.argv[1]):
        print(p)
