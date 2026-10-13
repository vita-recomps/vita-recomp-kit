"""Front end: the inventory's per-function decode (tools/inventory.py) plus Capstone instruction
details, as the input of the emitter."""
import os, sys
from dataclasses import dataclass, field

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, arm

CC_NAME = {arm.ARM_CC_EQ: 'eq', arm.ARM_CC_NE: 'ne', arm.ARM_CC_HS: 'hs', arm.ARM_CC_LO: 'lo', arm.ARM_CC_MI: 'mi',
           arm.ARM_CC_PL: 'pl', arm.ARM_CC_VS: 'vs', arm.ARM_CC_VC: 'vc', arm.ARM_CC_HI: 'hi', arm.ARM_CC_LS: 'ls',
           arm.ARM_CC_GE: 'ge', arm.ARM_CC_LT: 'lt', arm.ARM_CC_GT: 'gt', arm.ARM_CC_LE: 'le'}
# Capstone spells hs/lo as cs/cc in mnemonics
CC_SPELLINGS = {'hs': ('hs', 'cs'), 'lo': ('lo', 'cc')}

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'tools')
sys.path.insert(0, TOOLS)
import inventory  # noqa: E402
import vitaelf  # noqa: E402


@dataclass
class Instruction:
    address: int
    size: int
    it_cond: str  # condition from an enclosing IT block (Thumb) or of the instruction (ARM), or None
    cs: object  # capstone CsInsn with detail
    full: str = ''  # the mnemonic without an ARM condition suffix (adds, vadd.f32 ...)

    def __post_init__(self):
        if not self.full:
            self.full = self.cs.mnemonic

    @property
    def mnemonic(self):
        return self.full.split('.')[0]


@dataclass
class Function:
    start: int
    end: int
    thumb: bool
    kind: str  # code / import_stub / data_end_marker
    row: dict  # the inventory row (counts and flags)
    jumptables: dict = field(default_factory=dict)  # tbb/tbh site -> [case targets]
    landing_pads: list = field(default_factory=list)
    decoder: object = None  # () -> instructions
    _instructions: list = None

    @property
    def instructions(self):
        """sorted by address; decoded on first use and kept until release()"""
        if self._instructions is None:
            self._instructions = self.decoder() if self.decoder else []
        return self._instructions

    def release(self):
        """drop the decoded instructions (Capstone details take about 1.7 KB per instruction: the
        whole eboot at once needs over 6 GB)"""
        self._instructions = None

    @property
    def uses_neon(self):
        return int(self.row.get('neon') or 0) > 0


class Module:
    """load_base: where the loader puts the code segment (relocatable modules). Code addresses
    (functions, instructions, branch targets, import stubs) are then the runtime ones; data
    references stay as the loader patches them (see `patched`)."""

    def __init__(self, elf_path, dbdir=None, load_base=None):
        db = vitaelf.load_nid_db(dbdir) if dbdir else None
        self.elf = vitaelf.load(elf_path, db)
        self.inv = inventory.Inventory(self.elf)
        self.inv.analyse()
        text = self.inv.text.vaddr
        self.delta = (load_base - text) if load_base is not None else 0
        delta = self.delta
        self.md = {}
        for thumb in (True, False):
            md = Cs(CS_ARCH_ARM, CS_MODE_THUMB if thumb else CS_MODE_ARM)
            md.detail = True
            self.md[thumb] = md
        self.imports = {i.stub + delta: i for i in self.elf.imports}
        # words the loader patches in the code segment (ELF addresses): variable imports, and for
        # relocatable modules every relocation (movw/movt pairs and Abs32 literals)
        seg0 = self.elf.segments[0]
        relocs = [(a, c) for a, c in vitaelf.relocation_sites(self.elf) if seg0.vaddr <= a < seg0.vaddr + seg0.filesz]
        unexpected = {c for _, c in relocs} - {2, 47, 48}
        if unexpected:
            raise ValueError(f'{elf_path}: code relocations of types {sorted(unexpected)} are not handled')
        var_sites = {a for a, _, _ in self.elf.var_import_sites}
        # movw/movt whose immediates generated code reads at run time
        self.patched = frozenset(a + delta for a in var_sites | {a for a, c in relocs if c in (47, 48)})
        # every patched word, for the code hash (runtime addresses)
        self.patched_words = frozenset(a + delta for a in var_sites | {a for a, _ in relocs})
        # register jumps (bx rN) whose target the inventory resolved to a function entry (libc's
        # memmove: movw/movt ip, memcpy; bx ip): site -> target, runtime addresses, Thumb bit kept
        self.jump_targets = {int(r['site'], 16) + delta: int(r['target'], 16) + delta
                             for r in self.inv.indirect
                             if r['kind'] == 'jump_reg' and r['resolved'] and r['target_kind'] == 'function'}
        tables = {}
        for t in self.inv.jumptables:
            tables[int(t['site'], 16) + delta] = [int(x, 16) + delta for x in t['targets'].split()]
        self.functions = []
        for row in self.inv.functions:
            start, end = int(row['start'], 16), int(row['end'], 16)
            thumb = row['mode'] == 'thumb'
            f = Function(start + delta, end + delta, thumb, row['kind'], row)
            f.decoder = (lambda start=start, thumb=thumb: self._decode(start, thumb))
            for addr in self.inv.func_insns.get(start, {}):
                if addr + delta in tables:
                    f.jumptables[addr + delta] = tables[addr + delta]
            f.landing_pads = sorted(p + delta for p in self.inv.landing_pads.get(start, ()))
            self.functions.append(f)

    def _decode(self, start, thumb):
        """the instructions of the function at ELF address `start`"""
        text, delta = self.inv.text.vaddr, self.delta
        out = []
        for addr, (size, it_cond) in sorted(self.inv.func_insns.get(start, {}).items()):
            o = addr - text
            cs = next(self.md[thumb].disasm(self.inv.code[o:o + size], addr + delta, 1))
            full = cs.mnemonic
            if not thumb and cs.cc in CC_NAME:
                # ARM: every instruction may be conditional; Capstone appends the condition
                cond = CC_NAME[cs.cc]
                head, _, tail = full.partition('.')
                for spelling in CC_SPELLINGS.get(cond, (cond,)):
                    if head.endswith(spelling):
                        head = head[:-2]
                        break
                full = head + ('.' + tail if tail else '')
                # b<cond> is a conditional branch in the semantics; everything else is wrapped
                it_cond = None if head == 'b' else cond
            out.append(Instruction(addr + delta, size, it_cond, cs, full))
        return out

    def mode_of(self, addr):
        """Thumb (True) / ARM (False) of a function entry at a runtime address, or None"""
        return self.inv.mode_of.get(addr - self.delta)

    def code_bytes(self, addr, size):
        """the module's bytes at a runtime address, as in the ELF"""
        o = addr - self.delta - self.inv.text.vaddr
        return self.inv.code[o:o + size]
