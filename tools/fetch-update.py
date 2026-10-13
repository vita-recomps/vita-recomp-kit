"""Fetch a Vita game's update pkg from Sony's update server.

The server publishes, per title, an XML listing the latest patch: its version, URL,
size and SHA-1. The XML's path carries an HMAC-SHA256 of "np_<title id>" under a
key that is public knowledge (the same one NoPayStation and Vita3K use).

usage:
  python -I fetch-update.py <title id>                 show the update XML's entries
  python -I fetch-update.py <title id> <out dir>       download the latest pkg there
"""
import hashlib
import hmac
import os
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET

KEY = bytes.fromhex('E5E278AA1EE34082A088279C83F9BBC806821C52F2AB5D2B4ABD995450355114')


def xml_url(title):
    h = hmac.new(KEY, f'np_{title}'.encode(), hashlib.sha256).hexdigest()
    return f'https://gs-sec.ww.np.dl.playstation.net/pl/np/{title}/{h}/{title}-ver.xml'


def get(url):
    # Sony's update hosts present a certificate chain Python's store does not
    # always accept. Integrity comes from the SHA-1 in the XML, checked below.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.urlopen(url, context=ctx, timeout=60)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    title = sys.argv[1]
    with get(xml_url(title)) as r:
        root = ET.fromstring(r.read())
    pkgs = root.findall('.//package')
    for p in pkgs:
        print(f"{p.get('version')}  {int(p.get('size')):>10}  sha1 {p.get('sha1sum')}  {p.get('url')}")
    if len(sys.argv) < 3:
        return
    p = pkgs[-1]
    url, size, sha1 = p.get('url'), int(p.get('size')), p.get('sha1sum')
    out = os.path.join(sys.argv[2], url.rsplit('/', 1)[1])
    os.makedirs(sys.argv[2], exist_ok=True)
    with get(url) as r, open(out, 'wb') as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    if os.path.getsize(out) != size:
        sys.exit(f'size mismatch: {os.path.getsize(out)} != {size}')
    # The XML's sha1sum covers the pkg minus its last 32 bytes (checked on PCSE00015 1.02).
    with open(out, 'rb') as f:
        body = f.read(size - 0x20)
    if hashlib.sha1(body).hexdigest() != sha1.lower():
        sys.exit(f'sha1 mismatch for {out}')
    print(f'{out}: {size} bytes, sha1 ok')


if __name__ == '__main__':
    main()
