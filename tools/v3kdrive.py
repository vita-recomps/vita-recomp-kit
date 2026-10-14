r"""Drive the portable Vita3K (Qt frontend) for baseline runs, without taking keyboard focus.

Buttons go through our Vita3K fork's input pipe (\\.\pipe\v3kdrive, enabled by
VITA3K_INPUT_PIPE, which `launch` sets). Builds without it (the upstream release) get
WM_KEYDOWN/WM_KEYUP posted to the game window instead, which Qt ignores while the window
is unfocused. Neither path takes keyboard focus. Screenshots use PrintWindow on the game
window. FPS is sampled from the game window title ("... | 30 FPS (33 ms) | 960x544 | ...").

A single-game front end of the fork (ss-delta.exe, shinobido2.exe, ...) is driven the same way;
its game window title starts with the game's name ("Soul Sacrifice Delta | ... FPS ...").

usage:
  python -I v3kdrive.py <vita3k dir> launch <TITLEID> [Vulkan|OpenGL]  start Vita3K and boot the app
  python -I v3kdrive.py <vita3k dir> start                   start the front end (settings: its .ini)
  python -I v3kdrive.py <vita3k dir> run "<steps>" [fps.csv]  run steps against the open window
  python -I v3kdrive.py <vita3k dir> bootseq <TITLEID> <name>  set BootSequence in the installed config.ini
                                                             (Soul Sacrifice Delta's debug boot)
  python -I v3kdrive.py <vita3k dir> title                   print the game window title
  python -I v3kdrive.py <vita3k dir> close                   close Vita3K

  --exe NAME (anywhere): the front end's executable. Without it: the one known front end
  (FRONTENDS below) in <vita3k dir>, if there is exactly one; otherwise ss-delta.exe.
  --title PREFIX (anywhere): the start of the front end's window title; by default the one
  FRONTENDS lists for its executable (for an executable it does not list, any title).
  --player N (anywhere, front end): the copy of the game for Ad Hoc Play player N (2-4): start
  passes it on, and the other commands use that copy's window and input pipe (v3kdrive<N>).
  Without it they use player 1's.

Steps (comma separated):
  <button>          tap (100 ms)          cross circle square triangle start select
                                          up down left right l r ls_up ls_down ls_left ls_right
                                          rs_up rs_down rs_left rs_right
  <button>*N        tap N times (400 ms apart)
  hold:<button>:ms  hold for ms; several buttons/sticks with +, e.g. hold:ls_up+r:2000
  wait <ms>         sleep, sampling FPS every 500 ms
  shot:<path>       PNG of the game window (PrintWindow; no focus needed)
  frame:<path>      PNG of the frame at render resolution (front end, through the input pipe)
  touch:<x>:<y>:ms  hold a finger on the front touchscreen at (x, y) of 960x544 (front end)
  tdown:<x>:<y>     put the finger down and keep it there; tup lifts it
  record:<n>:<dir>  save the next n rendered frames into dir (front end); returns at once
  memdump:<addr>:<size>:<path>  write guest memory (hex address and size) to path (front end)
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
PIPE = r"\\.\pipe" + "\\"  # + v3kdrive, or v3kdrive<N> for player N
PLAYER = 1  # --player N: Ad Hoc Play's copy N of the front end (window "... (player N)")
STICKS = {"ls_up": (1, -1.0), "ls_down": (1, 1.0), "ls_left": (0, -1.0), "ls_right": (0, 1.0),
          "rs_up": (3, -1.0), "rs_down": (3, 1.0), "rs_left": (2, -1.0), "rs_right": (2, 1.0)}
# single-game front ends of the fork: executable -> start of its window title
FRONTENDS = {"ss-delta.exe": "Soul Sacrifice Delta", "shinobido2.exe": "Shinobido 2"}
EXE = "ss-delta.exe"  # --exe, or the one front end in <vita3k dir>
WINDOW_TITLE = FRONTENDS[EXE]  # --title
IMAGES = ("Vita3K.exe", EXE)
# a Vita3K game window shows the app's title ID, e.g. "(PCSA00152)", before its first FPS update
TITLE_ID_RE = re.compile(r"\([A-Z]{4}\d{5}\)")
TITLE_RE = re.compile(r"\| (?:\S+) \| (\d+) FPS \((\d+) ms\) \| (\d+)x(\d+)")


def windows(with_image=False):
    """(hwnd, title) for every visible top-level window owned by a Vita3K.exe or front-end process;
    (hwnd, title, image) with with_image."""
    out = []
    pids = {}
    for image in IMAGES:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True)
        for row in csv.reader(r.stdout.splitlines()):
            if len(row) > 1 and row[0].lower() == image.lower():
                pids[int(row[1])] = image

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            out.append((hwnd, buf.value, pids[pid.value]) if with_image else (hwnd, buf.value))
        return True
    user32.EnumWindows(cb, 0)
    return out


def is_game_window(title, image):
    """a front end's window must carry its title; then Vita3K's and its own game windows show FPS
    (or the title ID, before the first update)"""
    if image.lower() == EXE.lower() and not title.startswith(WINDOW_TITLE):
        return False
    return "FPS" in title or bool(TITLE_ID_RE.search(title))


def game_window():
    for hwnd, title, image in windows(with_image=True):
        if is_game_window(title, image) and player_of(title) == PLAYER:
            return hwnd, title
    return None, None


def player_of(title):
    m = re.search(r"\(player (\d)\)", title)
    return int(m.group(1)) if m else 1


def pipe_name():
    return "v3kdrive" + (str(PLAYER) if PLAYER > 1 else "")


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


class Pad:
    """Holds buttons through the input pipe, or posts keys to the window without it."""

    def __init__(self, hwnd):
        self.hwnd, self.pipe, self.connected = hwnd, None, False

    def connect(self):
        # The fork creates the pipe when the game first reads the pad, a few seconds after boot.
        # A build without it never does; fall back to posted keys after 15 s.
        if self.connected:
            return
        self.connected = True
        end = time.time() + 15
        while time.time() < end:
            try:
                # O_WRONLY without O_CREAT opens the existing pipe; open(..., "w") would try to create it
                self.pipe = os.fdopen(os.open(PIPE + pipe_name(), os.O_WRONLY | os.O_BINARY), "w", newline="\n")
                break
            except OSError:
                time.sleep(0.1)  # not created yet, or busy between clients
        print("input:", "pipe" if self.pipe else "posted keys")

    def set(self, names):
        self.connect()
        if not self.pipe:
            for b in names:
                key(self.hwnd, b, True)
            return
        axes = [0.0] * 4
        for b in names:
            if b in STICKS:
                axes[STICKS[b][0]] = STICKS[b][1]
        self.pipe.write("hold " + " ".join(b for b in names if b not in STICKS) + "\n")
        self.pipe.write("axes " + " ".join(str(a) for a in axes) + "\n")
        self.pipe.flush()

    def release(self, names):
        if not self.pipe:
            for b in names:
                key(self.hwnd, b, False)
            return
        self.pipe.write("hold none\naxes 0 0 0 0\n")
        self.pipe.flush()

    def touch(self, x, y):
        # the front end holds a front-touch finger at (x, y) on the 960x544 screen; None lifts it
        self.connect()
        if not self.pipe:
            print(f"touch: needs the input pipe ({EXE})")
            return
        self.pipe.write("touch none\n" if x is None else f"touch {x} {y}\n")
        self.pipe.flush()

    def record(self, count, path):
        # the front end saves the next `count` rendered frames into the folder; returns at once
        self.connect()
        if self.pipe:
            self.pipe.write(f"record {count} {os.path.abspath(path)}\n")
            self.pipe.flush()

    def memdump(self, address, size, path):
        # the front end writes guest memory to the file on its next tick; give it a moment
        self.connect()
        if not self.pipe:
            print(f"memdump: needs the input pipe ({EXE})")
            return
        self.pipe.write(f"memdump {address:x} {size:x} {os.path.abspath(path)}\n")
        self.pipe.flush()
        time.sleep(0.3)

    def frame(self, path):
        # the front end saves the frame at render resolution on its next tick; give it a moment
        self.connect()
        if not self.pipe:
            print(f"frame: needs the input pipe ({EXE})")
            return
        self.pipe.write("screenshot " + os.path.abspath(path) + "\n")
        self.pipe.flush()
        time.sleep(0.6)  # read back after the next frame, then written on a thread of its own


def run(steps, fps_csv):
    hwnd, _ = game_window()
    if not hwnd:
        sys.exit("no Vita3K game window")
    s = Sampler(fps_csv)
    pad = Pad(hwnd)
    for step in [x.strip() for x in steps.split(",") if x.strip()]:
        if step.startswith("wait"):
            s.sleep(int(step.split()[1]))
        elif step.startswith("shot:"):
            capture(hwnd, step[5:])
        elif step.startswith("frame:"):
            pad.frame(step[6:])
        elif step.startswith("memdump:"):
            _, address, size, path = step.split(":", 3)
            pad.memdump(int(address, 16), int(size, 16), path)
        elif step.startswith("record:"):
            _, n, path = step.split(":", 2)
            pad.record(int(n), path)
        elif step.startswith("tdown:"):
            _, x, y = step.split(":")
            pad.touch(float(x), float(y))
        elif step == "tup":
            pad.touch(None, None)
        elif step.startswith("touch:"):
            _, x, y, ms = step.split(":")
            pad.touch(float(x), float(y)); s.sleep(int(ms)); pad.touch(None, None); time.sleep(0.1)
        elif step.startswith("mark:"):
            s.label = step[5:]
        elif step.startswith("hold:"):
            _, b, ms = step.split(":")
            names = b.split("+")
            pad.set(names); s.sleep(int(ms)); pad.release(names); time.sleep(0.1)
        else:
            b, _, n = step.partition("*")
            for _ in range(int(n or 1)):
                pad.set([b]); time.sleep(0.1); pad.release([b]); time.sleep(0.3)


def wait_for_window():
    t0 = time.time()
    while time.time() - t0 < 60:
        hwnd, title = game_window()
        if hwnd:
            print(f"game window after {time.time() - t0:.1f}s: {title}")
            return
        time.sleep(0.5)
    print("no game window after 60 s; windows:", windows())


def take_option(name):
    """the value of option `name`, removed from sys.argv; None without it"""
    if name not in sys.argv:
        return None
    i = sys.argv.index(name)
    value = sys.argv[i + 1]
    del sys.argv[i:i + 2]
    return value


def select_frontend(root, exe, title):
    """EXE and WINDOW_TITLE: --exe/--title, else the one known front end in root, else ss-delta.exe"""
    global EXE, WINDOW_TITLE, IMAGES
    if exe:
        EXE = exe if exe.lower().endswith(".exe") else exe + ".exe"
    else:
        found = [e for e in FRONTENDS if os.path.isfile(os.path.join(root, e))]
        if len(found) == 1:
            EXE = found[0]
        elif found:
            # guessing starts the wrong game (whose missing install then shows a message box)
            sys.exit(f"several front ends in {root} ({', '.join(found)}): choose one with --exe")
    known = {k.lower(): v for k, v in FRONTENDS.items()}
    WINDOW_TITLE = title if title is not None else known.get(EXE.lower(), "")
    IMAGES = ("Vita3K.exe", EXE)


def main():
    global PLAYER
    player = take_option("--player")
    if player:
        PLAYER = int(player)
    exe, title = take_option("--exe"), take_option("--title")
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if sys.argv[1:2] in (["-h"], ["--help"]) else 2)
    root, cmd = sys.argv[1], sys.argv[2]
    select_frontend(root, exe, title)
    if cmd == "launch":
        exe = os.path.join(root, "Vita3K.exe")
        backend = sys.argv[4] if len(sys.argv) > 4 else "Vulkan"
        # -B is not reliable once config.yml holds a backend (a later -B Vulkan stayed on OpenGL),
        # so write the choice into config.yml too.
        cfg = os.path.join(root, "config.yml")
        text = open(cfg, encoding="utf-8").read()
        open(cfg, "w", encoding="utf-8").write(
            re.sub(r"^backend-renderer: .*$", f"backend-renderer: {backend}", text, flags=re.M))
        env = dict(os.environ, VITA3K_INPUT_PIPE="v3kdrive")
        subprocess.Popen([exe, "-B", backend, "-r", sys.argv[3]], cwd=root, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for_window()
    elif cmd == "start":
        env = dict(os.environ, VITA3K_INPUT_PIPE=pipe_name())
        args = ["--player", str(PLAYER)] if PLAYER > 1 else []
        subprocess.Popen([os.path.join(root, EXE)] + args, cwd=root, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for_window()
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
        # ask the window to close first (no focus needed), so the log's async queue is written;
        # force it after 8 s
        # while other players' copies run, only this copy's process is killed
        hwnd, _ = game_window()
        others = [t for _, t, image in windows(with_image=True)
                  if is_game_window(t, image) and player_of(t) != PLAYER]
        if hwnd:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            end = time.time() + 8
            while time.time() < end and game_window()[0]:
                time.sleep(0.25)
            subprocess.run(["taskkill", "/PID", str(pid.value), "/F"], capture_output=True)
        if PLAYER == 1 and not others:
            for image in IMAGES:
                subprocess.run(["taskkill", "/IM", image, "/F"], capture_output=True)


if __name__ == "__main__":
    main()
