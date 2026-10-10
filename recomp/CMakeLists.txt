# Builds the recompiled modules (generated/<module>/ from recomp/recompile.py) into static libraries,
# and the dynarmic comparison tests (recomp/tests). See docs/decisions.md D6.
#   cmake -S recomp -B build/recomp -G Ninja -DCMAKE_BUILD_TYPE=Release
#   cmake --build build/recomp      # gen_<module>.lib and ssd_aot_<module>.dll
cmake_minimum_required(VERSION 3.20)
project(armrecomp CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

get_filename_component(SSD_ROOT "${CMAKE_SOURCE_DIR}/.." ABSOLUTE)
set(GENERATED_DIR "${SSD_ROOT}/generated" CACHE PATH "recompiler output")
set(RECOMP_MODULES "eboot;libc;libfios2;libult" CACHE STRING "modules to build")
# generated buckets are big: limit parallel compiles to what the RAM allows
set(RECOMP_JOBS 4 CACHE STRING "parallel compiles of generated code")
set_property(GLOBAL APPEND PROPERTY JOB_POOLS recomp_pool=${RECOMP_JOBS})
# count the guest instructions native code runs (GuestContext::insns), for the fork's counters
option(RECOMP_COUNT_INSNS "count executed guest instructions in generated code" ON)
# route direct calls between recompiled functions through the host (debugging: SSD_AOT_RANGE then
# selects every native function, not only JIT entry points)
option(RECOMP_BISECT "direct calls go through call_indirect" OFF)

foreach(module IN LISTS RECOMP_MODULES)
    file(GLOB srcs CONFIGURE_DEPENDS "${GENERATED_DIR}/${module}/*.cpp")
    if(NOT srcs)
        message(WARNING "no generated code for ${module} in ${GENERATED_DIR}/${module}")
        continue()
    endif()
    add_library(gen_${module} STATIC ${srcs})
    target_include_directories(gen_${module} PRIVATE runtime "${GENERATED_DIR}/${module}")
    set_property(TARGET gen_${module} PROPERTY JOB_POOL_COMPILE recomp_pool)
    if(MSVC)
        # huge functions: /bigobj; keep optimisation moderate (compile time and memory)
        target_compile_options(gen_${module} PRIVATE /bigobj /O1 /w)
    endif()
    if(RECOMP_COUNT_INSNS)
        target_compile_definitions(gen_${module} PRIVATE RECOMP_COUNT_INSNS)
    endif()
    if(RECOMP_BISECT)
        target_compile_definitions(gen_${module} PRIVATE RECOMP_BISECT)
    endif()
    # the module as a DLL the Vita3K fork loads (SSD_AOT, docs/decisions.md D7)
    add_library(ssd_aot_${module} SHARED runtime/aot_dll.cpp)
    target_include_directories(ssd_aot_${module} PRIVATE runtime)
    target_link_libraries(ssd_aot_${module} PRIVATE gen_${module})
endforeach()

# --- per-instruction comparison tests against dynarmic ------------------------------------------
# Links the dynarmic that our Vita3K build (tools/build-vita3k.ps1, RelWithDebInfo) already built.
option(RECOMP_TESTS "build recomp_tests (needs generated/tests/<module>/, from make_tests.py)" ON)
set(RECOMP_TEST_MODULE "eboot" CACHE STRING "module whose generated tests to build")
set(VITA3K_SRC "${SSD_ROOT}/third_party/vita3k" CACHE PATH "")
set(VITA3K_BUILD "${SSD_ROOT}/build/vita3k" CACHE PATH "")
set(VITA3K_CONFIG "RelWithDebInfo" CACHE STRING "")
set(test_dir "${GENERATED_DIR}/tests/${RECOMP_TEST_MODULE}")
if(RECOMP_TESTS AND EXISTS "${test_dir}/tests.cpp")
    add_executable(recomp_tests tests/harness.cpp "${test_dir}/tests.cpp" "${test_dir}/functions.cpp")
    target_compile_definitions(recomp_tests PRIVATE RECOMP_CUSTOM_MEMORY RECOMP_TEST_BUDGET)
    target_include_directories(recomp_tests PRIVATE runtime tests
        "${VITA3K_SRC}/external/dynarmic/src"
        "${VITA3K_SRC}/external/dynarmic/externals/mcl/include"
        "${VITA3K_SRC}/external/fmt/include")
    set(dyn "${VITA3K_BUILD}/external/dynarmic")
    target_link_libraries(recomp_tests PRIVATE
        "${dyn}/src/dynarmic/${VITA3K_CONFIG}/dynarmic.lib"
        "${dyn}/externals/mcl/src/${VITA3K_CONFIG}/mcl.lib"
        "${dyn}/externals/zydis/${VITA3K_CONFIG}/Zydis.lib"
        "${dyn}/externals/zydis/zycore/${VITA3K_CONFIG}/Zycore.lib"
        "${VITA3K_BUILD}/external/fmt/${VITA3K_CONFIG}/fmt.lib")
    if(MSVC)
        target_compile_options(recomp_tests PRIVATE /bigobj /EHsc)
    endif()
endif()
