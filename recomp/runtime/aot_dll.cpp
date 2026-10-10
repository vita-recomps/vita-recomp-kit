// Entry point of a recompiled module built as a DLL (docs/decisions.md D7): the runtime calls
// armrecomp_aot_init with its hooks and gets the function table back.
#include "armrecomp.h"

#include <cstdlib>

namespace armrecomp {

extern const AotFunction g_functions[];
extern const uint32_t g_function_count;
extern const uint64_t g_code_hash;
extern const char g_module_name[];
extern const uint32_t g_patched[];
extern const uint32_t g_patched_count;

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

extern "C" __declspec(dllexport) const armrecomp::AotModule *armrecomp_aot_init(const armrecomp::AotHost *host) {
    using namespace armrecomp;
    if (!host || host->abi_version != AOT_ABI_VERSION || host->context_size != sizeof(GuestContext))
        return nullptr;
    g_host = *host;
    g_guest_base = host->guest_base;
    static const AotModule module{AOT_ABI_VERSION, sizeof(GuestContext), g_module_name, g_functions, g_function_count,
                                  g_code_hash, g_patched, g_patched_count};
    return &module;
}
