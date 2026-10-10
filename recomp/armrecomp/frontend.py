"""Front end: the inventory's per-function decode (tools/inventory.py) plus Capstone instruction
details, as the input of the emitter."""
import os, sys
from dataclasses import dataclass, field

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'tools')
sys.path.insert(0, TOOLS)
import inventory  # noqa: E402
import vitaelf  # noqa: E402


@dataclass
class Instruction:
    address: int
    size: int
    it_cond: str  # condition from an enclosing IT block, or None
    cs: object  # capstone CsInsn with detail

    @property
    def mnemonic(self):
        return self.cs.mnemonic.split('.')[0]


@dataclass
class Function:
    start: int
    end: int
    thumb: bool
    kind: str  # code / import_stub / data_end_marker
    row: dict  # the inventory row (counts and flags)
    instructions: list = field(default_factory=list)  # sorted by address
    jumptables: dict = field(default_factory=dict)  # tbb/tbh site -> [case targets]
    landing_pads: list = field(default_factory=list)

    @property
    def uses_neon(self):
        return int(self.row.get('neon') or 0) > 0


class Module:
    def __init__(self, elf_path, dbdir=None):
        db = vitaelf.load_nid_db(dbdir) if dbdir else None
        self.elf = vitaelf.load(elf_path, db)
        self.inv = inventory.Inventory(self.elf)
        self.inv.analyse()
        self.md = {}
        for thumb in (True, False):
            md = Cs(CS_ARCH_ARM, CS_MODE_THUMB if thumb else CS_MODE_ARM)
            md.detail = True
            self.md[thumb] = md
        self.imports = {i.stub: i for i in self.elf.imports}
        # instructions the loader patches for variable imports
        self.patched = frozenset(a for a, _, _ in self.elf.var_import_sites)
        tables = {}
        for t in self.inv.jumptables:
            tables[int(t['site'], 16)] = [int(x, 16) for x in t['targets'].split()]
        self.functions = []
        for row in self.inv.functions:
            start, end = int(row['start'], 16), int(row['end'], 16)
            thumb = row['mode'] == 'thumb'
            f = Function(start, end, thumb, row['kind'], row)
            for addr, (size, it_cond) in sorted(self.inv.func_insns.get(start, {}).items()):
                o = addr - self.inv.text.vaddr
                cs = next(self.md[thumb].disasm(self.inv.code[o:o + size], addr, 1))
                f.instructions.append(Instruction(addr, size, it_cond, cs))
                if addr in tables:
                    f.jumptables[addr] = tables[addr]
            f.landing_pads = sorted(self.inv.landing_pads.get(start, ()))
            self.functions.append(f)
