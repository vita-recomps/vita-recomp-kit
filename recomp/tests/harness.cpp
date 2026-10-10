// Comparison against dynarmic on random register/flag/VFP states and a shared, deterministic
// sparse memory (docs/03-plan.md Phase 3):
//  - per instruction: each generated test function (one recompiled instruction) against dynarmic
//    executing the same encoding;
//  - per function: whole leaf functions, run from their entry until they return to a sentinel lr.
//
//   recomp_tests [iterations per test] [filter substring, or * for all] [iterations per function test]
#include "armrecomp.h"
#include "tests.h"

#include <dynarmic/interface/A32/a32.h>
#include <dynarmic/interface/A32/config.h>

#include <cstdio>
#include <map>
#include <random>
#include <string>
#include <vector>

namespace armrecomp {
uint8_t *g_guest_base = nullptr;
}

using namespace armrecomp;

// --- shared memory model ------------------------------------------------------------------------

struct TestMemory {
    uint32_t code_addr = 0; // first byte of the code window (an IT instruction may precede the test)
    std::vector<uint8_t> code;
    std::map<uint32_t, uint8_t> written;
    bool native = false; // the native side's memory: capped (an exception must not cross dynarmic's frames)

    uint8_t read8(uint32_t a) const {
        if (a - code_addr < code.size())
            return code[a - code_addr];
        if (auto it = written.find(a); it != written.end())
            return it->second;
        uint32_t h = a * 2654435761u;
        h ^= h >> 15;
        h *= 0x2c1b3c6du;
        return uint8_t(h >> 24);
    }
    void write8(uint32_t a, uint8_t v);
};

static TestMemory *g_mem = nullptr;

namespace armrecomp {
uint8_t rd8(uint32_t a) { return g_mem->read8(a); }
uint16_t rd16(uint32_t a) { return uint16_t(rd8(a) | rd8(a + 1) << 8); }
uint32_t rd32(uint32_t a) { return uint32_t(rd16(a) | uint32_t(rd16(a + 2)) << 16); }
uint64_t rd64(uint32_t a) { return rd32(a) | uint64_t(rd32(a + 4)) << 32; }
void wr8(uint32_t a, uint8_t v) { g_mem->write8(a, v); }
void wr16(uint32_t a, uint16_t v) { wr8(a, uint8_t(v)); wr8(a + 1, uint8_t(v >> 8)); }
void wr32(uint32_t a, uint32_t v) { wr16(a, uint16_t(v)); wr16(a + 2, uint16_t(v >> 16)); }
void wr64(uint32_t a, uint64_t v) { wr32(a, uint32_t(v)); wr32(a + 4, uint32_t(v >> 32)); }

// generated code leaving native execution: an error in a test, reported as a difference
struct LeftNative {
    const char *what;
    uint32_t pc;
};
void call_indirect(GuestContext *, uint32_t addr) { throw LeftNative{"call_indirect", addr}; }

// runaway guards: a native function test that never ends (or writes without end) is a failure,
// not a hung machine
void test_budget(GuestContext *ctx, uint32_t n) {
    ctx->insns += n;
    if (ctx->insns > 20'000'000)
        throw LeftNative{"runaway (instruction budget)", ctx->r[15]};
}
void call_import(GuestContext *, uint32_t addr) { throw LeftNative{"call_import", addr}; }
void escape(GuestContext *, uint32_t pc) { throw LeftNative{"escape", pc}; }
void unsupported(GuestContext *, uint32_t pc) { throw LeftNative{"unsupported", pc}; }
} // namespace armrecomp

void TestMemory::write8(uint32_t a, uint8_t v) {
    if (native && written.size() > 4'000'000)
        throw LeftNative{"runaway (memory writes)", a};
    written[a] = v;
}

// --- dynarmic environment -----------------------------------------------------------------------

// function tests return to this address (lr = SENTINEL | 1), where dynarmic finds `svc #0`
constexpr uint32_t SENTINEL = 0xf0000000u;

struct DynEnv final : Dynarmic::A32::UserCallbacks {
    uint64_t ticks_left = 0;
    Dynarmic::A32::Jit *jit = nullptr;
    bool returned = false;
    bool arm = false; // the code under test is ARM: pad with the ARM `b .`

    std::optional<std::uint32_t> MemoryReadCode(std::uint32_t a) override {
        // the test window, `svc #0` at the sentinel, and `b .` (0xE7FE) in every other halfword,
        // so execution stops when it leaves the window
        if (arm && a != SENTINEL && a - g_mem->code_addr >= g_mem->code.size())
            return 0xEAFFFFFEu;
        const auto half = [](uint32_t x) -> uint32_t {
            if (x - g_mem->code_addr < g_mem->code.size())
                return g_mem->read8(x) | uint32_t(g_mem->read8(x + 1)) << 8;
            if (x == SENTINEL)
                return 0xDF00u;
            return 0xE7FEu;
        };
        return half(a) | half(a + 2) << 16;
    }
    std::uint8_t MemoryRead8(std::uint32_t a) override { return rd8(a); }
    std::uint16_t MemoryRead16(std::uint32_t a) override { return rd16(a); }
    std::uint32_t MemoryRead32(std::uint32_t a) override { return rd32(a); }
    std::uint64_t MemoryRead64(std::uint32_t a) override { return rd64(a); }
    void MemoryWrite8(std::uint32_t a, std::uint8_t v) override { wr8(a, v); }
    void MemoryWrite16(std::uint32_t a, std::uint16_t v) override { wr16(a, v); }
    void MemoryWrite32(std::uint32_t a, std::uint32_t v) override { wr32(a, v); }
    void MemoryWrite64(std::uint32_t a, std::uint64_t v) override { wr64(a, v); }
    void InterpreterFallback(std::uint32_t pc, size_t) override { std::fprintf(stderr, "dynarmic interpreter fallback at %08x\n", pc); }
    void CallSVC(std::uint32_t) override {
        returned = true;
        jit->HaltExecution();
    }
    void ExceptionRaised(std::uint32_t pc, Dynarmic::A32::Exception e) override { exception = true; }
    void AddTicks(std::uint64_t t) override { ticks_left = t > ticks_left ? 0 : ticks_left - t; }
    std::uint64_t GetTicksRemaining() override { return ticks_left; }
    bool exception = false;
};

// --- state ---------------------------------------------------------------------------------------

struct State {
    uint32_t r[16];
    uint32_t n, z, c, v, q, ge;
    uint32_t ext[64];
    uint32_t fpscr;
};

static uint32_t interesting(std::mt19937 &rng) {
    static const uint32_t special[] = {0, 1, 2, 0x7fffffff, 0x80000000, 0xffffffff, 0xfffffffe, 0x80000001, 0x10, 0xff, 0x100, 0xffff, 0x10000, 31, 32, 33};
    switch (rng() % 4) {
    case 0: return special[rng() % (sizeof(special) / 4)];
    case 1: return rng() % 256;
    default: return rng();
    }
}

static uint32_t random_float_bits(std::mt19937 &rng) {
    std::uniform_real_distribution<float> d(-1000.f, 1000.f);
    // zeros, denormals, infinities, quiet and signalling NaNs, and values the estimate/step
    // instructions treat specially
    static const uint32_t special[] = {0x00000000, 0x80000000, 0x00000001, 0x807fffff, 0x00400000, 0x7f800000,
                                       0xff800000, 0x7fc00000, 0x7f800001, 0xffc00001, 0x3f800000, 0xbf800000,
                                       0x7f7fffff, 0x00800000, 0x40000000, 0x3fc00000, 0x7e800000, 0x01000000};
    switch (rng() % 8) {
    case 0: return rng();
    case 7: return special[rng() % (sizeof(special) / 4)];
    case 1: { float f = float(int32_t(rng() % 2001) - 1000); uint32_t u; std::memcpy(&u, &f, 4); return u; }
    default: { float f = d(rng); uint32_t u; std::memcpy(&u, &f, 4); return u; }
    }
}

static State random_state(std::mt19937 &rng) {
    State s{};
    for (int i = 0; i < 15; i++)
        s.r[i] = interesting(rng);
    s.r[13] = 0x81f00000u + (rng() % 0x1000) * 4; // a plausible, aligned stack
    s.n = rng() & 1; s.z = rng() & 1; s.c = rng() & 1; s.v = rng() & 1; s.q = 0; s.ge = 0;
    for (int i = 0; i < 64; i += 2) {
        if (rng() % 2) { // a double
            std::uniform_real_distribution<double> d(-1e6, 1e6);
            double x = d(rng);
            uint64_t u; std::memcpy(&u, &x, 8);
            s.ext[i] = uint32_t(u); s.ext[i + 1] = uint32_t(u >> 32);
        } else {
            s.ext[i] = random_float_bits(rng); s.ext[i + 1] = random_float_bits(rng);
        }
    }
    s.fpscr = 0; // what Vita3K threads run with
    return s;
}

static uint32_t cpsr_of(const State &s, bool thumb = true) {
    return (thumb ? 0x30u : 0x10u) | s.n << 31 | s.z << 30 | s.c << 29 | s.v << 28 | s.q << 27 | (s.ge & 0xf) << 16; // Thumb, user mode
}

// differences between the native and the dynarmic end states (flags optional: a whole function
// drops flag writes nobody reads, so its flags at return are unspecified)
static void compare(const GuestContext &ctx, Dynarmic::A32::Jit &jit, const TestMemory &native_mem, const TestMemory &dyn_mem,
                    std::string &diff, bool flags) {
    char buf[256];
    for (int i = 0; i < 15; i++) {
        if (ctx.r[i] != jit.Regs()[i]) {
            std::snprintf(buf, sizeof(buf), "r%d native %08x dynarmic %08x; ", i, ctx.r[i], jit.Regs()[i]);
            diff += buf;
        }
    }
    const uint32_t cpsr = jit.Cpsr();
    const uint32_t nat = cpsr_of(State{{}, ctx.n, ctx.z, ctx.c, ctx.v, ctx.q, ctx.ge});
    if (flags && (cpsr & 0xf80f0000u) != (nat & 0xf80f0000u)) {
        std::snprintf(buf, sizeof(buf), "NZCVQ/GE native %08x dynarmic %08x; ", nat & 0xf80f0000u, cpsr & 0xf80f0000u);
        diff += buf;
    }
    for (int i = 0; i < 64; i++) {
        if (ctx.vfp.s[i] != jit.ExtRegs()[i]) {
            std::snprintf(buf, sizeof(buf), "s%d native %08x dynarmic %08x; ", i, ctx.vfp.s[i], jit.ExtRegs()[i]);
            diff += buf;
            break;
        }
    }
    // compare FPSCR NZCV and control bits; cumulative exception flags (bits 0-7) are not tracked natively
    if ((ctx.fpscr & 0xffffff00u) != (jit.Fpscr() & 0xffffff00u)) {
        std::snprintf(buf, sizeof(buf), "fpscr native %08x dynarmic %08x; ", ctx.fpscr, jit.Fpscr());
        diff += buf;
    }
    if (native_mem.written != dyn_mem.written) {
        diff += "memory writes differ; ";
    }
}

// --- one comparison ------------------------------------------------------------------------------

static bool run_one(const TestCase &tc, const State &in, std::string &diff, Dynarmic::A32::Jit &jit, DynEnv &env) {
    // code window: [IT] instruction
    TestMemory native_mem, dyn_mem;
    native_mem.native = true;
    for (TestMemory *m : {&native_mem, &dyn_mem}) {
        m->code_addr = tc.addr - (tc.it_cond >= 0 ? 2 : 0);
        m->code.clear();
        if (tc.it_cond >= 0) {
            const uint16_t it = uint16_t(0xBF00 | (tc.it_cond << 4) | 0x8);
            m->code.push_back(uint8_t(it));
            m->code.push_back(uint8_t(it >> 8));
        }
        for (int i = 0; i < tc.size; i++)
            m->code.push_back(tc.bytes[i]);
    }

    // native
    GuestContext ctx{};
    std::memcpy(ctx.r, in.r, sizeof(in.r));
    ctx.n = in.n; ctx.z = in.z; ctx.c = in.c; ctx.v = in.v; ctx.q = in.q; ctx.ge = in.ge;
    std::memcpy(ctx.vfp.s, in.ext, sizeof(in.ext));
    ctx.fpscr = in.fpscr;
    g_mem = &native_mem;
    try {
        tc.fn(&ctx);
    } catch (const LeftNative &e) {
        diff += std::string("native ") + e.what + "; ";
        g_mem = nullptr;
        return false;
    }

    // dynarmic (one JIT per test: the code is the same for every iteration)
    env.exception = false;
    jit.ClearExclusiveState();
    std::copy(in.r, in.r + 16, jit.Regs().begin());
    jit.Regs()[15] = native_mem.code_addr;
    env.arm = !tc.thumb;
    jit.SetCpsr(cpsr_of(in, tc.thumb));
    std::copy(in.ext, in.ext + 64, jit.ExtRegs().begin());
    jit.SetFpscr(in.fpscr);
    g_mem = &dyn_mem;
    env.ticks_left = tc.it_cond >= 0 ? 2 : 1;
    jit.Run();
    g_mem = nullptr;

    compare(ctx, jit, native_mem, dyn_mem, diff, true);
    if (env.exception)
        diff += "(dynarmic raised an exception) ";
    return diff.empty();
}

// --- one function --------------------------------------------------------------------------------

enum class Outcome { same, differ, skipped };

static Outcome run_function(const FunctionTest &ft, const State &in, std::string &diff, Dynarmic::A32::Jit &jit, DynEnv &env) {
    TestMemory native_mem, dyn_mem;
    native_mem.native = true;
    for (TestMemory *m : {&native_mem, &dyn_mem}) {
        m->code_addr = ft.start;
        m->code.assign(ft.code, ft.code + ft.size);
    }
    // dynarmic first: random inputs can make loops run (nearly) forever; skip those
    env.exception = false;
    env.returned = false;
    jit.ClearExclusiveState();
    std::copy(in.r, in.r + 16, jit.Regs().begin());
    jit.Regs()[14] = SENTINEL | 1;
    jit.Regs()[15] = ft.start;
    env.arm = !ft.thumb;
    jit.SetCpsr(cpsr_of(in, ft.thumb));
    std::copy(in.ext, in.ext + 64, jit.ExtRegs().begin());
    jit.SetFpscr(in.fpscr);
    g_mem = &dyn_mem;
    env.ticks_left = 200000;
    jit.Run();
    g_mem = nullptr;
    if (!env.returned || env.exception)
        return Outcome::skipped;

    GuestContext ctx{};
    std::memcpy(ctx.r, in.r, sizeof(in.r));
    ctx.r[14] = SENTINEL | 1;
    ctx.n = in.n; ctx.z = in.z; ctx.c = in.c; ctx.v = in.v; ctx.q = in.q; ctx.ge = in.ge;
    std::memcpy(ctx.vfp.s, in.ext, sizeof(in.ext));
    ctx.fpscr = in.fpscr;
    g_mem = &native_mem;
    try {
        ft.fn(&ctx);
    } catch (const LeftNative &e) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "native %s %08x; ", e.what, e.pc);
        diff += buf;
    }
    g_mem = nullptr;
    if (diff.empty() && ctx.r[15] != (SENTINEL | 1)) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "native returned to %08x; ", ctx.r[15]);
        diff += buf;
    }
    compare(ctx, jit, native_mem, dyn_mem, diff, false);
    return diff.empty() ? Outcome::same : Outcome::differ;
}

int main(int argc, char **argv) {
    const int iterations = argc > 1 ? std::atoi(argv[1]) : 64;
    const char *filter = argc > 2 && std::strcmp(argv[2], "*") != 0 ? argv[2] : nullptr; // "*": all
    std::mt19937 rng(12345);
    std::map<std::string, std::pair<int, int>> by_form; // form -> (tests, failing tests)
    int tests = 0, failing = 0;
    for (uint32_t i = 0; i < g_test_count; i++) {
        const TestCase &tc = g_tests[i];
        if (filter && !std::strstr(tc.text, filter) && !std::strstr(tc.form, filter))
            continue;
        tests++;
        by_form[tc.form].first++;
        DynEnv env;
        Dynarmic::A32::UserConfig config;
        config.callbacks = &env;
        Dynarmic::A32::Jit jit{config};
        env.jit = &jit;
        for (int k = 0; k < iterations; k++) {
            const State s = random_state(rng);
            std::string diff;
            if (!run_one(tc, s, diff, jit, env)) {
                failing++;
                by_form[tc.form].second++;
                std::printf("FAIL %08x %-36s [%s]%s: %s\n", tc.addr, tc.text, tc.form, tc.it_cond >= 0 ? " (IT)" : "", diff.c_str());
                break;
            }
        }
    }
    std::printf("\n%d tests, %d failing\n", tests, failing);
    for (auto &[f, c] : by_form)
        if (c.second)
            std::printf("  %-28s %d/%d failing\n", f.c_str(), c.second, c.first);

    // whole functions (not filtered: they are not per form)
    const int fn_iterations = argc > 3 ? std::atoi(argv[3]) : 64;
    int fn_failing = 0, fn_unexercised = 0;
    long long runs = 0, skipped = 0;
    for (uint32_t i = 0; filter == nullptr && i < g_function_test_count; i++) {
        const FunctionTest &ft = g_function_tests[i];
        DynEnv env;
        Dynarmic::A32::UserConfig config;
        config.callbacks = &env;
        Dynarmic::A32::Jit jit{config};
        env.jit = &jit;
        int compared = 0;
        for (int k = 0; k < fn_iterations; k++) {
            const State s = random_state(rng);
            std::string diff;
            const Outcome o = run_function(ft, s, diff, jit, env);
            runs++;
            if (o == Outcome::skipped) {
                skipped++;
                continue;
            }
            compared++;
            if (o == Outcome::differ) {
                fn_failing++;
                std::printf("FAIL function %08x (%u instructions): %s\n", ft.start, ft.instructions, diff.c_str());
                break;
            }
        }
        if (compared == 0)
            fn_unexercised++;
    }
    if (filter == nullptr)
        std::printf("%u function tests, %d failing, %d never returned within the tick budget; %lld of %lld runs skipped\n",
                    g_function_test_count, fn_failing, fn_unexercised, skipped, runs);
    return failing || fn_failing ? 1 : 0;
}
