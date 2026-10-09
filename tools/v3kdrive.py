"""Drive the portable Vita3K (Qt frontend) for baseline runs, without taking keyboard focus.

Buttons are posted to the game window as WM_KEYDOWN/WM_KEYUP with scancodes, so the
user's foreground window never receives them. Screenshots use Vita3K's own capture
(bound to F12 in config.yml), which reads the emulated framebuffer. FPS is sampled
from the game window title ("... | 30 FPS (33 ms) | 960x544 | ...").

usage:
  python -I v3kdrive.py <vita3k dir> launch <TITLEID> [Vulkan|OpenGL]  start Vita3K and boot the app
  python -I v3kdrive.py <vita3k dir> run "<steps>" [fps.csv]  run steps against the open window
  python -I v3kdrive.py <vita3k dir> bootseq <TITLEID> <name>  set BootSequence in the installed config.ini
  python -I v3kdrive.py <vita3k dir> title                   print the game window title
  python -I v3kdrive.py <vita3k dir> close                   close Vita3K

Steps (comma separated):
  <button>          tap (100 ms)          cross circle square triangle start select
                                          up down left right l r ls_up ls_down ls_left ls_right
                                          rs_up rs_down rs_left rs_right
  <button>*N        tap N times (400 ms apart)
  hold:<button>:ms  hold for ms
  wait <ms>         sleep, sampling FPS every 500 ms
  shot:<path>       PNG of the game window (PrintWindow; no focus needed)
  mark:<label>      label the following FPS samples in the CSV
"""
import csv, ctypes, os, re, subprocess, sys, time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
WM_KEYDOWN, WM_KEYUP = 0x100, 0x101

# Default Vita3K keyboard map -> (virtual key, set-1 scancode, extended)
KEYS = {
    "cross": (0x58, 0x2D, False), "circle": (0x43, 0x2E, False), "square": (0x5A, 0x2C, False),
    "triangle": (0x56, 0x2F, False), "start": (0x0D, 0x1C, False), "select": (0xA1, 0x36, False),
    "up": (0x26, 0x48, True), "down": (0x28, 0x50, True), "left": (0x25, 0x4B, True), "right": (0x27, 0x4D, True),
    "l": (0x51, 0x10, False), "r": (0x45, 0x12, False),
    "ls_up": (0x57, 0x11, False), "ls_down": (0x53, 0x1F, False), "ls_left": (0x41, 0x1E, False),
    "ls_right": (0x44, 0x20, False),
    "rs_up": (0x49, 0x17, False), "rs_down": (0x4B, 0x25, False), "rs_left": (0x4A, 0x24, False),
    "rs_right": (0x4C, 0x26, False),
    "f12": (0x7B, 0x58, False),
}
TITLE_RE = re.compile(r"\| (?:\S+) \| (\d+) FPS \((\d+) ms\) \| (\d+)x(\d+)")


def windows():
    """(hwnd, title) for every visible top-level window owned by a Vita3K.exe process."""
    out = []
    pids = set()
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Vita3K.exe", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True)
    for row in csv.reader(r.stdout.splitlines()):
        if len(row) > 1 and row[0].lower() == "vita3k.exe":
            pids.add(int(row[1]))

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            out.append((hwnd, buf.value))
        return True
    user32.EnumWindows(cb, 0)
    return out


def game_window():
    for hwnd, title in windows():
        if "FPS" in title or "(PCSA" in title:
            return hwnd, title
    return None, None


def key(hwnd, name, down):
    vk, sc, ext = KEYS[name]
    lparam = 1 | (sc << 16) | ((1 << 24) if ext else 0)
    if not down:
        lparam |= (1 << 30) | (1 << 31)
    user32.PostMessageW(hwnd, WM_KEYUP if not down else WM_KEYDOWN, vk, lparam)


def capture(hwnd, path):
    """PrintWindow(PW_RENDERFULLCONTENT) of the client area to a PNG; works without focus."""
    import struct, zlib
    gdi32 = ctypes.WinDLL("gdi32")
    rc = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    w, h = rc.right, rc.bottom
    hdc = user32.GetDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, 1 | 2)  # PW_CLIENTONLY | PW_RENDERFULLCONTENT

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]
    bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
    gdi32.DeleteObject(bmp); gdi32.DeleteDC(mdc); user32.ReleaseDC(hwnd, hdc)
    raw = bytearray()
    for y in range(h):
        row = buf.raw[y * w * 4:(y + 1) * w * 4]
        raw.append(0)
        rgb = bytearray(w * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
        raw += rgb

    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xFFFFFFFF)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


class Sampler:
    def __init__(self, path):
        self.f = open(path, "a", newline="") if path else None
        self.w = csv.writer(self.f) if self.f else None
        self.label = ""
        self.t0 = time.time()

    def sample(self):
        _, title = game_window()
        m = TITLE_RE.search(title or "")
        if m and self.w:
            self.w.writerow([f"{time.time() - self.t0:.1f}", self.label, m.group(1), m.group(2),
                             m.group(3), m.group(4)])
            self.f.flush()
        return m

    def sleep(self, ms):
        end = time.time() + ms / 1000
        while time.time() < end:
            self.sample()
            time.sleep(min(0.5, max(0.0, end - time.time())))


def run(steps, fps_csv):
    hwnd, _ = game_window()
    if not hwnd:
        sys.exit("no Vita3K game window")
    s = Sampler(fps_csv)
    for step in [x.strip() for x in steps.split(",") if x.strip()]:
        if step.startswith("wait"):
            s.sleep(int(step.split()[1]))
        elif step.startswith("shot:"):
            capture(hwnd, step[5:])
        elif step.startswith("mark:"):
            s.label = step[5:]
        elif step.startswith("hold:"):
            _, b, ms = step.split(":")
            key(hwnd, b, True); s.sleep(int(ms)); key(hwnd, b, False); time.sleep(0.1)
        else:
            b, _, n = step.partition("*")
            for _ in range(int(n or 1)):
                key(hwnd, b, True); time.sleep(0.1); key(hwnd, b, False); time.sleep(0.3)


def main():
    root, cmd = sys.argv[1], sys.argv[2]
    if cmd == "launch":
        exe = os.path.join(root, "Vita3K.exe")
        backend = sys.argv[4] if len(sys.argv) > 4 else "Vulkan"
        # -B is not reliable once config.yml holds a backend (a later -B Vulkan stayed on OpenGL),
        # so write the choice into config.yml too.
        cfg = os.path.join(root, "config.yml")
        text = open(cfg, encoding="utf-8").read()
        open(cfg, "w", encoding="utf-8").write(
            re.sub(r"^backend-renderer: .*$", f"backend-renderer: {backend}", text, flags=re.M))
        subprocess.Popen([exe, "-B", backend, "-r", sys.argv[3]], cwd=root,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while time.time() - t0 < 60:
            hwnd, title = game_window()
            if hwnd:
                print(f"game window after {time.time() - t0:.1f}s: {title}")
                return
            time.sleep(0.5)
        print("no game window after 60 s; windows:", windows())
    elif cmd == "run":
        run(sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    elif cmd == "bootseq":
        # Set the retail block's BootSequence in the installed config.ini (backup: config.ini.orig).
        ini = os.path.join(root, "fs", "ux0", "app", sys.argv[3], "resource", "config.ini")
        orig = ini + ".orig"
        if not os.path.exists(orig):
            with open(ini, "rb") as f, open(orig, "wb") as g:
                g.write(f.read())
        lines = open(orig, "rb").read().split(b"\r\n")
        idx = [i for i, l in enumerate(lines) if l.startswith(b"BootSequence\t")][-1]
        lines[idx] = b"BootSequence\t" + sys.argv[4].encode()
        open(ini, "wb").write(b"\r\n".join(lines))
        print(f"BootSequence = {sys.argv[4]!r}")
    elif cmd == "title":
        for hwnd, t in windows():
            print(hwnd, t)
    elif cmd == "close":
        subprocess.run(["taskkill", "/IM", "Vita3K.exe", "/F"], capture_output=True)


if __name__ == "__main__":
    main()
