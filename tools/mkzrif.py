"""Make a zRIF (base64) from a binary RIF/work.bin, using psvpfstools' dictionary.

usage: python -I mkzrif.py <keyflate.c> <work.bin>
"""
import base64
import re
import sys
import zlib

src, rif = sys.argv[1], sys.argv[2]
c = open(src, encoding='latin-1').read()
body = c[c.index('g_dict[] ='):]
body = body[body.index('{') + 1:body.index('};')]
toks = re.findall(r'"((?:[^"\\]|\\.)*)"|(\d+)', body)
d = bytearray()
for s, n in toks:
    if n:
        d.append(int(n))
    else:
        d += s.encode('latin-1').decode('unicode_escape').encode('latin-1')
d = bytes(d[:1024]).ljust(1024, b'\0')
key = open(rif, 'rb').read()
co = zlib.compressobj(9, zlib.DEFLATED, 10, 8, zlib.Z_DEFAULT_STRATEGY, zdict=d)
z = co.compress(key) + co.flush()
z += b'\0' * ((3 - len(z) % 3) % 3)
do = zlib.decompressobj(10, zdict=d)
assert do.decompress(z)[:len(key)] == key
print(base64.b64encode(z).decode())
