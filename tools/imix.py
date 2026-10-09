"""Linear sweep of every exidx function (start -> next start) with Capstone; instruction-class counts.
usage: python -I imix.py <eboot.elf> <eboot.json from velf.py>"""
import struct, sys, json, collections
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_ARM

d = open(sys.argv[1], 'rb').read()
info = json.load(open(sys.argv[2]))
SEG_OFF, SEG_VA = 0x100, 0x81000000
stubs = {int(f['stub'], 16) for l in info['imports'].values() for f in l['funcs']}
starts = sorted(set(info['exidx']))
md_t = Cs(CS_ARCH_ARM, CS_MODE_THUMB); md_a = Cs(CS_ARCH_ARM, CS_MODE_ARM)
c = collections.Counter(); mn = collections.Counter()
stop_reasons = collections.Counter()
EXIDX = 0x81b2acd0
for i, s in enumerate(starts):
    addr = s & ~1
    end = (starts[i + 1] & ~1) if i + 1 < len(starts) else EXIDX
    if addr in stubs or addr >= EXIDX:
        continue
    thumb = bool(s & 1)
    c['funcs_thumb' if thumb else 'funcs_arm'] += 1
    code = d[SEG_OFF + addr - SEG_VA: SEG_OFF + end - SEG_VA]
    n = 0; last = addr
    for ins in (md_t if thumb else md_a).disasm(code, addr):
        n += 1; last = ins.address + ins.size
        m = ins.mnemonic.split('.')[0]
        mn[m] += 1
        ops = ins.op_str
        c['insn'] += 1
        c['insn_thumb' if thumb else 'insn_arm'] += 1
        if m.startswith('v'):
            c['fp_simd'] += 1
            if 'q' in ops.replace('eq', '') and any(f'q{k}' in ops for k in range(16)) or m.startswith(('vld1','vld2','vld3','vld4','vst1','vst2','vst3','vst4','vdup','vzip','vuzp','vtrn','vext','vbsl','vbif','vbit','vrev','vtbl','vqadd','vpadd','vrecpe','vrsqrte','vrecps','vrsqrts')):
                c['neon'] += 1
            if m in ('vrecpe', 'vrsqrte', 'vrecps', 'vrsqrts'): c['neon_estimates'] += 1
            if m in ('vmsr',): c['vmsr'] += 1
        if m in ('ldrex', 'strex', 'ldrexd', 'strexd', 'ldrexb', 'strexb', 'ldrexh', 'strexh'): c['exclusive'] += 1
        if m in ('svc',): c['svc'] += 1
        if m in ('bkpt', 'udf'): c[m] += 1
        if m in ('tbb', 'tbh'): c['jumptable'] += 1
        if m == 'blx' and not ops.startswith('#'): c['blx_reg'] += 1
        if m == 'bx' and ops != 'lr': c['bx_nonlr'] += 1
        if m == 'mrc': c['mrc'] += 1
        if m in ('sdiv', 'udiv'): c['hwdiv'] += 1
        if m == 'it' or m.startswith('it'): c['it'] += 1 if m[:2] == 'it' and len(m) <= 4 else 0
    c['bytes_decoded'] += last - addr
    c['bytes_range'] += end - addr
for k, v in sorted(c.items()):
    print(f'{k:16s} {v}')
print('top mnemonics:', mn.most_common(25))
print('fp/simd share: %.1f%%  thumb share: %.2f%%' % (100 * c['fp_simd'] / c['insn'], 100 * c['insn_thumb'] / c['insn']))
