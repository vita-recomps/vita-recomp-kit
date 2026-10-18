"""Instruction semantics: one decoded ARMv7 instruction -> C++ statements over GuestContext.

translate() returns a Translation that tells the function emitter what the instruction does to
control flow (fall through, branch, call, return, ...) and which flags it reads and writes, so that
the emitter can place labels, wrap IT/conditional instructions in `if`, and drop flag updates that
nothing reads.

Conventions in the generated C++ (see recomp/runtime/armrecomp.h):
  ctx->r[n]   core registers        ctx->n/z/c/v/q   flags (0/1)
  ctx->vfp.f[n] / .fd[n] / .s[n] / .d[n]            VFP registers
  rd8/16/32/64, wr8/16/32/64                         guest memory
"""
from dataclasses import dataclass, field

from capstone import arm

from . import neon

REGS = {f'r{i}': i for i in range(16)}
REGS.update({'sb': 9, 'sl': 10, 'fp': 11, 'ip': 12, 'sp': 13, 'lr': 14, 'pc': 15})

COND_EXPR = {
    'eq': 'ctx->z', 'ne': '!ctx->z', 'cs': 'ctx->c', 'hs': 'ctx->c', 'cc': '!ctx->c', 'lo': '!ctx->c',
    'mi': 'ctx->n', 'pl': '!ctx->n', 'vs': 'ctx->v', 'vc': '!ctx->v',
    'hi': '(ctx->c && !ctx->z)', 'ls': '(!ctx->c || ctx->z)',
    'ge': '(ctx->n == ctx->v)', 'lt': '(ctx->n != ctx->v)',
    'gt': '(!ctx->z && ctx->n == ctx->v)', 'le': '(ctx->z || ctx->n != ctx->v)',
}
COND_FLAGS = {
    'eq': 'z', 'ne': 'z', 'cs': 'c', 'hs': 'c', 'cc': 'c', 'lo': 'c', 'mi': 'n', 'pl': 'n', 'vs': 'v', 'vc': 'v',
    'hi': 'cz', 'ls': 'cz', 'ge': 'nv', 'lt': 'nv', 'gt': 'znv', 'le': 'znv',
}
CC_NAME = {arm.ARM_CC_EQ: 'eq', arm.ARM_CC_NE: 'ne', arm.ARM_CC_HS: 'hs', arm.ARM_CC_LO: 'lo',
           arm.ARM_CC_MI: 'mi', arm.ARM_CC_PL: 'pl', arm.ARM_CC_VS: 'vs', arm.ARM_CC_VC: 'vc',
           arm.ARM_CC_HI: 'hi', arm.ARM_CC_LS: 'ls', arm.ARM_CC_GE: 'ge', arm.ARM_CC_LT: 'lt',
           arm.ARM_CC_GT: 'gt', arm.ARM_CC_LE: 'le'}

DP_FLAGSET = {'mov', 'mvn', 'add', 'adc', 'sub', 'sbc', 'rsb', 'rsc', 'and', 'orr', 'eor', 'bic', 'orn',
              'lsl', 'lsr', 'asr', 'ror', 'rrx', 'mul', 'mla', 'umull', 'smull', 'umlal', 'smlal', 'neg'}


class Unsupported(Exception):
    pass


@dataclass
class Translation:
    stmts: list = field(default_factory=list)
    kind: str = 'seq'  # seq, branch, cond_branch, call, call_import, call_indirect, return,
    #                    jump_indirect, jumptable, trap, it, nop
    target: int = None  # branch/call target
    cond: str = None  # C condition for cond_branch / cbz / cbnz
    flags_read: set = field(default_factory=set)
    flags_written: set = field(default_factory=set)  # flags this instruction may set
    jumptable: tuple = None  # (index expression, [targets])
    writes_pc_via: str = None  # for returns: how pc is produced ('lr' / 'pop')


def reg_index(cs, reg):
    name = cs.reg_name(reg)
    if name not in REGS:
        raise Unsupported(f'register {name}')
    return REGS[name]


class Ctx:
    """Per-instruction translation context."""

    def __init__(self, ins, thumb):
        self.ins = ins
        self.cs = ins.cs
        self.thumb = thumb
        self.addr = ins.address
        self.pc = self.addr + (4 if thumb else 8)
        self.t = Translation()
        self.patched = False

    def emit(self, s):
        self.t.stmts.append(s)

    def r(self, op_or_idx):
        """C expression reading a core register (pc reads give the constant pc value)"""
        n = op_or_idx if isinstance(op_or_idx, int) else reg_index(self.cs, op_or_idx.reg)
        if n == 15:
            return f'0x{self.pc:08x}u'
        return f'ctx->r[{n}]'

    def ridx(self, op):
        return reg_index(self.cs, op.reg)

    def set_flag(self, f, expr):
        """assign flag f: a ('flag', f, statement) marker the emitter drops when the flag is dead"""
        self.t.flags_written.add(f)
        self.t.stmts.append(('flag', f, f'ctx->{f} = {expr};'))

    def set_nz(self, val):
        self.set_flag('n', f'({val}) >> 31')
        self.set_flag('z', f'({val}) == 0')

    def read_flag(self, f):
        self.t.flags_read.add(f)
        return f'ctx->{f}'


# --- operand 2 --------------------------------------------------------------------------------

def imm_carry(cx):
    """carry-out of a modified immediate (ThumbExpandImm_C / ARMExpandImm_C): None = unchanged"""
    b = bytes(cx.cs.bytes)
    if cx.thumb:
        if len(b) != 4:
            return None
        hw1 = b[0] | b[1] << 8
        hw2 = b[2] | b[3] << 8
        if (hw1 & 0xf800) != 0xf000 or (hw2 & 0x8000):
            return None  # not a data-processing (modified immediate) encoding
        if (hw1 >> 9) & 1:
            return None  # plain binary immediate (addw/movw/...): no carry
        i = (hw1 >> 10) & 1
        imm3 = (hw2 >> 12) & 7
        if ((i << 1) | (imm3 >> 2)) == 0:
            return None
    else:
        w = int.from_bytes(b, 'little')
        if ((w >> 8) & 0xf) == 0:
            return None
    return 'rotated'


def operand2(cx, op, want_carry):
    """(value expression, carry expression or None if the carry is unchanged)"""
    cs = cx.cs
    if op.type == arm.ARM_OP_IMM:
        v = op.imm & 0xffffffff
        carry = None
        if want_carry and imm_carry(cx):
            carry = str(v >> 31)
        return f'0x{v:08x}u', carry
    if op.type != arm.ARM_OP_REG:
        raise Unsupported('operand2 type')
    x = cx.r(op)
    st, sv = op.shift.type, op.shift.value
    if st == arm.ARM_SFT_INVALID or (st == arm.ARM_SFT_LSL and sv == 0):
        return x, None
    if st == arm.ARM_SFT_LSL:
        return f'({x} << {sv})', f'(({x} >> {32 - sv}) & 1u)'
    if st == arm.ARM_SFT_LSR:
        if sv == 32:
            return '0u', f'({x} >> 31)'
        return f'({x} >> {sv})', f'(({x} >> {sv - 1}) & 1u)'
    if st == arm.ARM_SFT_ASR:
        n = min(sv, 31)
        return f'uint32_t(int32_t({x}) >> {n})', f'((uint32_t(int32_t({x}) >> {min(sv - 1, 31)})) & 1u)'
    if st == arm.ARM_SFT_ROR:
        return f'ror32({x}, {sv})', f'(ror32({x}, {sv}) >> 31)'
    if st == arm.ARM_SFT_RRX:
        cx.t.flags_read.add('c')
        return f'((ctx->c << 31) | ({x} >> 1))', f'({x} & 1u)'
    raise Unsupported(f'shift type {st}')


# --- data processing --------------------------------------------------------------------------

def dp(cx, base, s):
    cs = cx.cs
    ops = cs.operands
    t = cx.t
    logical = base in ('and', 'orr', 'eor', 'bic', 'orn', 'mov', 'mvn', 'tst', 'teq')
    compare = base in ('cmp', 'cmn', 'tst', 'teq')
    if compare:
        dst, srcs = None, ops
        s = True
    elif base in ('mov', 'mvn'):
        dst, srcs = ops[0], ops[1:]
    else:
        dst = ops[0]
        srcs = ops[1:] if len(ops) == 3 else ops  # two-operand Thumb forms: add r0, r1 = add r0, r0, r1
        if len(ops) == 2:
            srcs = [ops[0], ops[1]]
    if dst is not None and cx.ridx(dst) == 15:
        if base in ('mov', 'add') and not s:
            # mov pc, rX / add pc, ...: computed jump
            if base == 'mov' and ops[1].type == arm.ARM_OP_REG and cx.ridx(ops[1]) == 14:
                t.kind = 'return'
                t.writes_pc_via = 'lr'
                return
            if base == 'mov' and ops[1].type == arm.ARM_OP_REG and not ops[1].shift.type and cx.ridx(ops[1]) != 15:
                # mov pc, rX: in Thumb a branch that stays in Thumb (BranchWritePC), in ARM an
                # interworking one like bx (BXWritePC)
                r = cx.r(cx.ridx(ops[1]))
                cx.emit(f'ctx->r[15] = {r} | 1u;' if cx.thumb else f'ctx->r[15] = {r};')
                t.kind = 'jump_indirect'
                return
            raise Unsupported('write to pc')
        raise Unsupported('write to pc')

    if base in ('mov', 'mvn'):
        val, carry = operand2(cx, srcs[0], s)
        if base == 'mvn':
            val = f'~({val})'
        cx.emit('{')
        cx.emit(f'const uint32_t res = {val};')
        if s:
            cx.set_nz('res')
            if carry is not None:
                cx.set_flag('c', carry)
        return finish_dp(cx, dst, s)

    a = cx.r(srcs[0])
    b, carry = operand2(cx, srcs[1], s and logical)
    cx.emit('{')
    cx.emit(f'const uint32_t a = {a};')
    cx.emit(f'const uint32_t b = {b};')
    if logical:
        expr = {'and': 'a & b', 'tst': 'a & b', 'orr': 'a | b', 'eor': 'a ^ b', 'teq': 'a ^ b',
                'bic': 'a & ~b', 'orn': 'a | ~b'}[base]
        cx.emit(f'const uint32_t res = {expr};')
        if s:
            cx.set_nz('res')
            if carry is not None:
                cx.set_flag('c', carry)
        return finish_dp(cx, dst, s)

    if base in ('add', 'cmn'):
        args = 'a, b, 0'
    elif base in ('sub', 'cmp'):
        args = 'a, ~b, 1'
    elif base == 'rsb':
        args = 'b, ~a, 1'
    elif base == 'adc':
        args = f'a, b, {cx.read_flag("c")}'
    elif base == 'sbc':
        args = f'a, ~b, {cx.read_flag("c")}'
    elif base == 'rsc':
        args = f'b, ~a, {cx.read_flag("c")}'
    else:
        raise Unsupported(base)
    if s:
        cx.emit('uint32_t carry, overflow;')
        cx.emit(f'const uint32_t res = add_c({args}, carry, overflow);')
        cx.set_nz('res')
        cx.set_flag('c', 'carry')
        cx.set_flag('v', 'overflow')
    else:
        x, y, cin = [v.strip() for v in args.split(',')]
        cx.emit(f'const uint32_t res = {x} + {y} + {cin};')
    return finish_dp(cx, dst, s)


def finish_dp(cx, dst, s):
    if dst is not None:
        cx.emit(f'ctx->r[{cx.ridx(dst)}] = res;')
    cx.emit('}')


def dp_wide_imm(cx, base):
    """addw/subw/movw/movt/adr: plain immediates, never set flags"""
    ops = cx.cs.operands
    d = cx.ridx(ops[0])
    if d == 15:
        raise Unsupported('write to pc')
    if cx.patched:
        # the loader writes this immediate (a variable import's address): decode it at run time
        if base not in ('movw', 'movt') or not cx.thumb or cx.ins.size != 4:
            raise Unsupported(f'loader-patched {base}')
        imm = f'thumb_mov_imm16(0x{cx.addr:08x}u)'
        if base == 'movw':
            cx.emit(f'ctx->r[{d}] = {imm};')
        else:
            cx.emit(f'ctx->r[{d}] = (ctx->r[{d}] & 0xffffu) | ({imm} << 16);')
    elif base == 'movw':
        cx.emit(f'ctx->r[{d}] = 0x{ops[1].imm & 0xffff:04x}u;')
    elif base == 'movt':
        cx.emit(f'ctx->r[{d}] = (ctx->r[{d}] & 0xffffu) | 0x{(ops[1].imm & 0xffff) << 16:08x}u;')
    elif base in ('addw', 'subw'):
        src = cx.ridx(ops[1])
        a = f'0x{(cx.pc & ~3):08x}u' if src == 15 else cx.r(ops[1])
        sign = '+' if base == 'addw' else '-'
        cx.emit(f'ctx->r[{d}] = {a} {sign} 0x{ops[2].imm & 0xffffffff:08x}u;')
    elif base == 'adr':
        cx.emit(f'ctx->r[{d}] = 0x{((cx.pc & ~3) + ops[1].imm) & 0xffffffff:08x}u;')


def shift_insn(cx, base, s):
    """lsl/lsr/asr/ror with an immediate or a register amount"""
    ops = cx.cs.operands
    d = cx.ridx(ops[0])
    reg_amount = None
    if len(ops) == 2 and ops[1].shift.type != arm.ARM_SFT_INVALID:
        # ARM: lsl rd, rm, #n / rs is a mov with a shifted operand; the shift is on rm
        src, sh = ops[1], ops[1].shift
        if sh.type in (arm.ARM_SFT_ASR, arm.ARM_SFT_LSL, arm.ARM_SFT_LSR, arm.ARM_SFT_ROR):
            amt = type('Imm', (), {'type': arm.ARM_OP_IMM, 'imm': sh.value})()
        elif sh.type in (arm.ARM_SFT_ASR_REG, arm.ARM_SFT_LSL_REG, arm.ARM_SFT_LSR_REG, arm.ARM_SFT_ROR_REG):
            amt = None
            reg_amount = reg_index(cx.cs, sh.value)
        else:
            raise Unsupported('rrx shift')
    elif len(ops) == 2:
        src, amt = ops[0], ops[1]
    else:
        src, amt = ops[1], ops[2]
    x = cx.r(src)
    cx.emit('{')
    cx.emit(f'const uint32_t x = {x};')
    if amt is not None and amt.type == arm.ARM_OP_IMM:
        n = amt.imm
        if base == 'lsl':
            res, carry = (('x', None) if n == 0 else (f'x << {n}', f'(x >> {32 - n}) & 1u'))
        elif base == 'lsr':
            n = n or 32
            res, carry = (('0u', 'x >> 31') if n == 32 else (f'x >> {n}', f'(x >> {n - 1}) & 1u'))
        elif base == 'asr':
            n = n or 32
            res, carry = f'uint32_t(int32_t(x) >> {min(n, 31)})', f'uint32_t(int32_t(x) >> {min(n - 1, 31)}) & 1u'
        elif base == 'ror':
            res, carry = f'ror32(x, {n})', f'ror32(x, {n}) >> 31'
        cx.emit(f'const uint32_t res = {res};')
        if s:
            cx.set_nz('res')
            if carry is not None:
                cx.set_flag('c', carry)
    else:
        cx.emit(f'const uint32_t sh = {cx.r(reg_amount if reg_amount is not None else amt)} & 0xffu;')
        if base == 'lsl':
            cx.emit('const uint32_t res = sh >= 32 ? 0u : x << sh;')
            carry = 'sh == 0 ? ctx->c : (sh <= 32 ? (x >> (32 - sh)) & 1u : 0u)'
        elif base == 'lsr':
            cx.emit('const uint32_t res = sh >= 32 ? 0u : x >> sh;')
            carry = 'sh == 0 ? ctx->c : (sh <= 32 ? (x >> (sh - 1)) & 1u : 0u)'
        elif base == 'asr':
            cx.emit('const uint32_t res = uint32_t(int32_t(x) >> (sh >= 32 ? 31 : sh));')
            carry = 'sh == 0 ? ctx->c : (uint32_t(int32_t(x) >> (sh >= 32 ? 31 : sh - 1)) & 1u)'
        elif base == 'ror':
            cx.emit('const uint32_t res = ror32(x, sh);')
            carry = 'sh == 0 ? ctx->c : (res >> 31)'
        if s:
            cx.set_nz('res')
            cx.t.flags_read.add('c')
            cx.set_flag('c', carry)
    cx.emit(f'ctx->r[{d}] = res;')
    cx.emit('}')


def multiply(cx, base, s):
    ops = cx.cs.operands
    r = [cx.ridx(o) if o.type == arm.ARM_OP_REG else None for o in ops]
    R = [cx.r(o) if o.type == arm.ARM_OP_REG else None for o in ops]
    if base == 'mul':
        a, b = (R[1], R[2]) if len(ops) == 3 else (R[0], R[1])
        cx.emit('{')
        cx.emit(f'const uint32_t res = {a} * {b};')
        if s:
            cx.set_nz('res')
        cx.emit(f'ctx->r[{r[0]}] = res;')
        cx.emit('}')
    elif base in ('mla', 'mls'):
        op = '+' if base == 'mla' else '-'
        cx.emit('{')
        cx.emit(f'const uint32_t res = {R[3]} {op} {R[1]} * {R[2]};')
        if s:
            cx.set_nz('res')
        cx.emit(f'ctx->r[{r[0]}] = res;')
        cx.emit('}')
    elif base in ('umull', 'smull', 'umlal', 'smlal'):
        lo, hi = r[0], r[1]
        if base[0] == 'u':
            prod = f'uint64_t({R[2]}) * uint64_t({R[3]})'
        else:
            prod = f'uint64_t(int64_t(int32_t({R[2]})) * int64_t(int32_t({R[3]})))'
        cx.emit('{')
        if base.endswith('lal'):
            cx.emit(f'const uint64_t res = {prod} + ((uint64_t({R[1]}) << 32) | {R[0]});')
        else:
            cx.emit(f'const uint64_t res = {prod};')
        if s:
            cx.set_flag('n', 'uint32_t(res >> 63)')
            cx.set_flag('z', 'res == 0')
        cx.emit(f'ctx->r[{lo}] = uint32_t(res);')
        cx.emit(f'ctx->r[{hi}] = uint32_t(res >> 32);')
        cx.emit('}')
    elif base in ('sdiv', 'udiv'):
        a, b = (R[1], R[2]) if len(ops) == 3 else (R[0], R[1])
        if base == 'udiv':
            cx.emit(f'ctx->r[{r[0]}] = {b} == 0 ? 0u : {a} / {b};')
        else:
            cx.emit('{')
            cx.emit(f'const int32_t a = int32_t({a}), b = int32_t({b});')
            cx.emit(f'ctx->r[{r[0]}] = b == 0 ? 0u : (a == INT32_MIN && b == -1) ? 0x80000000u : uint32_t(a / b);')
            cx.emit('}')
    elif base in ('smulbb', 'smulbt', 'smultb', 'smultt'):
        x = f'int16_t({R[1]}{" >> 16" if base[4] == "t" else ""})'
        y = f'int16_t({R[2]}{" >> 16" if base[5] == "t" else ""})'
        cx.emit(f'ctx->r[{r[0]}] = uint32_t(int32_t({x}) * int32_t({y}));')
    elif base in ('smlabb', 'smlabt', 'smlatb', 'smlatt'):
        x = f'int16_t({R[1]}{" >> 16" if base[4] == "t" else ""})'
        y = f'int16_t({R[2]}{" >> 16" if base[5] == "t" else ""})'
        cx.emit('{')
        cx.emit('uint32_t carry, overflow;')
        cx.emit(f'const uint32_t res = add_c(uint32_t(int32_t({x}) * int32_t({y})), {R[3]}, 0, carry, overflow);')
        cx.emit(f'ctx->r[{r[0]}] = res;')
        cx.emit('if (overflow) ctx->q = 1;')
        cx.emit('}')
    elif base in ('smmul', 'smmla', 'smmls'):
        prod = f'int64_t(int32_t({R[1]})) * int64_t(int32_t({R[2]}))'
        if base == 'smmul':
            cx.emit(f'ctx->r[{r[0]}] = uint32_t(uint64_t({prod}) >> 32);')
        elif base == 'smmla':
            cx.emit(f'ctx->r[{r[0]}] = uint32_t((uint64_t({prod}) + (uint64_t({R[3]}) << 32)) >> 32);')
        else:
            cx.emit(f'ctx->r[{r[0]}] = uint32_t(((uint64_t({R[3]}) << 32) - uint64_t({prod})) >> 32);')
    else:
        raise Unsupported(base)


def bitfield(cx, base):
    ops = cx.cs.operands
    d = cx.ridx(ops[0])
    R = [cx.r(o) if o.type == arm.ARM_OP_REG else None for o in ops]
    if base in ('ubfx', 'sbfx'):
        lsb, width = ops[2].imm, ops[3].imm
        if base == 'ubfx':
            mask = (1 << width) - 1
            cx.emit(f'ctx->r[{d}] = ({R[1]} >> {lsb}) & 0x{mask:x}u;')
        else:
            cx.emit(f'ctx->r[{d}] = uint32_t(int32_t({R[1]} << {32 - lsb - width}) >> {32 - width});')
    elif base == 'bfi':
        lsb, width = ops[2].imm, ops[3].imm
        mask = ((1 << width) - 1) << lsb
        cx.emit(f'ctx->r[{d}] = (ctx->r[{d}] & 0x{~mask & 0xffffffff:08x}u) | (({R[1]} << {lsb}) & 0x{mask:08x}u);')
    elif base == 'bfc':
        lsb, width = ops[1].imm, ops[2].imm
        mask = ((1 << width) - 1) << lsb
        cx.emit(f'ctx->r[{d}] &= 0x{~mask & 0xffffffff:08x}u;')
    elif base in ('uxtb', 'uxth', 'sxtb', 'sxth', 'uxtab', 'uxtah', 'sxtab', 'sxtah', 'uxtb16', 'sxtb16'):
        src = ops[-1]
        x = cx.r(src)
        if src.shift.type == arm.ARM_SFT_ROR and src.shift.value:
            x = f'ror32({x}, {src.shift.value})'
        size = base[-1] if not base.endswith('16') else base[-3]
        ext = {'b': 'uint8_t', 'h': 'uint16_t'}[size]
        if base[0] == 's':
            ext = 'int8_t' if size == 'b' else 'int16_t'
        val = f'uint32_t(int32_t({ext}({x})))' if base[0] == 's' else f'uint32_t({ext}({x}))'
        if base.endswith('16'):
            raise Unsupported(base)
        if len(base) == 5:  # accumulate
            cx.emit(f'ctx->r[{d}] = {cx.r(ops[1])} + {val};')
        else:
            cx.emit(f'ctx->r[{d}] = {val};')
    elif base == 'clz':
        cx.emit('{')
        cx.emit(f'const uint32_t x = {R[1]};')
        cx.emit(f'uint32_t n = 0; while (n < 32 && !((x << n) & 0x80000000u)) n++;')
        cx.emit(f'ctx->r[{d}] = n;')
        cx.emit('}')
    elif base == 'rev':
        cx.emit(f'ctx->r[{d}] = bswap32({R[1]});')
    elif base == 'rev16':
        cx.emit(f'{{ const uint32_t x = {R[1]}; ctx->r[{d}] = ((x & 0x00ff00ffu) << 8) | ((x >> 8) & 0x00ff00ffu); }}')
    elif base == 'revsh':
        cx.emit(f'{{ const uint32_t x = {R[1]}; ctx->r[{d}] = uint32_t(int32_t(int16_t(((x & 0xffu) << 8) | ((x >> 8) & 0xffu)))); }}')
    elif base == 'rbit':
        cx.emit(f'{{ uint32_t x = {R[1]}, y = 0; for (int i = 0; i < 32; i++) {{ y = (y << 1) | (x & 1u); x >>= 1; }} ctx->r[{d}] = y; }}')
    elif base in ('usat', 'ssat'):
        bits = ops[1].imm
        src = ops[2]
        x = cx.r(src)
        if src.shift.type == arm.ARM_SFT_LSL:
            x = f'({x} << {src.shift.value})'
        elif src.shift.type == arm.ARM_SFT_ASR:
            x = f'uint32_t(int32_t({x}) >> {min(src.shift.value, 31)})'
        fn = 'usat' if base == 'usat' else 'ssat'
        cx.emit(f'{{ uint32_t sat = 0; ctx->r[{d}] = uint32_t({fn}(int64_t(int32_t({x})), {bits}, sat)); if (sat) ctx->q = 1; }}')
    else:
        raise Unsupported(base)


# --- memory -----------------------------------------------------------------------------------

def mem_address(cx, mop, post_imm=None):
    """(address expression used for the access, base register index, writeback value expression)"""
    cs = cx.cs
    m = mop.mem
    base = reg_index(cs, m.base)
    if base == 15:
        # pc-relative (literal): Align(PC, 4)
        b = f'0x{(cx.pc & ~3):08x}u'
    else:
        b = f'ctx->r[{base}]'
    if m.index:
        idx = cx.r(reg_index(cs, m.index))
        if mop.shift.type == arm.ARM_SFT_LSL and mop.shift.value:
            idx = f'({idx} << {mop.shift.value})'
        elif mop.shift.type not in (arm.ARM_SFT_INVALID, arm.ARM_SFT_LSL):
            raise Unsupported('index shift')
        off = f'- {idx}' if mop.subtracted else f'+ {idx}'
    else:
        d = m.disp
        off = f'- 0x{-d:x}u' if d < 0 else (f'+ 0x{d:x}u' if d else '')
    if cx.cs.post_index or post_imm is not None:
        # [rn], #imm / [rn], rm: access at rn, then rn += offset (an operand after the memory
        # operand always means post-indexing; Capstone's post_index flag is not set in ARM mode)
        if post_imm is None:
            raise Unsupported('post-index form')
        return b, base, post_imm(b)
    addr = f'({b} {off})' if off else b
    wb = addr if cx.cs.writeback else None
    return addr, base, wb


def post_offset(cx, ops, first_extra):
    """post-index offset operand(s) after the memory operand"""
    extra = ops[first_extra:]
    if not extra:
        return None
    o = extra[0]
    if o.type == arm.ARM_OP_IMM:
        v = o.imm
        neg = getattr(o, 'subtracted', False)
        if neg:
            v = -v
        return lambda b: f'({b} {"-" if v < 0 else "+"} 0x{abs(v):x}u)'
    if o.type == arm.ARM_OP_REG:
        idx = cx.r(o)
        if o.shift.type == arm.ARM_SFT_LSL and o.shift.value:
            idx = f'({idx} << {o.shift.value})'
        sign = '-' if getattr(o, 'subtracted', False) else '+'
        return lambda b: f'({b} {sign} {idx})'
    raise Unsupported('post-index operand')


LOAD = {'ldr': ('rd32', 'uint32_t', 4), 'ldrb': ('rd8', 'uint8_t', 1), 'ldrh': ('rd16', 'uint16_t', 2),
        'ldrsb': ('rd8', 'int8_t', 1), 'ldrsh': ('rd16', 'int16_t', 2),
        'ldrt': ('rd32', 'uint32_t', 4), 'ldrbt': ('rd8', 'uint8_t', 1)}
STORE = {'str': ('wr32', 'uint32_t'), 'strb': ('wr8', 'uint8_t'), 'strh': ('wr16', 'uint16_t')}


def load_store(cx, base):
    ops = cx.cs.operands
    t = cx.t
    if base in ('ldrd', 'strd'):
        r1, r2 = cx.ridx(ops[0]), cx.ridx(ops[1])
        mop = ops[2]
        addr, bidx, wb = mem_address(cx, mop, post_offset(cx, ops, 3))
        cx.emit('{')
        cx.emit(f'const uint32_t addr = {addr};')
        if base == 'ldrd':
            cx.emit(f'const uint32_t v0 = rd32(addr), v1 = rd32(addr + 4);')
            if wb is not None:
                cx.emit(f'ctx->r[{bidx}] = {wb};')
            cx.emit(f'ctx->r[{r1}] = v0;')
            cx.emit(f'ctx->r[{r2}] = v1;')
        else:
            cx.emit(f'wr32(addr, {cx.r(r1)});')
            cx.emit(f'wr32(addr + 4, {cx.r(r2)});')
            if wb is not None:
                cx.emit(f'ctx->r[{bidx}] = {wb};')
        cx.emit('}')
        return
    rt = cx.ridx(ops[0])
    mop = ops[1]
    addr, bidx, wb = mem_address(cx, mop, post_offset(cx, ops, 2))
    cx.emit('{')
    cx.emit(f'const uint32_t addr = {addr};')
    if base in LOAD:
        fn, ty, _ = LOAD[base]
        val = f'uint32_t(int32_t({ty}({fn}(addr))))' if ty.startswith('int') else f'uint32_t({fn}(addr))'
        cx.emit(f'const uint32_t v = {val};')
        if wb is not None:
            cx.emit(f'ctx->r[{bidx}] = {wb};')
        if rt == 15:
            # ldr pc, [...]: pop of pc (ldr pc, [sp], #4) is a return, anything else is a jump
            if bidx == 13 and cx.cs.post_index:
                cx.emit('ctx->r[15] = v;')
                cx.emit('}')
                t.kind = 'return'
                t.writes_pc_via = 'pop'
                return
            cx.emit('ctx->r[15] = v;')
            cx.emit('}')
            t.kind = 'jump_indirect'
            return
        cx.emit(f'ctx->r[{rt}] = v;')
    elif base in STORE:
        fn, ty = STORE[base]
        cx.emit(f'{fn}(addr, {ty}({cx.r(rt)}));')
        if wb is not None:
            cx.emit(f'ctx->r[{bidx}] = {wb};')
    else:
        raise Unsupported(base)
    cx.emit('}')


def load_store_multiple(cx, base):
    cs = cx.cs
    ops = cs.operands
    t = cx.t
    if base in ('push', 'pop'):
        rn = 13
        regs = [cx.ridx(o) for o in ops]
        mode = 'db' if base == 'push' else 'ia'
        load = base == 'pop'
        writeback = True
    else:
        rn = cx.ridx(ops[0])
        regs = [cx.ridx(o) for o in ops[1:]]
        load = base.startswith('ldm')
        mode = base[3:] or 'ia'
        if mode in ('fd',):
            mode = 'ia' if load else 'db'
        writeback = cs.writeback
    if mode not in ('ia', 'db'):
        raise Unsupported(f'{base} mode')
    n = len(regs)
    cx.emit('{')
    if mode == 'ia':
        cx.emit(f'uint32_t addr = ctx->r[{rn}];')
        final = f'ctx->r[{rn}] + {4 * n}u'
    else:
        cx.emit(f'uint32_t addr = ctx->r[{rn}] - {4 * n}u;')
        final = f'ctx->r[{rn}] - {4 * n}u'
    if writeback:
        cx.emit(f'const uint32_t final_addr = {final};')
    if load:
        for i, r in enumerate(regs):
            cx.emit(f'const uint32_t v{i} = rd32(addr + {4 * i}u);')
        if writeback and rn not in regs:
            cx.emit(f'ctx->r[{rn}] = final_addr;')
        for i, r in enumerate(regs):
            cx.emit(f'ctx->r[{r}] = v{i};')
        cx.emit('}')
        if 15 in regs:
            t.kind = 'return' if rn == 13 else 'jump_indirect'
            t.writes_pc_via = 'pop'
    else:
        for i, r in enumerate(regs):
            cx.emit(f'wr32(addr + {4 * i}u, {cx.r(r)});')
        if writeback:
            cx.emit(f'ctx->r[{rn}] = final_addr;')
        cx.emit('}')


def exclusive(cx, base):
    ops = cx.cs.operands
    if base in ('ldrex', 'ldrexb', 'ldrexh'):
        fn, ty = {'ldrex': ('rd32', 'uint32_t'), 'ldrexb': ('rd8', 'uint8_t'), 'ldrexh': ('rd16', 'uint16_t')}[base]
        rt = cx.ridx(ops[0])
        addr, _, _ = mem_address(cx, ops[1])
        cx.emit('{')
        cx.emit(f'const uint32_t addr = {addr};')
        cx.emit(f'const uint32_t v = uint32_t({fn}(addr));')
        cx.emit('ctx->excl_addr = addr; ctx->excl_valid = 1; ctx->excl_value = v;')
        cx.emit(f'ctx->r[{rt}] = v;')
        cx.emit('}')
    elif base in ('strex', 'strexb', 'strexh'):
        fn, ty = {'strex': ('wr32', 'uint32_t'), 'strexb': ('wr8', 'uint8_t'), 'strexh': ('wr16', 'uint16_t')}[base]
        rd, rt = cx.ridx(ops[0]), cx.ridx(ops[1])
        addr, _, _ = mem_address(cx, ops[2])
        # the store succeeds when the monitor is armed for this address and memory still holds the
        # value ldrex read: a host compare-and-swap, as Vita3K's dynarmic does it (value-based)
        cx.emit('{')
        cx.emit(f'const uint32_t addr = {addr};')
        cx.emit(f'const uint32_t ok = ctx->excl_valid && ctx->excl_addr == addr && cas<{ty}>(addr, {ty}(ctx->excl_value), {ty}({cx.r(rt)}));')
        cx.emit('ctx->excl_valid = 0;')
        cx.emit(f'ctx->r[{rd}] = ok ? 0u : 1u;')
        cx.emit('}')
    elif base == 'ldrexd':
        r1, r2 = cx.ridx(ops[0]), cx.ridx(ops[1])
        addr, _, _ = mem_address(cx, ops[2])
        cx.emit('{')
        cx.emit(f'const uint32_t addr = {addr};')
        cx.emit('const uint64_t v = rd64(addr);')
        cx.emit('ctx->excl_addr = addr; ctx->excl_valid = 1; ctx->excl_value = v;')
        cx.emit(f'ctx->r[{r1}] = uint32_t(v);')
        cx.emit(f'ctx->r[{r2}] = uint32_t(v >> 32);')
        cx.emit('}')
    elif base == 'strexd':
        rd, r1, r2 = cx.ridx(ops[0]), cx.ridx(ops[1]), cx.ridx(ops[2])
        addr, _, _ = mem_address(cx, ops[3])
        cx.emit('{')
        cx.emit(f'const uint32_t addr = {addr};')
        cx.emit(f'const uint32_t ok = ctx->excl_valid && ctx->excl_addr == addr && cas<uint64_t>(addr, ctx->excl_value, (uint64_t({cx.r(r2)}) << 32) | {cx.r(r1)});')
        cx.emit('ctx->excl_valid = 0;')
        cx.emit(f'ctx->r[{rd}] = ok ? 0u : 1u;')
        cx.emit('}')
    elif base == 'clrex':
        cx.emit('ctx->excl_valid = 0;')
    else:
        raise Unsupported(base)


# --- VFP (scalar) -----------------------------------------------------------------------------

def vreg(cx, op):
    name = cx.cs.reg_name(op.reg)
    if name in REGS:
        return 'r', REGS[name]
    if name[0] == 's':
        return 's', int(name[1:])
    if name[0] == 'd':
        return 'd', int(name[1:])
    if name[0] == 'q':
        raise Unsupported('q register')
    return 'r', reg_index(cx.cs, op.reg)


def fexpr(kind, n):
    return f'ctx->vfp.f[{n}]' if kind == 's' else f'ctx->vfp.fd[{n}]'


def vfp(cx, mnemonic):
    cs = cx.cs
    parts = cx.ins.full.split('.')
    base = parts[0]
    types = parts[1:]
    ops = cs.operands
    if base in ('vldr', 'vstr'):
        k, n = vreg(cx, ops[0])
        addr, _, _ = mem_address(cx, ops[1])
        cx.emit('{')
        cx.emit(f'const uint32_t addr = {addr};')
        if base == 'vldr':
            cx.emit(f'ctx->vfp.s[{n}] = rd32(addr);' if k == 's' else f'ctx->vfp.d[{n}] = rd64(addr);')
        else:
            cx.emit(f'wr32(addr, ctx->vfp.s[{n}]);' if k == 's' else f'wr64(addr, ctx->vfp.d[{n}]);')
        cx.emit('}')
        return
    if base in ('vpush', 'vpop', 'vldmia', 'vstmia', 'vldmdb', 'vstmdb'):
        if base in ('vpush', 'vpop'):
            rn, regs, writeback = 13, ops, True
            mode = 'db' if base == 'vpush' else 'ia'
            load = base == 'vpop'
        else:
            rn = cx.ridx(ops[0])
            regs = ops[1:]
            writeback = cs.writeback
            mode = base[-2:]
            load = base.startswith('vld')
        regs = [vreg(cx, o) for o in regs]
        size = sum(4 if k == 's' else 8 for k, _ in regs)
        cx.emit('{')
        cx.emit(f'uint32_t addr = ctx->r[{rn}]{" - " + str(size) + "u" if mode == "db" else ""};')
        if writeback:
            cx.emit(f'const uint32_t final_addr = ctx->r[{rn}] {"-" if mode == "db" else "+"} {size}u;')
        off = 0
        for k, n in regs:
            if k == 's':
                cx.emit(f'ctx->vfp.s[{n}] = rd32(addr + {off}u);' if load else f'wr32(addr + {off}u, ctx->vfp.s[{n}]);')
                off += 4
            else:
                cx.emit(f'ctx->vfp.d[{n}] = rd64(addr + {off}u);' if load else f'wr64(addr + {off}u, ctx->vfp.d[{n}]);')
                off += 8
        if writeback:
            cx.emit(f'ctx->r[{rn}] = final_addr;')
        cx.emit('}')
        return
    if base == 'vmov' and types == ['32'] and len(ops) == 2:
        # vmov.32 rt, dn[x] / vmov.32 dn[x], rt: one 32-bit lane of a d register
        o0, o1 = ops
        if o0.type == arm.ARM_OP_REG and cx.cs.reg_name(o0.reg) in REGS:
            lane = o1.vector_index
            cx.emit(f'ctx->r[{cx.ridx(o0)}] = ctx->vfp.s[{2 * vreg(cx, o1)[1] + lane}];')
        else:
            lane = o0.vector_index
            cx.emit(f'ctx->vfp.s[{2 * vreg(cx, o0)[1] + lane}] = {cx.r(o1)};')
        return
    if base == 'vmov':
        kinds = [vreg(cx, o) if o.type == arm.ARM_OP_REG else ('imm', o) for o in ops]
        if len(ops) == 2:
            (k0, n0), (k1, n1) = kinds
            if k0 == 's' and k1 == 'r':
                cx.emit(f'ctx->vfp.s[{n0}] = {cx.r(n1)};')
            elif k0 == 'r' and k1 == 's':
                cx.emit(f'ctx->r[{n0}] = ctx->vfp.s[{n1}];')
            elif k0 == 's' and k1 == 's':
                cx.emit(f'ctx->vfp.s[{n0}] = ctx->vfp.s[{n1}];')
            elif k0 == 'd' and k1 == 'd':
                cx.emit(f'ctx->vfp.d[{n0}] = ctx->vfp.d[{n1}];')
            elif k1 == 'imm' or ops[1].type == arm.ARM_OP_FP:
                o = ops[1]
                if k0 == 's':
                    cx.emit(f'ctx->vfp.f[{n0}] = {float_lit(o.fp if o.type == arm.ARM_OP_FP else o.imm)}f;')
                elif k0 == 'd' and types == ['f32']:
                    # NEON form: both 32-bit lanes
                    val = float_lit(o.fp if o.type == arm.ARM_OP_FP else o.imm)
                    cx.emit(f'ctx->vfp.f[{2 * n0}] = {val}f; ctx->vfp.f[{2 * n0 + 1}] = {val}f;')
                elif k0 == 'd':
                    cx.emit(f'ctx->vfp.fd[{n0}] = {float_lit(o.fp if o.type == arm.ARM_OP_FP else o.imm)};')
                else:
                    raise Unsupported('vmov imm')
            else:
                raise Unsupported(f'vmov {k0} {k1}')
        elif len(ops) == 3:
            (k0, n0), (k1, n1), (k2, n2) = kinds
            if k0 == 'd' and k1 == 'r' and k2 == 'r':
                cx.emit(f'ctx->vfp.d[{n0}] = (uint64_t({cx.r(n2)}) << 32) | {cx.r(n1)};')
            elif k0 == 'r' and k1 == 'r' and k2 == 'd':
                cx.emit(f'{{ const uint64_t x = ctx->vfp.d[{n2}]; ctx->r[{n0}] = uint32_t(x); ctx->r[{n1}] = uint32_t(x >> 32); }}')
            else:
                raise Unsupported('vmov 3 operands')
        elif len(ops) == 4:
            (k0, n0), (k1, n1), (k2, n2), (k3, n3) = kinds
            if k0 == 's' and k1 == 's' and k2 == 'r' and k3 == 'r':
                cx.emit(f'{{ const uint32_t a = {cx.r(n2)}, b = {cx.r(n3)}; ctx->vfp.s[{n0}] = a; ctx->vfp.s[{n1}] = b; }}')
            elif k0 == 'r' and k1 == 'r' and k2 == 's' and k3 == 's':
                cx.emit(f'{{ const uint32_t a = ctx->vfp.s[{n2}], b = ctx->vfp.s[{n3}]; ctx->r[{n0}] = a; ctx->r[{n1}] = b; }}')
            else:
                raise Unsupported('vmov 4 operands')
        else:
            raise Unsupported('vmov')
        return
    if base == 'vmrs':
        name0 = cs.reg_name(ops[0].reg) if ops[0].type == arm.ARM_OP_REG else ''
        if 'apsr' in cs.op_str.lower() or name0 == 'apsr_nzcv':
            for i, f in enumerate('nzcv'):
                cx.set_flag(f, f'(ctx->fpscr >> {31 - i}) & 1u')
            return
        cx.emit(f'ctx->r[{reg_index(cs, ops[0].reg)}] = ctx->fpscr;')
        return
    if base == 'vmsr':
        # the FPSCR bits ARMv7 VFP implements (dynarmic keeps the same)
        cx.emit(f'ctx->fpscr = {cx.r(reg_index(cs, ops[-1].reg))} & 0xfff7009fu;')
        return
    if base in ('vorr', 'vand', 'veor', 'vbic', 'vorn') and len(ops) == 3:
        kinds = [vreg(cx, o) for o in ops]
        if any(k != 'd' for k, _ in kinds):
            raise Unsupported(f'{base} on q registers')
        (_, d), (_, a), (_, b) = kinds
        op = {'vorr': '|', 'vand': '&', 'veor': '^', 'vbic': '& ~', 'vorn': '| ~'}[base]
        cx.emit(f'ctx->vfp.d[{d}] = ctx->vfp.d[{a}] {op}ctx->vfp.d[{b}];')
        return
    if base == 'vmvn' and len(ops) == 2:
        (k0, d), (k1, a) = vreg(cx, ops[0]), vreg(cx, ops[1])
        if k0 != 'd' or k1 != 'd':
            raise Unsupported('vmvn on q registers')
        cx.emit(f'ctx->vfp.d[{d}] = ~ctx->vfp.d[{a}];')
        return
    if base in ('vsli', 'vsri') and types == ['64']:
        (k0, d), (k1, a) = vreg(cx, ops[0]), vreg(cx, ops[1])
        if k0 != 'd' or k1 != 'd':
            raise Unsupported(f'{base} on q registers')
        n = ops[2].imm
        if base == 'vsli':
            mask = ((1 << 64) - 1) << n & ((1 << 64) - 1)
            cx.emit(f'ctx->vfp.d[{d}] = (ctx->vfp.d[{d}] & 0x{~mask & ((1 << 64) - 1):016x}ull) | ((ctx->vfp.d[{a}] << {n}) & 0x{mask:016x}ull);')
        else:
            mask = ((1 << 64) - 1) >> n
            shifted = f'(ctx->vfp.d[{a}] >> {n})' if n < 64 else '0ull'
            cx.emit(f'ctx->vfp.d[{d}] = (ctx->vfp.d[{d}] & 0x{~mask & ((1 << 64) - 1):016x}ull) | ({shifted} & 0x{mask:016x}ull);')
        return
    if base == 'vceq' and types == ['f32'] and len(ops) == 3:
        (k0, d), (_, a), (_, b) = [vreg(cx, o) for o in ops]
        if k0 != 'd':
            raise Unsupported('vceq on q registers')
        for lane in range(2):
            cx.emit(f'ctx->vfp.s[{2 * d + lane}] = ctx->vfp.f[{2 * a + lane}] == ctx->vfp.f[{2 * b + lane}] ? 0xffffffffu : 0u;')
        return
    if not types:
        raise Unsupported(base)
    dt = types[0]
    if base in ('vadd', 'vsub', 'vmul', 'vdiv', 'vnmul', 'vmla', 'vmls', 'vnmla', 'vnmls', 'vfma', 'vfms', 'vfnma', 'vfnms'):
        if dt not in ('f32', 'f64'):
            raise Unsupported(f'{base}.{dt}')
        k0, n0 = vreg(cx, ops[0])
        if k0 not in ('s', 'd') or (dt == 'f32') != (k0 == 's'):
            raise Unsupported('vfp register kind')
        if len(ops) == 3:
            a, b = fexpr(k0, vreg(cx, ops[1])[1]), fexpr(k0, vreg(cx, ops[2])[1])
        else:
            a, b = fexpr(k0, n0), fexpr(k0, vreg(cx, ops[1])[1])
        d = fexpr(k0, n0)
        ty = 'float' if k0 == 's' else 'double'
        # same structure as ARM's pseudocode (and dynarmic): FPAdd(d, FPNeg(FPMul(n, m))) etc. The
        # fp_* helpers apply ARM's NaN processing (signalling NaNs first, default NaN for invalid
        # operations); fneg() is FPNeg, a sign-bit flip that also applies to NaNs.
        three = base not in ('vadd', 'vsub', 'vmul', 'vdiv', 'vnmul')
        A, B, D = 'in0', 'in1', 'in2'
        p = f'fp_mul({A}, {B})'
        expr = {'vadd': f'fp_add({A}, {B})', 'vsub': f'fp_sub({A}, {B})', 'vmul': p, 'vdiv': f'fp_div(ctx, {A}, {B})',
                'vnmul': f'fneg({p})',
                'vmla': f'fp_add({D}, {p})', 'vmls': f'fp_add({D}, fneg({p}))',
                'vnmla': f'fp_add(fneg({D}), fneg({p}))', 'vnmls': f'fp_add(fneg({D}), {p})'}.get(base)
        if expr is None:
            raise Unsupported(f'{base} (fused multiply-add)')
        cx.emit('{')
        cx.emit(f'const {ty} in0 = {a}, in1 = {b}' + (f', in2 = {d};' if three else ';'))
        cx.emit(f'{d} = {expr};')
        cx.emit('}')
        return
    if base in ('vneg', 'vabs', 'vsqrt'):
        k0, n0 = vreg(cx, ops[0])
        k1, n1 = vreg(cx, ops[1])
        if k0 == 's':
            if base == 'vneg':
                cx.emit(f'ctx->vfp.s[{n0}] = ctx->vfp.s[{n1}] ^ 0x80000000u;')
            elif base == 'vabs':
                cx.emit(f'ctx->vfp.s[{n0}] = ctx->vfp.s[{n1}] & 0x7fffffffu;')
            else:
                cx.emit(f'ctx->vfp.f[{n0}] = process_nans(std::sqrt(ctx->vfp.f[{n1}]), ctx->vfp.f[{n1}], ctx->vfp.f[{n1}]);')
        elif k0 == 'd':
            if base == 'vneg':
                cx.emit(f'ctx->vfp.d[{n0}] = ctx->vfp.d[{n1}] ^ 0x8000000000000000ull;')
            elif base == 'vabs':
                cx.emit(f'ctx->vfp.d[{n0}] = ctx->vfp.d[{n1}] & 0x7fffffffffffffffull;')
            else:
                cx.emit(f'ctx->vfp.fd[{n0}] = process_nans(std::sqrt(ctx->vfp.fd[{n1}]), ctx->vfp.fd[{n1}], ctx->vfp.fd[{n1}]);')
        else:
            raise Unsupported(base)
        return
    if base in ('vcmp', 'vcmpe'):
        k0, n0 = vreg(cx, ops[0])
        a = fexpr(k0, n0)
        if len(ops) == 1 or ops[1].type != arm.ARM_OP_REG:
            b = '0.0f' if k0 == 's' else '0.0'
        else:
            b = fexpr(k0, vreg(cx, ops[1])[1])
        cx.emit(f'ctx->fpscr = (ctx->fpscr & 0x0fffffffu) | vfp_compare({a}, {b});')
        return
    if base == 'vcvt':
        convert(cx, types, ops)
        return
    if base in ('vcvtb', 'vcvtt') and types == ['f32', 'f16']:
        # half precision in the bottom (vcvtb) or top (vcvtt) half of s<m> -> single
        (_, n0), (_, n1) = vreg(cx, ops[0]), vreg(cx, ops[1])
        shift = 0 if base == 'vcvtb' else 16
        cx.emit(f'ctx->vfp.s[{n0}] = half_to_single(uint16_t(ctx->vfp.s[{n1}] >> {shift}));')
        return
    if base in ('vcvtb', 'vcvtt') and types == ['f16', 'f32']:
        # single s<m> -> half precision into the bottom (vcvtb) or top (vcvtt) half of s<d>; the
        # other half of s<d> is kept
        (_, n0), (_, n1) = vreg(cx, ops[0]), vreg(cx, ops[1])
        if base == 'vcvtb':
            cx.emit(f'ctx->vfp.s[{n0}] = (ctx->vfp.s[{n0}] & 0xffff0000u) | single_to_half(ctx->vfp.s[{n1}], ctx->fpscr);')
        else:
            cx.emit(f'ctx->vfp.s[{n0}] = (ctx->vfp.s[{n0}] & 0x0000ffffu) | (uint32_t(single_to_half(ctx->vfp.s[{n1}], ctx->fpscr)) << 16);')
        return
    raise Unsupported(base)


def float_lit(v):
    return repr(float(v))


def convert(cx, types, ops):
    """vcvt.<to>.<from>, and the single-precision fixed-point forms (a third operand: fraction bits)"""
    to, frm = types[0], types[1]
    if len(ops) == 3:
        # VCVT between single precision and fixed point, in place: to fixed rounds towards zero and
        # saturates (16-bit results extended to the register); from fixed rounds once, as FPSCR says
        k, n = vreg(cx, ops[0])
        if k != 's' or ops[2].type != arm.ARM_OP_IMM:
            raise Unsupported(f'vcvt.{to}.{frm} fixed point')
        scale = float_lit(2.0 ** ops[2].imm)
        if frm == 'f32' and to in ('s32', 'u32', 's16', 'u16'):
            x = f'double(ctx->vfp.f[{n}]) * {scale}'
            expr = {
                's32': f'vfp_to_s32({x})',
                'u32': f'vfp_to_u32({x})',
                's16': f'uint32_t(int32_t(std::clamp<int32_t>(int32_t(vfp_to_s32({x})), -32768, 32767)))',
                'u16': f'std::min<uint32_t>(vfp_to_u32({x}), 0xffffu)',
            }[to]
            cx.emit(f'ctx->vfp.s[{n}] = {expr};')
        elif to == 'f32' and frm in ('s32', 'u32', 's16', 'u16'):
            src = {'s32': f'int32_t(ctx->vfp.s[{n}])', 'u32': f'ctx->vfp.s[{n}]',
                   's16': f'int16_t(ctx->vfp.s[{n}])', 'u16': f'uint16_t(ctx->vfp.s[{n}])'}[frm]
            cx.emit(f'ctx->vfp.f[{n}] = float(double({src}) / {scale});')
        else:
            raise Unsupported(f'vcvt.{to}.{frm} fixed point')
        return
    if len(ops) != 2:
        raise Unsupported('vcvt form')
    k0, n0 = vreg(cx, ops[0])
    k1, n1 = vreg(cx, ops[1])
    if to == 'f64' and frm == 'f32':
        cx.emit(f'ctx->vfp.fd[{n0}] = double(ctx->vfp.f[{n1}]);')
    elif to == 'f32' and frm == 'f64':
        cx.emit(f'ctx->vfp.f[{n0}] = float(ctx->vfp.fd[{n1}]);')
    elif to in ('f32', 'f64') and frm in ('s32', 'u32'):
        src = f'int32_t(ctx->vfp.s[{n1}])' if frm == 's32' else f'ctx->vfp.s[{n1}]'
        cx.emit(f'{fexpr(k0, n0)} = {"float" if to == "f32" else "double"}({src});')
    elif to in ('s32', 'u32') and frm in ('f32', 'f64'):
        fn = 'vfp_to_s32' if to == 's32' else 'vfp_to_u32'
        cx.emit(f'ctx->vfp.s[{n0}] = {fn}({fexpr(k1, n1)});')
    else:
        raise Unsupported(f'vcvt.{to}.{frm}')


# --- entry point ------------------------------------------------------------------------------

def translate(ins, thumb, imports, func_start, func_end, jumptables, patched=frozenset()):
    """patched: addresses of instructions the loader rewrites (variable imports); their
    immediates are read from guest memory at run time"""
    cx = Ctx(ins, thumb)
    cx.patched = ins.address in patched
    t = cx.t
    cs = cx.cs
    m = ins.mnemonic
    ops = cs.operands

    # flag-setting: Capstone decodes one instruction at a time, so inside an IT block it reports
    # the flag-setting form for 16-bit encodings that do not set flags there
    # Capstone's update_flags is unreliable (it reports True for adc.w with S clear), so the
    # mnemonic decides: an S suffix, or a compare
    s = m in ('cmp', 'cmn', 'tst', 'teq') or (m.endswith('s') and m[:-1] in DP_FLAGSET)
    if ins.it_cond and ins.size == 2 and m not in ('cmp', 'cmn', 'tst', 'teq'):
        s = False
    base = m
    if s and m.endswith('s') and m[:-1] in DP_FLAGSET:
        base = m[:-1]
    elif m.endswith('s') and m[:-1] in DP_FLAGSET and ins.size == 2:
        base = m[:-1]  # 16-bit form inside an IT block

    try:
        if m.startswith('it') and len(m) <= 5 and set(m[1:]) <= {'t', 'e'}:
            t.kind = 'it'
        elif m in ('dmb', 'dsb'):
            # other threads (native or on the JIT) see memory in program order around barriers
            cx.emit('std::atomic_thread_fence(std::memory_order_seq_cst);')
        elif m in ('nop', 'yield', 'isb', 'pld', 'pli', 'sev', 'wfe', 'wfi'):
            t.kind = 'nop'
        elif m in ('udf', 'bkpt'):
            t.kind = 'trap' if m == 'udf' else 'nop'
        elif m == 'b' or (m[0] == 'b' and m[1:] in COND_EXPR and len(m) == 3):
            target = ops[0].imm & 0xffffffff
            t.target = target
            cond = CC_NAME.get(cs.cc)
            if cond:
                t.kind = 'cond_branch'
                t.cond = COND_EXPR[cond]
                t.flags_read |= set(COND_FLAGS[cond])
            else:
                t.kind = 'branch'
        elif m in ('cbz', 'cbnz'):
            r = cx.r(ops[0])
            t.kind = 'cond_branch'
            t.target = ops[1].imm & 0xffffffff
            t.cond = f'{r} == 0' if m == 'cbz' else f'{r} != 0'
        elif m in ('bl', 'blx') and ops[0].type == arm.ARM_OP_IMM:
            target = ops[0].imm & 0xffffffff
            t.target = target
            ret = (ins.address + ins.size) | (1 if thumb else 0)
            cx.emit(f'ctx->r[14] = 0x{ret:08x}u;')
            if (target & ~1) in imports:
                t.kind = 'call_import'
            else:
                t.kind = 'call'
                t.target = target if m == 'bl' else target  # mode switch handled by the callee's mode
        elif m in ('blx', 'bx'):
            r = cx.ridx(ops[0])
            if m == 'bx' and r == 14:
                t.kind = 'return'
                t.writes_pc_via = 'lr'
            elif m == 'blx':
                ret = (ins.address + ins.size) | (1 if thumb else 0)
                cx.emit(f'{{ const uint32_t target = {cx.r(r)};')
                cx.emit(f'ctx->r[14] = 0x{ret:08x}u;')
                cx.emit('call_indirect(ctx, target); }')
                t.kind = 'call_indirect'
            else:
                cx.emit(f'ctx->r[15] = {cx.r(r)};')
                t.kind = 'jump_indirect'
        elif m in ('tbb', 'tbh'):
            mop = ops[0]
            idx = cx.r(reg_index(cs, mop.mem.index))
            t.kind = 'jumptable'
            t.jumptable = (idx, jumptables.get(ins.address, []))
        elif thumb and m == 'ldr' and ins.address in jumptables and ops[0].type == arm.ARM_OP_REG and \
                cx.ridx(ops[0]) == 15 and ops[1].type == arm.ARM_OP_MEM and ops[1].mem.index:
            # ldr.w pc, [rB, rI, lsl #2] over a table of case addresses (the inventory's ldr_table)
            idx = cx.r(reg_index(cs, ops[1].mem.index))
            t.kind = 'jumptable'
            t.jumptable = (idx, jumptables[ins.address])
        elif thumb and m == 'mov' and ins.address in jumptables and len(ops) == 2 and ops[0].type == arm.ARM_OP_REG and \
                cx.ridx(ops[0]) == 15 and ops[1].type == arm.ARM_OP_REG and len(jumptables[ins.address]) > 1:
            # mov pc, rX into evenly spaced steps (the inventory's stride_table): a switch over the steps
            targets = jumptables[ins.address]
            stride = targets[1] - targets[0]
            idx = f'((ctx->r[{cx.ridx(ops[1])}] - 0x{targets[0]:08x}u) / {stride}u)'
            t.kind = 'jumptable'
            t.jumptable = (idx, targets)
        elif base in ('mov', 'mvn', 'add', 'adc', 'sub', 'sbc', 'rsb', 'rsc', 'and', 'orr', 'eor', 'bic',
                      'orn', 'cmp', 'cmn', 'tst', 'teq'):
            ops2 = ops
            if base in ('lsl', 'lsr', 'asr', 'ror'):
                pass
            if base == 'mov' and len(ops) == 2 and ops[1].type == arm.ARM_OP_REG and \
                    ops[1].shift.type in (arm.ARM_SFT_ASR_REG, arm.ARM_SFT_LSL_REG, arm.ARM_SFT_LSR_REG, arm.ARM_SFT_ROR_REG):
                raise Unsupported('mov with register shift')
            if base in ('add', 'sub') and not s and len(ops) == 3 and ops[1].type == arm.ARM_OP_REG and \
                    cx.ridx(ops[1]) == 15 and ops[2].type == arm.ARM_OP_IMM:
                # add rd, pc, #imm (adr): Align(PC, 4)
                d = cx.ridx(ops[0])
                sign = 1 if base == 'add' else -1
                cx.emit(f'ctx->r[{d}] = 0x{((cx.pc & ~3) + sign * ops[2].imm) & 0xffffffff:08x}u;')
            else:
                dp(cx, base, s)
        elif base in ('neg',):
            # neg rd, rm = rsbs rd, rm, #0 (old syntax)
            raise Unsupported('neg')
        elif m in ('movw', 'movt', 'addw', 'subw', 'adr'):
            dp_wide_imm(cx, m)
        elif base in ('lsl', 'lsr', 'asr', 'ror'):
            shift_insn(cx, base, s)
        elif base == 'rrx':
            d = cx.ridx(ops[0])
            cx.emit('{')
            cx.emit(f'const uint32_t x = {cx.r(ops[1])};')
            cx.emit(f'const uint32_t res = ({cx.read_flag("c")} << 31) | (x >> 1);')
            if s:
                cx.set_nz('res')
                cx.set_flag('c', 'x & 1u')
            cx.emit(f'ctx->r[{d}] = res;')
            cx.emit('}')
        elif base in ('mul', 'mla', 'mls', 'umull', 'smull', 'umlal', 'smlal', 'sdiv', 'udiv',
                      'smulbb', 'smulbt', 'smultb', 'smultt', 'smlabb', 'smlabt', 'smlatb', 'smlatt',
                      'smmul', 'smmla', 'smmls'):
            multiply(cx, base, s)
        elif m in ('ubfx', 'sbfx', 'bfi', 'bfc', 'uxtb', 'uxth', 'sxtb', 'sxth', 'uxtab', 'uxtah', 'sxtab',
                   'sxtah', 'clz', 'rev', 'rev16', 'revsh', 'rbit', 'usat', 'ssat'):
            bitfield(cx, m)
        elif m in ('ldr', 'ldrb', 'ldrh', 'ldrsb', 'ldrsh', 'str', 'strb', 'strh', 'ldrd', 'strd'):
            load_store(cx, m)
        elif m in ('push', 'pop', 'ldm', 'ldmia', 'ldmdb', 'stm', 'stmia', 'stmdb', 'stmfd', 'ldmfd'):
            load_store_multiple(cx, m)
        elif m in ('ldrex', 'ldrexb', 'ldrexh', 'strex', 'strexb', 'strexh', 'ldrexd', 'strexd', 'clrex'):
            exclusive(cx, m)
        elif m == 'mrc':
            # mrc p15, 0, rd, c13, c0, 3: TPIDRURO
            vals = [o.imm if o.type == arm.ARM_OP_IMM else None for o in ops]
            if 'c13, c0, #3' in cs.op_str and 'p15' in cs.op_str:
                cx.emit(f'ctx->r[{cx.ridx(ops[2])}] = ctx->tpidruro;')
            else:
                raise Unsupported(f'mrc {cs.op_str}')
        elif m == 'mcr':
            # mcr p15, 0, rd, c13, c0, 3: write TPIDRURO (kernel modules: the firmware's sysmodule)
            if 'c13, c0, #3' in cs.op_str and 'p15' in cs.op_str:
                cx.emit(f'ctx->tpidruro = {cx.r(cx.ridx(ops[2]))};')
            else:
                raise Unsupported(f'mcr {cs.op_str}')
        elif m.startswith('v'):
            if neon.is_neon(cx):
                neon.translate(cx)
            else:
                vfp(cx, m)
        else:
            raise Unsupported(m)
    except Exception as e:  # Unsupported, or a decoder shape this code does not expect yet
        if not isinstance(e, Unsupported):
            e = f'internal {type(e).__name__}: {e}'
        t.kind = 'unsupported'
        t.stmts = [f'unsupported(ctx, 0x{ins.address:08x}u); // {cs.mnemonic} {cs.op_str}: {e}']
        t.flags_read, t.flags_written = set(), set()
    if ins.it_cond:
        t.flags_read |= set(COND_FLAGS[ins.it_cond])
    return t
