"""Read a decrypted PS Vita SCE ELF (eboot or module): segments, module_info, NID-named imports and
exports, and exidx entries. Library for inventory.py; velf.py prints the same facts."""
import glob, os, struct
from dataclasses import dataclass, field

import yaml


@dataclass
class Segment:
    type: int
    offset: int
    vaddr: int
    filesz: int
    memsz: int
    flags: int


@dataclass
class Import:
    stub: int
    library: str
    library_nid: int
    nid: int
    name: str


@dataclass
class ExidxEntry:
    start: int  # function start, bit 0 set for Thumb
    word1: int  # 1 = EXIDX_CANTUNWIND, bit 31 = inline unwind data, otherwise an extab pointer

    @property
    def cantunwind(self):
        return self.word1 == 1


def load_nid_db(dbdir):
    """vitasdk vita-headers db (db/360): NID -> name for libraries and functions/variables."""
    lib_names, func_names = {}, {}
    for path in glob.glob(os.path.join(dbdir, '**', '*.yml'), recursive=True):
        try:
            doc = yaml.safe_load(open(path, encoding='utf-8'))
        except Exception:
            continue
        for module in (doc.get('modules') or {}).values():
            for lname, lib in (module.get('libraries') or {}).items():
                if lib.get('nid') is not None:
                    lib_names[lib['nid']] = lname
                for fname, nid in (lib.get('functions') or {}).items():
                    func_names[nid] = fname
                for vname, nid in (lib.get('variables') or {}).items():
                    func_names[nid] = vname
    return lib_names, func_names


@dataclass
class VitaElf:
    path: str
    data: bytes
    e_type: int
    segments: list
    name: str = ''
    module_nid: int = 0
    module_start: int = 0
    exidx: list = field(default_factory=list)
    exidx_range: tuple = (0, 0)
    extab_range: tuple = (0, 0)
    module_info_va: int = 0
    imports: list = field(default_factory=list)
    # instructions/words the loader patches for variable imports: (address, ELF reloc code, nid)
    var_import_sites: list = field(default_factory=list)
    exports: list = field(default_factory=list)  # (library, nid, address, name)

    def v2o(self, va):
        for s in self.segments:
            if s.type == 1 and s.vaddr <= va < s.vaddr + s.filesz:
                return s.offset + va - s.vaddr
        return None

    def read(self, va, n):
        o = self.v2o(va)
        return None if o is None else self.data[o:o + n]

    def u32(self, va):
        return struct.unpack_from('<I', self.data, self.v2o(va))[0]

    def cstr(self, va):
        o = self.v2o(va)
        return self.data[o:self.data.index(b'\0', o)].decode('ascii', 'replace')

    @property
    def text(self):
        return self.segments[0]


def _uleb(elf, va):
    value = shift = 0
    while True:
        b = elf.read(va, 1)[0]
        va += 1
        value |= (b & 0x7f) << shift
        shift += 7
        if not b & 0x80:
            return value, va


def _prel31(elf, va):
    off = elf.u32(va) & 0x7fffffff
    if off & 0x40000000:
        off -= 0x80000000
    return (va + off) & 0xffffffff


def exception_scopes(elf):
    """ARM EHABI exception data per function: {function start (Thumb bit cleared): (model,
    [landing pad, ...])}. Functions without catch/cleanup scopes are omitted.

    * Compact model, personality 1/2 (__aeabi_unwind_cpp_pr1/2): after the unwind opcodes comes a
      list of scope descriptors ended by a zero word. Each is a (length, offset) pair, 16-bit for
      pr1 and 32-bit for pr2, whose low bits give the kind: cleanup (landing pad follows), catch
      (landing pad + type), function exception specification (count + types, optional pad).
    * Generic model (a prel31 personality routine, here __gxx_personality_v0): one word of unwind
      opcodes (plus N more), then a GCC LSDA whose call-site table gives landing pads relative to
      the function start.
    """
    scopes = {}
    for i, e in enumerate(elf.exidx):
        if e.cantunwind or e.word1 & 0x80000000:
            continue  # no unwind data or inline compact data: no scopes
        func = e.start & ~1
        ex = _prel31(elf, elf.exidx_range[0] + 8 * i + 4)
        w = elf.u32(ex)
        pads = []
        if w & 0x80000000:
            pr = (w >> 24) & 0xf
            if pr == 0:
                continue
            d = ex + 4 + 4 * ((w >> 16) & 0xff)
            for _ in range(10000):
                if pr == 1:
                    first = elf.u32(d)
                    if first == 0:
                        break
                    length, offset = first & 0xffff, first >> 16
                    d += 4
                else:
                    length, offset = elf.u32(d), elf.u32(d + 4)
                    if length == 0:
                        break
                    d += 8
                kind = (length & 1) << 1 | (offset & 1)
                if kind == 0:  # cleanup
                    pads.append(_prel31(elf, d) & ~1); d += 4
                elif kind == 2:  # catch
                    pads.append(_prel31(elf, d) & ~1); d += 8
                else:  # function exception specification
                    n = elf.u32(d) & 0x7fffffff
                    has_pad = bool(elf.u32(d) & 0x80000000)
                    d += 4 + 4 * n
                    if has_pad:
                        pads.append(_prel31(elf, d) & ~1); d += 4
            model = f'compact_pr{pr}'
        else:
            lsda = ex + 8 + 4 * ((elf.u32(ex + 4) >> 24) & 0xff)
            lp_enc = elf.read(lsda, 1)[0]
            p = lsda + 1
            if lp_enc != 0xff:
                raise ValueError(f'{elf.path}: LSDA at {lsda:#x} has an LPStart (encoding {lp_enc:#x})')
            tt_enc = elf.read(p, 1)[0]
            p += 1
            if tt_enc != 0xff:
                _, p = _uleb(elf, p)
            cs_enc = elf.read(p, 1)[0]
            cs_len, p = _uleb(elf, p + 1)
            if cs_enc != 0x01:
                raise ValueError(f'{elf.path}: LSDA at {lsda:#x} uses call-site encoding {cs_enc:#x}')
            end = p + cs_len
            while p < end:
                _, p = _uleb(elf, p)    # call-site start
                _, p = _uleb(elf, p)    # length
                lp, p = _uleb(elf, p)   # landing pad, relative to the function start
                _, p = _uleb(elf, p)    # action
                if lp:
                    pads.append((func + lp) & ~1)
            model = 'gcc_lsda'
        if pads:
            scopes[func] = (model, sorted(set(pads)))
    return scopes


def var_import_relocs(elf, table, nid):
    """Sites a variable import's reference table patches (Vita3K kernel/src/relocation.cpp, formats
    1 and 2 in their variable-import layout): [(address, reloc code, nid)]. The table starts with a
    header word whose bits 4-27 hold its size in bytes, header included."""
    size = (elf.u32(table) >> 4) & 0xffffff
    out = []
    va, end = table + 4, table + size
    while va < end:
        w = elf.u32(va)
        fmt, seg, code = w & 0xf, (w >> 4) & 0xf, (w >> 8) & 0xff
        if fmt not in (1, 2):
            raise ValueError(f'variable import {nid:#010x}: relocation format {fmt} at {va:#x}')
        out.append((elf.segments[seg].vaddr + elf.u32(va + 4), code, nid))
        va += 8 if fmt == 1 else 12
    return out


def load(path, nid_db=None):
    d = open(path, 'rb').read()
    e_type = struct.unpack_from('<H', d, 16)[0]
    e_entry, e_phoff = struct.unpack_from('<II', d, 24)
    e_phentsize, e_phnum = struct.unpack_from('<HH', d, 42)
    # struct order in the program header: type, offset, vaddr, paddr, filesz, memsz, flags, align
    segs = []
    for i in range(e_phnum):
        p = struct.unpack_from('<8I', d, e_phoff + i * e_phentsize)
        segs.append(Segment(p[0], p[1], p[2], p[4], p[5], p[6]))
    elf = VitaElf(path, d, e_type, segs)
    lib_names, func_names = nid_db or ({}, {})

    # module_info: e_entry holds the segment index (top 2 bits) and the offset within it
    s0 = segs[e_entry >> 30]
    mo = s0.offset + (e_entry & 0x3fffffff)
    elf.module_info_va = s0.vaddr + (e_entry & 0x3fffffff)
    elf.name = d[mo + 4:mo + 31].split(b'\0')[0].decode()
    (gp, exp_top, exp_end, imp_top, imp_end, mod_nid, tls_start, tls_fsz, tls_msz, mstart, mstop,
     exidx_top, exidx_end, extab_top, extab_end) = struct.unpack_from('<15I', d, mo + 32)
    elf.module_nid = mod_nid
    elf.module_start = s0.vaddr + mstart
    elf.exidx_range = (s0.vaddr + exidx_top, s0.vaddr + exidx_end)
    elf.extab_range = (s0.vaddr + extab_top, s0.vaddr + extab_end)

    o, end = s0.offset + imp_top, s0.offset + imp_end
    while o < end:
        size = struct.unpack_from('<H', d, o)[0]
        if size == 0x34:
            _, _, _, nf, nv, _, _, lnid, lname, _, fnt, fet, vnt, vet, _, _ = struct.unpack_from('<6H10I', d, o)
        elif size == 0x24:
            _, _, _, nf, nv, _, lnid, lname, fnt, fet, vnt, vet = struct.unpack_from('<6H6I', d, o)
        else:
            raise ValueError(f'{path}: unknown import entry size {size:#x}')
        lib = elf.cstr(lname) if lname else lib_names.get(lnid, f'{lnid:#010x}')
        for k in range(nf):
            nid = elf.u32(fnt + 4 * k)
            elf.imports.append(Import(elf.u32(fet + 4 * k), lib, lnid, nid, func_names.get(nid, '')))
        for k in range(nv):
            elf.var_import_sites += var_import_relocs(elf, elf.u32(vet + 4 * k), elf.u32(vnt + 4 * k))
        o += size

    o, end = s0.offset + exp_top, s0.offset + exp_end
    while o < end:
        size = d[o]
        _, _, _, _, nf, nv, _, _, lnid, lname, nidt, entt = struct.unpack_from('<BBHHHHHIIIII', d, o)
        lib = elf.cstr(lname) if lname else f'(noname {lnid:#x})'
        for k in range(nf + nv):
            nid = elf.u32(nidt + 4 * k)
            elf.exports.append((lib, nid, elf.u32(entt + 4 * k), func_names.get(nid, '')))
        o += size

    for va in range(*elf.exidx_range, 8):
        w0, w1 = elf.u32(va), elf.u32(va + 4)
        off = w0 & 0x7fffffff
        if off & 0x40000000:
            off -= 0x80000000
        elf.exidx.append(ExidxEntry((va + off) & 0xffffffff, w1))
    return elf
