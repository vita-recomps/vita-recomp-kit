"""Vita SCE ELF analyzer: segments, module_info, imports/exports (NID-resolved), exidx function starts.
usage: python -I velf.py <elf> <vita-headers db dir> [--json out.json]"""
import struct, sys, os, glob, json, collections
import yaml

elf_path, dbdir = sys.argv[1], sys.argv[2]
d = open(elf_path, 'rb').read()

# NID db
lib_names, func_names = {}, {}
for y in glob.glob(os.path.join(dbdir, '**', '*.yml'), recursive=True):
    try:
        doc = yaml.safe_load(open(y, encoding='utf-8'))
    except Exception:
        continue
    for mname, m in (doc.get('modules') or {}).items():
        for lname, l in (m.get('libraries') or {}).items():
            if l.get('nid') is not None:
                lib_names[l['nid']] = lname
            for fname, nid in (l.get('functions') or {}).items():
                func_names[nid] = fname
            for vname, nid in (l.get('variables') or {}).items():
                func_names[nid] = vname

e_type, e_machine = struct.unpack_from('<HH', d, 16)
e_entry, e_phoff = struct.unpack_from('<II', d, 24)
e_phentsize, e_phnum = struct.unpack_from('<HH', d, 42)
print(f'e_type={e_type:#x} machine={e_machine} entry={e_entry:#x} phnum={e_phnum}')
segs = []
for i in range(e_phnum):
    p = struct.unpack_from('<8I', d, e_phoff + i * e_phentsize)
    segs.append(p)
    print(f'  seg{i}: type={p[0]:#x} off={p[1]:#x} vaddr={p[2]:#x} filesz={p[4]:#x} memsz={p[5]:#x} flags={p[6]:#x}')

def v2o(va):
    for p in segs:
        if p[0] == 1 and p[2] <= va < p[2] + p[4]:
            return p[1] + va - p[2]
    return None
def u32(va): return struct.unpack_from('<I', d, v2o(va))[0]
def cstr(va):
    o = v2o(va); return d[o:d.index(b'\0', o)].decode('ascii', 'replace')

seg_idx, mi_off = e_entry >> 30, e_entry & 0x3fffffff
s0 = segs[seg_idx]
mo = s0[1] + mi_off
attr, ver = struct.unpack_from('<HH', d, mo)
name = d[mo + 4:mo + 31].split(b'\0')[0].decode()
f = struct.unpack_from('<15I', d, mo + 32)
gp, exp_top, exp_end, imp_top, imp_end, mod_nid, tls_start, tls_fsz, tls_msz, mstart, mstop, exidx_top, exidx_end, extab_top, extab_end = f
base = s0[2]
print(f'module "{name}" attr={attr:#x} ver={ver:#x} nid={mod_nid:#x} module_start={base+mstart:#x} tls={tls_fsz:#x}/{tls_msz:#x}')
print(f'  exidx {base+exidx_top:#x}-{base+exidx_end:#x} extab {base+extab_top:#x}-{base+extab_end:#x}')

out = {'module': name, 'imports': {}, 'exports': {}}
# imports
o = s0[1] + imp_top; end = s0[1] + imp_end
total_f = total_v = 0
while o < end:
    size = struct.unpack_from('<H', d, o)[0]
    if size == 0x34:
        _, ver_, at, nf, nv, nt, _r, lnid, lname, _r2, fnt, fet, vnt, vet, tnt, tet = struct.unpack_from('<6H10I', d, o)
    elif size == 0x24:
        _, ver_, at, nf, nv, _u, lnid, lname, fnt, fet, vnt, vet = struct.unpack_from('<6H6I', d, o)
    else:
        print('unknown import size', size); break
    ln = cstr(lname) if lname else lib_names.get(lnid, f'{lnid:#x}')
    funcs = []
    for k in range(nf):
        nid = u32(fnt + 4 * k); funcs.append((nid, u32(fet + 4 * k), func_names.get(nid)))
    vars_ = [(u32(vnt + 4 * k), func_names.get(u32(vnt + 4 * k))) for k in range(nv)]
    out['imports'].setdefault(ln, {'nid': lnid, 'funcs': [], 'vars': []})
    out['imports'][ln]['funcs'] += [{'nid': f'{n:#010x}', 'stub': f'{s:#x}', 'name': nm} for n, s, nm in funcs]
    out['imports'][ln]['vars'] += [{'nid': f'{n:#010x}', 'name': nm} for n, nm in vars_]
    total_f += nf; total_v += nv
    o += size
unres = sum(1 for l in out['imports'].values() for x in l['funcs'] if not x['name'])
print(f'imports: {len(out["imports"])} libraries, {total_f} functions, {total_v} variables, {unres} functions without a db name')
for ln, l in sorted(out['imports'].items()):
    print(f'  {ln:28s} {len(l["funcs"]):4d} f {len(l["vars"]):2d} v  unresolved={sum(1 for x in l["funcs"] if not x["name"])}')
# exports
o = s0[1] + exp_top; end = s0[1] + exp_end
while o < end:
    size = d[o]
    _, _, ver_, at, nf, nv, nt, _u, lnid, lname, nidt, entt = struct.unpack_from('<BBHHHHHIIIII', d, o)
    ln = cstr(lname) if lname else f'(noname {lnid:#x})'
    ents = [(u32(nidt + 4 * k), u32(entt + 4 * k)) for k in range(nf + nv)]
    out['exports'][ln] = [{'nid': f'{n:#010x}', 'addr': f'{a:#x}', 'name': func_names.get(n)} for n, a in ents]
    print(f'  export {ln}: {nf} f {nv} v')
    o += size

# exidx
starts = []
for o in range(s0[1] + exidx_top, s0[1] + exidx_end, 8):
    w0, w1 = struct.unpack_from('<II', d, o)
    va = s0[2] + (o - s0[1])
    off = w0 & 0x7fffffff
    if off & 0x40000000: off -= 0x80000000
    starts.append((va + off) & 0xffffffff)
print(f'exidx entries: {len(starts)}; thumb-bit set: {sum(1 for s in starts if s & 1)}; distinct: {len(set(starts))}')
out['exidx'] = starts
if '--json' in sys.argv:
    json.dump(out, open(sys.argv[sys.argv.index('--json') + 1], 'w'), indent=1)
