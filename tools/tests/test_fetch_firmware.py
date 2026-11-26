"""fetch-firmware.py's guardrails, against a local HTTP server (no network): only the pinned size and
SHA-256 are kept, from whichever URL in the list serves them, and a refused or broken download leaves
nothing on disk. On Windows also the curl.exe branch (taken when Python's certificate check fails).
Last, the real URL lists: Sony first, the Wayback copies, RetroBIOS's re-upload last.

usage: python -I tools/tests/test_fetch_firmware.py      (exit code 1 on a failure)
"""
import hashlib
import http.server
import importlib.util
import os
import sys
import tempfile
import threading
import urllib.error

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    """A fresh copy of fetch-firmware.py (the tests change its lists and its urlopen)."""
    spec = importlib.util.spec_from_file_location('fetch_firmware', os.path.join(TOOLS, 'fetch-firmware.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOOD = os.urandom(300_000)
SIZE, DIGEST = len(GOOD), hashlib.sha256(GOOD).hexdigest()
# path -> (body, send Content-Length)
ROUTES = {
    '/good': (GOOD, True),
    '/good-nolen': (GOOD, False),
    '/swapped': (bytes(b ^ 0xFF for b in GOOD), True),  # same size, other bytes: a replaced file
    '/big': (GOOD + b'x' * 50_000, True),
    '/big-nolen': (GOOD + b'x' * 50_000, False),
    '/short-nolen': (GOOD[:200_000], False),
    '/short': (GOOD[:1000], True),
}
served = []


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'  # without Content-Length the body ends when the connection closes

    def do_GET(self):
        served.append(self.path)
        if self.path not in ROUTES:
            self.send_error(404)
            return
        body, length = ROUTES[self.path]
        self.send_response(200)
        if length:
            self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass  # the client stopped reading: what a guardrail does

    def log_message(self, *args):
        pass


failures = 0


def check(what, ok, detail=''):
    global failures
    failures += not ok
    print(f'{"ok  " if ok else "FAIL"} {what}{": " + detail if detail else ""}')


def parts(d):
    return [f for f in os.listdir(d) if f.endswith('.part')] if os.path.isdir(d) else []


def try_download(fw, url, part):
    try:
        fw.download(url, part, SIZE, DIGEST)
        return None
    except (OSError, ValueError) as e:
        return str(e)


def download_cases(fw, base, scratch, cases, label):
    for route, expect in cases:
        part = os.path.join(scratch, 'X.PUP.part')
        if os.path.exists(part):
            os.remove(part)
        err = try_download(fw, base + route, part)
        if expect is None:
            check(f'{label} {route} accepted', err is None and os.path.getsize(part) == SIZE, err or '')
        else:
            check(f'{label} {route} refused ({expect})', err is not None and expect in err and not os.path.exists(part),
                  err or 'accepted')


def main():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    with tempfile.TemporaryDirectory() as scratch:
        # download() alone
        download_cases(load(), base, scratch, [
            ('/good', None), ('/good-nolen', None), ('/swapped', 'wrong SHA-256'), ('/big', 'announces'),
            ('/big-nolen', 'more than the pinned'), ('/short-nolen', 'cut short'), ('/missing', 'HTTP Error 404'),
        ], 'download')

        # fetch(): the list in order, the good copy last
        fw = load()
        d = os.path.join(scratch, 'fetch')
        fw.FIRMWARE = [('X.PUP', base + '/missing', SIZE, DIGEST)]
        fw.MIRRORS = {'X.PUP': [base + r for r in ('/swapped', '/big', '/big-nolen', '/short-nolen', '/good')]}
        served.clear()
        with open(fw.fetch(d)[0], 'rb') as f:
            data = f.read()
        check('fetch takes the good copy after the bad ones', data == GOOD and served[-1] == '/good', ' '.join(served))
        check('fetch leaves no .part', not parts(d), str(parts(d)))
        served.clear()
        fw.fetch(d)
        check('a second fetch downloads nothing', not served, ' '.join(served))

        # fetch(): no good copy anywhere -> exits naming the file and each refusal, keeps nothing
        d = os.path.join(scratch, 'fetch-bad')
        fw.MIRRORS = {'X.PUP': [base + '/swapped', base + '/big']}
        try:
            fw.fetch(d)
            check('fetch with no good copy exits', False, 'returned')
        except SystemExit as e:
            msg = str(e.code)
            check('fetch with no good copy exits', 'X.PUP' in msg and 'wrong SHA-256' in msg and 'announces' in msg,
                  msg.replace('\n', ' | ')[:200])
        check('nothing kept after the failure', not os.path.exists(os.path.join(d, 'X.PUP')) and not parts(d),
              str(os.listdir(d)))

        # Windows: the curl.exe branch, forced by a certificate failure in Python's urlopen
        curl = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'curl.exe')
        if sys.platform == 'win32' and os.path.exists(curl):
            fw = load()

            def cert_failure(*args, **kwargs):
                raise urllib.error.URLError('[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed')
            fw.urllib.request.urlopen = cert_failure
            download_cases(fw, base, scratch, [
                ('/good', None), ('/swapped', 'wrong SHA-256'), ('/big', 'more than the pinned'),
                ('/short', 'not the pinned'),
            ], 'curl.exe')
        else:
            print('skip curl.exe branch (Windows only)')
    server.shutdown()

    # the real lists
    fw = load()
    for name, url, size, digest in fw.FIRMWARE:
        urls = [url] + fw.MIRRORS[name]
        check(f'{name}: Sony first, the Wayback copies, RetroBIOS last',
              'playstation.net' in urls[0] and 'github.com/Abdess/retrobios/' in urls[-1]
              and len(urls) > 2 and all('web.archive.org' in u for u in urls[1:-1]),
              ' '.join(u.split('/')[2] for u in urls))

    print(f'{failures} failures')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
