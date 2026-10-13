"""Build a game project's recompiled modules (generated/<module>/, from recomp/recompile.py) into
<prefix><module>.dll with MSVC and Ninja, in <project>/build/recomp.

usage:
  python -I build-recomp.py [--project DIR] [--modules eboot libc ...] [--jobs N]

  --modules  default: every folder in generated/ that has a funcs.h
  --jobs     parallel compiles of generated code (RECOMP_JOBS, default 4; memory, not cores, is
             the usual limit: the largest buckets take ~0.5 GB each)
"""
import argparse
import glob
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import msvc  # noqa: E402

RECOMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'recomp')


def main():
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument('--project', default=os.getcwd())
    ap.add_argument('--modules', nargs='*', default=[])
    ap.add_argument('--jobs', type=int, default=4)
    a = ap.parse_args()

    root = os.path.abspath(a.project)
    gen = os.path.join(root, 'generated')
    modules = a.modules or sorted(os.path.basename(os.path.dirname(p))
                                  for p in glob.glob(os.path.join(gen, '*', 'funcs.h')))
    if not modules:
        sys.exit(f'nothing to build: no generated/<module>/funcs.h in {root}')
    build = os.path.join(root, 'build', 'recomp')
    env = msvc.environment()
    configure = ['cmake', '-S', RECOMP, '-B', build, '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release',
                 f'-DGENERATED_DIR={gen}', '-DRECOMP_MODULES=' + ';'.join(modules), '-DRECOMP_TESTS=OFF',
                 f'-DRECOMP_JOBS={a.jobs}']
    # configure every time: the module list and paths may change; CMake keeps the build incremental
    if subprocess.run(configure, env=env, capture_output=True).returncode:
        sys.exit("configuring the recompiled modules failed")
    print(f'building {", ".join(modules)} ({a.jobs} jobs)')
    start = time.time()
    if subprocess.run(['cmake', '--build', build], env=env).returncode:
        sys.exit('build failed')
    dlls = sorted(glob.glob(os.path.join(build, '*.dll')))
    print(f'built in {(time.time() - start) / 60:.1f} min: ' + ', '.join(os.path.basename(d) for d in dlls))


if __name__ == '__main__':
    main()
