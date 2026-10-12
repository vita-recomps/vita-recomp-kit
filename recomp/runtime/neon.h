// Advanced SIMD (NEON) helpers for generated code (recomp/armrecomp/neon.py).
//
// The register file is ctx->vfp.d[0..31]; q<n> is d<2n>:d<2n+1>. Lanes are little-endian, as on the
// Vita, so a vector of N elements of type T is the memcpy of N * sizeof(T) bytes from d<k>.
//
// Floating-point NEON operations ignore FPSCR and use ARM's "standard FPSCR value": inputs and
// results that are denormal are flushed to zero (keeping the sign), every NaN result is the default
// NaN 0x7fc00000, rounding is to nearest. The reciprocal and reciprocal-square-root estimates and
// steps follow dynarmic (common/fp/op, 0BSD), the test oracle.
#pragma once

#include "armrecomp.h"

#include <bit>
#include <cstdint>
#include <cstring>
#include <limits>
#include <type_traits>

namespace armrecomp {

template <typename T, int N>
struct Vec {
    T e[N];
};

template <typename T, int N>
inline Vec<T, N> vget(const GuestContext *c, unsigned d) {
    Vec<T, N> v;
    std::memcpy(v.e, &c->vfp.d[d], sizeof(v.e));
    return v;
}

template <typename T, int N>
inline void vset(GuestContext *c, unsigned d, const Vec<T, N> &v) {
    std::memcpy(&c->vfp.d[d], v.e, sizeof(v.e));
}

template <typename T>
inline T vlane(const GuestContext *c, unsigned d, unsigned lane) {
    T x;
    std::memcpy(&x, reinterpret_cast<const uint8_t *>(&c->vfp.d[d]) + lane * sizeof(T), sizeof(T));
    return x;
}

template <typename T>
inline void vsetlane(GuestContext *c, unsigned d, unsigned lane, T x) {
    std::memcpy(reinterpret_cast<uint8_t *>(&c->vfp.d[d]) + lane * sizeof(T), &x, sizeof(T));
}

inline void set_qc(GuestContext *c, bool sat) {
    if (sat)
        c->fpscr |= 1u << 27;
}

// --- element-wise templates: F is a functor taking the lanes and a saturation flag --------------

template <typename T, typename R, int N, typename F>
inline void map1(GuestContext *c, unsigned d, unsigned m, F f) {
    const auto a = vget<T, N>(c, m);
    Vec<R, N> r;
    bool sat = false;
    for (int i = 0; i < N; i++)
        r.e[i] = f(a.e[i], sat);
    vset(c, d, r);
    set_qc(c, sat);
}

template <typename T, typename R, int N, typename F>
inline void map2(GuestContext *c, unsigned d, unsigned n, unsigned m, F f) {
    const auto a = vget<T, N>(c, n);
    const auto b = vget<T, N>(c, m);
    Vec<R, N> r;
    bool sat = false;
    for (int i = 0; i < N; i++)
        r.e[i] = f(a.e[i], b.e[i], sat);
    vset(c, d, r);
    set_qc(c, sat);
}

// second operand: one lane of d<m>
template <typename T, int N, typename F>
inline void map2s(GuestContext *c, unsigned d, unsigned n, unsigned m, unsigned lane, F f) {
    const auto a = vget<T, N>(c, n);
    const T b = vlane<T>(c, m, lane);
    Vec<T, N> r;
    bool sat = false;
    for (int i = 0; i < N; i++)
        r.e[i] = f(a.e[i], b, sat);
    vset(c, d, r);
    set_qc(c, sat);
}

// accumulating: f(d, n, m)
template <typename T, int N, typename F>
inline void map3(GuestContext *c, unsigned d, unsigned n, unsigned m, F f) {
    const auto acc = vget<T, N>(c, d);
    const auto a = vget<T, N>(c, n);
    const auto b = vget<T, N>(c, m);
    Vec<T, N> r;
    bool sat = false;
    for (int i = 0; i < N; i++)
        r.e[i] = f(acc.e[i], a.e[i], b.e[i], sat);
    vset(c, d, r);
    set_qc(c, sat);
}

template <typename T, int N, typename F>
inline void map3s(GuestContext *c, unsigned d, unsigned n, unsigned m, unsigned lane, F f) {
    const auto acc = vget<T, N>(c, d);
    const auto a = vget<T, N>(c, n);
    const T b = vlane<T>(c, m, lane);
    Vec<T, N> r;
    bool sat = false;
    for (int i = 0; i < N; i++)
        r.e[i] = f(acc.e[i], a.e[i], b, sat);
    vset(c, d, r);
    set_qc(c, sat);
}

// long: d (q) = f(widen(n), widen(m)) with n, m d registers
template <typename TN, typename TW, int N, typename F>
inline void long2(GuestContext *c, unsigned d, unsigned n, unsigned m, F f) {
    const auto a = vget<TN, N>(c, n);
    const auto b = vget<TN, N>(c, m);
    Vec<TW, N> r;
    bool sat = false;
    for (int i = 0; i < N; i++)
        r.e[i] = f(TW(a.e[i]), TW(b.e[i]), sat);
    vset(c, d, r);
    set_qc(c, sat);
}

template <typename TN, typename TW, int N, typename F>
inline void long3(GuestContext *c, unsigned d, unsigned n, unsigned m, F f) {
    const auto acc = vget<TW, N>(c, d);
    const auto a = vget<TN, N>(c, n);
    const auto b = vget<TN, N>(c, m);
    Vec<TW, N> r;
    bool sat = false;
    for (int i = 0; i < N; i++)
        r.e[i] = f(acc.e[i], TW(a.e[i]), TW(b.e[i]), sat);
    vset(c, d, r);
    set_qc(c, sat);
}

// wide: d (q) = f(n (q), widen(m (d)))
template <typename TN, typename TW, int N, typename F>
inline void wide2(GuestContext *c, unsigned d, unsigned n, unsigned m, F f) {
    const auto a = vget<TW, N>(c, n);
    const auto b = vget<TN, N>(c, m);
    Vec<TW, N> r;
    bool sat = false;
    for (int i = 0; i < N; i++)
        r.e[i] = f(a.e[i], TW(b.e[i]), sat);
    vset(c, d, r);
    set_qc(c, sat);
}

// narrow: d (d) = f(m (q)) -> TN
template <typename TW, typename TN, int N, typename F>
inline void narrow1(GuestContext *c, unsigned d, unsigned m, F f) {
    const auto a = vget<TW, N>(c, m);
    Vec<TN, N> r;
    bool sat = false;
    for (int i = 0; i < N; i++)
        r.e[i] = f(a.e[i], sat);
    vset(c, d, r);
    set_qc(c, sat);
}

template <typename TN, typename TW, int N>
inline void widen1(GuestContext *c, unsigned d, unsigned m) {
    const auto a = vget<TN, N>(c, m);
    Vec<TW, N> r;
    for (int i = 0; i < N; i++)
        r.e[i] = TW(a.e[i]);
    vset(c, d, r);
}

// pairwise (d registers): r = f(n0,n1), f(n2,n3), ..., f(m0,m1), ...
template <typename T, int N, typename F>
inline void pairwise(GuestContext *c, unsigned d, unsigned n, unsigned m, F f) {
    const auto a = vget<T, N>(c, n);
    const auto b = vget<T, N>(c, m);
    Vec<T, N> r;
    bool sat = false;
    for (int i = 0; i < N / 2; i++) {
        r.e[i] = f(a.e[2 * i], a.e[2 * i + 1], sat);
        r.e[N / 2 + i] = f(b.e[2 * i], b.e[2 * i + 1], sat);
    }
    vset(c, d, r);
}

// --- moves and permutations ----------------------------------------------------------------------

template <typename T, int N>
inline void vdup(GuestContext *c, unsigned d, T x) {
    Vec<T, N> r;
    for (int i = 0; i < N; i++)
        r.e[i] = x;
    vset(c, d, r);
}

// vext: bytes [imm, imm + size) of n:m, size 8 (d) or 16 (q)
template <int Size>
inline void vext(GuestContext *c, unsigned d, unsigned n, unsigned m, unsigned imm) {
    uint8_t both[2 * Size], r[Size];
    std::memcpy(both, &c->vfp.d[n], Size);
    std::memcpy(both + Size, &c->vfp.d[m], Size);
    std::memcpy(r, both + imm, Size);
    std::memcpy(&c->vfp.d[d], r, Size);
}

// vrev64: reverse the T elements of every 64-bit doubleword
template <typename T, int Dwords>
inline void vrev64(GuestContext *c, unsigned d, unsigned m) {
    constexpr int per = 8 / sizeof(T);
    const auto a = vget<T, per * Dwords>(c, m);
    Vec<T, per * Dwords> r;
    for (int w = 0; w < Dwords; w++)
        for (int i = 0; i < per; i++)
            r.e[w * per + i] = a.e[w * per + per - 1 - i];
    vset(c, d, r);
}

// vtrn: Vd[2i+1] <-> Vm[2i]
template <typename T, int N>
inline void vtrn(GuestContext *c, unsigned d, unsigned m) {
    auto a = vget<T, N>(c, d);
    auto b = vget<T, N>(c, m);
    for (int i = 0; i < N / 2; i++) {
        const T t = a.e[2 * i + 1];
        a.e[2 * i + 1] = b.e[2 * i];
        b.e[2 * i] = t;
    }
    vset(c, d, a);
    vset(c, m, b);
}

// vzip: interleave d and m; the low half goes to d, the high half to m
template <typename T, int N>
inline void vzip(GuestContext *c, unsigned d, unsigned m) {
    const auto a = vget<T, N>(c, d);
    const auto b = vget<T, N>(c, m);
    T z[2 * N];
    for (int i = 0; i < N; i++) {
        z[2 * i] = a.e[i];
        z[2 * i + 1] = b.e[i];
    }
    Vec<T, N> lo, hi;
    for (int i = 0; i < N; i++) {
        lo.e[i] = z[i];
        hi.e[i] = z[N + i];
    }
    vset(c, d, lo);
    vset(c, m, hi);
}

// vtbl.8 d, {n .. n+len-1}, m: byte lookup, out of range -> 0
inline void vtbl(GuestContext *c, unsigned d, unsigned n, unsigned len, unsigned m) {
    uint8_t table[32];
    std::memcpy(table, &c->vfp.d[n], 8 * len);
    const auto idx = vget<uint8_t, 8>(c, m);
    Vec<uint8_t, 8> r;
    for (int i = 0; i < 8; i++)
        r.e[i] = idx.e[i] < 8 * len ? table[idx.e[i]] : 0;
    vset(c, d, r);
}

// --- integer lane operations ---------------------------------------------------------------------

template <typename T>
using Wider = std::conditional_t<std::is_signed_v<T>, int64_t, uint64_t>;

template <typename T>
inline T saturate(int64_t v, bool &sat) {
    constexpr int64_t lo = static_cast<int64_t>(std::numeric_limits<T>::min());
    constexpr int64_t hi = static_cast<int64_t>(std::numeric_limits<T>::max());
    if (v < lo) {
        sat = true;
        return static_cast<T>(lo);
    }
    if (v > hi) {
        sat = true;
        return static_cast<T>(hi);
    }
    return static_cast<T>(v);
}

struct IAdd { template <typename T> T operator()(T a, T b, bool &) const { return T(a + b); } };
struct ISub { template <typename T> T operator()(T a, T b, bool &) const { return T(a - b); } };
struct IMul { template <typename T> T operator()(T a, T b, bool &) const { return T(a * b); } };
struct IMla { template <typename T> T operator()(T acc, T a, T b, bool &) const { return T(acc + a * b); } };
struct IMls { template <typename T> T operator()(T acc, T a, T b, bool &) const { return T(acc - a * b); } };
struct IMax { template <typename T> T operator()(T a, T b, bool &) const { return a > b ? a : b; } };
struct IMin { template <typename T> T operator()(T a, T b, bool &) const { return a < b ? a : b; } };
struct INeg { template <typename T> T operator()(T a, bool &) const { return T(T(0) - a); } };
struct IAbs { template <typename T> T operator()(T a, bool &) const { return a < 0 ? T(T(0) - a) : a; } };
struct IMvn { template <typename T> T operator()(T a, bool &) const { return T(~a); } };
struct IAnd { template <typename T> T operator()(T a, T b, bool &) const { return T(a & b); } };
struct IOrr { template <typename T> T operator()(T a, T b, bool &) const { return T(a | b); } };
struct IEor { template <typename T> T operator()(T a, T b, bool &) const { return T(a ^ b); } };
struct IBic { template <typename T> T operator()(T a, T b, bool &) const { return T(a & ~b); } };
struct IOrn { template <typename T> T operator()(T a, T b, bool &) const { return T(a | ~b); } };
// bitwise selects, accumulating form f(d, n, m)
struct IBsl { template <typename T> T operator()(T d, T n, T m, bool &) const { return T((d & n) | (~d & m)); } };
struct IBit { template <typename T> T operator()(T d, T n, T m, bool &) const { return T((n & m) | (d & ~m)); } };
struct IBif { template <typename T> T operator()(T d, T n, T m, bool &) const { return T((d & m) | (n & ~m)); } };

template <typename T>
inline T mask(bool b) { return b ? T(~T(0)) : T(0); }

struct ICeq { template <typename T> T operator()(T a, T b, bool &) const { return mask<T>(a == b); } };
struct ICge { template <typename T> T operator()(T a, T b, bool &) const { return mask<T>(a >= b); } };
struct ICgt { template <typename T> T operator()(T a, T b, bool &) const { return mask<T>(a > b); } };
struct ITst { template <typename T> T operator()(T a, T b, bool &) const { return mask<T>((a & b) != 0); } };

struct IQAdd { template <typename T> T operator()(T a, T b, bool &s) const { return saturate<T>(int64_t(a) + int64_t(b), s); } };
struct IQSub { template <typename T> T operator()(T a, T b, bool &s) const { return saturate<T>(int64_t(a) - int64_t(b), s); } };
struct IQAbs { template <typename T> T operator()(T a, bool &s) const { return saturate<T>(a < 0 ? -int64_t(a) : int64_t(a), s); } };

// comparisons against #0 (vceq/vcge/vcgt/vcle/vclt #0): the zero as the right or the left operand
template <typename F>
struct ZeroRight {
    F f;
    template <typename T> auto operator()(T a, bool &s) const { return f(a, T(0), s); }
};
template <typename F>
struct ZeroLeft {
    F f;
    template <typename T> auto operator()(T a, bool &s) const { return f(T(0), a, s); }
};

// immediate shifts; Amount may equal the element size for right shifts
struct IShrImm {
    unsigned amount;
    template <typename T> T operator()(T a, bool &) const {
        constexpr unsigned bits = sizeof(T) * 8;
        if (amount >= bits)
            return std::is_signed_v<T> ? T(a < 0 ? -1 : 0) : T(0);
        return T(a >> amount);
    }
};
struct ISraImm {
    unsigned amount;
    template <typename T> T operator()(T acc, T a, T, bool &s) const { return T(acc + IShrImm{amount}(a, s)); }
};
struct IShlImm {
    unsigned amount;
    template <typename T> T operator()(T a, bool &) const { return amount >= sizeof(T) * 8 ? T(0) : T(std::make_unsigned_t<T>(a) << amount); }
};
// vshl (register): the shift is the signed low byte of each lane of m; negative shifts right
struct IShlReg {
    template <typename T> T operator()(T a, T b, bool &) const {
        constexpr int bits = sizeof(T) * 8;
        const int shift = static_cast<int8_t>(static_cast<uint8_t>(b));
        if (shift >= 0)
            return shift >= bits ? T(0) : T(std::make_unsigned_t<T>(a) << shift);
        if (-shift >= bits)
            return std::is_signed_v<T> ? T(a < 0 ? -1 : 0) : T(0);
        return T(a >> -shift);
    }
};

struct ICnt { template <typename T> T operator()(T a, bool &) const { return T(std::popcount(std::make_unsigned_t<T>(a))); } };
struct IClz { template <typename T> T operator()(T a, bool &) const { return T(std::countl_zero(std::make_unsigned_t<T>(a))); } };
// shift and insert: vsli keeps the low `amount` bits of d, vsri the high ones (f(d, m, m))
struct ISliImm {
    unsigned amount;
    template <typename T> T operator()(T d, T m, T, bool &) const {
        using U = std::make_unsigned_t<T>;
        const U keep = amount >= sizeof(T) * 8 ? U(~U(0)) : U((U(1) << amount) - 1);
        const U shifted = amount >= sizeof(T) * 8 ? U(0) : U(U(m) << amount);
        return T(shifted | (U(d) & keep));
    }
};
struct ISriImm {
    unsigned amount;
    template <typename T> T operator()(T d, T m, T, bool &) const {
        using U = std::make_unsigned_t<T>;
        constexpr unsigned bits = sizeof(T) * 8;
        const U keep = amount >= bits ? U(~U(0)) : U(~(U(~U(0)) >> amount));
        const U shifted = amount >= bits ? U(0) : U(U(m) >> amount);
        return T(shifted | (U(d) & keep));
    }
};

// a two-operand functor with its second operand fixed (vbic/vorr #imm)
template <typename F, typename T>
struct WithImm {
    T imm;
    T operator()(T a, bool &s) const { return F{}(a, imm, s); }
};

// vshll: each narrow lane of m widened (by its sign), then shifted left; N narrow lanes
template <typename TN, typename TW, int N>
inline void shll(GuestContext *c, unsigned d, unsigned m, unsigned shift) {
    const auto a = vget<TN, N>(c, m);
    Vec<TW, N> r;
    for (int i = 0; i < N; i++)
        r.e[i] = TW(std::make_unsigned_t<TW>(TW(a.e[i])) << shift);
    vset(c, d, r);
}

// vpaddl: adjacent pairs of m added into lanes twice as wide; NW wide lanes
template <typename TN, typename TW, int NW>
inline void paddl(GuestContext *c, unsigned d, unsigned m) {
    const auto a = vget<TN, 2 * NW>(c, m);
    Vec<TW, NW> r;
    for (int i = 0; i < NW; i++)
        r.e[i] = TW(TW(a.e[2 * i]) + TW(a.e[2 * i + 1]));
    vset(c, d, r);
}

// vswp: exchange two d (Count 1) or q (Count 2) registers
template <int Count>
inline void vswp(GuestContext *c, unsigned d, unsigned m) {
    for (int i = 0; i < Count; i++) {
        const uint64_t t = c->vfp.d[d + i];
        c->vfp.d[d + i] = c->vfp.d[m + i];
        c->vfp.d[m + i] = t;
    }
}

// narrowing: N is the narrow type
template <typename TN>
struct NarrowTrunc {
    unsigned shift = 0;
    template <typename TW> TN operator()(TW a, bool &) const { return TN(a >> shift); }
};
template <typename TN>
struct NarrowSat {
    unsigned shift = 0;
    template <typename TW> TN operator()(TW a, bool &s) const { return saturate<TN>(int64_t(a >> shift), s); }
};

// --- floating point (standard FPSCR value) ----------------------------------------------------------

inline uint32_t fbits(float x) {
    uint32_t u;
    std::memcpy(&u, &x, 4);
    return u;
}
inline float ffrom(uint32_t u) {
    float x;
    std::memcpy(&x, &u, 4);
    return x;
}
inline float nf_in(float x) {
    const uint32_t u = fbits(x);
    return (u & 0x7f800000u) == 0 ? ffrom(u & 0x80000000u) : x;
}
inline bool nf_nan(float x) {
    return (fbits(x) & 0x7fffffffu) > 0x7f800000u;
}
inline float nf_out(float r) {
    const uint32_t u = fbits(r);
    if ((u & 0x7f800000u) == 0)
        return ffrom(u & 0x80000000u);
    if ((u & 0x7fffffffu) > 0x7f800000u)
        return ffrom(0x7fc00000u);
    return r;
}

struct FAdd { float operator()(float a, float b, bool &) const { return nf_out(nf_in(a) + nf_in(b)); } };
struct FSub { float operator()(float a, float b, bool &) const { return nf_out(nf_in(a) - nf_in(b)); } };
struct FMul { float operator()(float a, float b, bool &) const { return nf_out(nf_in(a) * nf_in(b)); } };
// vmla/vmls: an unfused multiply, then the add (FPNeg of the product for vmls)
struct FMla { float operator()(float acc, float a, float b, bool &s) const { return FAdd{}(acc, FMul{}(a, b, s), s); } };
struct FMls { float operator()(float acc, float a, float b, bool &s) const { return FAdd{}(acc, ffrom(fbits(FMul{}(a, b, s)) ^ 0x80000000u), s); } };
struct FMax {
    float operator()(float a, float b, bool &) const {
        a = nf_in(a);
        b = nf_in(b);
        if (nf_nan(a) || nf_nan(b))
            return ffrom(0x7fc00000u);
        if (a == b)
            return ffrom(fbits(a) & fbits(b)); // max(+0, -0) = +0
        return a > b ? a : b;
    }
};
struct FMin {
    float operator()(float a, float b, bool &) const {
        a = nf_in(a);
        b = nf_in(b);
        if (nf_nan(a) || nf_nan(b))
            return ffrom(0x7fc00000u);
        if (a == b)
            return ffrom(fbits(a) | fbits(b)); // min(+0, -0) = -0
        return a < b ? a : b;
    }
};
// sign-bit operations, no flushing
struct FNeg { uint32_t operator()(uint32_t a, bool &) const { return a ^ 0x80000000u; } };
struct FAbs { uint32_t operator()(uint32_t a, bool &) const { return a & 0x7fffffffu; } };
// comparisons: all ones when true, NaN compares false
struct FCeq { uint32_t operator()(float a, float b, bool &) const { return mask<uint32_t>(nf_in(a) == nf_in(b)); } };
struct FCge { uint32_t operator()(float a, float b, bool &) const { return mask<uint32_t>(nf_in(a) >= nf_in(b)); } };
struct FCgt { uint32_t operator()(float a, float b, bool &) const { return mask<uint32_t>(nf_in(a) > nf_in(b)); } };
struct FAcge { uint32_t operator()(float a, float b, bool &) const { return mask<uint32_t>(std::fabs(nf_in(a)) >= std::fabs(nf_in(b))); } };
struct FAcgt { uint32_t operator()(float a, float b, bool &) const { return mask<uint32_t>(std::fabs(nf_in(a)) > std::fabs(nf_in(b))); } };

// conversions
struct FToS32 {
    int32_t operator()(float a, bool &) const {
        a = nf_in(a);
        if (a != a)
            return 0;
        if (a >= 2147483648.0f)
            return std::numeric_limits<int32_t>::max();
        if (a <= -2147483648.0f)
            return std::numeric_limits<int32_t>::min();
        return static_cast<int32_t>(a);
    }
};
struct FToU32 {
    uint32_t operator()(float a, bool &) const {
        a = nf_in(a);
        if (a != a || a <= 0.0f)
            return 0;
        if (a >= 4294967296.0f)
            return std::numeric_limits<uint32_t>::max();
        return static_cast<uint32_t>(a);
    }
};
struct S32ToF { float operator()(int32_t a, bool &) const { return static_cast<float>(a); } };
struct U32ToF { float operator()(uint32_t a, bool &) const { return static_cast<float>(a); } };

// reciprocal estimate (dynarmic FPRecipEstimate with FZ, DN, round to nearest)
inline uint32_t recip_estimate_lut(uint32_t a) { // a in [256, 512): u1.8
    const uint32_t x = a * 2 + 1;
    const uint32_t b = (1u << 19) / x;
    return (b + 1) / 2;
}
// The estimates of zero (or a flushed denormal) set FPSCR.DZC, like vdiv (fp_div in armrecomp.h)
struct FRecpe {
    GuestContext *c;
    uint32_t operator()(uint32_t op, bool &) const {
        const uint32_t sign = op & 0x80000000u;
        const uint32_t exp = (op >> 23) & 0xff, frac = op & 0x7fffff;
        if (exp == 0xff)
            return frac ? 0x7fc00000u : sign; // NaN -> default NaN; inf -> 0
        if (exp == 0) {
            c->fpscr |= 2u;
            return sign | 0x7f800000u; // zero or flushed denormal -> inf
        }
        const int e = int(exp) - 127;
        if (e >= 126)
            return sign; // the result would be denormal: flushed
        const uint32_t scaled = (frac | 0x800000u) >> 15; // 9 bits, [256, 512)
        const uint32_t estimate = recip_estimate_lut(scaled) << 15;
        const int result_exponent = -(e + 1);
        return sign | (uint32_t(result_exponent + 127) << 23) | (estimate & 0x7fffffu);
    }
};

inline uint32_t rsqrt_estimate_lut(uint32_t a) { // a: u0.9 in [128, 512)
    uint64_t x = a < 256 ? a * 2 + 1 : (a | 1) * 2;
    uint64_t b = 512;
    while (x * (b + 1) * (b + 1) < (1ull << 28))
        b++;
    return uint32_t((b + 1) / 2);
}
struct FRsqrte {
    GuestContext *c;
    uint32_t operator()(uint32_t op, bool &) const {
        const uint32_t sign = op & 0x80000000u;
        const uint32_t exp = (op >> 23) & 0xff, frac = op & 0x7fffff;
        if (exp == 0xff && frac)
            return 0x7fc00000u;
        if (exp == 0) {
            c->fpscr |= 2u;
            return sign | 0x7f800000u;
        }
        if (sign)
            return 0x7fc00000u;
        if (exp == 0xff)
            return 0; // +inf -> +0
        const int e = int(exp) - 127;
        const int result_exponent = (-(e + 1)) >> 1;
        const bool odd = e % 2 == 0;
        const uint64_t mantissa = uint64_t(frac | 0x800000u) << (62 - 23);
        const uint32_t scaled = uint32_t(mantissa >> (62 - (odd ? 7 : 8)));
        const uint32_t estimate = rsqrt_estimate_lut(scaled & 0x1ff);
        return (uint32_t(result_exponent + 127) << 23) | ((estimate << 15) & 0x7fffffu);
    }
};

// steps (fused, dynarmic FPRecipStepFused / FPRSqrtStepFused): 2 - a*b and (3 - a*b) / 2
struct FRecps {
    float operator()(float x, float y, bool &) const {
        const float a = nf_in(ffrom(fbits(x) ^ 0x80000000u)), b = nf_in(y);
        if (nf_nan(a) || nf_nan(b))
            return ffrom(0x7fc00000u);
        const bool inf_a = std::isinf(a), inf_b = std::isinf(b);
        if ((inf_a && b == 0.0f) || (a == 0.0f && inf_b))
            return 2.0f;
        if (inf_a || inf_b)
            return ffrom(((fbits(a) ^ fbits(b)) & 0x80000000u) | 0x7f800000u);
        const float r = std::fmaf(a, b, 2.0f);
        return r == 0.0f ? 0.0f : nf_out(r);
    }
};
struct FRsqrts {
    float operator()(float x, float y, bool &) const {
        const float a = nf_in(ffrom(fbits(x) ^ 0x80000000u)), b = nf_in(y);
        if (nf_nan(a) || nf_nan(b))
            return ffrom(0x7fc00000u);
        const bool inf_a = std::isinf(a), inf_b = std::isinf(b);
        if ((inf_a && b == 0.0f) || (a == 0.0f && inf_b))
            return 1.5f;
        if (inf_a || inf_b)
            return ffrom(((fbits(a) ^ fbits(b)) & 0x80000000u) | 0x7f800000u);
        // (3 + a*b) / 2 rounded once: 1.5 + a*(b/2), where b/2 is exact unless b is tiny (and then
        // nothing overflows, so the halving can follow the fused operation)
        const float r = std::fabs(b) >= 0x1p-125f ? std::fmaf(a, b * 0.5f, 1.5f) : std::fmaf(a, b, 3.0f) * 0.5f;
        return r == 0.0f ? 0.0f : nf_out(r);
    }
};

// --- loads and stores (vld1/vst1) -------------------------------------------------------------------

inline void vld1_regs(GuestContext *c, unsigned d, unsigned count, uint32_t addr) {
    for (unsigned i = 0; i < count; i++)
        c->vfp.d[d + i] = rd64(addr + 8 * i);
}
inline void vst1_regs(GuestContext *c, unsigned d, unsigned count, uint32_t addr) {
    for (unsigned i = 0; i < count; i++)
        wr64(addr + 8 * i, c->vfp.d[d + i]);
}
template <typename T>
inline T rdT(uint32_t a);
template <typename T>
inline void wrT(uint32_t a, T v);

// vld2..vld4 / vst2..vst4, multiple Structs-element structures: memory holds the structures one
// after another; `regs` lists, for each block of 8 bytes per member, the members' d registers in
// structure order (Structs * Per entries)
template <typename T, int Structs>
inline void vldn(GuestContext *c, const uint8_t *regs, unsigned per, uint32_t addr) {
    constexpr unsigned lanes = 8 / sizeof(T);
    for (unsigned r = 0; r < per; r++)
        for (unsigned e = 0; e < lanes; e++)
            for (unsigned k = 0; k < Structs; k++, addr += sizeof(T))
                vsetlane<T>(c, regs[r * Structs + k], e, rdT<T>(addr));
}
template <typename T, int Structs>
inline void vstn(GuestContext *c, const uint8_t *regs, unsigned per, uint32_t addr) {
    constexpr unsigned lanes = 8 / sizeof(T);
    for (unsigned r = 0; r < per; r++)
        for (unsigned e = 0; e < lanes; e++)
            for (unsigned k = 0; k < Structs; k++, addr += sizeof(T))
                wrT<T>(addr, vlane<T>(c, regs[r * Structs + k], e));
}

template <typename T>
inline T rdT(uint32_t a) {
    if constexpr (sizeof(T) == 1) return T(rd8(a));
    else if constexpr (sizeof(T) == 2) return T(rd16(a));
    else if constexpr (sizeof(T) == 4) return T(rd32(a));
    else return T(rd64(a));
}
template <typename T>
inline void wrT(uint32_t a, T v) {
    if constexpr (sizeof(T) == 1) wr8(a, uint8_t(v));
    else if constexpr (sizeof(T) == 2) wr16(a, uint16_t(v));
    else if constexpr (sizeof(T) == 4) wr32(a, uint32_t(v));
    else wr64(a, uint64_t(v));
}

} // namespace armrecomp
