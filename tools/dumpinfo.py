"""What a dump holds, read from its param.sfo files: the game, its update (patch) and add-ons, with
versions and whether a licence is inside (sce_sys/package/work.bin, as NoNpDrm dumps keep it, or a
.rif in license/addcont/<title>/<dir>/ or license/app/<title>/ when the dump is laid out as ux0), and
rePatch mods (files under rePatch/<title>/, no param.sfo; kind 'mod'). For the setup, so it doesn't
ask for parts the dump already has. Zips, vpks, folders and pkgs (a pkg's header keeps its
param.sfo unencrypted).

  python -I dumpinfo.py <zip|vpk|folder>   prints what it finds
"""
import os
import re
import struct
import sys
import zipfile

# Vita categories (param.sfo CATEGORY): the game, its update, an add-on
KINDS = {'gd': 'game', 'gp': 'update', 'ac': 'add-on'}
TITLE_ID = re.compile(r'^[A-Z]{4}[0-9]{5}$')
REPATCH_MEMBER = re.compile(r'^(?:.*/)?(?i:repatch)/([A-Z]{4}[0-9]{5})/.*[^/]$')


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


def ux0_licence_dir(sfo):
    """Where a NoNpDrm dump laid out as on a Vita's ux0: keeps this part's licence instead of
    sce_sys/package/work.bin, relative to the folder above app/ or addcont/; None for other parts."""
    tid, cid = str(sfo.get('TITLE_ID', '')), str(sfo.get('CONTENT_ID', ''))
    if not tid:
        return None
    if sfo.get('CATEGORY') == 'ac' and len(cid) > 20:
        return f'license/addcont/{tid}/{cid[20:]}/', f'addcont/{tid}/{cid[20:]}/'
    if str(sfo.get('CATEGORY', '')).startswith('gd'):
        return f'license/app/{tid}/', f'app/{tid}/'
    return None


def find_licence(path):
    """A pkg's licence next to it, as dumps keep it: work.bin, or a single .rif ('' when there's none)."""
    folder = os.path.dirname(os.path.abspath(path))
    if os.path.exists(os.path.join(folder, 'work.bin')):
        return os.path.join(folder, 'work.bin')
    try:
        rifs = [f for f in os.listdir(folder) if f.lower().endswith('.rif')]
    except OSError:
        return ''
    return os.path.join(folder, rifs[0]) if len(rifs) == 1 else ''


def pkg_sfo(path):
    """A PS Vita pkg's param.sfo, which its header keeps unencrypted (info block 14: offset, size), as
    Vita3K's install_pkg reads it; {} for anything else."""
    with open(path, 'rb') as f:
        head = f.read(0x20)
        if len(head) < 0x20 or head[:4] != b'\x7fPKG':
            return {}
        info_offset, info_count = struct.unpack_from('>II', head, 8)
        for _ in range(min(info_count, 64)):
            f.seek(info_offset)
            block = f.read(16)
            if len(block) < 16:
                return {}
            kind, size, a, b = struct.unpack('>IIII', block)
            if kind == 14 and 0 < b <= 1 << 16:
                f.seek(a)
                return parse_sfo(f.read(b))
            info_offset += 8 + size
    return {}


def repatch_ids(path):
    """The title IDs of the rePatch mods in a folder or zip, found as the front end's install_repatch
    finds them: files under rePatch/<title ID>/ up to three levels down; the folder may also be
    rePatch/ itself or rePatch/<title ID>/."""
    if os.path.isdir(path):
        base = os.path.abspath(path)
        name, parent = os.path.basename(base), os.path.basename(os.path.dirname(base))
        if TITLE_ID.match(name) and parent.lower() == 'repatch':
            return [name]

        def ids(repatch):
            return sorted(d for d in os.listdir(repatch)
                          if TITLE_ID.match(d) and os.path.isdir(os.path.join(repatch, d)))
        if name.lower() == 'repatch':
            return ids(base)
        found = []
        for root, dirs, _ in os.walk(base):
            if os.path.relpath(root, base).count(os.sep) >= 2 and root != base:
                dirs[:] = []
            for d in [d for d in dirs if d.lower() == 'repatch']:
                found += ids(os.path.join(root, d))
                dirs.remove(d)
        return sorted(set(found))
    if os.path.isfile(path) and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            return sorted({m.group(1) for n in z.namelist() for m in [REPATCH_MEMBER.match(n.replace(chr(92), '/'))]
                           if m})
    return []


def _mods(path):
    return [{'kind': 'mod', 'title_id': t, 'version': '', 'licence': True} for t in repatch_ids(path)]


def inspect(path, max_depth=4):
    """The parts in a zip, vpk, folder or pkg: [{'kind', 'title_id', 'version', 'licence'}], or None
    when it can't be read (a missing path, a broken archive). A pkg's licence is the one beside it;
    a rePatch mod (kind 'mod') needs none."""
    parts = []
    try:
        if os.path.isfile(path) and path.lower().endswith('.pkg'):
            entry = _entry(pkg_sfo(path), bool(find_licence(path)))
            return [entry] if entry else None
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
                    has = os.path.exists(os.path.join(root, 'package', 'work.bin'))
                    dirs = ux0_licence_dir(sfo)
                    content = os.path.dirname(root)
                    if not has and dirs and content.replace(os.sep, '/').lower().endswith(dirs[1].rstrip('/').lower()):
                        top = content
                        for _ in range(dirs[1].count('/')):
                            top = os.path.dirname(top)
                        lic = os.path.join(top, *dirs[0].split('/'))
                        has = os.path.isdir(lic) and any(f.lower().endswith('.rif') for f in os.listdir(lic))
                    entry = _entry(sfo, has)
                    if entry:
                        parts.append(entry)
            return parts + _mods(path)
        if os.path.isfile(path) and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                lower = {n.lower() for n in names}
                for n in names:
                    if n.lower().endswith('sce_sys/param.sfo') or n.lower() == 'sce_sys/param.sfo':
                        prefix = n[:-len('param.sfo')]
                        sfo = parse_sfo(z.read(n))
                        has = (prefix + 'package/work.bin').lower() in lower
                        dirs = ux0_licence_dir(sfo)
                        content = prefix[:-len('sce_sys/')]
                        if not has and dirs and content.lower().endswith(dirs[1].lower()):
                            lic = (content[:-len(dirs[1])] + dirs[0]).lower()
                            has = any(x.startswith(lic) and x.endswith('.rif') for x in lower)
                        entry = _entry(sfo, has)
                        if entry:
                            parts.append(entry)
            return parts + _mods(path)
    except (OSError, zipfile.BadZipFile, KeyError, ValueError):
        return None
    return None


def summary(parts, title_ids):
    """For one game (any of title_ids): the dump's game and update entries and its newest version.
    {'game': entry or None, 'update': entry or None, 'version': (major, minor) or None,
     'add-ons': count, 'mods': count}"""
    mine = [p for p in parts or [] if p['title_id'] in title_ids]
    game = next((p for p in mine if p['kind'] == 'game'), None)
    update = max((p for p in mine if p['kind'] == 'update'), key=lambda p: version(p['version']) or (0, 0),
                 default=None)
    versions = [version(p['version']) for p in mine if p['kind'] in ('game', 'update')]
    versions = [v for v in versions if v]
    return {'game': game, 'update': update, 'version': max(versions) if versions else None,
            'add-ons': sum(p['kind'] == 'add-on' for p in mine), 'mods': sum(p['kind'] == 'mod' for p in mine)}


def missing_game(paths, title_ids):
    """For a setup's parts (paths, in the order given): why the game can't be installed from them when
    none holds the game itself (an update-only folder, say), else None. Anything inspect() can't read
    may be the game: no message then."""
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
        sys.exit(f'{sys.argv[1]}: not readable as a zip, vpk, folder or pkg')
    for p in found:
        print(f"{p['kind']:7} {p['title_id']} {p['version']}"
              f"{'  licence inside' if p['licence'] and p['kind'] != 'mod' else ''}")
    if found and not any(p['kind'] == 'game' for p in found):
        print('no game: only', ', '.join(sorted({p['kind'] for p in found})))
