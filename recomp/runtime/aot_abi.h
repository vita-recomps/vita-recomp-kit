// Binary interface between a recompiled module (a DLL built from generated/<module>/) and the
// runtime that loads it (the Vita3K fork, docs/decisions.md D7). The fork keeps an identical copy
// (apart from this comment) in vita3k/cpu/include/cpu/aot_abi.h; bump AOT_ABI_VERSION in both on
// any change.
#pragma once

#include <cstdint>

namespace armrecomp {

constexpr uint32_t AOT_ABI_VERSION = 2;

struct GuestContext {
    uint32_t r[16]; // r15 holds the address of the last branch target written by generated code
    // flags as 0/1 values; generated code reads and writes them directly
    uint32_t n, z, c, v, q;
    uint32_t ge; // GE[3:0]
    union {
        uint64_t d[32];
        uint32_t s[64];
        float f[64];
        double fd[32];
    } vfp;
    uint32_t fpscr;
    uint32_t tpidruro; // user read-only thread ID register (TLS), read by mrc p15, 0, rd, c13, c0, 3
    // exclusive monitor
    uint32_t excl_addr;
    uint32_t excl_valid;
    uint64_t excl_value;
    void *embedder; // opaque pointer for the embedder
    uint64_t insns; // guest instructions run natively, counted by modules built with RECOMP_COUNT_INSNS
    // nonzero: the next function called starts at this resume point (AotResume) instead of its
    // entry, and clears it
    uint32_t resume;
    uint32_t reserved;
};

// Provided by the runtime. The hooks have the semantics documented in armrecomp.h; escape and
// unsupported must not return (they throw or longjmp).
struct AotHost {
    uint32_t abi_version;
    uint32_t context_size; // sizeof(GuestContext)
    uint8_t *guest_base;
    void (*call_indirect)(GuestContext *ctx, uint32_t addr);
    void (*call_import)(GuestContext *ctx, uint32_t stub_addr);
    void (*escape)(GuestContext *ctx, uint32_t pc);
    void (*unsupported)(GuestContext *ctx, uint32_t pc);
};

struct AotFunction {
    uint32_t addr; // entry address, bit 0 set for Thumb
    uint32_t size; // bytes of guest code (literal pools included) the function was generated from
    void (*fn)(GuestContext *ctx);
};

// A call's return address inside a function, where the function can also start (ctx->resume)
struct AotResume {
    uint32_t addr; // bit 0 set for Thumb
    uint32_t function; // entry address of the function, bit 0 set for Thumb
    void (*fn)(GuestContext *ctx);
};

// Provided by the module.
struct AotModule {
    uint32_t abi_version;
    uint32_t context_size;
    const char *name; // ELF file name the code was generated from
    const AotFunction *functions; // sorted by address
    uint32_t function_count;
    // FNV-1a 64 over the `size` bytes of every function in `functions`, in table order, with the
    // 4 bytes at every `patched` address read as zero: the runtime checks it against guest memory
    // before running any native code
    uint64_t code_hash;
    // instructions the loader rewrites (variable imports), sorted; generated code reads their
    // immediates from guest memory
    const uint32_t *patched;
    uint32_t patched_count;
    const AotResume *resumes; // sorted by address
    uint32_t resume_count;
};

// The module DLL exports `extern "C" const AotModule *armrecomp_aot_init(const AotHost *)`. It
// returns null when the host's ABI version or context size does not match.
using AotInitFn = const AotModule *(*)(const AotHost *host);

} // namespace armrecomp

#define ARMRECOMP_AOT_INIT "armrecomp_aot_init"
