"""Configure and build a game project's Vita3K fork (its third_party/vita3k submodule) with MSVC and
Ninja Multi-Config into <project>/build/vita3k. The executables land in
build/vita3k/bin/<config>/ (Vita3K.exe and the games' single-game front ends).

usage:
  python -I build-vita3k.py [--project DIR] [--config RelWithDebInfo|Debug|Release] [--jobs N]
                            [--reconfigure] [--configure-only] [--no-jit] [--games-only] [--target NAME ...] [--qt DIR]

  --project   the game project's root (default: the current directory)
  --no-jit    build without dynarmic (VITA3K_DYNARMIC=OFF: recompiled code only) into
              build/vita3k-nojit
  --games-only  only the single-game executables, without Vita3K.exe and its Qt interface
              (VITA3K_GAMES_ONLY=ON: no Qt needed), into build/vita3k-games[-nojit]
  --target    build only these targets (e.g. shinobido2), default everything
  --qt        Qt 6 (MSVC x64) for Vita3K's interface; default Qt6_ROOT, or the newest C:/Qt/<v>/msvc*_64

On Linux it builds the games only (as --games-only) with GCC, g++-14 when there is one (the Steam
Runtime sniper SDK's), and libstdc++ linked in; the executables are <id>.x86_64. On macOS (Apple
Silicon) the games only too, with Apple's clang (Xcode or the Command Line Tools), OpenSSL and Boost
built from source; the executables are <id>.arm64, next to libMoltenVK.dylib. Run it with Python
3.12 or newer (Vita3K's language catalog generator needs it).
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import msvc  # noqa: E402


def find_qt():
    versions = sorted(glob.glob(r'C:\Qt\[0-9]*\msvc20*_64'),
                      key=lambda d: [int(x) for x in os.path.basename(os.path.dirname(d)).split('.') if x.isdigit()])
    return versions[-1] if versions else ''


def build_linux(a, src):
    """Linux: the games only (no Qt), with GCC (g++-14 when there is one: the Steam Runtime sniper
    SDK's), libstdc++ and libgcc linked in, into build/vita3k-games[-nojit]. macOS: the games only,
    with Apple's clang, into the same folder."""
    if sys.version_info < (3, 12):
        sys.exit('Vita3K\'s language catalog generator needs Python 3.12 or newer: run this with one')
    root = os.path.abspath(a.project)
    build = os.path.join(root, 'build', 'vita3k-games' + ('-nojit' if a.no_jit else ''))
    env = dict(os.environ, CMAKE_POLICY_VERSION_MINIMUM='3.5')
    if sys.platform == 'darwin':
        # Apple's clang and libc++. OpenSSL and Boost from source: a player's Mac has neither
        # Homebrew's nor MacPorts' libraries, and the executable must not load them
        compilers = ['-DCMAKE_C_COMPILER=' + env.get('CC', 'cc'), '-DCMAKE_CXX_COMPILER=' + env.get('CXX', 'c++')]
        platform = ['-DFORCE_BUILD_OPENSSL_MAC=ON', '-DVITA3K_FORCE_CUSTOM_BOOST=ON']
    else:
        cc, cxx = ('gcc-14', 'g++-14') if shutil.which('g++-14') else ('gcc', 'g++')
        compilers = ['-DCMAKE_C_COMPILER=' + env.get('CC', cc), '-DCMAKE_CXX_COMPILER=' + env.get('CXX', cxx)]
        static = '-static-libstdc++ -static-libgcc'
        platform = [f'-DCMAKE_EXE_LINKER_FLAGS={static}', f'-DCMAKE_SHARED_LINKER_FLAGS={static}']
    if a.reconfigure or a.configure_only or not os.path.exists(os.path.join(build, 'CMakeCache.txt')) \
            or not os.path.exists(os.path.join(build, 'build.ninja')):
        r = subprocess.run(['cmake', '-S', src, '-B', build, '-G', 'Ninja Multi-Config', *compilers,
                            '-DVITA3K_DYNARMIC=' + ('OFF' if a.no_jit else 'ON'), '-DVITA3K_GAMES_ONLY=ON',
                            # the Discord SDK's headers don't build with GCC 14 (no <cstdint>), and it
                            # has no arm64 macOS library
                            '-DUSE_DISCORD_RICH_PRESENCE=OFF', f'-DPython3_EXECUTABLE={sys.executable}',
                            *platform, *configure_options(a)], env=env)
        if r.returncode:
            sys.exit('cmake configure failed')
    if a.configure_only:
        return
    cmd = ['cmake', '--build', build, '--config', a.config]
    if a.jobs:
        cmd += ['-j', str(a.jobs)]
    if a.target:
        cmd += ['--target'] + a.target
    start = time.time()
    if subprocess.run(cmd, env=env).returncode:
        sys.exit('build failed')
    print(f'build ({a.config}) finished in {(time.time() - start) / 60:.1f} min: {os.path.join(build, "bin", a.config)}')


def configure_options(args):
    return ['-DVITA3K_RUNTIME_TESTS=ON'] if args.runtime_tests else []


def main():
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument('--project', default=os.getcwd())
    ap.add_argument('--config', default='RelWithDebInfo', choices=['RelWithDebInfo', 'Debug', 'Release'])
    ap.add_argument('--jobs', type=int, default=0)
    ap.add_argument('--reconfigure', action='store_true')
    ap.add_argument('--configure-only', action='store_true', help='configure and exit before compiling')
    ap.add_argument('--runtime-tests', action='store_true', help='enable CTest pause and queue targets on configure')
    ap.add_argument('--no-jit', action='store_true')
    ap.add_argument('--games-only', action='store_true')
    ap.add_argument('--target', nargs='*', default=[])
    ap.add_argument('--qt', default='')
    a = ap.parse_args()

    root = os.path.abspath(a.project)
    src = os.path.join(root, 'third_party', 'vita3k')
    build = os.path.join(root, 'build', 'vita3k' + ('-games' if a.games_only else '') + ('-nojit' if a.no_jit else ''))
    if not os.path.exists(os.path.join(src, 'external', 'dynarmic', 'CMakeLists.txt')):
        sys.exit(f'submodules missing under {src}: git submodule update --init --recursive')

    if sys.platform != 'win32':
        return build_linux(a, src)
    env = msvc.environment()
    # Qt 6 for Vita3K's own interface (Vita3K.exe), which the build includes: Qt6_ROOT, --qt, or the
    # newest C:/Qt/<version>/msvc20*_64 (aqtinstall's and the Qt installer's layout)
    # (os.environ's names are upper case on Windows)
    given = [k for k in env if k.lower() == 'qt6_root']
    qt = a.qt or (env[given[0]] if given else '') or find_qt()
    for k in given:
        del env[k]
    if a.games_only:
        qt = ''
    elif not qt:
        sys.exit('Qt 6 not found: install it (Vita3K wants 6.11, MSVC x64) and pass --qt <dir> or set Qt6_ROOT')
    if qt:
        env['Qt6_ROOT'] = qt
    # Vita3K's presets set this for older external CMakeLists
    env['CMAKE_POLICY_VERSION_MINIMUM'] = '3.5'
    if a.reconfigure or a.configure_only or not os.path.exists(os.path.join(build, 'CMakeCache.txt')) \
            or not os.path.exists(os.path.join(build, 'build.ninja')):
        # The first configure bootstraps Boost's b2, whose build.bat runs 'call guess_toolset.bat'
        # from its own folder; that fails when NoDefaultCurrentDirectoryInExePath is set
        configure_env = dict(env)
        # (environment names are case-insensitive on Windows, so match any spelling)
        for key in [k for k in configure_env if k.lower() == 'nodefaultcurrentdirectoryinexepath']:
            del configure_env[key]
        r = subprocess.run(['cmake', '-S', src, '-B', build, '-G', 'Ninja Multi-Config',
                            '-DCMAKE_C_COMPILER=cl', '-DCMAKE_CXX_COMPILER=cl',
                            '-DVITA3K_DYNARMIC=' + ('OFF' if a.no_jit else 'ON'),
                            '-DVITA3K_GAMES_ONLY=' + ('ON' if a.games_only else 'OFF'),
                            *configure_options(a)], env=configure_env)
        if r.returncode:
            sys.exit('cmake configure failed')
    if a.configure_only:
        return
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
