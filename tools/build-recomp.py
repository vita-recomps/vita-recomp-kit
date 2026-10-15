"""Build a game project's recompiled modules (generated/<module>/, from recomp/recompile.py) into
<prefix><module>.dll.

usage:
  python -I build-recomp.py [--project DIR] [--modules eboot libc ...] [--jobs N] [--toolchain DIR]

  --modules    default: every folder in generated/ that has a funcs.h
  --jobs       parallel compiles of generated code (default 4; memory, not cores, is the usual
               limit: the largest buckets take ~0.5 GB each)
  --toolchain  build with the clang-cl toolchain toolchain.py unpacked into DIR, without Visual Studio,
               CMake or Ninja, into build/recomp-llvm; otherwise MSVC, CMake and Ninja, into build/recomp
"""
import argparse
import concurrent.futures
import glob
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import msvc  # noqa: E402

RECOMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'recomp')
RUNTIME = os.path.join(RECOMP, 'runtime')
DLL_PREFIX = 'ssd_aot_'  # the name the Vita3K fork loads (RECOMP_DLL_PREFIX in recomp/CMakeLists.txt)
# recomp/CMakeLists.txt's Release flags, as clang-cl takes them
CXXFLAGS = ['/nologo', '/TP', '/DWIN32', '/D_WINDOWS', '/EHsc', '/O2', '/Ob2', '/DNDEBUG', '/std:c++20', '/MD',
            '--target=x86_64-pc-windows-msvc']
GEN_FLAGS = ['/bigobj', '/O1', '/w', '/DRECOMP_COUNT_INSNS']
SYSTEM_LIBS = ['kernel32.lib', 'user32.lib', 'gdi32.lib', 'winspool.lib', 'shell32.lib', 'ole32.lib',
               'oleaut32.lib', 'uuid.lib', 'comdlg32.lib', 'advapi32.lib']


def build_with_cmake(root, gen, modules, jobs):
    build = os.path.join(root, 'build', 'recomp')
    env = msvc.environment()
    configure = ['cmake', '-S', RECOMP, '-B', build, '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release',
                 f'-DGENERATED_DIR={gen}', '-DRECOMP_MODULES=' + ';'.join(modules), '-DRECOMP_TESTS=OFF',
                 f'-DRECOMP_JOBS={jobs}']
    # configure every time: the module list and paths may change; CMake keeps the build incremental
    if subprocess.run(configure, env=env, capture_output=True).returncode:
        sys.exit("configuring the recompiled modules failed")
    if subprocess.run(['cmake', '--build', build], env=env).returncode:
        sys.exit('build failed')
    return build


def run(cmd, env, rsp=None):
    """Run a tool, its arguments in a response file when they are many; return its output if it failed."""
    if rsp:
        with open(rsp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(f'"{a}"' if ' ' in a else a for a in cmd[1:]))
        cmd = [cmd[0], '@' + rsp]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, errors='replace')
    return (r.stdout + r.stderr).strip() if r.returncode else None


def build_with_toolchain(root, gen, modules, jobs, toolchain_dir):
    import toolchain
    tc = toolchain.environment(toolchain_dir)
    env = dict(os.environ, INCLUDE=tc['INCLUDE'], LIB=tc['LIB'])
    env['PATH'] = tc['path'] + os.pathsep + env.get('PATH', '')
    build = os.path.join(root, 'build', 'recomp-llvm')
    headers = max(os.path.getmtime(h) for h in glob.glob(os.path.join(RUNTIME, '*.h')))

    # compile: every generated file whose object is older than it or than the headers
    work = []
    for m in modules:
        obj_dir = os.path.join(build, 'obj', m)
        os.makedirs(obj_dir, exist_ok=True)
        newest = max(headers, *(os.path.getmtime(h) for h in glob.glob(os.path.join(gen, m, '*.h'))))
        for src in sorted(glob.glob(os.path.join(gen, m, '*.cpp'))):
            obj = os.path.join(obj_dir, os.path.basename(src)[:-4] + '.obj')
            if not os.path.exists(obj) or os.path.getmtime(obj) < max(newest, os.path.getmtime(src)):
                work.append([tc['cl'], *CXXFLAGS, *GEN_FLAGS, '/I', RUNTIME, '/I', os.path.join(gen, m),
                             '/c', src, '/Fo' + obj])
    entry = os.path.join(build, 'obj', 'aot_dll.obj')
    if not os.path.exists(entry) or os.path.getmtime(entry) < max(headers, os.path.getmtime(os.path.join(RUNTIME, 'aot_dll.cpp'))):
        work.append([tc['cl'], *CXXFLAGS, '/I', RUNTIME, '/c', os.path.join(RUNTIME, 'aot_dll.cpp'), '/Fo' + entry])
    print(f'{len(work)} files to compile')
    done = 0
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        for cmd, err in zip(work, pool.map(lambda c: run(c, env), work)):
            done += 1
            if err is not None:
                pool.shutdown(cancel_futures=True)
                sys.exit(f'{cmd[-2]}: compile failed\n{err}')
            if done % 25 == 0 or done == len(work):
                print(f'  {done}/{len(work)}', flush=True)

    # archive and link, as the CMake build does: gen_<module>.lib, then the DLL around aot_dll.obj
    for m in modules:
        objs = sorted(glob.glob(os.path.join(build, 'obj', m, '*.obj')))
        lib = os.path.join(build, f'gen_{m}.lib')
        dll = os.path.join(build, f'{DLL_PREFIX}{m}.dll')
        if os.path.exists(dll) and os.path.getmtime(dll) >= max(os.path.getmtime(o) for o in objs + [entry]):
            continue
        err = run([tc['lib'], '/nologo', '/out:' + lib, *objs], env, os.path.join(build, f'gen_{m}.rsp'))
        if err is None:
            err = run([tc['link'], '/nologo', '/dll', '/machine:x64', '/INCREMENTAL:NO', '/out:' + dll,
                       '/implib:' + os.path.join(build, f'{DLL_PREFIX}{m}.lib'), entry, lib, *SYSTEM_LIBS], env)
        if err is not None:
            sys.exit(f'{m}: linking failed\n{err}')
    return build


def main():
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument('--project', default=os.getcwd())
    ap.add_argument('--modules', nargs='*', default=[])
    ap.add_argument('--jobs', type=int, default=4)
    ap.add_argument('--toolchain')
    a = ap.parse_args()

    root = os.path.abspath(a.project)
    gen = os.path.join(root, 'generated')
    modules = a.modules or sorted(os.path.basename(os.path.dirname(p))
                                  for p in glob.glob(os.path.join(gen, '*', 'funcs.h')))
    if not modules:
        sys.exit(f'nothing to build: no generated/<module>/funcs.h in {root}')
    print(f'building {", ".join(modules)} ({a.jobs} jobs)')
    start = time.time()
    if a.toolchain:
        build = build_with_toolchain(root, gen, modules, a.jobs, a.toolchain)
    else:
        build = build_with_cmake(root, gen, modules, a.jobs)
    dlls = sorted(glob.glob(os.path.join(build, '*.dll')))
    print(f'built in {(time.time() - start) / 60:.1f} min: ' + ', '.join(os.path.basename(d) for d in dlls))


if __name__ == '__main__':
    main()
