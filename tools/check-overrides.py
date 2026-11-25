"""Checks a game project's recompiler overrides (game.json recomp_overrides) before they reach a build:
each file is there and parses, and its C++ uses nothing a module built for Android can't link.

An override's prelude and code are compiled into the module on every platform, Android's too. There the
front end links libc++ statically and exports none of it, so a module that calls into libc++ (a clock
from <chrono>, <thread>, <iostream>, std::string ...) isn't loaded and the game stops at its first
native call (utakata 0.2.0, 2026-11-25). Header-only pieces (<cmath>, <cstdint>, <algorithm>, ...) and
the C library are fine. make-setup.py and android-build-tools.py run this too; toolchain.py checks the
Android modules themselves after linking.

usage: python -I check-overrides.py <project folder>
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gameproject  # noqa: E402

# headers whose use compiles to inline code or C library calls only
ALLOWED_HEADERS = {
    'algorithm', 'array', 'bit', 'cfloat', 'climits', 'cmath', 'cstddef', 'cstdint', 'cstdlib', 'cstring',
    'limits', 'type_traits', 'utility', 'float.h', 'limits.h', 'math.h', 'stddef.h', 'stdint.h', 'stdlib.h',
    'string.h', 'time.h',
}
# library parts that call into libc++'s compiled code
FORBIDDEN = re.compile(r'std::(chrono|this_thread|thread|mutex|condition_variable|string|wstring|vector|map|'
                       r'unordered_\w+|set|function|cout|cerr|clog|ostream|istream|\w*stringstream|filesystem|'
                       r'random_device|mt19937\w*|to_string|stoi|stof|stod|regex|locale|format|print)\b')
INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')


def check_file(path):
    """The problems in one overrides file (a list of messages)."""
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
    except FileNotFoundError:
        return [f'{path}: missing']
    except ValueError as e:
        return [f'{path}: not JSON ({e})']
    lines = [('prelude', i + 1, line) for i, line in enumerate(doc.get('prelude', []))]
    for o in doc.get('overrides', []):
        lines += [(f'override {o.get("address", "?")}', i + 1, line) for i, line in enumerate(o.get('code', []))]
    problems = []
    for where, n, line in lines:
        m = INCLUDE.match(line)
        if m and m.group(1) not in ALLOWED_HEADERS:
            problems.append(f'{path}: {where} line {n}: #include <{m.group(1)}> (not header-only or C library: '
                            f'Android modules can\'t use it)')
        m = FORBIDDEN.search(line)
        if m:
            problems.append(f'{path}: {where} line {n}: {m.group(0)} needs libc++, which Android modules can\'t use')
    return problems


def check_project(root):
    """The problems in a project's overrides (a list of messages)."""
    p = gameproject.Project(root)
    problems = []
    for rel in p.build_files():
        problems += check_file(os.path.join(p.root, rel))
    return problems


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    problems = check_project(sys.argv[1])
    for line in problems:
        print(line)
    if problems:
        sys.exit(1)
    print('overrides: ok')


if __name__ == '__main__':
    main()
