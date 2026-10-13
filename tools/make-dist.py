"""Lay out a self-contained folder of a game's port: dist/<name>/ with its front end (<id>.exe), the
DLLs it needs, Vita3K's data, the recompiled modules (built locally from the player's own dump,
never shared), a commented <id>.ini and a README. Nothing here is committed (dist/ is ignored).

usage:
  python -I make-dist.py [--project DIR] [--name NAME] [--vita-fs DIR] [--recompiled-dir DIR]
                         [--no-recompiled] [--no-jit]

  --name            the folder's name (default <id>-<commit>)
  --vita-fs         set [paths] vita_fs in the ini (default: the fs folder next to the exe)
  --recompiled-dir  where <prefix><module>.dll are (default build/recomp)
  --no-jit          the front end from the build without dynarmic (build-vita3k.py --no-jit)
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gameproject import Project  # noqa: E402


def main():
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument('--project', default=os.getcwd())
    ap.add_argument('--name', default='')
    ap.add_argument('--vita-fs', default='')
    ap.add_argument('--recompiled-dir', default='')
    ap.add_argument('--no-recompiled', action='store_true')
    ap.add_argument('--no-jit', action='store_true')
    a = ap.parse_args()

    p = Project(a.project)
    exe = p.exe(a.no_jit)
    bin_dir = os.path.dirname(exe)
    name = a.name or p.id + '-' + subprocess.run(['git', '-C', p.root, 'rev-parse', '--short', 'HEAD'],
                                                  capture_output=True, text=True).stdout.strip()
    out = os.path.join(p.root, 'dist', name)
    os.makedirs(out, exist_ok=True)

    # the executable and the DLLs it loads (Windows and the VC++ runtime are the player's)
    shutil.copy2(exe, out)
    dlls = ['discord_game_sdk.dll', 'libssl-4-x64.dll', 'libcrypto-4-x64.dll']
    dlls += [os.path.basename(f) for f in glob.glob(os.path.join(bin_dir, 'av*.dll'))]
    dlls += [os.path.basename(f) for f in glob.glob(os.path.join(bin_dir, 'sw*.dll'))]
    for d in dlls:
        if os.path.exists(os.path.join(bin_dir, d)):
            shutil.copy2(os.path.join(bin_dir, d), out)
    # static assets Vita3K reads next to the executable
    for d in ('data', 'shaders-builtin', 'icons'):
        if os.path.isdir(os.path.join(bin_dir, d)):
            shutil.copytree(os.path.join(bin_dir, d), os.path.join(out, d), dirs_exist_ok=True)

    # the recompiled modules
    if not a.no_recompiled:
        rdir = a.recompiled_dir or os.path.join(p.root, 'build', 'recomp')
        mods = glob.glob(os.path.join(rdir, 'ssd_aot_*.dll'))
        if not mods:
            print(f'warning: no ssd_aot_*.dll in {rdir}: everything will run on the JIT')
        for m in mods:
            shutil.copy2(m, out)

    # <id>.ini: the default the front end writes, with vita_fs set
    ini = os.path.join(out, p.id + '.ini')
    if not os.path.exists(ini):
        if subprocess.run([exe, '--write-ini', ini], cwd=bin_dir).returncode or not os.path.exists(ini):
            sys.exit(f'{p.id}.exe --write-ini failed')
        if a.vita_fs:
            with open(ini, encoding='utf-8') as f:
                lines = f.read().splitlines(keepends=True)
            fs = a.vita_fs.replace('\\', '/')
            lines = [f'vita_fs = {fs}\n' if line.startswith(';vita_fs =') else line for line in lines]
            with open(ini, 'w', encoding='utf-8') as f:
                f.writelines(lines)

    if p.dist_readme:
        shutil.copy2(os.path.join(p.root, p.dist_readme), os.path.join(out, 'README.txt'))
    for fname, text in p.dist_files.items():
        with open(os.path.join(out, fname), 'w', encoding='utf-8', newline='\r\n') as f:
            f.write(text)

    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
    print(f'{out}: {size / 2**20:,.0f} MB')


if __name__ == '__main__':
    main()
