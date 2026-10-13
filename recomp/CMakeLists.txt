# Builds the recompiled modules (<GENERATED_DIR>/<module>/ from recomp/recompile.py) into static
# libraries and DLLs, and the dynarmic comparison tests (recomp/tests).
# The game project says where its files are; nothing here assumes a directory layout:
#   cmake -S <this repo>/recomp -B build/recomp -G Ninja -DCMAKE_BUILD_TYPE=Release
#         -DGENERATED_DIR=<game>/generated -DVITA3K_SRC=<fork source> -DVITA3K_BUILD=<fork build>
#   cmake --build build/recomp      # gen_<module>.lib and <RECOMP_DLL_PREFIX><module>.dll
cmake_minimum_required(VERSION 3.20)
project(armrecomp CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(GENERATED_DIR "" CACHE PATH "recompiler output (required): <dir>/<module>/*.cpp, <dir>/tests/<module>/")
if(NOT GENERATED_DIR)
    message(FATAL_ERROR "GENERATED_DIR is not set: pass -DGENERATED_DIR=<dir>, the directory recompile.py "
                        "writes <module>/ into (and make_tests.py tests/<module>/)")
endif()
if(NOT IS_DIRECTORY "${GENERATED_DIR}")
    message(FATAL_ERROR "GENERATED_DIR=${GENERATED_DIR} is not a directory: pass -DGENERATED_DIR=<dir>, "
                        "the directory recompile.py writes <module>/ into")
endif()
set(RECOMP_MODULES "eboot;libc;libfios2;libult" CACHE STRING "modules to build")
# the Vita3K fork loads <prefix><module>.dll (ssd_aot_*, enabled by its SSD_AOT* environment variables)
set(RECOMP_DLL_PREFIX "ssd_aot_" CACHE STRING "file name prefix of the module DLLs the Vita3K fork loads")
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
    # the module as a DLL the Vita3K fork loads (SSD_AOT)
    add_library(${RECOMP_DLL_PREFIX}${module} SHARED runtime/aot_dll.cpp)
    target_include_directories(${RECOMP_DLL_PREFIX}${module} PRIVATE runtime)
    target_link_libraries(${RECOMP_DLL_PREFIX}${module} PRIVATE gen_${module})
endforeach()

# --- per-instruction comparison tests against dynarmic ------------------------------------------
# Links the dynarmic that the Vita3K fork's build (RelWithDebInfo) already built: VITA3K_SRC and
# VITA3K_BUILD are required when the tests are built.
option(RECOMP_TESTS "build recomp_tests (needs <GENERATED_DIR>/tests/<module>/, from make_tests.py)" ON)
set(RECOMP_TEST_MODULE "eboot" CACHE STRING "module whose generated tests to build")
set(VITA3K_SRC "" CACHE PATH "Vita3K fork source tree (the tests' dynarmic headers)")
set(VITA3K_BUILD "" CACHE PATH "Vita3K fork build tree (the tests' dynarmic libraries)")
set(VITA3K_CONFIG "RelWithDebInfo" CACHE STRING "")
set(test_dir "${GENERATED_DIR}/tests/${RECOMP_TEST_MODULE}")
if(RECOMP_TESTS AND EXISTS "${test_dir}/tests.cpp")
    foreach(var VITA3K_SRC VITA3K_BUILD)
        if(NOT ${var} OR NOT IS_DIRECTORY "${${var}}")
            message(FATAL_ERROR "recomp_tests needs ${var} (now '${${var}}'): pass -D${var}=<dir>, "
                                "or -DRECOMP_TESTS=OFF")
        endif()
    endforeach()
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
