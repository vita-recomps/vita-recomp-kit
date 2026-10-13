"""Configure and build a game project's Vita3K fork (its third_party/vita3k submodule) with MSVC and
Ninja Multi-Config into <project>/build/vita3k. The executables land in
build/vita3k/bin/<config>/ (Vita3K.exe and the game's single-game front end).

usage:
  python -I build-vita3k.py [--project DIR] [--config RelWithDebInfo|Debug|Release] [--jobs N]
                            [--reconfigure] [--no-jit] [--target NAME ...]

  --project   the game project's root (default: the current directory)
  --no-jit    build without dynarmic (VITA3K_DYNARMIC=OFF: recompiled code only) into
              build/vita3k-nojit
  --target    build only these targets (e.g. shinobido2), default everything
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import msvc  # noqa: E402


def main():
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument('--project', default=os.getcwd())
    ap.add_argument('--config', default='RelWithDebInfo', choices=['RelWithDebInfo', 'Debug', 'Release'])
    ap.add_argument('--jobs', type=int, default=0)
    ap.add_argument('--reconfigure', action='store_true')
    ap.add_argument('--no-jit', action='store_true')
    ap.add_argument('--target', nargs='*', default=[])
    a = ap.parse_args()

    root = os.path.abspath(a.project)
    src = os.path.join(root, 'third_party', 'vita3k')
    build = os.path.join(root, 'build', 'vita3k-nojit' if a.no_jit else 'vita3k')
    if not os.path.exists(os.path.join(src, 'external', 'dynarmic', 'CMakeLists.txt')):
        sys.exit(f'submodules missing under {src}: git submodule update --init --recursive')

    env = msvc.environment()
    # Vita3K's presets set this for older external CMakeLists
    env['CMAKE_POLICY_VERSION_MINIMUM'] = '3.5'
    if a.reconfigure or not os.path.exists(os.path.join(build, 'CMakeCache.txt')) \
            or not os.path.exists(os.path.join(build, 'build.ninja')):
        # The first configure bootstraps Boost's b2, whose build.bat runs 'call guess_toolset.bat'
        # from its own folder; that fails when NoDefaultCurrentDirectoryInExePath is set
        configure_env = dict(env)
        configure_env.pop('NoDefaultCurrentDirectoryInExePath', None)
        r = subprocess.run(['cmake', '-S', src, '-B', build, '-G', 'Ninja Multi-Config',
                            '-DCMAKE_C_COMPILER=cl', '-DCMAKE_CXX_COMPILER=cl',
                            '-DVITA3K_DYNARMIC=' + ('OFF' if a.no_jit else 'ON')], env=configure_env)
        if r.returncode:
            sys.exit('cmake configure failed')
    cmd = ['cmake', '--build', build, '--config', a.config]
    if a.jobs:
        cmd += ['-j', str(a.jobs)]
    if a.target:
        cmd += ['--target'] + a.target
    start = time.time()
    if subprocess.run(cmd, env=env).returncode:
        sys.exit('build failed')
    print(f'build ({a.config}) finished in {(time.time() - start) / 60:.1f} min: {os.path.join(build, "bin", a.config)}')


if __name__ == '__main__':
    main()
