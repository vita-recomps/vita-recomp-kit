"""Function emitter: a Function from the front end -> one C++ function.

Each guest function becomes `void f_<start>(GuestContext *ctx)`. Instructions are emitted in
address order with labels at branch targets; IT-conditional instructions are wrapped in `if`.
Flag writes that no path reads are dropped (backward liveness over the instruction graph).

Return protocol: a function sets ctx->r[15] to the address it returns to (lr for `bx lr`, the
popped value for `pop {.., pc}`) and returns. Every call site checks that ctx->r[15] is its own
return address and escapes to the fallback CPU otherwise.

Resume points: the return address of every call outside an IT block is also an entry. With
ctx->resume set to it, the function starts there (all guest state is in ctx between instructions),
so a frame the fallback CPU took over after an escape (a fiber switch) runs natively again once its
callee returns. The function's orphan entries (code the inventory found no path to, such as the
helpers a hand-written routine calls and jumps to through computed pc-relative addresses) are resume
points too, so those computed calls and jumps find native code.
"""
from . import semantics

ALL_FLAGS = frozenset('nzcv')
FALLS_THROUGH = {'seq', 'nop', 'it', 'call', 'call_import', 'call_indirect', 'cond_branch'}
# kinds after which the next instruction starts a basic block (calls return into the same block)
ENDS_BLOCK = {'branch', 'cond_branch', 'jumptable', 'return', 'jump_indirect', 'trap', 'unsupported'}


def fname(addr):
    return f'f_{addr:08x}'


class FunctionEmitter:
    def __init__(self, func, module, recompiled):
        self.f = func
        self.module = module
        self.recompiled = recompiled  # set of function starts that have native code
        self.imports = module.imports

    def translate_all(self):
        f = self.f
        self.tr = []
        for ins in f.instructions:
            self.tr.append(semantics.translate(ins, f.thumb, self.imports, f.start, f.end, f.jumptables,
                                               self.module.patched))
        self.addr_index = {ins.address: i for i, ins in enumerate(f.instructions)}
        return self.tr

    def successors(self, i):
        ins, t = self.f.instructions[i], self.tr[i]
        nxt = ins.address + ins.size
        out = []
        cond = ins.it_cond is not None
        k = t.kind
        if k in FALLS_THROUGH or cond:
            if nxt in self.addr_index:
                out.append(self.addr_index[nxt])
        if k in ('branch', 'cond_branch') and t.target in self.addr_index:
            out.append(self.addr_index[t.target])
        if k == 'jumptable':
            out += [self.addr_index[x] for x in t.jumptable[1] if x in self.addr_index]
        return out

    def liveness(self):
        """flags live after each instruction"""
        n = len(self.tr)
        succ = [self.successors(i) for i in range(n)]
        pred = [[] for _ in range(n)]
        for i, ss in enumerate(succ):
            for j in ss:
                pred[j].append(i)
        live_in = [frozenset()] * n
        live_out = [frozenset()] * n
        work = list(range(n))
        on = [True] * n
        while work:
            i = work.pop()
            on[i] = False
            t = self.tr[i]
            ins = self.f.instructions[i]
            out = frozenset().union(*[live_in[j] for j in succ[i]]) if succ[i] else frozenset()
            if t.kind in ('jump_indirect', 'unsupported', 'trap'):
                out = ALL_FLAGS  # the fallback CPU continues with the flags in ctx
            elif (t.kind in ('branch', 'cond_branch') and t.target not in self.addr_index
                  and not self.tail_call(t.target)):
                out = out | ALL_FLAGS  # an escape, as above
            live_out[i] = out
            if t.kind in ('call', 'call_import', 'call_indirect') and ins.it_cond is None:
                inn = frozenset()  # AAPCS: flags are not preserved across calls
            elif ins.it_cond is None:
                inn = out - t.flags_written
            else:
                inn = out
            inn = inn | t.flags_read
            if inn != live_in[i]:
                live_in[i] = inn
                for p in pred[i]:
                    if not on[p]:
                        on[p] = True
                        work.append(p)
        return live_out

    def falls_through(self, ins, t):
        return t.kind in FALLS_THROUGH or ins.it_cond is not None

    def emit(self):
        f = self.f
        tr = self.translate_all()
        live_out = self.liveness()
        insns = f.instructions
        labels = set()
        for i, (ins, t) in enumerate(zip(insns, tr)):
            if t.kind in ('branch', 'cond_branch') and t.target in self.addr_index:
                labels.add(t.target)
            if t.kind == 'jumptable':
                labels.update(x for x in t.jumptable[1] if x in self.addr_index)
            nxt = ins.address + ins.size
            following = insns[i + 1].address if i + 1 < len(insns) else None
            if self.falls_through(ins, t) and nxt in self.addr_index and nxt != following:
                labels.add(nxt)
        # resume points (module docstring): they start basic blocks
        resumes = []
        for i, (ins, t) in enumerate(zip(insns, tr)):
            nxt = ins.address + ins.size
            if (t.kind in ('call', 'call_import', 'call_indirect') and ins.it_cond is None
                    and nxt in self.addr_index and insns[self.addr_index[nxt]].it_cond is None):
                resumes.append(nxt)
                labels.add(nxt)
        for a in f.orphan_entries:
            if a in self.addr_index and insns[self.addr_index[a]].it_cond is None and a not in resumes:
                resumes.append(a)
                labels.add(a)
        resumes.sort()
        # basic blocks, for the executed-instruction counter (RECOMP_COUNT)
        starts = [i for i, (ins, t) in enumerate(zip(insns, tr))
                  if i == 0 or ins.address in labels or tr[i - 1].kind in ENDS_BLOCK]
        block_len = {s: e - s for s, e in zip(starts, starts[1:] + [len(insns)])}
        out = [f'void {fname(f.start)}(GuestContext *ctx) {{']
        if resumes:
            out.append('    if (ctx->resume) {')
            out.append('        const uint32_t at = ctx->resume;')
            out.append('        ctx->resume = 0;')
            out.append('        switch (at) {')
            out += [f'        case 0x{a:08x}u: goto L_{a:08x};' for a in resumes]
            out.append('        default: unsupported(ctx, at);')
            out.append('        }')
            out.append('    }')
        stats = {'insns': 0, 'unsupported': 0, 'flags_dropped': 0, 'resumes': resumes}
        for i, (ins, t) in enumerate(zip(insns, tr)):
            cs = ins.cs
            if ins.address in labels:
                out.append(f'L_{ins.address:08x}:;')
            if i in block_len:
                out.append(f'    RECOMP_COUNT({block_len[i]});')
            stats['insns'] += 1
            if t.kind == 'unsupported':
                stats['unsupported'] += 1
            body = []
            for s in t.stmts:
                if isinstance(s, tuple):
                    _, flag, stmt = s
                    if flag in live_out[i]:
                        body.append(stmt)
                    else:
                        stats['flags_dropped'] += 1
                else:
                    body.append(s)
            body += self.control(i, ins, t)
            out.append(f'    // {ins.address:08x}: {cs.mnemonic} {cs.op_str}')
            if body:
                if ins.it_cond:
                    out.append(f'    if ({semantics.COND_EXPR[ins.it_cond]}) {{')
                    out += ['        ' + b for b in body]
                    out.append('    }')
                else:
                    out += ['    ' + b for b in body]
            nxt = ins.address + ins.size
            following = insns[i + 1].address if i + 1 < len(insns) else None
            if self.falls_through(ins, t):
                if nxt in self.addr_index:
                    if nxt != following:
                        out.append(f'    goto L_{nxt:08x};')
                else:
                    # nothing decoded after this instruction (a call that does not return)
                    out.append(f'    escape(ctx, 0x{nxt | (1 if f.thumb else 0):08x}u);')
        out.append('}')
        return '\n'.join(out), stats

    def control(self, i, ins, t):
        k = t.kind
        f = self.f
        ret = (ins.address + ins.size) | (1 if f.thumb else 0)
        if k == 'branch':
            if t.target in self.addr_index:
                return [f'goto L_{t.target:08x};']
            if self.tail_call(t.target):
                return [f'RECOMP_CALL({fname(t.target)}, 0x{t.target | (1 if f.thumb else 0):08x}u);', 'return;']
            return [f'ctx->r[15] = 0x{t.target | (1 if f.thumb else 0):08x}u;', f'escape(ctx, ctx->r[15]);']
        if k == 'cond_branch':
            if t.target in self.addr_index:
                return [f'if ({t.cond}) goto L_{t.target:08x};']
            if self.tail_call(t.target):
                return [f'if ({t.cond}) {{',
                        f'    RECOMP_CALL({fname(t.target)}, 0x{t.target | (1 if f.thumb else 0):08x}u);',
                        '    return;',
                        '}']
            return [f'if ({t.cond}) escape(ctx, 0x{t.target | (1 if f.thumb else 0):08x}u);']
        if k in ('call', 'call_import', 'call_indirect'):
            lines = []
            if k == 'call':
                target = t.target
                # bl keeps the mode, blx switches; the callee's own mode decides its entry
                callee = target & ~1
                if callee in self.recompiled:
                    lines.append(f'RECOMP_CALL({fname(callee)}, 0x{callee | self.module_mode(callee, ins):08x}u);')
                else:
                    mode = self.module_mode(callee, ins)
                    lines.append(f'call_indirect(ctx, 0x{callee | mode:08x}u);')
            elif k == 'call_import':
                lines.append(f'call_import(ctx, 0x{t.target & ~1:08x}u);')
            lines.append(f'if (ctx->r[15] != 0x{ret:08x}u) escape(ctx, ctx->r[15]);')
            return lines
        if k == 'return':
            if t.writes_pc_via == 'lr':
                return ['ctx->r[15] = ctx->r[14];', 'return;']
            return ['return;']
        if k == 'jump_indirect':
            # a register jump to a known function entry is a tail call, as a direct branch to it
            target = self.module.jump_targets.get(ins.address)
            if target is not None and self.tail_call(target & ~1):
                return [f'RECOMP_CALL({fname(target & ~1)}, 0x{target:08x}u);', 'return;']
            return ['escape(ctx, ctx->r[15]);']
        if k == 'jumptable':
            idx, targets = t.jumptable
            lines = [f'switch ({idx}) {{']
            for n, x in enumerate(targets):
                lines.append(f'    case {n}: goto L_{x:08x};')
            lines.append(f'    default: unsupported(ctx, 0x{ins.address:08x}u);')
            lines.append('}')
            return lines
        if k == 'trap':
            return [f'unsupported(ctx, 0x{ins.address:08x}u);']
        return []

    def tail_call(self, target):
        """a branch to another recompiled function's entry in the same mode: the callee returns
        to this function's caller, with ctx->r[15] set"""
        return target in self.recompiled and self.module.mode_of(target) in (None, self.f.thumb)

    def module_mode(self, callee, ins):
        mode = self.module.mode_of(callee)
        if mode is None:
            mode = (ins.cs.mnemonic == 'bl') == self.f.thumb
        return 1 if mode else 0
