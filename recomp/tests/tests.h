// Table produced by recomp/make_tests.py (generated/tests/<module>/tests.cpp).
#pragma once

#include <cstdint>

namespace armrecomp {

struct GuestContext;

struct TestCase {
    uint32_t addr; // original address (pc-relative operands depend on it)
    uint8_t bytes[4];
    uint8_t size; // 2 or 4
    int8_t it_cond; // ARM condition code of an enclosing IT block, -1 if none
    void (*fn)(GuestContext *);
    const char *text;
    const char *form;
};

extern const TestCase g_tests[];
extern const uint32_t g_test_count;

// A whole leaf function (generated/tests/<module>/functions.cpp)
struct FunctionTest {
    uint32_t start;
    uint32_t size; // bytes of code, literal pools included
    const uint8_t *code;
    void (*fn)(GuestContext *);
    uint32_t instructions;
};

extern const FunctionTest g_function_tests[];
extern const uint32_t g_function_test_count;

} // namespace armrecomp
