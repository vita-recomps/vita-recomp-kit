// Entry point of a recompiled module built as a DLL (docs/decisions.md D7): the runtime calls
// armrecomp_aot_init with its hooks and gets the function table back.
#include "armrecomp.h"

#include <cstdlib>

#ifdef _WIN32
#define ARMRECOMP_EXPORT __declspec(dllexport)
#else
// the module is built with hidden visibility: only the entry points are exported
#define ARMRECOMP_EXPORT __attribute__((visibility("default")))
#endif

namespace armrecomp {

extern const AotFunction g_functions[];
extern const uint32_t g_function_count;
extern const uint64_t g_code_hash;
extern const char g_module_name[];
extern const uint32_t g_patched[];
extern const uint32_t g_patched_count;
extern const AotResume g_resumes[];
extern const uint32_t g_resume_count;

uint8_t *g_guest_base = nullptr;

static AotHost g_host{};

void call_indirect(GuestContext *ctx, uint32_t addr) { g_host.call_indirect(ctx, addr); }
void call_import(GuestContext *ctx, uint32_t stub_addr) { g_host.call_import(ctx, stub_addr); }
void escape(GuestContext *ctx, uint32_t pc) {
    g_host.escape(ctx, pc);
    std::abort(); // the host's escape does not return
}
void unsupported(GuestContext *ctx, uint32_t pc) {
    g_host.unsupported(ctx, pc);
    std::abort();
}

} // namespace armrecomp

extern "C" ARMRECOMP_EXPORT const armrecomp::AotModule *armrecomp_aot_init(const armrecomp::AotHost *host) {
    using namespace armrecomp;
    if (!host || host->abi_version != AOT_ABI_VERSION || host->context_size != sizeof(GuestContext))
        return nullptr;
    g_host = *host;
    g_guest_base = host->guest_base;
    static const AotModule module{AOT_ABI_VERSION, sizeof(GuestContext), g_module_name, g_functions, g_function_count,
                                  g_code_hash, g_patched, g_patched_count, g_resumes, g_resume_count};
    return &module;
}

// The features the game project's overrides name (table.cpp, from recompile.py --overrides;
// aot_abi.h ARMRECOMP_AOT_FEATURES). Code generated before they existed has no table entry: the
// reference then falls back to an empty string, so such a module still links.
extern "C" const char armrecomp_features[];
#ifdef _MSC_VER
extern "C" const char armrecomp_features_none[] = "";
#pragma comment(linker, "/alternatename:armrecomp_features=armrecomp_features_none")
extern "C" ARMRECOMP_EXPORT const char *armrecomp_aot_features() { return armrecomp_features; }
#else
extern "C" __attribute__((weak)) const char armrecomp_features[];
extern "C" ARMRECOMP_EXPORT const char *armrecomp_aot_features() { return armrecomp_features ? armrecomp_features : ""; }
#endif
