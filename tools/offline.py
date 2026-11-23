"""Files the setup would download, taken from the player's own folders instead (setup.py --offline):
the firmware PUPs, the compiler's archives, adb. Every download is pinned by size and SHA-256, so a
file is matched by those alone, whatever its name and wherever it sits in the folder: a PUP saved
from Vita3K's guide works as is, and so does a folder setup.py --download-only made (its layout is
the downloads folder's, with offline.json listing what it holds).

Standard library only: android-setup.py (on the device) loads fetch-firmware.py, which uses this.

usage (a check of a folder, without the setup):
  python -I offline.py <folder|file> ...     which pinned downloads of this kit's lock files are there
"""
import glob
import hashlib
import json
import os
import shutil
import sys

DEPTH = 4  # folder levels searched below each path: a bundle's are two deep; a whole drive isn't walked
PATHS = []  # the folders and files use() was given
_index = None  # size -> [paths], built on first use
_hashes = {}  # path -> SHA-256, each file hashed once


def use(paths):
    """Look in these folders (with their subfolders) and files from now on; missing ones are skipped."""
    global PATHS, _index
    PATHS = [os.path.abspath(p) for p in paths if p]
    _index = None


def _build():
    index = {}

    def add(path):
        try:
            index.setdefault(os.path.getsize(path), []).append(path)
        except OSError:
            pass

    for root in PATHS:
        if os.path.isfile(root):
            add(root)
            continue
        base = root.rstrip(os.sep).count(os.sep)
        for folder, dirs, files in os.walk(root):
            if folder.rstrip(os.sep).count(os.sep) - base >= DEPTH:
                dirs[:] = []
            for name in files:
                if not name.endswith('.part'):
                    add(os.path.join(folder, name))
    return index


def sha256(path):
    if path not in _hashes:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        _hashes[path] = h.hexdigest()
    return _hashes[path]


def find(size, digest):
    """A file of that size and SHA-256 in the offline paths, or None."""
    global _index
    if not PATHS:
        return None
    if _index is None:
        _index = _build()
    for path in _index.get(size, []):
        try:
            if sha256(path) == digest.lower():
                return path
        except OSError:
            continue
    return None


def provide(dest, size, digest):
    """dest from the offline paths when a match is there (a hard link when the volume allows, else a
    copy): True if so."""
    src = find(size, digest)
    if not src:
        return False
    if os.path.exists(dest) and os.path.samefile(src, dest):
        return True
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    part = dest + '.part'
    for p in (part, dest):
        if os.path.exists(p):
            os.remove(p)
    try:
        os.link(src, part)
    except OSError:  # another volume, FAT (a USB stick), no permission
        shutil.copyfile(src, part)
    os.replace(part, dest)
    print(f'  {os.path.basename(dest)}: from {src}', flush=True)
    return True


def missing(name, size, digest, url, error):
    """The message when a file is neither offline nor downloadable."""
    where = ('it is not in ' + ', '.join(PATHS)) if PATHS else 'no --offline folder was given'
    return (f'{name} could not be downloaded ({error}), and {where}.\n'
            f'  It is {size:,} bytes, SHA-256 {digest}, from {url}\n'
            f'  Without the network: give a folder or file that holds it with --offline (any name).')


def pinned():
    """Every download this kit's lock files and fetch-firmware.py pin: (what, file, size, sha256, url)."""
    tools = os.path.dirname(os.path.abspath(__file__))
    out = []
    import importlib.util
    spec = importlib.util.spec_from_file_location('fetch_firmware', os.path.join(tools, 'fetch-firmware.py'))
    fw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fw)
    out += [('firmware', name, size, digest, url) for name, url, size, digest in fw.FIRMWARE]
    for lock_path in sorted(glob.glob(os.path.join(tools, 'toolchain-*.json'))):
        with open(lock_path, encoding='utf-8') as f:
            lock = json.load(f)
        what = os.path.basename(lock_path)[:-5]
        out += [(what, p['file'], p['size'], p['sha256'], p['url']) for p in lock['payloads']]
    with open(os.path.join(tools, 'android-tools.json'), encoding='utf-8') as f:
        lock = json.load(f)
    out += [(f'adb-{h}', p['file'], p['size'], p['sha256'], p['url']) for h, p in lock['payloads'].items()]
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    use(sys.argv[1:])
    found = 0
    entries = pinned()
    for what, name, size, digest, _ in entries:
        path = find(size, digest)
        found += bool(path)
        print(f'{"ok " if path else "-- "} {what:24} {name[:60]:60} {path or ""}')
    print(f'{found} of {len(entries)} pinned downloads found')


if __name__ == '__main__':
    main()
