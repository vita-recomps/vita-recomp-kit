"""Lay out a self-contained folder of a game's port: dist/<name>/ with its front end (<id>.exe; Linux
<id>.x86_64, macOS <id>.arm64 and libMoltenVK.dylib), the DLLs it needs, Vita3K's data, the recompiled modules (built locally from the player's own dump,
never shared), a commented <id>.ini and a README. Nothing here is committed (dist/ is ignored).

usage:
  python -I make-dist.py [--project DIR] [--name NAME | --out DIR] [--vita-fs DIR] [--recompiled-dir DIR]
                         [--no-recompiled] [--no-jit] [--bin DIR] [--redist DIR]

  --name            the folder's name (default <id>-<commit>)
  --out             the folder itself, anywhere (default dist/<name>)
  --bin             the front end's folder, a prebuilt release (default the project's build); when it
                    is --out itself (a release folder building in place), the front end's files are
                    left as they are (Windows can't replace a running exe or its loaded DLLs)
  --exe             the front end's executable when it isn't <bin>/<id><suffix> (a macOS app bundle)
  --redist          copy the VC++ runtime DLLs from DIR (msvcp140*, vcruntime140*) into the folder, so
                    the player needs no VC++ redistributable installed
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
from gameproject import MODULE_SUFFIX, WINDOWS, Project, ico_to_png  # noqa: E402


def main():
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument('--project', default=os.getcwd())
    ap.add_argument('--name', default='')
    ap.add_argument('--vita-fs', default='')
    ap.add_argument('--recompiled-dir', default='')
    ap.add_argument('--no-recompiled', action='store_true')
    ap.add_argument('--no-jit', action='store_true')
    ap.add_argument('--bin', default='')
    ap.add_argument('--exe', default='')
    ap.add_argument('--out', default='')
    ap.add_argument('--redist', default='')
    a = ap.parse_args()

    p = Project(a.project)
    exe = os.path.abspath(a.exe) if a.exe else p.exe(a.no_jit, a.bin)
    bin_dir = os.path.abspath(a.bin) if a.bin else os.path.dirname(exe)
    if a.out:
        out = os.path.abspath(a.out)
    else:
        name = a.name or p.id + '-' + subprocess.run(['git', '-C', p.root, 'rev-parse', '--short', 'HEAD'],
                                                      capture_output=True, text=True).stdout.strip()
        out = os.path.join(p.root, 'dist', name)
    os.makedirs(out, exist_ok=True)
    # a release folder building in place: its front end, DLLs and static assets are already there
    in_place = os.path.normcase(bin_dir) == os.path.normcase(out)

    # the executable and the DLLs it loads (Windows' own DLLs are the player's)
    if not in_place:
        shutil.copy2(exe, out)
        dlls = ['discord_game_sdk.dll', 'libssl-4-x64.dll', 'libcrypto-4-x64.dll']
        dlls += [os.path.basename(f) for f in glob.glob(os.path.join(bin_dir, 'av*.dll'))]
        dlls += [os.path.basename(f) for f in glob.glob(os.path.join(bin_dir, 'sw*.dll'))]
        # the VC++ runtime a release carries beside its exe
        dlls += [os.path.basename(f) for f in glob.glob(os.path.join(bin_dir, 'msvcp140*.dll'))
                 + glob.glob(os.path.join(bin_dir, 'vcruntime140*.dll'))]
        # macOS: MoltenVK, Vulkan over Metal (the executable's rpath is its own folder)
        dlls += ['libMoltenVK.dylib']
        for d in dlls:
            if os.path.exists(os.path.join(bin_dir, d)):
                shutil.copy2(os.path.join(bin_dir, d), out)
    # the VC++ runtime in the application's own folder (what the exe and the modules import); one
    # already there (a release's, possibly loaded by the running front end) stays
    if a.redist:
        for f in glob.glob(os.path.join(a.redist, 'msvcp140*.dll')) + glob.glob(os.path.join(a.redist, 'vcruntime140*.dll')):
            if not os.path.exists(os.path.join(out, os.path.basename(f))):
                shutil.copy2(f, out)
    # Linux: the icon a desktop entry shows, from the .ico the build puts beside the executable (macOS:
    # the setup's app bundle makes its icon from it)
    png = os.path.join(out, p.id + '.png')
    if not WINDOWS and os.path.exists(os.path.join(bin_dir, p.id + '.ico')) and not os.path.exists(png):
        ico_to_png(os.path.join(bin_dir, p.id + '.ico'), png)
    # static assets Vita3K reads next to the executable
    if not in_place:
        for d in ('data', 'shaders-builtin', 'icons'):
            if os.path.isdir(os.path.join(bin_dir, d)):
                shutil.copytree(os.path.join(bin_dir, d), os.path.join(out, d), dirs_exist_ok=True)

    # the recompiled modules
    if not a.no_recompiled:
        rdir = a.recompiled_dir or os.path.join(p.root, 'build', 'recomp')
        mods = glob.glob(os.path.join(rdir, 'ssd_aot_*' + MODULE_SUFFIX))
        if not mods:
            print(f'warning: no ssd_aot_*{MODULE_SUFFIX} in {rdir}: everything will run on the JIT')
        for m in mods:
            shutil.copy2(m, out)

    # <id>.ini: the default the front end writes, with vita_fs set
    ini = os.path.join(out, p.id + '.ini')
    if not os.path.exists(ini):
        if subprocess.run([exe, '--write-ini', ini], cwd=bin_dir).returncode or not os.path.exists(ini):
            sys.exit(f'{p.exe_name} --write-ini failed')
        if a.vita_fs:
            with open(ini, encoding='utf-8') as f:
                lines = f.read().splitlines(keepends=True)
            fs = a.vita_fs.replace('\\', '/')
            lines = [f'vita_fs = {fs}\n' if line.startswith(';vita_fs =') else line for line in lines]
            with open(ini, 'w', encoding='utf-8') as f:
                f.writelines(lines)

    if p.dist_readme and not (in_place and os.path.exists(os.path.join(out, 'README.txt'))):
        shutil.copy2(os.path.join(p.root, p.dist_readme), os.path.join(out, 'README.txt'))
    for fname, text in p.dist_files.items():
        if in_place and os.path.exists(os.path.join(out, fname)):
            continue
        with open(os.path.join(out, fname), 'w', encoding='utf-8', newline='\r\n' if WINDOWS else '\n') as f:
            f.write(text)
        if not WINDOWS:
            os.chmod(os.path.join(out, fname), 0o755)  # a launcher script

    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
    print(f'{out}: {size / 2**20:,.0f} MB')


if __name__ == '__main__':
    main()
