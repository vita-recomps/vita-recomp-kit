// recomp/runtime/aot_android.cpp: compiled into every module that toolchain.py builds for Android.
// What a recompiled module needs from the C++ runtime on Android when built with zig without libc++
// (-nostdlib): thread-safe static locals (__cxa_guard_*, libc++abi's otherwise) and __dso_handle (the
// NDK's crtbegin_so.o's). bionic's libc.so provides the rest (__cxa_atexit, abort, the string
// functions). No libc types cross here (the headers are musl's, the library bionic): the guard waits
// with atomics and sched_yield. Hidden: each module keeps its own.
#include <sched.h>
#include <stdint.h>

extern "C" {

__attribute__((visibility("hidden"))) void *__dso_handle = &__dso_handle;

// the Itanium ABI's 64-bit guard: byte 0 is set once initialised; byte 1 while a thread initialises
__attribute__((visibility("hidden"))) int __cxa_guard_acquire(uint64_t *guard) {
    uint8_t *g = reinterpret_cast<uint8_t *>(guard);
    for (;;) {
        if (__atomic_load_n(&g[0], __ATOMIC_ACQUIRE))
            return 0;
        uint8_t idle = 0;
        if (__atomic_compare_exchange_n(&g[1], &idle, 1, false, __ATOMIC_ACQUIRE, __ATOMIC_RELAXED)) {
            if (__atomic_load_n(&g[0], __ATOMIC_ACQUIRE)) {
                __atomic_store_n(&g[1], 0, __ATOMIC_RELEASE);
                return 0;
            }
            return 1;
        }
        sched_yield();
    }
}

__attribute__((visibility("hidden"))) void __cxa_guard_release(uint64_t *guard) {
    uint8_t *g = reinterpret_cast<uint8_t *>(guard);
    __atomic_store_n(&g[0], 1, __ATOMIC_RELEASE);
    __atomic_store_n(&g[1], 0, __ATOMIC_RELEASE);
}

__attribute__((visibility("hidden"))) void __cxa_guard_abort(uint64_t *guard) {
    __atomic_store_n(&reinterpret_cast<uint8_t *>(guard)[1], 0, __ATOMIC_RELEASE);
}

} // extern "C"
