"""Advanced SIMD (NEON) instructions -> C++ calls into recomp/runtime/neon.h.

Every instruction becomes one statement block that reads its source registers whole, computes and
writes the destination, so overlapping operands behave as on hardware. Element types come from
the mnemonic suffix (vadd.f32, vshr.s32, vld1.8 ...); q<n> is passed as d register 2n.
"""
import re

from capstone import arm

FLOAT = {'f32': 'float'}
INT = {'s8': 'int8_t', 's16': 'int16_t', 's32': 'int32_t', 's64': 'int64_t',
       'u8': 'uint8_t', 'u16': 'uint16_t', 'u32': 'uint32_t', 'u64': 'uint64_t',
       'i8': 'uint8_t', 'i16': 'uint16_t', 'i32': 'uint32_t', 'i64': 'uint64_t',
       '8': 'uint8_t', '16': 'uint16_t', '32': 'uint32_t', '64': 'uint64_t'}
SIZE = {'int8_t': 1, 'uint8_t': 1, 'int16_t': 2, 'uint16_t': 2, 'int32_t': 4, 'uint32_t': 4,
        'int64_t': 8, 'uint64_t': 8, 'float': 4}
SIGNED_OF = {'uint8_t': 'int8_t', 'uint16_t': 'int16_t', 'uint32_t': 'int32_t', 'uint64_t': 'int64_t'}
UNSIGNED_OF = {v: k for k, v in SIGNED_OF.items()}
WIDER = {'int8_t': 'int16_t', 'int16_t': 'int32_t', 'int32_t': 'int64_t',
         'uint8_t': 'uint16_t', 'uint16_t': 'uint32_t', 'uint32_t': 'uint64_t'}
NARROWER = {v: k for k, v in WIDER.items()}

NEON_ONLY = {'vdup', 'vbif', 'vbit', 'vbsl', 'vext', 'vrev64', 'vrev32', 'vrev16', 'vtrn', 'vzip', 'vuzp',
             'vtbl', 'vtbx', 'vld1', 'vst1', 'vld2', 'vst2', 'vld3', 'vst3', 'vld4', 'vst4', 'vshll', 'vpadd', 'vpmax', 'vpmin', 'vmull', 'vmlal', 'vmlsl', 'vmovn',
             'vqmovn', 'vqmovun', 'vmovl', 'vaddw', 'vsubw', 'vaddl', 'vsubl', 'vshrn', 'vqshrn', 'vqshrun',
             'vrshrn', 'vshr', 'vsra', 'vshl', 'vqadd', 'vqsub', 'vqabs', 'vqneg', 'vtst', 'vceq', 'vcge',
             'vcgt', 'vcle', 'vclt', 'vacgt', 'vacge', 'vrecpe', 'vrecps', 'vrsqrte', 'vrsqrts', 'vmax',
             'vmin', 'vswp', 'vcnt', 'vclz', 'vpaddl', 'vsli', 'vsri'}


class Unsupported(Exception):
    pass


def is_neon(cx):
    """True for Advanced SIMD instructions (as opposed to VFP ones the scalar code handles)."""
    cs = cx.cs
    parts = cx.ins.full.split('.')
    base, types = parts[0], parts[1:]
    names = [cs.reg_name(o.reg) for o in cs.operands if o.type == arm.ARM_OP_REG]
    if any(n.startswith('q') for n in names):
        return True
    if base in NEON_ONLY:
        return True
    if base == 'vmov' and types and types[0] in ('u8', 's8', 'u16', 's16', '8', '16'):
        return True  # lane moves narrower than 32 bits
    if base in ('vmov', 'vmvn') and types and types[0] in ('i8', 'i16', 'i32', 'i64'):
        return True  # immediate forms on d registers
    if types and (types[0] == 'f32' or types[0] in INT) and names and all(n.startswith('d') for n in names) \
            and base not in ('vmov', 'vldr', 'vstr', 'vcvtb', 'vcvtt') and 'f64' not in types:
        return True  # f32 / integer arithmetic on d registers is the 2-lane (or 8-byte) SIMD form
    return False


def is_core(cx, op):
    from . import semantics as S
    return cx.cs.reg_name(op.reg) in S.REGS


def reg(cx, op):
    """(d index, bytes, lane or None) of a d/q operand"""
    name = cx.cs.reg_name(op.reg)
    if name[0] == 'q':
        return 2 * int(name[1:]), 16, None
    if name[0] == 'd':
        lane = op.vector_index if op.vector_index >= 0 else None
        return int(name[1:]), 8, lane
    raise Unsupported(f'operand {name}')


def ctype(t):
    if t in FLOAT:
        return FLOAT[t]
    if t in INT:
        return INT[t]
    raise Unsupported(f'type {t}')


def translate(cx):
    cs = cx.cs
    parts = cx.ins.full.split('.')
    base, types = parts[0], parts[1:]
    ops = cs.operands
    emit = cx.emit

    def lanes(t, nbytes):
        return nbytes // SIZE[t]

    def regs():
        return [reg(cx, o) for o in ops if o.type == arm.ARM_OP_REG]

    t0 = types[0] if types else None

    # --- loads and stores ---
    if base in ('vld1', 'vst1', 'vld2', 'vst2', 'vld3', 'vst3', 'vld4', 'vst4'):
        return load_store(cx, base, t0)

    # --- moves ---
    if base in ('vmov', 'vmvn') and len(ops) == 2 and ops[1].type in (arm.ARM_OP_IMM, arm.ARM_OP_FP):
        d, nb, _ = reg(cx, ops[0])
        if ops[1].type == arm.ARM_OP_FP:
            import struct
            bits = struct.unpack('<I', struct.pack('<f', ops[1].fp))[0]
            t, val = 'uint32_t', bits
        else:
            t = ctype(t0)
            val = ops[1].imm & ((1 << (8 * SIZE[t])) - 1)
            if base == 'vmvn':
                val = ~val & ((1 << (8 * SIZE[t])) - 1)
            elif t0 == 'i64':
                # each bit of the encoded imm8 (i:imm3:imm4) is a whole byte; Capstone's value is
                # ambiguous, so decode the instruction. i is bit 28 of the Thumb-2 word (the
                # halfwords in order), bit 24 of the ARM one.
                b = bytes(cs.bytes)
                if cx.thumb:
                    word = (b[0] | b[1] << 8) << 16 | (b[2] | b[3] << 8)
                    i_bit = word >> 28 & 1
                else:
                    word = int.from_bytes(b, 'little')
                    i_bit = word >> 24 & 1
                imm8 = i_bit << 7 | (word >> 16 & 7) << 4 | (word & 0xf)
                val = sum(0xff << (8 * k) for k in range(8) if imm8 >> k & 1)
        emit(f'vdup<{t}, {lanes(t, nb)}>(ctx, {d}, {t}(0x{val:x}ull));')
        return
    if base == 'vmov' and len(ops) == 2 and ops[1].type == arm.ARM_OP_REG and is_core(cx, ops[1]):
        # vmov.8/16 dn[x], rt: the low bits of rt into one lane
        d, _, lane = reg(cx, ops[0])
        if lane is None:
            raise Unsupported('vmov core to d without a lane')
        t = ctype(t0)
        emit(f'vsetlane<{t}>(ctx, {d}, {lane}, {t}({cx.r(ops[1])}));')
        return
    if base in ('vbic', 'vorr') and len(ops) == 2 and ops[1].type == arm.ARM_OP_IMM:
        # vbic/vorr.i16/i32 dn, #imm: every lane with the (already expanded) immediate
        d, nb, _ = reg(cx, ops[0])
        t = ctype(t0)
        val = ops[1].imm & ((1 << (8 * SIZE[t])) - 1)
        f = 'IBic' if base == 'vbic' else 'IOrr'
        emit(f'map1<{t}, {t}, {lanes(t, nb)}>(ctx, {d}, {d}, WithImm<{f}, {t}>{{{t}(0x{val:x}u)}});')
        return
    if base == 'vmov' and len(ops) == 2 and ops[0].type == arm.ARM_OP_REG and is_core(cx, ops[0]):
        # vmov.u16/s16/u8/s8 rt, dn[x]
        d, _, lane = reg(cx, ops[1])
        t = ctype(t0)
        cast = 'int32_t' if t.startswith('int') else 'uint32_t'
        emit(f'ctx->r[{cx.ridx(ops[0])}] = uint32_t({cast}(vlane<{t}>(ctx, {d}, {lane})));')
        return
    if base == 'vmov' and len(ops) == 2:
        (d, nb, _), (m, _, _) = regs()
        emit(f'map1<uint64_t, uint64_t, {nb // 8}>(ctx, {d}, {m}, [](uint64_t a, bool &) {{ return a; }});')
        return
    if base == 'vdup':
        d, nb, _ = reg(cx, ops[0])
        t = INT[t0]
        src = ops[1]
        name = cs.reg_name(src.reg)
        if name[0] == 'd':
            m, _, lane = reg(cx, src)
            emit(f'vdup<{t}, {lanes(t, nb)}>(ctx, {d}, vlane<{t}>(ctx, {m}, {lane}));')
        else:
            emit(f'vdup<{t}, {lanes(t, nb)}>(ctx, {d}, {t}({cx.r(src)}));')
        return

    if base == 'vswp':
        (d, nb, _), (m, _, _) = regs()
        emit(f'vswp<{nb // 8}>(ctx, {d}, {m});')
        return

    # --- permutations ---
    if base == 'vext':
        (d, nb, _), (n, _, _), (m, _, _) = regs()
        esize = SIZE[INT[t0]]
        emit(f'vext<{nb}>(ctx, {d}, {n}, {m}, {ops[3].imm * esize}u);')
        return
    if base == 'vrev64':
        (d, nb, _), (m, _, _) = regs()
        emit(f'vrev64<{INT[t0]}, {nb // 8}>(ctx, {d}, {m});')
        return
    if base in ('vrev32', 'vrev16'):
        (d, nb, _), (m, _, _) = regs()
        emit(f'vrev<{INT[t0]}, {int(base[4:]) // 8}, {nb // 8}>(ctx, {d}, {m});')
        return
    if base in ('vtrn', 'vzip'):
        (d, nb, _), (m, _, _) = regs()
        t = INT[t0]
        emit(f'{base}<{t}, {lanes(t, nb)}>(ctx, {d}, {m});')
        return
    if base in ('vtbl', 'vtbx'):
        rs = regs()
        d, n, m = rs[0][0], rs[1][0], rs[-1][0]
        emit(f'{base}(ctx, {d}, {n}, {len(rs) - 2}u, {m});')
        return

    # --- bitwise (no element type) ---
    BITWISE = {'vand': 'IAnd', 'vorr': 'IOrr', 'veor': 'IEor', 'vbic': 'IBic', 'vorn': 'IOrn'}
    if base in BITWISE and len(ops) == 3 and all(o.type == arm.ARM_OP_REG for o in ops):
        (d, nb, _), (n, _, _), (m, _, _) = regs()
        emit(f'map2<uint64_t, uint64_t, {nb // 8}>(ctx, {d}, {n}, {m}, {BITWISE[base]}{{}});')
        return
    if base in ('vbsl', 'vbit', 'vbif'):
        (d, nb, _), (n, _, _), (m, _, _) = regs()
        emit(f'map3<uint64_t, {nb // 8}>(ctx, {d}, {n}, {m}, I{base[1:].capitalize()}{{}});')
        return
    if base == 'vmvn' and len(ops) == 2:
        (d, nb, _), (m, _, _) = regs()
        emit(f'map1<uint64_t, uint64_t, {nb // 8}>(ctx, {d}, {m}, IMvn{{}});')
        return

    # --- floating point ---
    if t0 == 'f32' and base not in ('vcvt',):
        return float_op(cx, base, ops)
    if base == 'vcvt':
        (d, nb, _), (m, _, _) = regs()
        if len(ops) != 2:
            raise Unsupported('vcvt with fraction bits')
        n = nb // 4
        conv = {('f32', 's32'): ('int32_t', 'float', 'S32ToF'), ('f32', 'u32'): ('uint32_t', 'float', 'U32ToF'),
                ('s32', 'f32'): ('float', 'int32_t', 'FToS32'), ('u32', 'f32'): ('float', 'uint32_t', 'FToU32')}
        if (types[0], types[1]) == ('f32', 'f16'):
            # 4 halves of d<m> -> 4 singles in q<d>
            emit(f'map1<uint16_t, uint32_t, 4>(ctx, {d}, {m}, H2F{{}});')
            return
        if (types[0], types[1]) == ('f16', 'f32'):
            # 4 singles of q<m> -> 4 halves in d<d>
            emit(f'map1<uint32_t, uint16_t, 4>(ctx, {d}, {m}, F2H{{}});')
            return
        if (types[0], types[1]) not in conv:
            raise Unsupported(f'vcvt.{".".join(types)}')
        src, dst, f = conv[(types[0], types[1])]
        emit(f'map1<{src}, {dst}, {n}>(ctx, {d}, {m}, {f}{{}});')
        return

    # --- integer ---
    return int_op(cx, base, types, ops)


def float_op(cx, base, ops):
    emit = cx.emit
    rs = [reg(cx, o) for o in ops if o.type == arm.ARM_OP_REG]
    d, nb, _ = rs[0]
    n_lanes = nb // 4
    BIN = {'vadd': 'FAdd', 'vsub': 'FSub', 'vmul': 'FMul', 'vmax': 'FMax', 'vmin': 'FMin',
           'vrecps': 'FRecps', 'vrsqrts': 'FRsqrts'}
    CMP = {'vceq': 'FCeq', 'vcge': 'FCge', 'vcgt': 'FCgt', 'vacge': 'FAcge', 'vacgt': 'FAcgt'}
    if base in BIN and len(rs) == 3:
        n, m, lane = rs[1][0], rs[2][0], rs[2][2]
        if lane is not None:
            emit(f'map2s<float, {n_lanes}>(ctx, {d}, {n}, {m}, {lane}, {BIN[base]}{{}});')
        else:
            emit(f'map2<float, float, {n_lanes}>(ctx, {d}, {n}, {m}, {BIN[base]}{{}});')
        return
    if base in ('vmla', 'vmls') and len(rs) == 3:
        n, m, lane = rs[1][0], rs[2][0], rs[2][2]
        f = 'FMla' if base == 'vmla' else 'FMls'
        if lane is not None:
            emit(f'map3s<float, {n_lanes}>(ctx, {d}, {n}, {m}, {lane}, {f}{{}});')
        else:
            emit(f'map3<float, {n_lanes}>(ctx, {d}, {n}, {m}, {f}{{}});')
        return
    if base in CMP or base in ('vcle', 'vclt'):
        if len(rs) == 2 and ops[-1].type in (arm.ARM_OP_IMM, arm.ARM_OP_FP):
            m = rs[1][0]
            if base == 'vcle':
                f = 'ZeroLeft<FCge>{}'
            elif base == 'vclt':
                f = 'ZeroLeft<FCgt>{}'
            else:
                f = f'ZeroRight<{CMP[base]}>{{}}'
            emit(f'map1<float, uint32_t, {n_lanes}>(ctx, {d}, {m}, {f});')
            return
        if base in CMP and len(rs) == 3:
            emit(f'map2<float, uint32_t, {n_lanes}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, {CMP[base]}{{}});')
            return
    if base in ('vneg', 'vabs') and len(rs) == 2:
        emit(f'map1<uint32_t, uint32_t, {n_lanes}>(ctx, {d}, {rs[1][0]}, F{base[1:].capitalize()}{{}});')
        return
    if base in ('vrecpe', 'vrsqrte') and len(rs) == 2:
        f = 'FRecpe' if base == 'vrecpe' else 'FRsqrte'
        emit(f'map1<uint32_t, uint32_t, {n_lanes}>(ctx, {d}, {rs[1][0]}, {f}{{ctx}});')
        return
    if base in ('vpadd', 'vpmax', 'vpmin') and len(rs) == 3:
        f = {'vpadd': 'FAdd', 'vpmax': 'FMax', 'vpmin': 'FMin'}[base]
        emit(f'pairwise<float, {n_lanes}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, {f}{{}});')
        return
    raise Unsupported(f'{base}.f32 form')


def int_op(cx, base, types, ops):
    emit = cx.emit
    rs = [reg(cx, o) for o in ops if o.type == arm.ARM_OP_REG]
    imm = ops[-1].imm if ops and ops[-1].type == arm.ARM_OP_IMM else None
    t0 = types[0] if types else None
    t = ctype(t0) if t0 else 'uint64_t'
    d, nb, _ = rs[0]
    n = nb // SIZE[t]
    BIN = {'vadd': 'IAdd', 'vsub': 'ISub', 'vmul': 'IMul', 'vmax': 'IMax', 'vmin': 'IMin', 'vtst': 'ITst',
           'vqadd': 'IQAdd', 'vqsub': 'IQSub'}
    CMP = {'vceq': 'ICeq', 'vcge': 'ICge', 'vcgt': 'ICgt'}
    # vceq.i32 compares bit patterns; vcge/vcgt.sNN/uNN by sign
    if base in BIN and len(rs) == 3 and imm is None:
        tt = SIGNED_OF.get(t, t) if base in ('vqadd', 'vqsub') and t0.startswith('s') else t
        lane = rs[2][2]
        if lane is not None:
            emit(f'map2s<{tt}, {n}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, {lane}, {BIN[base]}{{}});')
        else:
            emit(f'map2<{tt}, {tt}, {n}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, {BIN[base]}{{}});')
        return
    if base in ('vmla', 'vmls') and len(rs) == 3:
        f = 'IMla' if base == 'vmla' else 'IMls'
        lane = rs[2][2]
        if lane is not None:
            emit(f'map3s<{t}, {n}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, {lane}, {f}{{}});')
        else:
            emit(f'map3<{t}, {n}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, {f}{{}});')
        return
    if base in CMP or base in ('vcle', 'vclt'):
        if len(rs) == 2 and imm is not None:
            if base == 'vcle':
                f = 'ZeroLeft<ICge>{}'
            elif base == 'vclt':
                f = 'ZeroLeft<ICgt>{}'
            else:
                f = f'ZeroRight<{CMP[base]}>{{}}'
            emit(f'map1<{t}, {t}, {n}>(ctx, {d}, {rs[1][0]}, {f});')
            return
        if base in CMP and len(rs) == 3:
            emit(f'map2<{t}, {t}, {n}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, {CMP[base]}{{}});')
            return
    if base in ('vneg', 'vabs', 'vqabs') and len(rs) == 2:
        f = {'vneg': 'INeg', 'vabs': 'IAbs', 'vqabs': 'IQAbs'}[base]
        emit(f'map1<{t}, {t}, {n}>(ctx, {d}, {rs[1][0]}, {f}{{}});')
        return
    if base == 'vpadd' or base in ('vpmax', 'vpmin'):
        f = {'vpadd': 'IAdd', 'vpmax': 'IMax', 'vpmin': 'IMin'}[base]
        emit(f'pairwise<{t}, {n}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, {f}{{}});')
        return
    if base in ('vcnt', 'vclz') and len(rs) == 2:
        emit(f'map1<{t}, {t}, {n}>(ctx, {d}, {rs[1][0]}, {"ICnt" if base == "vcnt" else "IClz"}{{}});')
        return
    if base == 'vpaddl' and len(rs) == 2:
        tw = WIDER[t]
        emit(f'paddl<{t}, {tw}, {nb // SIZE[tw]}>(ctx, {d}, {rs[1][0]});')
        return
    if base in ('vsli', 'vsri') and imm is not None:
        f = 'ISliImm' if base == 'vsli' else 'ISriImm'
        emit(f'map3<{t}, {n}>(ctx, {d}, {rs[1][0]}, {rs[1][0]}, {f}{{{imm}u}});')
        return
    if base == 'vshr' and imm is not None:
        emit(f'map1<{t}, {t}, {n}>(ctx, {d}, {rs[1][0]}, IShrImm{{{imm}u}});')
        return
    if base == 'vsra' and imm is not None:
        emit(f'map3<{t}, {n}>(ctx, {d}, {rs[1][0]}, {rs[1][0]}, ISraImm{{{imm}u}});')
        return
    if base == 'vshl':
        if imm is not None:
            emit(f'map1<{t}, {t}, {n}>(ctx, {d}, {rs[1][0]}, IShlImm{{{imm}u}});')
        else:
            emit(f'map2<{t}, {t}, {n}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, IShlReg{{}});')
        return
    # long, wide, narrow
    if base == 'vshll' and imm is not None:
        # vshll.sN/uN (shift below N) and vshll.iN #N: widen, then shift left
        if t0.startswith('i'):
            t = UNSIGNED_OF.get(t, t)
        tw = WIDER[t]
        emit(f'shll<{t}, {tw}, {8 // SIZE[t]}>(ctx, {d}, {rs[1][0]}, {imm}u);')
        return
    if base in ('vmull', 'vmlal', 'vaddl', 'vsubl'):
        tw = WIDER[t]
        ln = 8 // SIZE[t]
        if base == 'vmull':
            emit(f'long2<{t}, {tw}, {ln}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, IMul{{}});')
        elif base == 'vmlal':
            emit(f'long3<{t}, {tw}, {ln}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, IMla{{}});')
        else:
            emit(f'long2<{t}, {tw}, {ln}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, {"IAdd" if base == "vaddl" else "ISub"}{{}});')
        return
    if base in ('vaddw', 'vsubw'):
        tw = WIDER[t]
        ln = 8 // SIZE[t]
        emit(f'wide2<{t}, {tw}, {ln}>(ctx, {d}, {rs[1][0]}, {rs[2][0]}, {"IAdd" if base == "vaddw" else "ISub"}{{}});')
        return
    if base == 'vmovl':
        tw = WIDER[t]
        emit(f'widen1<{t}, {tw}, {8 // SIZE[t]}>(ctx, {d}, {rs[1][0]});')
        return
    if base in ('vmovn', 'vqmovn', 'vqmovun', 'vshrn', 'vqshrn'):
        # the type names the wide source (vmovn.i64, vqmovn.s32, vqmovun.s16, vshrn.i32, vqshrn.s64)
        tw = t
        tn = NARROWER[tw]
        if base == 'vqmovun':
            tn = UNSIGNED_OF.get(tn, tn)
        elif base in ('vqmovn', 'vqshrn') and t0.startswith('s'):
            tn = SIGNED_OF.get(tn, tn)
        shift = imm if imm is not None else 0
        f = 'NarrowTrunc' if base in ('vmovn', 'vshrn') else 'NarrowSat'
        emit(f'narrow1<{tw}, {tn}, {16 // SIZE[tw]}>(ctx, {d}, {rs[1][0]}, {f}<{tn}>{{{shift}u}});')
        return
    raise Unsupported(f'{base}.{".".join(types)}')


# the alignment hint (:64 / :0x40 ...) only adds a fault check on hardware, and is ignored
LIST_RE = re.compile(r'\{([^}]*)\},\s*\[(\w+)(?::(0x[0-9a-fA-F]+|\d+))?\](!?)(?:,\s*(\w+))?$')
ITEM_RE = re.compile(r'd(\d+)(\[(\d*)\])?$')


def load_store(cx, base, t0):
    from . import semantics as S
    cs = cx.cs
    emit = cx.emit
    m = LIST_RE.match(cs.op_str.strip())
    if not m:
        raise Unsupported(f'{base} operands {cs.op_str}')
    items = [ITEM_RE.match(x.strip()) for x in m.group(1).split(',')]
    if not all(items):
        raise Unsupported(f'{base} list {m.group(1)}')
    dregs = [int(i.group(1)) for i in items]
    lane_spec = [i.group(2) for i in items]
    rn = S.REGS[m.group(2)] if m.group(2) in S.REGS else int(m.group(2)[1:])
    t = INT[t0]
    esize = SIZE[t]
    load = base.startswith('vld')
    structs = int(base[3])  # elements per structure: vld1 .. vld4
    if structs > 1:
        # multiple n-element structures, interleaved in memory: element e of the structure's
        # members k = 0 .. n-1 goes to (or comes from) d register list[k * per + r], where the
        # list holds `per` registers per member (vld2 {d16-d19}: d16, d17 even, d18, d19 odd)
        if any(s is not None for s in lane_spec) or len(dregs) % structs:
            raise Unsupported(f'{base} lane or list form {m.group(1)}')
        per = len(dregs) // structs
        order = [dregs[k * per + r] for r in range(per) for k in range(structs)]
        emit('{')
        emit(f'const uint32_t addr = ctx->r[{rn}];')
        emit(f'static constexpr uint8_t regs[] = {{{", ".join(map(str, order))}}};')
        emit(f'{"vldn" if load else "vstn"}<{t}, {structs}>(ctx, regs, {per}u, addr);')
        total = 8 * len(dregs)
        if m.group(4):
            emit(f'ctx->r[{rn}] = addr + {total}u;')
        elif m.group(5):
            rm = S.REGS[m.group(5)] if m.group(5) in S.REGS else int(m.group(5)[1:])
            emit(f'ctx->r[{rn}] = addr + ctx->r[{rm}];')
        emit('}')
        return
    emit('{')
    emit(f'const uint32_t addr = ctx->r[{rn}];')
    if all(s is None for s in lane_spec):
        if dregs != list(range(dregs[0], dregs[0] + len(dregs))):
            raise Unsupported(f'{base} non-consecutive list')
        total = 8 * len(dregs)
        emit(f'{"vld1_regs" if load else "vst1_regs"}(ctx, {dregs[0]}, {len(dregs)}u, addr);')
    elif all(s == '[]' for s in lane_spec):
        if not load:
            raise Unsupported('vst1 all lanes')
        total = esize
        emit(f'const {t} v = rdT<{t}>(addr);')
        for dr in dregs:
            emit(f'vdup<{t}, {8 // esize}>(ctx, {dr}, v);')
    elif all(s and s[1:-1].isdigit() for s in lane_spec):
        total = esize * len(dregs)
        for k, (dr, s) in enumerate(zip(dregs, lane_spec)):
            lane = int(s[1:-1])
            if load:
                emit(f'vsetlane<{t}>(ctx, {dr}, {lane}, rdT<{t}>(addr + {k * esize}u));')
            else:
                emit(f'wrT<{t}>(addr + {k * esize}u, vlane<{t}>(ctx, {dr}, {lane}));')
    else:
        raise Unsupported(f'{base} mixed lanes')
    if m.group(4):
        emit(f'ctx->r[{rn}] = addr + {total}u;')
    elif m.group(5):
        rm = S.REGS[m.group(5)] if m.group(5) in S.REGS else int(m.group(5)[1:])
        emit(f'ctx->r[{rn}] = addr + ctx->r[{rm}];')
    emit('}')
