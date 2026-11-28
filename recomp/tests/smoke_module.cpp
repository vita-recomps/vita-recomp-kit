// Run the synthetic module through the same C ABI used by the Vita runtime.
#include "aot_abi.h"

#include <array>
#include <bit>
#include <cstdlib>
#include <cstring>
#include <iostream>

#if defined(_WIN32)
#include <windows.h>
#else
#include <dlfcn.h>
#endif

using namespace armrecomp;

static void require(bool condition, const char *reason) {
    if (!condition) {
        std::cerr << reason << '\n';
        std::exit(1);
    }
}

static void unexpected_hook(GuestContext *, uint32_t) {
    require(false, "unexpected call out of the smoke module");
}

int main(int argc, char **argv) {
    require(argc == 2, "expected the module path");
#if defined(_WIN32)
    const auto library = LoadLibraryA(argv[1]);
    const auto symbol = [library](const char *name) { return GetProcAddress(library, name); };
#else
    const auto library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    const auto symbol = [library](const char *name) { return dlsym(library, name); };
    if (!library)
        std::cerr << dlerror() << '\n';
#endif
    require(library != nullptr, "failed to load the smoke module");
    const auto init = reinterpret_cast<AotInitFn>(symbol(ARMRECOMP_AOT_INIT));
    const auto features = reinterpret_cast<const char *(*)()>(symbol(ARMRECOMP_AOT_FEATURES));
    require(init && features && features()[0] == '\0', "missing or invalid ABI entry points");

    constexpr uint32_t base = 0x1000;
    std::array<uint8_t, base + 64> guest{};
    AotHost host{ AOT_ABI_VERSION, sizeof(GuestContext), guest.data(), unexpected_hook, unexpected_hook,
        unexpected_hook, unexpected_hook };
    const AotModule *module = init(&host);
    require(module && module->function_count == 6, "invalid module function table");
    const auto *functions = module->functions;

    GuestContext ctx{};
    constexpr std::array<uint16_t, 16> lanes{ 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15 };
    constexpr std::array<uint16_t, 16> unzipped{ 0, 2, 4, 6, 8, 10, 12, 14, 1, 3, 5, 7, 9, 11, 13, 15 };
    std::memcpy(ctx.vfp.d, lanes.data(), sizeof(lanes));
    functions[0].fn(&ctx);
    require(std::memcmp(ctx.vfp.d, unzipped.data(), sizeof(unzipped)) == 0, "VUZP.16 lane order");

    ctx.vfp.f[8] = -1.505f;
    functions[1].fn(&ctx);
    require(ctx.vfp.s[8] == static_cast<uint32_t>(-385), "scalar VCVT to fixed truncation");
    functions[2].fn(&ctx);
    require(ctx.vfp.f[8] == -385.0f / 256.0f, "scalar VCVT from fixed scaling");

    constexpr std::array<float, 4> floats{ 1.505f, -1.505f, 0.0f, 2.0f };
    constexpr std::array<int32_t, 4> fixed{ 385, -385, 0, 512 };
    for (size_t i = 0; i < floats.size(); ++i)
        ctx.vfp.f[12 + i] = floats[i];
    functions[3].fn(&ctx);
    for (size_t i = 0; i < fixed.size(); ++i)
        require(ctx.vfp.s[12 + i] == std::bit_cast<uint32_t>(fixed[i]), "NEON VCVT to fixed");
    functions[4].fn(&ctx);
    for (size_t i = 0; i < fixed.size(); ++i)
        require(ctx.vfp.f[12 + i] == fixed[i] / 256.0f, "NEON VCVT from fixed");

    require(module->patched_count == 2 && module->patched[0] == base + 40 && module->patched[1] == base + 44,
        "missing ARM MOV relocations");
    // Relocated ARM instructions: movw r0, #0xcdef; movt r0, #0x89ab.
    constexpr std::array<uint32_t, 2> relocated{ 0xe30c0def, 0xe34809ab };
    std::memcpy(guest.data() + module->patched[0], relocated.data(), sizeof(relocated));
    functions[5].fn(&ctx);
    require(ctx.r[0] == 0x89abcdef, "ARM MOV did not read relocated immediates");
    require(ctx.insns != 0, "release instruction-counting flag is missing");
}
