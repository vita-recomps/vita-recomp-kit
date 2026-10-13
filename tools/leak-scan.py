"""Leak scan: stops game data and local or private details from reaching a public remote.

Checks the commits being pushed (as a git pre-push hook) or a given range:
  * files: game-data types (ELFs, SELFs, pkgs, PUPs, licences, archives, dumps, media) and
    files over 5 MB (--max-mb);
  * text, in added lines and in commit metadata (authors, messages): zRIF-like strings, access
    tokens, private keys, Windows user folders;
  * private patterns: one regular expression per line, from the file that
    `git config leakscan.patterns <file>` names (host names, e-mail addresses, local folders...).
    Keep that file outside the repo: it lists what must not be published.
Matches are printed masked, so the scan itself doesn't echo a secret.

Commits the remote already has, and commits on remotes listed in `git config leakscan.trusted`
(default: upstream), are not scanned: for a fork, only our own commits are.

usage:
  python -I leak-scan.py install [<repo dir>]        install as the repo's pre-push hook
  python -I leak-scan.py check <rev range> [...]     scan commits, e.g. 'main --not --remotes=github'
  (pre-push: git runs the hook with <remote> <url> and the refs being pushed on stdin)
"""
import os
import re
import stat
import subprocess
import sys

ZERO = '0' * 40
MAX_MB = 5
DATA_EXT = {'.elf', '.self', '.suprx', '.skprx', '.sprx', '.pkg', '.rif', '.pup', '.vpk', '.zip', '.7z',
            '.rar', '.gxt', '.gxp', '.at9', '.mp4', '.wav', '.png', '.jpg', '.jpeg', '.iso', '.cso',
            '.psarc', '.pfs', '.pk', '.pkh', '.dmp', '.sav'}
DATA_NAMES = {'work.bin', 'param.sfo', 'eboot.bin', 'keystone', 'clearsign'}
BUILTIN = [
    ('zRIF-like licence string', r'KO5if[A-Za-z0-9+/=]{20,}'),
    ('GitHub token', r'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'),
    ('private key', r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    ('Windows user folder', r'(?i)\b[a-z]:[\\/]+(users|documents and settings)[\\/]+[^\\/\s`\'"]+'),
    ('user folder (Git Bash)', r'(?i)/[a-z]/users/[^/\s`\'"]+'),
]


def git(*args, input=None, cwd=None):
    r = subprocess.run(['git', *args], capture_output=True, input=input, cwd=cwd)
    if r.returncode:
        sys.exit(f'leak-scan: git {" ".join(args[:3])} failed: {r.stderr.decode(errors="replace").strip()}')
    return r.stdout


def private_patterns():
    path = git('config', '--default', '', 'leakscan.patterns').decode().strip()
    if not path:
        print('leak-scan: warning: no private patterns (git config leakscan.patterns <file>)', file=sys.stderr)
        return []
    out = []
    with open(os.path.expanduser(path), encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if line and not line.startswith('#'):
                out.append((f'private pattern {n}', line))
    return out


def mask(text, m):
    s = m.group(0)
    shown = s[:2] + '*' * max(3, len(s) - 2)
    return (text[:m.start()] + shown + text[m.end():]).strip()[:160]


def scan(revs):
    """revs: rev-list arguments. Returns a list of findings (strings)."""
    commits = git('rev-list', *revs).split()
    if not commits:
        return []
    findings = []
    # files: type and size
    listing = git('rev-list', '--objects', *revs).decode(errors='replace').splitlines()
    named = [(line[:40], line[41:]) for line in listing if len(line) > 41]
    if named:
        sizes = git('cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)',
                    input='\n'.join(o for o, _ in named).encode()).decode().splitlines()
        info = {s.split()[0]: (s.split()[1], int(s.split()[2])) for s in sizes if len(s.split()) == 3}
        for oid, path in named:
            kind, size = info.get(oid, ('', 0))
            if kind != 'blob':
                continue
            name = path.rsplit('/', 1)[-1].lower()
            ext = os.path.splitext(name)[1]
            if ext in DATA_EXT or name in DATA_NAMES:
                findings.append(f'game-data file type: {path}')
            elif size > MAX_MB * 2**20:
                findings.append(f'file over {MAX_MB} MB: {path} ({size / 2**20:.1f} MB)')
    # text: added lines and metadata
    patterns = [(n, re.compile(p)) for n, p in BUILTIN + private_patterns()]
    log = git('log', '-p', '--no-ext-diff', '--format=commit %H%nA: %an <%ae>%nC: %cn <%ce>%n%B', *revs)
    commit, path = '', ''
    for raw in log.decode(errors='replace').splitlines():
        if raw.startswith('commit ') and len(raw) == 47:
            commit, path = raw[7:15], '(message)'
            continue
        if raw.startswith('+++ '):
            path = raw[6:] if raw.startswith('+++ b/') else raw[4:]
            continue
        if raw.startswith(('--- ', 'diff --git', 'index ', '@@')):
            continue
        if raw.startswith('-'):
            continue
        text = raw[1:] if raw.startswith('+') else raw
        for name, rx in patterns:
            m = rx.search(text)
            if m:
                findings.append(f'{name}: {commit} {path}: {mask(text, m)}')
    return findings


def report(findings, what):
    if not findings:
        print(f'leak-scan: {what}: clean')
        return 0
    print(f'leak-scan: {what}: {len(findings)} finding(s); nothing was pushed:', file=sys.stderr)
    for f in findings[:200]:
        print('  ' + f, file=sys.stderr)
    if len(findings) > 200:
        print(f'  ... and {len(findings) - 200} more', file=sys.stderr)
    return 1


def pre_push(remote):
    trusted = git('config', '--default', 'upstream', 'leakscan.trusted').decode().split()
    excludes = ['--not', f'--remotes={remote}'] + [f'--remotes={t}' for t in trusted]
    findings = []
    for line in sys.stdin.read().splitlines():
        local_ref, local_sha, remote_ref, remote_sha = line.split()
        if local_sha == ZERO:
            continue
        # the remote's old tip, when we have it, is excluded too (after --not)
        known = remote_sha != ZERO and subprocess.run(['git', 'cat-file', '-e', remote_sha],
                                                       capture_output=True).returncode == 0
        revs = [local_sha] + excludes + ([remote_sha] if known else [])
        findings += scan(revs)
    return report(findings, f'push to {remote}')


def install(repo):
    hooks = git('-C', repo, 'rev-parse', '--git-path', 'hooks').decode().strip()
    hooks = os.path.join(repo, hooks) if not os.path.isabs(hooks) else hooks
    os.makedirs(hooks, exist_ok=True)
    hook = os.path.join(hooks, 'pre-push')
    me = os.path.abspath(__file__).replace('\\', '/')
    with open(hook, 'w', encoding='utf-8', newline='\n') as f:
        f.write(f'#!/bin/sh\n# leak scan (vita-recomp-kit tools/leak-scan.py)\nexec python -I "{me}" pre-push "$@"\n')
    os.chmod(hook, os.stat(hook).st_mode | stat.S_IEXEC)
    print(f'leak-scan: pre-push hook installed in {hook}')


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == 'install':
        install(sys.argv[2] if len(sys.argv) > 2 else '.')
    elif cmd == 'check':
        sys.exit(report(scan(sys.argv[2:] or ['HEAD']), ' '.join(sys.argv[2:]) or 'HEAD'))
    elif cmd == 'pre-push':
        sys.exit(pre_push(sys.argv[2] if len(sys.argv) > 2 else 'origin'))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
