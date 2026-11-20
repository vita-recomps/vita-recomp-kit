"""What a dump holds, read from its param.sfo files: the game, its update (patch) and add-ons, with
versions and whether a licence (sce_sys/package/work.bin, as NoNpDrm dumps keep it) is inside. For the
setup, so it doesn't ask for parts the dump already has. Zips, vpks and folders; a pkg is encrypted
and stays unknown (None).

  python -I dumpinfo.py <zip|vpk|folder>   prints what it finds
"""
import os
import re
import struct
import sys
import zipfile

# Vita categories (param.sfo CATEGORY): the game, its update, an add-on
KINDS = {'gd': 'game', 'gp': 'update', 'ac': 'add-on'}


def parse_sfo(data):
    """param.sfo's keys and values (strings and integers); {} for anything that isn't one."""
    if len(data) < 20 or data[:4] != b'\0PSF':
        return {}
    _, _, key_table, data_table, count = struct.unpack_from('<4sIIII', data, 0)
    out = {}
    try:
        for i in range(count):
            key_off, fmt, length, _, data_off = struct.unpack_from('<HHIII', data, 20 + i * 16)
            key = data[key_table + key_off:data.index(b'\0', key_table + key_off)].decode('ascii', 'replace')
            raw = data[data_table + data_off:data_table + data_off + length]
            out[key] = (struct.unpack('<I', raw)[0] if fmt == 0x404 and len(raw) == 4
                        else raw.rstrip(b'\0').decode('utf-8', 'replace'))
    except (struct.error, ValueError):
        return {}
    return out


def version(text):
    """'01.30' or '1.30' as (1, 30), for comparing; None if it isn't a version."""
    m = re.match(r'^\s*(\d+)\.(\d+)', text or '')
    return (int(m.group(1)), int(m.group(2))) if m else None


def _entry(sfo, has_licence):
    kind = KINDS.get(str(sfo.get('CATEGORY', '')))
    if not kind:
        return None
    return {'kind': kind, 'title_id': str(sfo.get('TITLE_ID', '')),
            'version': str(sfo.get('APP_VER') or sfo.get('VERSION') or ''), 'licence': has_licence}


def inspect(path, max_depth=4):
    """The parts in a zip, vpk or folder: [{'kind', 'title_id', 'version', 'licence'}], or None when
    it can't be read (a pkg, a missing path, a broken archive)."""
    parts = []
    try:
        if os.path.isdir(path):
            base = os.path.abspath(path)
            for root, dirs, files in os.walk(base):
                depth = os.path.relpath(root, base).count(os.sep)
                if depth >= max_depth:
                    dirs[:] = []
                if os.path.basename(root).lower() == 'sce_sys' and 'param.sfo' in (f.lower() for f in files):
                    name = next(f for f in files if f.lower() == 'param.sfo')
                    with open(os.path.join(root, name), 'rb') as f:
                        sfo = parse_sfo(f.read(1 << 16))
                    entry = _entry(sfo, os.path.exists(os.path.join(root, 'package', 'work.bin')))
                    if entry:
                        parts.append(entry)
            return parts
        if os.path.isfile(path) and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                lower = {n.lower() for n in names}
                for n in names:
                    if n.lower().endswith('sce_sys/param.sfo') or n.lower() == 'sce_sys/param.sfo':
                        prefix = n[:-len('param.sfo')]
                        entry = _entry(parse_sfo(z.read(n)), (prefix + 'package/work.bin').lower() in lower)
                        if entry:
                            parts.append(entry)
            return parts
    except (OSError, zipfile.BadZipFile, KeyError, ValueError):
        return None
    return None


def summary(parts, title_ids):
    """For one game (any of title_ids): the dump's game and update entries and its newest version.
    {'game': entry or None, 'update': entry or None, 'version': (major, minor) or None,
     'add-ons': count}"""
    mine = [p for p in parts or [] if p['title_id'] in title_ids]
    game = next((p for p in mine if p['kind'] == 'game'), None)
    update = max((p for p in mine if p['kind'] == 'update'), key=lambda p: version(p['version']) or (0, 0),
                 default=None)
    versions = [version(p['version']) for p in mine if p['kind'] in ('game', 'update')]
    versions = [v for v in versions if v]
    return {'game': game, 'update': update, 'version': max(versions) if versions else None,
            'add-ons': sum(p['kind'] == 'add-on' for p in mine)}


def missing_game(paths, title_ids):
    """For a setup's parts (paths, in the order given): why the game can't be installed from them when
    none holds the game itself (an update-only folder, say), else None. A pkg, or anything else
    inspect() can't read, may be the game: no message then."""
    updates = []
    for path in paths:
        found = inspect(path)
        if found is None:
            return None
        info = summary(found, title_ids)
        if info['game']:
            return None
        if info['update']:
            updates.append((path, info['update']))
    ids = ' or '.join(title_ids)
    if not updates:
        return f'no game {ids} found in {", ".join(os.path.normpath(x) for x in paths)}'
    path, update = os.path.normpath(updates[0][0]), updates[0][1]
    tid = update['title_id']
    return (f'{path} holds only the update {label(version(update["version"]))} of {tid}, not the game. '
            f'A NoNpDrm dump keeps the game in app/{tid} and the update in patch/{tid}: give the folder '
            f'that holds both (or the dump\'s zip), or the game\'s folder then the update\'s. The game and '
            f'its update unpacked into one {tid} folder don\'t work: the update\'s files replace the game\'s.')


def supported_version(game):
    """The newest version game.json's eboot_sha256 labels name ('1.30 (US)', '1.02'), as (1, 30)."""
    versions = [version(label) for label in game.get('eboot_sha256', {}).values()]
    versions = [v for v in versions if v]
    return max(versions) if versions else None


def label(v):
    return f'{v[0]}.{v[1]:02}' if v else '?'


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    found = inspect(sys.argv[1])
    if found is None:
        sys.exit(f'{sys.argv[1]}: not readable as a zip, vpk or folder (a pkg is encrypted)')
    for p in found:
        print(f"{p['kind']:7} {p['title_id']} {p['version']}{'  licence inside' if p['licence'] else ''}")
    if found and not any(p['kind'] == 'game' for p in found):
        print('no game: only', ', '.join(sorted({p['kind'] for p in found})))
