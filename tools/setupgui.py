"""The setup's window (Tk): asks for the dump's parts, the playable folder and Microsoft's licence, then
runs setup.py with them in a hidden console and shows its progress. setup.py stays the one place that
does the work, so the window and the command line do the same thing.
"""
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

STAGES = [
    # (pattern in setup.py's output, what the window says)
    (r'^1/3 ', 'Downloading the firmware (from Sony) and the compiler'),
    (r'^installing ', 'Installing the game into the playable folder'),
    (r'^running the game for (\d+)', 'Starting the game once to load its code (a window opens; leave it alone)'),
    (r'^recompiling ', 'Translating the game\'s code into C++'),
    (r'^compiling', 'Compiling the translated code'),
    (r'^3/3 done', 'Done'),
]


def find_licence(path):
    """A pkg's licence next to it, as dumps keep it: work.bin, or a single .rif."""
    folder = os.path.dirname(path)
    if os.path.exists(os.path.join(folder, 'work.bin')):
        return os.path.join(folder, 'work.bin')
    rifs = [f for f in os.listdir(folder) if f.lower().endswith('.rif')]
    return os.path.join(folder, rifs[0]) if len(rifs) == 1 else ''


class Part:
    """One part of the dump: a path and, for a pkg, its licence."""

    def __init__(self, parent, row, label, required):
        self.path = tk.StringVar()
        self.licence = tk.StringVar()
        self.required = required
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=(0, 8), pady=2)
        ttk.Entry(parent, textvariable=self.path, width=60).grid(row=row, column=1, sticky='ew', pady=2)
        ttk.Button(parent, text='File...', command=self.pick_file).grid(row=row, column=2, padx=2)
        ttk.Button(parent, text='Folder...', command=self.pick_folder).grid(row=row, column=3, padx=2)
        ttk.Label(parent, text='Licence').grid(row=row + 1, column=0, sticky='w', padx=(0, 8))
        ttk.Entry(parent, textvariable=self.licence, width=60).grid(row=row + 1, column=1, sticky='ew', pady=(0, 6))
        ttk.Button(parent, text='Browse...', command=self.pick_licence).grid(row=row + 1, column=2, padx=2,
                                                                            pady=(0, 6))

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
        if not path:
            return []
        out = ['--install', path]
        lic = self.licence.get().strip().strip('"')
        if lic:
            out += ['--license', lic] if os.path.exists(lic) else ['--zrif', lic]
        return out


class Window:
    def __init__(self, game, setup_py, default_out, licence_url):
        self.game = game
        self.setup_py = setup_py
        self.root = tk.Tk()
        self.root.title(f'{game["name"]}: setup')
        self.root.minsize(760, 0)
        self.lines = queue.Queue()
        self.proc = None
        self.out = tk.StringVar(value=default_out)
        self.accepted = tk.BooleanVar()
        self.stage = tk.StringVar(value='')
        self.extra_args = []  # more setup.py arguments (tests: --cache)

        f = ttk.Frame(self.root, padding=12)
        f.grid(sticky='nsew')
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        ttk.Label(f, text=game['name'], font=('Segoe UI', 14, 'bold')).grid(sticky='w')
        ttk.Label(f, wraplength=720, justify='left', text=(
            f'This turns your own dump of the game ({game["title_id"]}) into a playable PC port. It downloads '
            'the PS Vita firmware from Sony and a compiler, then translates the game\'s code and compiles it on '
            'this PC, which takes a few minutes. Nothing of the game leaves your PC.')).grid(sticky='w', pady=(4, 10))

        dump = ttk.LabelFrame(f, text='Your dump: a pkg (with its work.bin or .rif), a folder, a zip or a vpk',
                              padding=8)
        dump.grid(sticky='ew')
        dump.columnconfigure(1, weight=1)
        self.parts = [Part(dump, 0, 'Game', True), Part(dump, 2, 'Update', False)]
        self.addons = []
        self.addon_frame = dump
        self.addon_row = 4
        self.add_button = ttk.Button(dump, text='Add an add-on (DLC)...', command=self.add_addon)
        self.add_button.grid(row=100, column=1, sticky='w', pady=(4, 0))
        ttk.Label(dump, foreground='gray', text='The update and add-ons are optional. A licence can also be '
                  'a zRIF typed in.').grid(row=101, column=1, sticky='w')

        where = ttk.Frame(f)
        where.grid(sticky='ew', pady=(10, 0))
        where.columnconfigure(1, weight=1)
        ttk.Label(where, text='Playable folder').grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(where, textvariable=self.out).grid(row=0, column=1, sticky='ew')
        ttk.Button(where, text='Browse...', command=self.pick_out).grid(row=0, column=2, padx=2)

        lic = ttk.Frame(f)
        lic.grid(sticky='w', pady=(10, 0))
        ttk.Checkbutton(lic, variable=self.accepted, text='I accept Microsoft\'s licence for the C++ runtime and '
                        'Windows SDK the compiler uses').grid(row=0, column=0)
        link = ttk.Label(lic, text='(read it)', foreground='blue', cursor='hand2')
        link.grid(row=0, column=1, padx=4)
        link.bind('<Button-1>', lambda e: webbrowser.open(licence_url))

        buttons = ttk.Frame(f)
        buttons.grid(sticky='ew', pady=(12, 0))
        self.install = ttk.Button(buttons, text='Install', command=self.start)
        self.install.grid(row=0, column=0)
        self.play = ttk.Button(buttons, text='Play', command=self.launch, state='disabled')
        self.play.grid(row=0, column=1, padx=6)
        self.open = ttk.Button(buttons, text='Open folder', command=self.open_folder, state='disabled')
        self.open.grid(row=0, column=2)

        ttk.Label(f, textvariable=self.stage, font=('Segoe UI', 10, 'bold')).grid(sticky='w', pady=(12, 2))
        self.bar = ttk.Progressbar(f, mode='determinate', maximum=len(STAGES) - 1)
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
        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def add_addon(self):
        part = Part(self.addon_frame, self.addon_row, f'Add-on {len(self.addons) + 1}', False)
        self.addons.append(part)
        self.addon_row += 2
        part.pick_file()

    def pick_out(self):
        d = filedialog.askdirectory()
        if d:
            self.out.set(os.path.normpath(d))

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
        if not parts[0].path.get().strip():
            messagebox.showerror('Setup', 'Choose the game first: its pkg, folder, zip or vpk.')
            return
        for p in parts:
            path = p.path.get().strip().strip('"')
            if path and not os.path.exists(path):
                messagebox.showerror('Setup', f'{path} does not exist.')
                return
        if not self.accepted.get():
            messagebox.showerror('Setup', 'The compiler needs Microsoft\'s licence accepted.')
            return
        out = self.out.get().strip()
        if os.path.exists(os.path.join(out, self.game['id'] + '.exe')) and not messagebox.askyesno(
                'Setup', f'{out} already holds the game. Install over it? (Saves in it are kept.)'):
            return
        args = [a for p in parts for a in p.args()]
        cmd = [os.path.join(os.path.dirname(sys.executable), 'python.exe'), '-I', '-u', self.setup_py,
               *args, '--out', out, '--accept-microsoft-license', *self.extra_args]
        self.install['state'] = 'disabled'
        self.bar['value'] = 0
        self.stage.set(STAGES[0][1])
        # a hidden console, which every program the setup starts shares
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding='utf-8', errors='replace',
                                     creationflags=subprocess.CREATE_NO_WINDOW)
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
                for i, (pattern, text) in enumerate(STAGES):
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
            self.bar['value'] = self.bar['maximum']
            self.stage.set(f'Done: {self.game["name"]} is ready to play.')
            self.play['state'] = 'normal'
            self.open['state'] = 'normal'
        else:
            self.stage.set('The setup failed: see the messages above.')

    def launch(self):
        out = self.out.get().strip()
        subprocess.Popen([os.path.join(out, self.game['id'] + '.exe')], cwd=out)

    def open_folder(self):
        os.startfile(self.out.get().strip())

    def close(self):
        if self.proc and not messagebox.askyesno('Setup', 'The setup is still running. Stop it?'):
            return
        if self.proc:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(self.proc.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        self.root.destroy()

    def run(self):
        self.root.mainloop()
