"""Build a game project's recompiled modules (generated/<module>/, from recomp/recompile.py) into
<prefix><module>.dll (with zig's toolchain on Linux and macOS: .so, .dylib).

usage:
  python -I build-recomp.py [--project DIR] [--modules eboot libc ...] [--jobs N] [--toolchain DIR]
                            [--bisect] [--android]

  --modules    default: every folder in generated/ that has a funcs.h
  --jobs       parallel compiles of generated code (default 4; memory, not cores, is the usual
               limit: the largest buckets take ~0.5 GB each)
  --toolchain  build with the zig toolchain.py unpacked into DIR (toolchain.py fetch
               toolchain-<platform>.json DIR), without Visual Studio, CMake or Ninja, into build/recomp-zig;
               otherwise MSVC, CMake and Ninja, into build/recomp
  --bisect     a RECOMP_BISECT build (every direct call goes through the host, so SSD_AOT_RANGE can
               bisect down to one function and SSD_AOT_DIFF can check every call), into
               build/recomp-zig-bisect or build/recomp-bisect
  --android    the modules for Android arm64 (.so), with the zig in --toolchain DIR, into
               build/recomp-android-zig
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


def build_with_cmake(root, gen, modules, jobs, bisect=False):
    build = os.path.join(root, 'build', 'recomp-bisect' if bisect else 'recomp')
    env = msvc.environment()
    configure = ['cmake', '-S', RECOMP, '-B', build, '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release',
                 f'-DGENERATED_DIR={gen}', '-DRECOMP_MODULES=' + ';'.join(modules), '-DRECOMP_TESTS=OFF',
                 f'-DRECOMP_JOBS={jobs}', f'-DRECOMP_BISECT={"ON" if bisect else "OFF"}']
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
    ap.add_argument('--bisect', action='store_true')
    ap.add_argument('--android', action='store_true')
    a = ap.parse_args()
    if a.android and not a.toolchain:
        sys.exit('--android needs --toolchain DIR (zig: python -I toolchain.py fetch <lock file> DIR)')

    root = os.path.abspath(a.project)
    gen = os.path.join(root, 'generated')
    modules = a.modules or sorted(os.path.basename(os.path.dirname(p))
                                  for p in glob.glob(os.path.join(gen, '*', 'funcs.h')))
    if not modules:
        sys.exit(f'nothing to build: no generated/<module>/funcs.h in {root}')
    print(f'building {", ".join(modules)} ({a.jobs} jobs)')
    start = time.time()
    if a.toolchain:
        name = 'recomp-android-zig' if a.android else 'recomp-zig'
        build = os.path.join(root, 'build', name + ('-bisect' if a.bisect else ''))
        toolchain.build_modules(gen, modules, build, a.toolchain, a.jobs, ('RECOMP_BISECT',) if a.bisect else (),
                                android=a.android)
    else:
        build = build_with_cmake(root, gen, modules, a.jobs, a.bisect)
    dlls = sorted(f for f in glob.glob(os.path.join(build, toolchain.DLL_PREFIX + '*'))
                  if f.endswith(('.dll', '.so', '.dylib')))
    print(f'built in {(time.time() - start) / 60:.1f} min: ' + ', '.join(os.path.basename(d) for d in dlls))


if __name__ == '__main__':
    main()
