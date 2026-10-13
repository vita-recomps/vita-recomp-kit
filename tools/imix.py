"""Linear sweep of every exidx function (start -> next start) with Capstone; instruction-class counts.

The code segment (file offset and address) and the end of the code (the start of .ARM.exidx) are
read from the ELF's program headers and module_info; the options override them.

usage: python -I imix.py <eboot.elf> [<eboot.json from velf.py>] [--seg-off HEX] [--seg-va HEX]
                         [--exidx HEX]
Without the velf.py JSON, the import stubs and exidx entries are read from the ELF (vitaelf.py)."""
import collections, json, os, sys
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_ARM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vitaelf  # noqa: E402

args = sys.argv[1:]
if not args or args[0] in ('-h', '--help'):
    print(__doc__)
    sys.exit(0 if args else 2)
over = {}
for opt in ('--seg-off', '--seg-va', '--exidx'):
    if opt in args:
        i = args.index(opt)
        over[opt] = int(args[i + 1], 16)
        del args[i:i + 2]
elf = vitaelf.load(args[0])
d = elf.data
# the code segment is the one holding module_info (its index is in e_entry's top bits)
text = next(g for g in elf.segments
            if g.type == 1 and g.vaddr <= elf.module_info_va < g.vaddr + g.filesz)
SEG_OFF = over.get('--seg-off', text.offset)
SEG_VA = over.get('--seg-va', text.vaddr)
EXIDX = over.get('--exidx', elf.exidx_range[0])
print(f'code segment: offset {SEG_OFF:#x} address {SEG_VA:#x}; code ends at exidx {EXIDX:#x}', file=sys.stderr)
if len(args) > 1:
    info = json.load(open(args[1]))
    stubs = {int(f['stub'], 16) for l in info['imports'].values() for f in l['funcs']}
    starts = sorted(set(info['exidx']))
else:
    stubs = {i.stub for i in elf.imports}
    starts = sorted({e.start for e in elf.exidx})
md_t = Cs(CS_ARCH_ARM, CS_MODE_THUMB); md_a = Cs(CS_ARCH_ARM, CS_MODE_ARM)
c = collections.Counter(); mn = collections.Counter()
stop_reasons = collections.Counter()
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
