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
import glob
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import msvc  # noqa: E402
import toolchain  # noqa: E402

RECOMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'recomp')


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
        build = os.path.join(root, 'build', 'recomp-llvm')
        toolchain.build_modules(gen, modules, build, a.toolchain, a.jobs)
    else:
        build = build_with_cmake(root, gen, modules, a.jobs)
    dlls = sorted(glob.glob(os.path.join(build, '*.dll')))
    print(f'built in {(time.time() - start) / 60:.1f} min: ' + ', '.join(os.path.basename(d) for d in dlls))


if __name__ == '__main__':
    main()
