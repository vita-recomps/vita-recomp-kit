"""Restrict a Zig-linked 64-bit Mach-O dylib's dyld exports.

Zig's Mach-O linker does not apply an exported symbols list to its own
runtime symbols. Keep the linker's terminal records for the requested
exports and replace the trie in place. The caller must re-sign the dylib.
"""
from os.path import commonprefix
from pathlib import Path
import struct


def _read_uleb(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError('invalid Mach-O export trie integer')


def _find_export(trie, name):
    node = 0
    while True:
        terminal_size, terminal = _read_uleb(trie, node)
        pos = terminal + terminal_size
        if not name:
            if terminal_size:
                return trie[node:pos] + b'\0'
            break
        child_count = trie[pos]
        pos += 1
        for _ in range(child_count):
            end = trie.index(0, pos)
            edge = trie[pos:end]
            child, pos = _read_uleb(trie, end + 1)
            if edge and name.startswith(edge):
                node = child
                name = name[len(edge):]
                break
        else:
            break
    raise ValueError('missing required Mach-O export')


def restrict_exports(path, names):
    path = Path(path)
    data = bytearray(path.read_bytes())
    if len(data) < 32:
        raise ValueError('truncated Mach-O header')
    magic, _, _, filetype, commands = struct.unpack_from('<5I', data)
    if magic != 0xfeedfacf or filetype != 6:  # MH_MAGIC_64, MH_DYLIB
        raise ValueError('expected a little-endian 64-bit Mach-O dylib')

    pos = 32
    export_range = None
    for _ in range(commands):
        if pos + 8 > len(data):
            raise ValueError('truncated Mach-O load command')
        command, size = struct.unpack_from('<II', data, pos)
        if size < 8 or pos + size > len(data):
            raise ValueError('invalid Mach-O load command size')
        if command in (0x22, 0x80000022):  # LC_DYLD_INFO and LC_DYLD_INFO_ONLY
            if size < 48:
                raise ValueError('truncated Mach-O dynamic linker information')
            candidate = struct.unpack_from('<II', data, pos + 40)
        elif command == 0x80000033:  # LC_DYLD_EXPORTS_TRIE
            if size < 16:
                raise ValueError('truncated Mach-O export table command')
            candidate = struct.unpack_from('<II', data, pos + 8)
        else:
            candidate = None
        if candidate and candidate[1]:
            if export_range and export_range != candidate:
                raise ValueError('multiple Mach-O export tables')
            export_range = candidate
        pos += size

    if not export_range:
        raise ValueError('missing Mach-O export table')
    offset, size = export_range
    if offset + size > len(data):
        raise ValueError('truncated Mach-O export table')
    trie = data[offset:offset + size]
    names = [name.encode('ascii') for name in names]
    leaves = [_find_export(trie, name) for name in names]
    prefix = commonprefix(names)
    root = bytearray()
    if prefix:
        root += b'\0\1' + prefix + bytes((0, len(prefix) + 4))
        names = [name[len(prefix):] for name in names]
    root += bytes((0, len(names)))  # no terminal value, followed by the child count
    leaf_offset = len(root) + sum(len(name) + 2 for name in names)
    # The two short AOT names and their terminals fit within one-byte child offsets.
    for name, leaf in zip(names, leaves):
        if leaf_offset >= 128:
            raise ValueError('AOT export offset needs more than one byte')
        root += name + bytes((0, leaf_offset))
        leaf_offset += len(leaf)
    trie = root + b''.join(leaves)
    if len(trie) > size:
        raise ValueError('AOT exports do not fit in the Mach-O export table')
    data[offset:offset + size] = trie.ljust(size, b'\0')
    path.write_bytes(data)
