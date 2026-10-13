"""The MSVC x64 build environment (cl, link, the Windows SDK) for the build scripts.

When the current environment already has it (a Developer shell: VCToolsVersion is set), that is
used. Otherwise vswhere finds the newest Visual Studio or Build Tools with the C++ tools, and the
variables vcvars64.bat sets are read from it.
"""
import os
import subprocess
import sys


def environment():
    """os.environ plus MSVC's variables, as a new dict."""
    env = dict(os.environ)
    if env.get('VCToolsVersion'):
        return env
    if sys.platform != 'win32':
        return env
    vswhere = os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                           'Microsoft Visual Studio', 'Installer', 'vswhere.exe')
    if not os.path.exists(vswhere):
        sys.exit('msvc: vswhere.exe not found; install Visual Studio or its Build Tools with C++')
    found = subprocess.run([vswhere, '-latest', '-products', '*', '-requires',
                            'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
                            '-property', 'installationPath'],
                           capture_output=True, text=True).stdout.strip().splitlines()
    if not found:
        sys.exit('msvc: no Visual Studio with the C++ x64 tools')
    vcvars = os.path.join(found[0], 'VC', 'Auxiliary', 'Build', 'vcvars64.bat')
    out = subprocess.run(f'"{vcvars}" >nul 2>&1 && set', shell=True, capture_output=True, text=True,
                         env=env).stdout
    for line in out.splitlines():
        key, sep, value = line.partition('=')
        if sep and key:
            env[key] = value
    if not env.get('VCToolsVersion'):
        sys.exit(f'msvc: {vcvars} did not set up the environment')
    return env
