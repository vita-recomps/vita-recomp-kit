"""Histogram of instruction forms (mnemonic + operand shape) in a module's code functions.
usage: python -I recomp/forms.py <elf> [top N]"""
import collections, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from armrecomp.frontend import Module  # noqa: E402
from capstone import arm  # noqa: E402

OPT = {arm.ARM_OP_REG: 'r', arm.ARM_OP_IMM: 'i', arm.ARM_OP_MEM: 'm', arm.ARM_OP_FP: 'f',
       arm.ARM_OP_SYSREG: 's', arm.ARM_OP_CIMM: 'c', arm.ARM_OP_PIMM: 'p', arm.ARM_OP_SETEND: 'e'}


def form(ins):
    cs = ins.cs
    shape = []
    for op in cs.operands:
        t = OPT.get(op.type, '?')
        if op.type == arm.ARM_OP_REG and cs.reg_name(op.reg).startswith(('d', 's', 'q')):
            t = cs.reg_name(op.reg)[0]
        if op.shift.type:
            t += '<<'
        shape.append(t)
    return f'{ins.mnemonic} {",".join(shape)}'


def main():
    mod = Module(sys.argv[1])
    top = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    forms, mnems = collections.Counter(), collections.Counter()
    nfun = 0
    for f in mod.functions:
        if f.kind != 'code' or f.uses_neon or not f.thumb:
            continue
        nfun += 1
        for ins in f.instructions:
            forms[form(ins)] += 1
            mnems[ins.mnemonic] += 1
    total = sum(mnems.values())
    print(f'{nfun} Thumb non-NEON functions, {total} instructions, {len(mnems)} mnemonics, {len(forms)} forms')
    acc = 0
    for i, (m, n) in enumerate(mnems.most_common()):
        acc += n
        if i < top:
            print(f'{m:10s} {n:8d}  {100 * acc / total:6.2f}%')
    print('mnemonics needed for 99% / 99.9% / 100%:',
          *[next(i + 1 for i, a in enumerate(__import__('itertools').accumulate(n for _, n in mnems.most_common())) if a >= q * total) for q in (0.99, 0.999, 1.0)])


if __name__ == '__main__':
    main()
