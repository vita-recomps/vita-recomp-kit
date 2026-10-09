"""Static inventory of a PS Vita ELF for the recompiler (plan Phase 2).

Every exidx entry is a function start. Each function is decoded by recursive descent from its
start within [start, next start): branches and IT blocks are followed, tbb/tbh tables are
decoded, and literal pools are skipped. Constants loaded by movw/movt, ldr-literal and adr are
propagated within a path so that indirect branches through them are resolved.

usage: python -I inventory.py <elf> <vita-headers db dir> <out dir>

Writes to <out dir>:
  functions.csv   one row per exidx entry: mode, range, kind, instruction/byte counts, calls,
                  returns, tail calls, jump tables, indirect branches, NEON/VFP counts, flags
  jumptables.csv  every tbb/tbh/ARM computed-pc table with its case targets
  indirect.csv    every indirect branch (blx/bx/mov/ldr/add pc through a register or memory),
                  with the constant target when it was resolved
  calls.csv       direct calls/branches that leave the function: target kind (function start,
                  import stub, mid-function), mode switches
  addrtaken.csv   function starts whose address appears in data or code constants
  entrypoints.csv entries into the middle of functions: C++ exception landing pads (parsed from the
                  EHABI tables), orphan code no path reaches, and data words that look like
                  Thumb pointers into code (usually coincidence)
  imports.csv     import stub -> library / NID / name
  importcalls.csv every direct call (bl/blx) to an import stub, with its caller
  unreached.csv   bytes inside functions that no path reaches, classified (dead epilogue, trailing
                  filler, self loop, other)
  summary.txt     totals
"""
import csv, collections, os, struct, sys

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vitaelf  # noqa: E402

CONDS = {'eq', 'ne', 'cs', 'hs', 'cc', 'lo', 'mi', 'pl', 'vs', 'vc', 'hi', 'ls', 'ge', 'lt', 'gt', 'le', 'al'}
CORE_REGS = {f'r{i}' for i in range(13)} | {'sb', 'sl', 'fp', 'ip', 'sp', 'lr', 'pc'}
REG_ALIAS = {'sb': 'r9', 'sl': 'r10', 'fp': 'r11', 'ip': 'r12'}
# mnemonics whose first operand is not a destination register
NO_DEST = ('str', 'cmp', 'cmn', 'tst', 'teq', 'b', 'push', 'pop', 'stm', 'vst', 'vpush', 'vpop', 'it',
           'nop', 'dmb', 'dsb', 'isb', 'pld', 'pli', 'svc', 'bkpt', 'udf', 'cbz', 'cbnz', 'tbb', 'tbh',
           'vcmp', 'vmsr', 'mcr', 'clrex', 'sev', 'wfe', 'wfi', 'yield')
NEON_PREFIXES = ('vld1', 'vld2', 'vld3', 'vld4', 'vst1', 'vst2', 'vst3', 'vst4', 'vdup', 'vzip', 'vuzp',
                 'vtrn', 'vext', 'vbsl', 'vbif', 'vbit', 'vrev', 'vtbl', 'vqadd', 'vpadd', 'vrecpe',
                 'vrsqrte', 'vrecps', 'vrsqrts')


def split_cond(m, bases):
    """'bxeq' -> ('bx', 'eq'); returns (None, None) when m is not one of bases (+ condition)."""
    if m in bases:
        return m, None
    for base in sorted(bases, key=len, reverse=True):
        if m.startswith(base) and m[len(base):] in CONDS:
            return base, m[len(base):]
    return None, None


def imm(op):
    op = op.strip().lstrip('#')
    return int(op, 0)


def reglist(op_str):
    return [r.strip() for r in op_str[op_str.index('{') + 1:op_str.index('}')].replace('-', ',').split(',')]


class Inventory:
    def __init__(self, elf):
        self.elf = elf
        self.text = elf.text
        self.code = elf.data[self.text.offset:self.text.offset + self.text.filesz]
        self.md = {True: Cs(CS_ARCH_ARM, CS_MODE_THUMB), False: Cs(CS_ARCH_ARM, CS_MODE_ARM)}
        self.stubs = {i.stub: i for i in elf.imports}
        entries = sorted(elf.exidx, key=lambda e: e.start & ~1)
        self.entries = entries
        self.starts = [e.start & ~1 for e in entries]
        self.start_set = set(self.starts)
        self.mode_of = {e.start & ~1: bool(e.start & 1) for e in entries}
        self.functions, self.jumptables, self.indirect, self.calls = [], [], [], []
        self.const_refs = collections.defaultdict(set)  # target -> sources (code constants)
        # C++ exception landing pads from the EHABI tables. They are only reached by unwinding, so
        # they are extra decode roots of their function.
        self.eh_scopes = vitaelf.exception_scopes(elf)
        self.landing_pads = {f: set(p) for f, (_, p) in self.eh_scopes.items()}
        self.code_bytes = bytearray(len(self.code))  # 1 = instruction, 2 = table, 3 = literal, 4 = dead epilogue
        self.dead_epilogues = 0
        self.unreached_runs = []
        self.orphans = []
        self.import_calls = []

    def insn(self, addr, thumb):
        o = addr - self.text.vaddr
        if o < 0 or o + 2 > len(self.code):
            return None
        for i in self.md[thumb].disasm_lite(self.code[o:o + 4], addr, 1):
            return i
        return None

    def word(self, va):
        o = va - self.text.vaddr
        if 0 <= o <= len(self.code) - 4:
            return struct.unpack_from('<I', self.code, o)[0]
        b = self.elf.read(va, 4)
        return struct.unpack('<I', b)[0] if b and len(b) == 4 else None

    def owner(self, addr):
        """index of the function whose range contains addr"""
        import bisect
        i = bisect.bisect_right(self.starts, addr) - 1
        return i if i >= 0 else None

    def classify_target(self, target):
        t = target & ~1
        if t in self.stubs:
            return 'import'
        if t in self.start_set:
            return 'function'
        i = self.owner(t)
        return 'mid_function' if i is not None and self.text.vaddr <= t < self.text.vaddr + self.text.filesz else 'outside'

    def analyse(self):
        """Decode every function. The exidx Thumb bit is wrong for some hand-written routines
        (libc's memcpy is Thumb code with an ARM-mode entry), so when every direct call into a
        function implies the other mode, the function is decoded again in that mode."""
        self.mode_overrides = {}
        self.implied_modes = collections.defaultdict(collections.Counter)
        self.analyse_pass()
        for target, modes in self.implied_modes.items():
            if len(modes) == 1:
                (implied,) = modes
                if implied != self.mode_of.get(target):
                    self.mode_overrides[target] = implied
        if self.mode_overrides:
            self.mode_of.update(self.mode_overrides)
            self.functions, self.jumptables, self.indirect, self.calls = [], [], [], []
            self.const_refs.clear()
            self.code_bytes = bytearray(len(self.code))
            self.dead_epilogues = 0
            self.unreached_runs = []
            self.orphans = []
            self.import_calls = []
            self.analyse_pass()

    def analyse_pass(self):
        end_of_code = self.elf.module_info_va
        for idx, e in enumerate(self.entries):
            start = e.start & ~1
            thumb = self.mode_of[start]
            end = self.starts[idx + 1] if idx + 1 < len(self.starts) else end_of_code
            row = dict(start=f'{start:#x}', mode='thumb' if thumb else 'arm', end=f'{end:#x}',
                       size=end - start, exidx='cantunwind' if e.cantunwind else ('inline' if e.word1 & 0x80000000 else 'extab'))
            if start in self.mode_overrides:
                row['name'] = 'exidx mode bit overridden by its callers'
            if start in self.stubs:
                imp = self.stubs[start]
                row.update(kind='import_stub', name=f'{imp.library}:{imp.name or hex(imp.nid)}')
            elif start == self.elf.module_info_va or start >= end_of_code:
                row.update(kind='data_end_marker')
            else:
                row.update(self.decode_function(start, end, thumb))
            self.functions.append(row)

    def decode_function(self, start, end, thumb):
        md_word = 4 if not thumb else 2
        c = collections.Counter()
        flags = set()
        seen = set()
        pushed_lr = False
        # addr, IT instructions left, known register constants, last cmp. The start is popped first
        # (LIFO), so the prologue is seen before any landing pad.
        work = [(pad, 0, {}, None) for pad in sorted(self.landing_pads.get(start, ()))]
        work.append((start, 0, {}, None))
        tried_orphans = set()
        if self.landing_pads.get(start):
            flags.add('landing_pads')
        while work:
            addr, it_left, regs, last_cmp = work.pop()
            after_call = False  # only nops since the last direct call on this path
            while True:
                if addr in seen:
                    break
                if not (start <= addr < end):
                    # a call followed by padding up to the next function never returns
                    # (__stack_chk_fail, abort, exit, the eboot's own no-return helpers)
                    if after_call:
                        c['noreturn_calls'] += 1
                    else:
                        flags.add('falls_out_of_range')
                    break
                ins = self.insn(addr, thumb)
                if ins is None:
                    flags.add('undecodable')
                    break
                seen.add(addr)
                a, size, mn, ops = ins
                for k in range(size):
                    if 0 <= a - self.text.vaddr + k < len(self.code_bytes):
                        self.code_bytes[a - self.text.vaddr + k] = 1
                c['insns'] += 1
                c['bytes_code'] += size
                m = mn.split('.')[0]
                if m != 'nop':
                    after_call = False
                pc = a + (4 if thumb else 8)
                conditional = it_left > 0
                if it_left:
                    it_left -= 1
                nxt = a + size

                if m.startswith('v'):
                    c['vfp_neon'] += 1
                    if m.startswith(NEON_PREFIXES) or any(f'q{k}' in ops.split() or f'q{k},' in ops for k in range(16)):
                        c['neon'] += 1
                if m in ('push', 'stmdb', 'stmfd') and 'lr' in ops:
                    pushed_lr = True
                if m.startswith('it') and len(m) <= 4 and set(m[1:]) <= {'t', 'e'}:
                    it_left = len(m) - 1
                    addr = nxt
                    continue

                # --- literal pools: mark the bytes of every pc-relative load
                if '[pc' in ops and m.startswith(('ldr', 'vldr')):
                    off = ops.split('[pc')[1]
                    off = int(off.split('#')[1].rstrip(']! '), 0) if '#' in off else 0
                    lit = ((pc & ~3) if thumb else pc) + off
                    width = 8 if (m.startswith('ldrd') or (m.startswith('vldr') and ops.startswith('d'))) else 4
                    o = lit - self.text.vaddr
                    for k in range(width):
                        if 0 <= o + k < len(self.code_bytes) and self.code_bytes[o + k] == 0:
                            self.code_bytes[o + k] = 3

                # --- constants (per path)
                dest = ops.split(',')[0].strip() if ops else ''
                if m == 'movw' and dest in CORE_REGS:
                    regs = dict(regs); regs[dest] = imm(ops.split(',')[1])
                elif m == 'movt' and dest in CORE_REGS:
                    regs = dict(regs)
                    regs[dest] = (regs.get(dest, 0) & 0xffff) | (imm(ops.split(',')[1]) << 16) if dest in regs else None
                    if regs[dest] is None:
                        del regs[dest]
                    else:
                        self.const_refs[regs[dest]].add(a)
                elif m == 'ldr' and '[pc' in ops and dest in CORE_REGS and dest != 'pc':
                    lit = (pc & ~3) + (imm(ops.split('#')[1].rstrip(']')) if '#' in ops else 0) if thumb else pc + (int(ops.split('#')[1].rstrip(']'), 0) if '#' in ops else 0)
                    val = self.word(lit)
                    if 0 <= lit - self.text.vaddr <= len(self.code_bytes) - 4:
                        for k in range(4):
                            if self.code_bytes[lit - self.text.vaddr + k] == 0:
                                self.code_bytes[lit - self.text.vaddr + k] = 3
                    regs = dict(regs)
                    if val is not None:
                        regs[dest] = val
                        self.const_refs[val].add(a)
                    else:
                        regs.pop(dest, None)
                elif m == 'adr' and dest in CORE_REGS:
                    regs = dict(regs); regs[dest] = ((pc & ~3) if thumb else pc) + imm(ops.split(',')[1])
                elif m in ('pop', 'ldm', 'ldmia', 'ldmfd') or m.startswith(('ldm', 'pop')):
                    regs = {}
                elif dest in CORE_REGS and not m.startswith(NO_DEST) and dest in regs:
                    regs = dict(regs); regs.pop(dest, None)

                # --- control flow
                b, cond = split_cond(m, {'b', 'bl', 'blx', 'bx'})
                if cond is not None and cond != 'al':
                    conditional = True
                if m == 'cmp' and ops.count(',') == 1:
                    rhs = ops.split(',')[1].strip()
                    val = imm(rhs) if rhs.startswith('#') else regs.get(rhs)
                    last_cmp = (dest, val) if val is not None else None
                elif last_cmp and dest == last_cmp[0] and not m.startswith(NO_DEST):
                    last_cmp = None  # the bounded register was overwritten
                if last_cmp and not m.startswith(NO_DEST) and dest in CORE_REGS and ops.count(',') == 2:
                    _, src, rhs = [x.strip() for x in ops.split(',')]
                    if src == last_cmp[0] and rhs.startswith('#'):
                        if m in ('sub', 'subs', 'subw'):
                            last_cmp = (dest, last_cmp[1] - imm(rhs))  # index rebased to 0
                        elif m in ('adds', 'add', 'addw') and imm(rhs) == 0:
                            last_cmp = (dest, last_cmp[1])  # copy
                elif last_cmp and m in ('mov', 'movs') and ops.split(',')[1].strip() == last_cmp[0]:
                    last_cmp = (dest, last_cmp[1])

                if m in ('cbz', 'cbnz'):
                    target = imm(ops.split(',')[1])
                    work.append((target, 0, regs, last_cmp))
                    c['branches'] += 1
                    addr = nxt
                    continue
                if b == 'b':
                    target = imm(ops)
                    c['branches'] += 1
                    if start <= target < end:
                        if conditional:
                            work.append((target, 0, regs, last_cmp))
                            addr = nxt
                            continue
                        addr = target
                        it_left = 0
                        continue
                    kind = self.classify_target(target | (1 if thumb else 0))
                    c['tail_calls'] += 1
                    if kind != 'function' and kind != 'import':
                        flags.add('branch_to_' + kind)
                    self.calls.append(dict(site=f'{a:#x}', func=f'{start:#x}', insn=f'{mn} {ops}',
                                           kind='conditional_tail_call' if conditional else 'tail_call',
                                           target=f'{target:#x}', target_kind=kind, mode_switch=False))
                    if conditional:
                        addr = nxt
                        continue
                    break
                if b in ('bl', 'blx') and ops.startswith('#'):
                    target = imm(ops)
                    switch = b == 'blx'
                    target_thumb = (not thumb) if switch else thumb
                    kind = self.classify_target(target)
                    if kind == 'function':
                        self.implied_modes[target & ~1][target_thumb] += 1
                        if self.mode_of.get(target & ~1) != target_thumb:
                            flags.add('call_mode_mismatch')
                    c['calls'] += 1
                    if switch:
                        c['mode_switch_calls'] += 1
                    if kind not in ('function', 'import') or (switch and kind == 'function'):
                        self.calls.append(dict(site=f'{a:#x}', func=f'{start:#x}', insn=f'{mn} {ops}', kind='call',
                                               target=f'{target:#x}', target_kind=kind, mode_switch=switch))
                    if kind == 'import':
                        c['import_calls'] += 1
                        imp = self.stubs[target & ~1]
                        self.import_calls.append(dict(site=f'{a:#x}', func=f'{start:#x}', stub=f'{target & ~1:#x}',
                                                      library=imp.library, name=imp.name or f'{imp.nid:#010x}'))
                    regs = {k: v for k, v in regs.items() if k not in ('r0', 'r1', 'r2', 'r3', 'r12', 'ip', 'lr')}
                    after_call = True
                    addr = nxt
                    continue
                if b in ('bx', 'blx'):
                    reg = ops.strip()
                    if b == 'bx' and reg == 'lr':
                        c['returns'] += 1
                        if conditional:
                            addr = nxt
                            continue
                        break
                    target = regs.get(reg)
                    self.record_indirect(a, start, mn, ops, 'call_reg' if b == 'blx' else 'jump_reg', reg, target)
                    if b == 'blx':
                        c['indirect_calls'] += 1
                        regs = {k: v for k, v in regs.items() if k not in ('r0', 'r1', 'r2', 'r3', 'r12', 'ip', 'lr')}
                        addr = nxt
                        continue
                    c['indirect_jumps'] += 1
                    if conditional:
                        addr = nxt
                        continue
                    break
                if m in ('tbb', 'tbh'):
                    self.decode_tbb(a, start, end, m, ops, last_cmp, regs, work)
                    c['jumptables'] += 1
                    break
                if not thumb and split_cond(m, {'pop', 'ldm', 'ldmia', 'ldmfd', 'ldr', 'mov', 'add', 'sub'})[1] not in (None, 'al'):
                    conditional = True
                writes_pc = (m.startswith(('pop', 'ldm')) and 'pc' in ops) or \
                            (dest == 'pc' and not m.startswith(NO_DEST))
                if writes_pc:
                    if m.startswith('pop') or (m.startswith('ldm') and ops.startswith('sp')) or \
                            (m == 'ldr' and ops.startswith('pc, [sp]')) or (m == 'mov' and ops == 'pc, lr'):
                        c['returns'] += 1
                        if not pushed_lr:
                            flags.add('pc_pop_without_lr_push')
                    elif not thumb and m.startswith(('add', 'ldr')) and 'pc, pc' in ops.replace('[', '') and last_cmp:
                        self.decode_arm_table(a, start, end, m, ops, last_cmp, work)
                        c['jumptables'] += 1
                        addr = nxt  # the default case follows the table instruction
                        continue
                    else:
                        src = ops.split(',', 1)[1].strip() if ',' in ops else ''
                        self.record_indirect(a, start, mn, ops, 'jump_' + m, src, regs.get(src))
                        c['indirect_jumps'] += 1
                    if conditional:
                        addr = nxt
                        continue
                    break
                if m == 'udf':
                    c['traps'] += 1
                    if not conditional:
                        break
                if m == 'bkpt':
                    c['traps'] += 1  # assertion breakpoint: execution continues after it
                addr = nxt
            if not work:
                # Code no path reaches but that decodes as code: landing pads the exception tables
                # describe in a format not parsed here (libc), or other entries. Decode it too and
                # list it: the recompiler needs a runtime fallback for these addresses.
                orphan = self.find_orphan(start, end, thumb, tried_orphans)
                if orphan is not None:
                    tried_orphans.add(orphan)
                    self.orphans.append(dict(target=f'{orphan:#x}', func=f'{start:#x}', kind='orphan_code',
                                             target_state='instruction', refs=0, sources=''))
                    c['orphan_roots'] += 1
                    flags.add('orphan_code')
                    work.append((orphan, 0, {}, None))

        unreached = self.unreached(start, end)
        if unreached:
            flags.add('unreached_bytes')
        return dict(kind=self.function_kind(start, end, thumb), insns=c['insns'], bytes_code=c['bytes_code'],
                    unreached=unreached, calls=c['calls'], import_calls=c['import_calls'],
                    mode_switch_calls=c['mode_switch_calls'], indirect_calls=c['indirect_calls'],
                    indirect_jumps=c['indirect_jumps'], returns=c['returns'], tail_calls=c['tail_calls'],
                    jumptables=c['jumptables'], vfp_neon=c['vfp_neon'], neon=c['neon'], traps=c['traps'],
                    noreturn_calls=c['noreturn_calls'], orphan_roots=c['orphan_roots'],
                    flags=' '.join(sorted(flags)))

    def is_filler(self, o):
        """Stray halfwords 0x8xx0 (they decode as ldrh/strh r0, [rN, #imm]) that the compiler
        leaves after returns and between functions"""
        hw = self.code[o] | self.code[o + 1] << 8
        return hw & 0xf007 == 0x8000

    def find_orphan(self, start, end, thumb, tried):
        if not thumb:
            return None  # ARM routines keep constant tables after their code
        padding = (b'\0\0', b'\x00\xbf', b'\xc0\x46')
        for o in range(start - self.text.vaddr, end - self.text.vaddr, 2):
            a = self.text.vaddr + o
            if self.code_bytes[o] != 0 or a in tried or self.code[o:o + 2] in padding:
                continue
            if self.is_filler(o):
                continue
            ins = self.insn(a, True)
            if ins is None:
                continue
            if (ins[2] == 'bx' and ins[3] == 'lr') or (ins[2].startswith('pop') and 'pc' in ins[3]) or \
                    (ins[2] == 'b' and ins[3].startswith('#') and int(ins[3][1:], 0) == a):
                continue  # dead returns and self loops are classified by unreached()
            if self.plausible_block(a, end):
                return a
            tried.add(a)
        return None

    def plausible_block(self, a, end):
        """a linear Thumb decode from a reaches a return or branch before the function ends"""
        for _ in range(256):
            if a >= end:
                return False
            ins = self.insn(a, True)
            if ins is None:
                return False
            m = ins[2].split('.')[0]
            if (m == 'bx') or (m.startswith('pop') and 'pc' in ins[3]) or m in ('b', 'tbb', 'tbh'):
                return True
            a += ins[1]
        return False

    def function_kind(self, start, end, thumb):
        if not thumb and end - start == 16:
            ins = list(self.md[False].disasm_lite(self.code[start - self.text.vaddr:start - self.text.vaddr + 8], start))
            if [(i[2], i[3]) for i in ins] == [('mvn', 'r0, #0'), ('bx', 'lr')]:
                return 'return_minus_one'
        return 'code'

    def unreached(self, start, end):
        """bytes in the range that are neither decoded code, tables nor literals, ignoring padding
        and dead epilogues (a second return the compiler leaves right after the real one)"""
        o0, o1 = start - self.text.vaddr, end - self.text.vaddr
        thumb = self.mode_of.get(start, True)
        padding = (b'\0\0', b'\x00\xbf', b'\xc0\x46')  # zero / Thumb nop / mov r8, r8
        n = 0
        o = o0
        while o < o1:
            if self.code_bytes[o] != 0 or self.code[o:o + 2] in padding:
                o += 2
                continue
            # previous non-padding halfword
            p = o - 2
            while p >= o0 and self.code_bytes[p] == 0 and self.code[p:p + 2] in padding:
                p -= 2
            after_code = p >= o0 and self.code_bytes[p] in (1, 4)
            ins = self.insn(self.text.vaddr + o, thumb)
            category = 'other'
            size = 2
            is_return = ins and ((ins[2] == 'bx' and ins[3] == 'lr') or (ins[2].startswith('pop') and 'pc' in ins[3]))
            if not thumb:
                # hand-written ARM/NEON routines keep constant tables after their code
                size = 4
                category = 'padding' if self.code[o:o + 4] in (b'\x00\xf0\x20\xe3', b'\x00\x00\xa0\xe1') else 'arm_data'
            elif is_return:
                # a return no path reaches: the compiler leaves extra epilogues after the real one
                category, size = ('dead_epilogue' if after_code else 'dead_return'), ins[1]
                self.dead_epilogues += 1
            elif ins and ins[2] == 'b' and int(ins[3].lstrip('#'), 0) == ins[0]:
                category, size = 'self_loop', ins[1]
            elif self.is_filler(o):
                category = 'filler'
            elif after_code and all(self.code_bytes[k] == 0 and self.code[k:k + 2] in padding
                                    for k in range(o + 2, o1, 2)):
                category = 'trailing_filler'  # one stray halfword before the next function
            for k in range(size):
                self.code_bytes[o + k] = 4 if category != 'other' else 5
            self.unreached_runs.append(dict(addr=f'{self.text.vaddr + o:#x}', func=f'{start:#x}', size=size,
                                            category=category, bytes=self.code[o:o + size].hex()))
            if category == 'other':
                n += size
            o += size
        return n

    def record_indirect(self, site, func, mn, ops, kind, reg, target):
        self.indirect.append(dict(site=f'{site:#x}', func=f'{func:#x}', insn=f'{mn} {ops}', kind=kind, reg=reg,
                                  target=f'{target:#x}' if target is not None else '',
                                  target_kind=self.classify_target(target) if target is not None else '',
                                  resolved=target is not None))

    def decode_tbb(self, a, start, end, m, ops, last_cmp, regs, work):
        # tbb [pc, rN] / tbh [pc, rN, lsl #1]: the table follows the instruction (pc = a + 4)
        parts = [p.strip(' []') for p in ops.split(',')]
        base, index = parts[0], parts[1]
        esize = 1 if m == 'tbb' else 2
        table = a + 4 if base == 'pc' else regs.get(base)
        # The table ends where its first case begins: read entries up to the lowest target seen.
        # The bounds check (cmp + conditional branch to the default case) is only a cross-check.
        cmp_count = last_cmp[1] + 1 if last_cmp and last_cmp[0] == index else None
        targets = []
        source = 'unresolved_base'
        if table is not None:
            first_target = None
            for k in range(4096):
                pos = table + k * esize
                if first_target is not None and pos >= first_target:
                    break
                o = pos - self.text.vaddr
                if not (0 <= o < len(self.code) - esize + 1):
                    break
                v = self.code[o] if esize == 1 else struct.unpack_from('<H', self.code, o)[0]
                t = a + 4 + 2 * v
                if not (start <= t < end):
                    break
                targets.append(t)
                first_target = t if first_target is None else min(first_target, t)
            if esize == 1 and cmp_count == len(targets) - 1 and cmp_count % 2 == 1:
                targets.pop()  # tbb tables with an odd count are padded to a halfword
            if cmp_count is None:
                source = 'table_end'
            elif cmp_count == len(targets):
                source = 'table_end=cmp'
            elif cmp_count < len(targets):
                source = 'table_end>stale_cmp'  # the tracked cmp belonged to an earlier test
            else:
                source = f'table_end<cmp({cmp_count})'
            for k in range(len(targets) * esize):
                o = table - self.text.vaddr + k
                if 0 <= o < len(self.code_bytes):
                    self.code_bytes[o] = 2
            for t in set(targets):
                if start <= t < end:
                    work.append((t, 0, {}, None))
        out_of_range = sum(1 for t in targets if not start <= t < end)
        self.jumptables.append(dict(site=f'{a:#x}', func=f'{start:#x}', kind=m, base=base, index=index,
                                    count=len(targets), count_source=source,
                                    targets_out_of_function=out_of_range,
                                    targets=' '.join(f'{t:#x}' for t in targets)))

    def decode_arm_table(self, a, start, end, m, ops, last_cmp, work):
        # addls pc, pc, rN, lsl #2 (a branch per case) / ldrls pc, [pc, rN, lsl #2] (a word per case)
        count = last_cmp[1] + 1
        table = a + 8
        targets = []
        for k in range(count):
            if m.startswith('add'):
                targets.append(table + 4 * k)
            else:
                w = self.word(table + 4 * k)
                if w is not None:
                    targets.append(w & ~1)
        for t in set(targets):
            if start <= t < end:
                work.append((t, 0, {}, None))
        self.jumptables.append(dict(site=f'{a:#x}', func=f'{start:#x}', kind='arm_' + m, base='pc',
                                    index=last_cmp[0], count=count, count_source='cmp',
                                    targets_out_of_function=sum(1 for t in targets if not start <= t < end),
                                    targets=' '.join(f'{t:#x}' for t in targets)))

    def address_taken(self):
        """function starts referenced by data words (all loaded segments outside decoded code) and
        by code constants (movw/movt, literal loads)"""
        starts = {(s | (1 if self.mode_of[s] else 0)): s for s in self.starts}
        refs = collections.defaultdict(lambda: collections.Counter())
        code_lo, code_hi = self.text.vaddr, self.elf.module_info_va
        ex0, ex1 = self.elf.extab_range
        mid = collections.defaultdict(list)  # Thumb pointers into code that are not function starts
        for seg in self.elf.segments:
            if seg.type != 1:
                continue
            blob = self.elf.data[seg.offset:seg.offset + seg.filesz]
            for o in range(0, len(blob) - 3, 4):
                va = seg.vaddr + o
                if seg is self.text and self.code_bytes[o] in (1, 2):
                    continue
                if self.elf.exidx_range[0] <= va < self.elf.extab_range[1]:
                    continue  # unwind tables: compact entries (0x81xxxxxx) only look like code pointers
                w = struct.unpack_from('<I', blob, o)[0]
                if w in starts:
                    src = 'literal' if seg is self.text and self.code_bytes[o] == 3 else ('rodata' if seg is self.text else 'data')
                    refs[starts[w]][src] += 1
                elif code_lo <= w < code_hi and w & 1 and (w & ~1) not in self.start_set:
                    mid[w & ~1].append(va)  # usually a data word that only looks like a pointer
        for val, sites in self.const_refs.items():
            if val in starts:
                refs[starts[val]]['code_const'] += len(sites)
        self.entrypoints = []
        for func, (model, pads) in sorted(self.eh_scopes.items()):
            for pad in pads:
                self.entrypoints.append(dict(target=f'{pad:#x}', func=f'{func:#x}', kind=f'landing_pad_{model}',
                                             target_state={1: 'instruction'}.get(self.code_bytes[pad - code_lo], 'not_decoded'),
                                             refs=0, sources=''))
        for t, srcs in sorted(mid.items()):
            state = self.code_bytes[t - code_lo]
            i = self.owner(t)
            self.entrypoints.append(dict(
                target=f'{t:#x}', func=f'{self.starts[i]:#x}' if i is not None else '',
                kind='data_word',
                target_state={0: 'not_decoded', 1: 'instruction', 2: 'table', 3: 'literal'}.get(state, 'unreached'),
                refs=len(srcs), sources=' '.join(f'{x:#x}' for x in srcs[:8])))
        return refs


def main():
    elf_path, dbdir, out = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out, exist_ok=True)
    db = vitaelf.load_nid_db(dbdir)
    elf = vitaelf.load(elf_path, db)
    inv = Inventory(elf)
    inv.analyse()
    refs = inv.address_taken()

    def write(name, rows, fields=None):
        with open(os.path.join(out, name), 'w', newline='', encoding='utf-8') as f:
            fields = fields or (list(rows[0].keys()) if rows else ['empty'])
            w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            w.writeheader()
            w.writerows(rows)

    fn_fields = ['start', 'end', 'mode', 'size', 'exidx', 'kind', 'name', 'insns', 'bytes_code', 'unreached',
                 'calls', 'import_calls', 'mode_switch_calls', 'indirect_calls', 'indirect_jumps', 'returns',
                 'tail_calls', 'jumptables', 'vfp_neon', 'neon', 'traps', 'noreturn_calls', 'orphan_roots', 'address_taken', 'flags']
    for row in inv.functions:
        r = refs.get(int(row['start'], 16))
        row['address_taken'] = ' '.join(f'{k}:{v}' for k, v in sorted(r.items())) if r else ''
    write('functions.csv', inv.functions, fn_fields)
    write('jumptables.csv', inv.jumptables, ['site', 'func', 'kind', 'base', 'index', 'count', 'count_source',
                                             'targets_out_of_function', 'targets'])
    write('indirect.csv', inv.indirect, ['site', 'func', 'insn', 'kind', 'reg', 'resolved', 'target', 'target_kind'])
    write('calls.csv', inv.calls, ['site', 'func', 'insn', 'kind', 'target', 'target_kind', 'mode_switch'])
    write('addrtaken.csv', [dict(function=f'{s:#x}', **{k: v for k, v in r.items()}) for s, r in sorted(refs.items())],
          ['function', 'data', 'rodata', 'literal', 'code_const'])
    write('entrypoints.csv', inv.entrypoints + inv.orphans, ['target', 'func', 'kind', 'target_state', 'refs', 'sources'])
    write('unreached.csv', inv.unreached_runs, ['addr', 'func', 'size', 'category', 'bytes'])
    write('importcalls.csv', inv.import_calls, ['site', 'func', 'stub', 'library', 'name'])
    write('imports.csv', [dict(stub=f'{i.stub:#x}', library=i.library, library_nid=f'{i.library_nid:#010x}',
                               nid=f'{i.nid:#010x}', name=i.name) for i in elf.imports],
          ['stub', 'library', 'library_nid', 'nid', 'name'])

    kinds = collections.Counter(r['kind'] for r in inv.functions)
    code = [r for r in inv.functions if r['kind'] == 'code']
    flags = collections.Counter(f for r in code for f in r['flags'].split())
    ind = collections.Counter((r['kind'], r['resolved']) for r in inv.indirect)
    tk = collections.Counter(r['target_kind'] for r in inv.calls)
    jt = collections.Counter(r['count_source'] for r in inv.jumptables)
    lines = [
        f'module {elf.name} ({os.path.basename(elf_path)}), {len(inv.functions)} exidx entries',
        f'kinds: {dict(kinds)}',
        f'exidx mode bits overridden by callers: {len(inv.mode_overrides)} '
        f'{[hex(a) for a in sorted(inv.mode_overrides)][:8]}',
        f'code functions: thumb {sum(1 for r in code if r["mode"] == "thumb")}, arm {sum(1 for r in code if r["mode"] == "arm")}',
        f'instructions decoded: {sum(r["insns"] for r in code)}; code bytes {sum(r["bytes_code"] for r in code)} '
        f'of {sum(r["size"] for r in code)} in range; unreached (non-padding) {sum(r["unreached"] for r in code)} bytes '
        f'in {sum(1 for r in code if r["unreached"])} functions',
        f'flags: {dict(flags)}',
        f'jump tables: {len(inv.jumptables)} ({dict(jt)}); with targets outside their function: '
        f'{sum(1 for r in inv.jumptables if r["targets_out_of_function"])}',
        f'indirect branches: {len(inv.indirect)} {dict(ind)}',
        f'calls/branches leaving functions listed in calls.csv: {len(inv.calls)} by target kind {dict(tk)}',
        f'tail calls: {sum(r["tail_calls"] for r in code)}; mode-switch calls: {sum(r["mode_switch_calls"] for r in code)}; '
        f'no-return calls (call + padding up to the next function): {sum(r["noreturn_calls"] for r in code)}',
        f'dead epilogues: {inv.dead_epilogues}; unreached halfword runs by category: '
        f'{dict(collections.Counter(r["category"] for r in inv.unreached_runs))}',
        f'address-taken functions: {len(refs)}',
        f'orphan code roots (decoded although no path reaches them): {len(inv.orphans)}',
        f'mid-function Thumb pointers: '
        f'{dict(collections.Counter((r["kind"], r["target_state"]) for r in inv.entrypoints))}',
        f'imports: {len(elf.imports)}',
    ]
    open(os.path.join(out, 'summary.txt'), 'w').write('\n'.join(lines) + '\n')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
