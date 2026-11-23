"""The setup's windows (Tk). Window asks for the dump's parts, the playable folder and (Windows)
Microsoft's licence, then runs setup.py with them in a hidden console and shows its progress.
AndroidWindow (setup.py --android) asks how the Android device is connected and runs setup.py
--android --console the same way. setup.py stays the one place that does the work, so the windows and
the command line do the same thing.
"""
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import dumpinfo
from gameproject import MACOS, WINDOWS

# a console of its own that no one sees, which every program the setup starts shares (Windows)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

STAGES = [
    # (pattern in setup.py's output, what the window says)
    (r'^1/3 ', 'Downloading the firmware (from Sony) and the compiler'),
    (r'^installing ', 'Installing the game into the playable folder'),
    (r'^running the game for (\d+)', 'Starting the game once, unseen, to load its code (about a minute)'),
    (r'^recompiling ', 'Translating the game\'s code into C++'),
    (r'^compiling', 'Compiling the translated code'),
    (r'^3/3 done', 'Done'),
]
ANDROID_STAGES = [
    (r'^android 1/3 ', 'Building the game\'s modules for Android (several minutes)'),
    (r'^android 2/3 ', 'Downloading adb (from Google)'),
    (r'^android 3/3 ', 'Copying the game to the device'),
    (r'^installing ', 'Installing the game on the device'),
    (r'^copying .* to the device', 'Copying the game\'s files to the device (minutes over Wi-Fi)'),
    (r'^copying \d+ modules', 'Copying the modules'),
    (r'^android done', 'Done'),
]


find_licence = dumpinfo.find_licence  # a pkg's licence next to it


class Part:
    """One part of the dump: a path and, for a pkg (or a dump without one inside), its licence. The
    licence row shows only when it is needed; the whole part can stand aside as included in another
    part's dump (the update in the game's zip)."""

    def __init__(self, parent, row, label, required):
        self.path = tk.StringVar()
        self.licence = tk.StringVar()
        self.required = required
        self.included = False
        self.needs_licence = None  # the window's verdict for a zip or folder (dumpinfo); None: only a pkg
        self.label = ttk.Label(parent, text=label)
        self.label.grid(row=row, column=0, sticky='w', padx=(0, 8), pady=2)
        self.entry = ttk.Entry(parent, textvariable=self.path, width=60)
        self.entry.grid(row=row, column=1, sticky='ew', pady=2)
        self.note = ttk.Label(parent, foreground='gray')
        self.note.grid(row=row, column=1, sticky='w', pady=2)
        self.note.grid_remove()
        self.buttons = [ttk.Button(parent, text='File...', command=self.pick_file),
                        ttk.Button(parent, text='Folder...', command=self.pick_folder)]
        self.buttons[0].grid(row=row, column=2, padx=2)
        self.buttons[1].grid(row=row, column=3, padx=2)
        self.licence_row = [ttk.Label(parent, text='Licence'),
                            ttk.Entry(parent, textvariable=self.licence, width=60),
                            ttk.Button(parent, text='Browse...', command=self.pick_licence)]
        self.licence_row[0].grid(row=row + 1, column=0, sticky='w', padx=(0, 8))
        self.licence_row[1].grid(row=row + 1, column=1, sticky='ew', pady=(0, 6))
        self.licence_row[2].grid(row=row + 1, column=2, padx=2, pady=(0, 6))
        self.path.trace_add('write', lambda *_: self.show_licence())
        self.show_licence()

    def show_licence(self):
        """The licence row: for a pkg, or a zip or folder found without its licence inside."""
        path = self.path.get().strip().strip('"').lower()
        show = not self.included and (path.endswith('.pkg') or bool(self.needs_licence))
        self.licence_shown = show
        for w in self.licence_row:
            w.grid() if show else w.grid_remove()

    def set_included(self, text):
        """text: this part is in another part's dump (a note replaces the path); '' asks for it again."""
        self.included = bool(text)
        self.note['text'] = text
        for w in [self.entry] + self.buttons:
            w.grid_remove() if self.included else w.grid()
        self.note.grid() if self.included else self.note.grid_remove()
        self.show_licence()

    def pick_file(self):
        f = filedialog.askopenfilename(filetypes=[('Game dump', '*.pkg *.zip *.vpk'), ('All files', '*.*')])
        if f:
            self.path.set(os.path.normpath(f))
            if f.lower().endswith('.pkg') and not self.licence.get():
                self.licence.set(find_licence(f))

    def pick_folder(self):
        d = filedialog.askdirectory()
        if d:
            self.path.set(os.path.normpath(d))
            self.licence.set('')

    def pick_licence(self):
        f = filedialog.askopenfilename(filetypes=[('Licence', 'work.bin *.rif'), ('All files', '*.*')])
        if f:
            self.licence.set(os.path.normpath(f))

    def args(self):
        path = self.path.get().strip().strip('"')
        if not path or self.included:
            return []
        out = ['--install', path]
        lic = self.licence.get().strip().strip('"')
        if lic and self.licence_shown:
            out += ['--license', lic] if os.path.exists(lic) else ['--zrif', lic]
        return out


class Window:
    def __init__(self, game, setup_py, out, exe, licence_url):
        """out: the game's folder, where it is built; exe: the game's executable (which started this
        window and waits for it); licence_url: Microsoft's licence the Windows compiler needs accepted,
        None when there is none."""
        self.game = game
        self.exe = exe
        self.setup_py = setup_py
        self.root = tk.Tk()
        self.root.title(f'{game["name"]}: setup')
        self.root.minsize(760, 0)
        self.lines = queue.Queue()
        self.proc = None
        self.out = out
        self.built = False
        self.result = 1  # setup.py's exit code: 1 cancelled, 0 Play, 2 built but closed
        self.accepted = tk.BooleanVar(value=licence_url is None)
        self.shortcut = tk.BooleanVar(value=True)
        self.steam = tk.BooleanVar(value=True)  # Linux: setup.py --steam
        self.stage = tk.StringVar(value='')
        self.extra_args = []  # more setup.py arguments (tests: --cache)
        self.stages = STAGES

        f = ttk.Frame(self.root, padding=12)
        f.grid(sticky='nsew')
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        ttk.Label(f, text=game['name'], font=('Segoe UI', 14, 'bold')).grid(sticky='w')
        ttk.Label(f, wraplength=720, justify='left', text=(
            f'This turns your own dump of the game ({" or ".join([game["title_id"]] + game.get("other_title_ids", []))}) into the game, once. It downloads '
            'the PS Vita firmware from Sony and a compiler, then translates the game\'s code and compiles it on '
            'this computer, which takes a few minutes. Everything stays in the game\'s folder; the downloads are '
            'deleted afterwards. Nothing of the game leaves your computer.')).grid(sticky='w', pady=(4, 10))

        dump = ttk.LabelFrame(f, text='Your dump: a pkg (with its work.bin or .rif), a folder, a zip or a vpk',
                              padding=8)
        dump.grid(sticky='ew')
        dump.columnconfigure(1, weight=1)
        self.parts = [Part(dump, 0, 'Game', True), Part(dump, 2, 'Update', False)]
        self.addons = []
        self.addon_frame = dump
        self.addon_row = 4
        self.add_button = ttk.Button(dump, text='Add an add-on (DLC) or mod...', command=self.add_addon)
        self.add_button.grid(row=100, column=1, sticky='w', pady=(4, 0))
        self.hint = ('The update, add-ons and mods (a rePatch folder or zip) are optional. A licence can also '
                     'be a zRIF typed in. Once the game is built, add-ons and mods can be added alone.')
        self.dump_note = ttk.Label(dump, foreground='gray', text=self.hint, wraplength=560, justify='left')
        self.dump_note.grid(row=101, column=1, sticky='w')
        self.check_pending = None
        self.parts[0].path.trace_add('write', self.watch_dump)

        ttk.Label(f, foreground='gray', wraplength=720, justify='left',
                  text=f'The game\'s folder: {out}').grid(sticky='w', pady=(10, 0))

        if licence_url:
            lic = ttk.Frame(f)
            lic.grid(sticky='w', pady=(10, 0))
            ttk.Checkbutton(lic, variable=self.accepted, text='I accept Microsoft\'s licence for the C++ runtime '
                            'and Windows SDK the compiler uses').grid(row=0, column=0)
            link = ttk.Label(lic, text='(read it)', foreground='blue', cursor='hand2')
            link.grid(row=0, column=1, padx=4)
            link.bind('<Button-1>', lambda e: webbrowser.open(licence_url))
        shortcut = ('Put a shortcut to the game on the desktop' if WINDOWS else
                    'Put the game in Applications too (Launchpad and Spotlight list it)' if MACOS else
                    'Add the game to the applications menu and the desktop (Steam\'s "Add a Non-Steam Game" '
                    'lists it)')
        ttk.Checkbutton(f, variable=self.shortcut, text=shortcut).grid(sticky='w', pady=(4, 0))
        if not WINDOWS and not MACOS:
            ttk.Checkbutton(f, variable=self.steam, text='Add the game to Steam (a non-Steam game: on a Steam Deck '
                            'it then starts from Game Mode; Steam must be running)').grid(sticky='w')

        buttons = ttk.Frame(f)
        buttons.grid(sticky='ew', pady=(12, 0))
        self.install = ttk.Button(buttons, text='Build', command=self.start)
        self.install.grid(row=0, column=0)
        self.play = ttk.Button(buttons, text='Play', command=self.launch, state='disabled')
        self.play.grid(row=0, column=1, padx=6)
        self.open = ttk.Button(buttons, text='Open folder', command=self.open_folder, state='disabled')
        self.open.grid(row=0, column=2)

        ttk.Label(f, textvariable=self.stage, font=('Segoe UI', 10, 'bold'), wraplength=720, justify='left').grid(sticky='w', pady=(12, 2))
        self.add_log(f)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def add_log(self, f):
        """The progress bar and the setup's output, at the bottom of frame f."""
        self.bar = ttk.Progressbar(f, mode='determinate', maximum=len(self.stages) - 1)
        self.bar.grid(sticky='ew')
        log = ttk.Frame(f)
        log.grid(sticky='nsew', pady=(6, 0))
        f.rowconfigure(log.grid_info()['row'], weight=1)
        log.columnconfigure(0, weight=1)
        log.rowconfigure(0, weight=1)
        self.log = tk.Text(log, height=12, wrap='none', font=('Consolas', 9), state='disabled')
        self.log.grid(row=0, column=0, sticky='nsew')
        sb = ttk.Scrollbar(log, command=self.log.yview)
        sb.grid(row=0, column=1, sticky='ns')
        self.log['yscrollcommand'] = sb.set

    def watch_dump(self, *_):
        """The game's path changed (picked or typed): look into the dump once typing pauses."""
        if self.check_pending:
            self.root.after_cancel(self.check_pending)
        self.check_pending = self.root.after(400, self.check_dump)

    def check_dump(self):
        """What the game's dump already holds (dumpinfo): its licence and the update aren't asked for
        again. A pkg is encrypted: it keeps both rows."""
        self.check_pending = None
        game, update = self.parts
        path = game.path.get().strip().strip('"')
        found = None
        if path and not path.lower().endswith('.pkg') and os.path.exists(path):
            found = dumpinfo.inspect(path)
        ids = [self.game['title_id']] + self.game.get('other_title_ids', [])
        info = dumpinfo.summary(found, ids) if found is not None else None
        game.needs_licence = bool(info and info['game'] and not info['game']['licence'])
        game.show_licence()
        need = dumpinfo.supported_version(self.game)
        have = info['version'] if info else None
        update.set_included(f'in the game\'s dump ({dumpinfo.label(have)})'
                            if info and info['game'] and have and need and have >= need else '')
        if not info:
            self.dump_note['text'] = self.hint
        elif not info['game'] and not info['update']:
            self.dump_note['text'] = f'This dump holds no {" or ".join(ids)}.'
        elif not info['game']:
            tid = info['update']['title_id']
            self.dump_note['text'] = (
                f'This holds only the update {dumpinfo.label(dumpinfo.version(info["update"]["version"]))}, '
                f'not the game. Choose the folder that holds both app/{tid} and patch/{tid} (or the dump\'s '
                f'zip), or the game here and the update below.')
        else:
            held = []
            if info['game']:
                held.append(f'the game {dumpinfo.label(dumpinfo.version(info["game"]["version"]))}'
                            + (' with its licence' if info['game']['licence'] else
                               ', with your PSN account\'s licence (usable only on your Vita)'
                               if info['game'].get('account_licence') else ', no licence inside'))
            if info['update']:
                held.append(f'the update {dumpinfo.label(dumpinfo.version(info["update"]["version"]))}')
            if info['add-ons']:
                held.append(f'{info["add-ons"]} add-on{"s" if info["add-ons"] > 1 else ""}')
            if info['mods']:
                held.append('a rePatch mod')
            text = 'Found in this dump: ' + ', '.join(held) + '.'
            if need and (not have or have < need):
                text += f' The port needs version {dumpinfo.label(need)}: add the update.'
            if game.needs_licence and info['game'].get('account_licence'):
                tid = info['game']['title_id']
                text += (f' Add its NoNpDrm licence: with the NoNpDrm plugin, start the game once on the Vita, '
                         f'then give ux0:nonpdrm/license/app/{tid}/6488b73b912a753a492e2714e9b38bc7.rif.')
            elif game.needs_licence:
                text += ' Add the game\'s licence (work.bin, .rif or zRIF).'
            self.dump_note['text'] = text

    def add_addon(self):
        part = Part(self.addon_frame, self.addon_row, f'Add-on {len(self.addons) + 1}', False)
        self.addons.append(part)
        self.addon_row += 2
        part.pick_file()

    def write(self, text):
        self.log['state'] = 'normal'
        # progress lines ("  12 / 447 MB ...", "  25/82") replace the previous one
        if re.match(r'^\s+[\d,]+ ?/ ?[\d,]+', text) and getattr(self, 'last_progress', False):
            self.log.delete('end-2l', 'end-1l')
        self.last_progress = bool(re.match(r'^\s+[\d,]+ ?/ ?[\d,]+', text))
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log['state'] = 'disabled'

    def start(self):
        parts = [p for p in self.parts + self.addons]
        ids = [self.game['title_id']] + self.game.get('other_title_ids', [])
        built = any(os.path.exists(os.path.join(self.out, 'fs', 'ux0', 'app', i, 'eboot.bin')) for i in ids)
        # the game built already: add-ons and mods alone are installed (setup.py builds nothing again)
        addons_only = built and not any(p.path.get().strip() for p in self.parts) and \
            any(p.path.get().strip() for p in self.addons)
        if not parts[0].path.get().strip() and not addons_only:
            messagebox.showerror('Setup', 'Choose the game first: its pkg, folder, zip or vpk.'
                                 + (' (Or only add-ons and mods, to add them to the game built here.)' if built
                                    else ''))
            return
        for p in parts:
            path = p.path.get().strip().strip('"')
            if path and not os.path.exists(path):
                messagebox.showerror('Setup', f'{path} does not exist.')
                return
        if not self.accepted.get():
            messagebox.showerror('Setup', 'The compiler needs Microsoft\'s licence accepted.')
            return
        # only the update, not the game: the install would fail after the downloads (an update alone
        # is fine over a built game)
        missing = None if built else dumpinfo.missing_game(
            [p.path.get().strip().strip('"') for p in parts if p.path.get().strip()], ids)
        if missing:
            messagebox.showerror('Setup', f'The game can\'t be installed from this: {missing}')
            return
        if built and not addons_only and \
                not messagebox.askyesno('Setup', 'The game is built already. Build it again from this dump? '
                                        '(Saves and settings are kept. To add only an add-on or mod, leave the '
                                        'game\'s rows empty.)'):
            return
        args = [a for p in parts for a in p.args()]
        # Windows: python.exe beside the pythonw.exe that runs the window, for a console the setup's
        # programs share; Linux and macOS: the same interpreter
        python = os.path.join(os.path.dirname(sys.executable), 'python.exe') if WINDOWS else sys.executable
        cmd = [python, '-I', '-u', self.setup_py, *args, '--exe', self.exe, *self.extra_args]
        if WINDOWS:
            cmd.append('--accept-microsoft-license')
        if self.shortcut.get():
            cmd.append('--shortcut')
        if not WINDOWS and not MACOS and self.steam.get():
            cmd.append('--steam')
        self.install['state'] = 'disabled'
        self.bar['value'] = 0
        self.stage.set(STAGES[0][1])
        # Windows: a hidden console; Linux and macOS: a process group of its own, so Close can stop all of it
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding='utf-8', errors='replace',
                                     creationflags=NO_WINDOW, start_new_session=not WINDOWS)
        threading.Thread(target=self.read, daemon=True).start()
        self.root.after(100, self.poll)

    def read(self):
        for line in self.proc.stdout:
            self.lines.put(line.rstrip('\n'))
        self.lines.put(None)

    def poll(self):
        try:
            while True:
                line = self.lines.get_nowait()
                if line is None:
                    self.finished(self.proc.wait())
                    return
                self.write(line)
                for i, (pattern, text) in enumerate(self.stages):
                    if re.match(pattern, line) and i >= self.bar['value']:
                        self.bar['value'] = i
                        self.stage.set(text)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def finished(self, code):
        self.proc = None
        self.install['state'] = 'normal'
        if code == 0:
            self.built = True
            self.bar['value'] = self.bar['maximum']
            self.stage.set('Done: the game is built. Play starts it; from now on, starting the game goes '
                           'straight to it.')
            self.play['state'] = 'normal'
            self.open['state'] = 'normal'
            self.play.focus_set()
        else:
            self.stage.set('The setup failed: see the messages above.')

    def launch(self):
        """Play: the game, which started this window and waits for it, carries on."""
        self.result = 0
        self.root.destroy()

    def open_folder(self):
        if WINDOWS:
            os.startfile(self.out)
        elif MACOS:
            subprocess.Popen(['open', self.out])
        else:
            subprocess.Popen(['xdg-open', self.out])

    def close(self):
        if self.proc and not messagebox.askyesno('Setup', 'The setup is still running. Stop it?'):
            return
        self.result = 2 if self.built else 1
        if self.proc and WINDOWS:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(self.proc.pid)], capture_output=True,
                           creationflags=NO_WINDOW)
        elif self.proc:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.root.destroy()

    def run(self):
        """The window until it closes; setup.py's exit code (0 Play, 2 built and closed, 1 not built)."""
        self.root.mainloop()
        return self.result


class AndroidWindow(Window):
    """setup.py --android: the game, built on this computer, onto an Android device (the release's APK,
    the game's files, its modules built for Android). Asks how the device is connected and for Google's
    licence (adb), then runs setup.py --android --console with that."""

    def __init__(self, game, setup_py, out, exe):
        import android as droid
        self.game, self.exe, self.setup_py, self.out = game, exe, setup_py, out
        self.root = tk.Tk()
        self.root.title(f'{game["name"]}: Android device')
        self.root.minsize(760, 0)
        self.lines = queue.Queue()
        self.proc = None
        self.built = False
        self.result = 1  # 0: copied to the device
        self.stages = ANDROID_STAGES
        self.stage = tk.StringVar(value='')
        self.extra_args = []
        self.accepted = tk.BooleanVar(value=False)
        self.way = tk.StringVar(value='wifi')
        self.pair = tk.StringVar()
        self.code = tk.StringVar()
        self.connect = tk.StringVar()
        modules = os.path.join(out, 'android')
        self.have_modules = os.path.isdir(modules) and any(n.endswith('.so') for n in os.listdir(modules))
        self.rebuild = tk.BooleanVar(value=not self.have_modules)
        with open(droid.LOCK, encoding='utf-8') as fh:
            licence_url = json.load(fh)['license_url']

        f = ttk.Frame(self.root, padding=12)
        f.grid(sticky='nsew')
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        ttk.Label(f, text=f'{game["name"]} on an Android device', font=('Segoe UI', 14, 'bold')).grid(sticky='w')
        ttk.Label(f, wraplength=720, justify='left', text=(
            'This puts the game you built on this computer onto an Android device with a 64-bit ARM chip and '
            'Vulkan (made for Snapdragon handhelds such as the AYN Thor): the game\'s app, its files and its '
            'code built for Android, which takes a few minutes. The files are copied over the cable or Wi-Fi '
            '(several GB: minutes over Wi-Fi). Saves already on the device stay.')).grid(sticky='w', pady=(4, 10))

        dev = ttk.LabelFrame(f, text='The device: first turn on Developer options (Settings > About: tap the '
                             'build number seven times)', padding=8)
        dev.grid(sticky='ew')
        dev.columnconfigure(1, weight=1)
        ttk.Radiobutton(dev, variable=self.way, value='wifi', text='Over Wi-Fi (the same network as this '
                        'computer)', command=self.show_way).grid(row=0, column=0, columnspan=3, sticky='w')
        self.wifi = ttk.Frame(dev)
        self.wifi.grid(row=1, column=0, columnspan=3, sticky='ew', padx=(22, 0))
        self.wifi.columnconfigure(4, weight=1)  # the fields stay beside their labels
        ttk.Label(self.wifi, foreground='gray', wraplength=660, justify='left', text=(
            'On the device: Developer options > Wireless debugging: turn it on, then "Pair device with pairing '
            'code". Type the IP address and port and the six-digit code it shows.')).grid(
            row=0, column=0, columnspan=4, sticky='w')
        ttk.Label(self.wifi, text='IP address and port').grid(row=1, column=0, sticky='w', pady=(4, 0))
        ttk.Entry(self.wifi, textvariable=self.pair, width=24).grid(row=1, column=1, sticky='w', pady=(4, 0))
        ttk.Label(self.wifi, text='Pairing code').grid(row=1, column=2, sticky='w', padx=(12, 4), pady=(4, 0))
        ttk.Entry(self.wifi, textvariable=self.code, width=10).grid(row=1, column=3, sticky='w', pady=(4, 0))
        ttk.Label(self.wifi, foreground='gray', wraplength=660, justify='left', text=(
            'Paired before? Leave these empty. If the device isn\'t found, type the address and port shown on '
            'the Wireless debugging screen itself here:')).grid(row=2, column=0, columnspan=4, sticky='w', pady=(4, 0))
        ttk.Entry(self.wifi, textvariable=self.connect, width=24).grid(row=3, column=1, sticky='w')
        ttk.Radiobutton(dev, variable=self.way, value='usb', text='With a USB cable (Developer options > USB '
                        'debugging on; allow this computer when the device asks)',
                        command=self.show_way).grid(row=2, column=0, columnspan=3, sticky='w', pady=(6, 0))

        lic = ttk.Frame(f)
        lic.grid(sticky='w', pady=(10, 0))
        ttk.Checkbutton(lic, variable=self.accepted, text='I accept Google\'s Android SDK licence for adb, the '
                        'tool that copies the game').grid(row=0, column=0)
        link = ttk.Label(lic, text='(read it)', foreground='blue', cursor='hand2')
        link.grid(row=0, column=1, padx=4)
        link.bind('<Button-1>', lambda e: webbrowser.open(licence_url))
        if self.have_modules:
            ttk.Checkbutton(f, variable=self.rebuild, text='Build the modules for Android again (needed after '
                            'building the game again on this computer)').grid(sticky='w', pady=(4, 0))

        buttons = ttk.Frame(f)
        buttons.grid(sticky='ew', pady=(12, 0))
        self.install = ttk.Button(buttons, text='Copy to the device', command=self.start)
        self.install.grid(row=0, column=0)
        ttk.Button(buttons, text='Close', command=self.close).grid(row=0, column=1, padx=6)
        self.play = self.open = None

        ttk.Label(f, textvariable=self.stage, font=('Segoe UI', 10, 'bold'), wraplength=720,
                  justify='left').grid(sticky='w', pady=(12, 2))
        self.add_log(f)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        built = any(os.path.exists(os.path.join(out, 'fs', 'ux0', 'app', i, 'eboot.bin'))
                    for i in [game['title_id']] + game.get('other_title_ids', []))
        if not built:
            self.install['state'] = 'disabled'
            self.stage.set('The game isn\'t built on this computer yet: start it once and build it from your '
                           'dump first.')

    def show_way(self):
        if self.way.get() == 'wifi':
            self.wifi.grid()
        else:
            self.wifi.grid_remove()

    def start(self):
        if not self.accepted.get():
            messagebox.showerror('Android device', 'adb needs Google\'s licence accepted.')
            return
        args = ['--android', '--console', '--accept-android-sdk-license', '--exe', self.exe, *self.extra_args]
        if not self.rebuild.get():
            args.append('--push-only')
        pair, code, connect = (v.get().strip() for v in (self.pair, self.code, self.connect))
        if self.way.get() == 'wifi':
            if pair and not code:
                messagebox.showerror('Android device', 'Type the pairing code too (six digits).')
                return
            if pair:
                args += ['--pair', pair, '--code', code]
            if connect:
                args += ['--connect', connect]
        python = os.path.join(os.path.dirname(sys.executable), 'python.exe') if WINDOWS else sys.executable
        cmd = [python, '-I', '-u', self.setup_py, *args]
        self.install['state'] = 'disabled'
        self.bar['value'] = 0
        self.stage.set(self.stages[0][1] if self.rebuild.get() else self.stages[1][1])
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding='utf-8', errors='replace',
                                     creationflags=NO_WINDOW, start_new_session=not WINDOWS)
        threading.Thread(target=self.read, daemon=True).start()
        self.root.after(100, self.poll)

    def finished(self, code):
        self.proc = None
        self.install['state'] = 'normal'
        if code == 0:
            self.result = 0
            self.built = True
            self.have_modules = True
            self.rebuild.set(False)
            self.bar['value'] = self.bar['maximum']
            self.stage.set(f'Done: start {self.game["name"]} on the device. (On a Snapdragon device it offers '
                           'a better GPU driver the first time.)')
        else:
            self.stage.set('Copying to the device failed: see the messages above.')

    def close(self):
        if self.proc and not messagebox.askyesno('Android device', 'Copying is still running. Stop it?'):
            return
        if self.proc and WINDOWS:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(self.proc.pid)], capture_output=True,
                           creationflags=NO_WINDOW)
        elif self.proc:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.root.destroy()
