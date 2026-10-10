"""Print the call stack of every thread of a running process (x64), with symbols from its PDBs.

For diagnosing hangs without a debugger: suspends each thread briefly, walks its stack with
DbgHelp's StackWalk64 and resolves frames with SymFromAddr. Read-only otherwise.

usage: python -I tools/stackdump.py <pid or image name> [max frames]
"""
import ctypes
import subprocess
import sys
from ctypes import wintypes

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
dbghelp = ctypes.WinDLL('dbghelp', use_last_error=True)

PROCESS_ALL_ACCESS = 0x1F0FFF
THREAD_ALL_ACCESS = 0x1F03FF
TH32CS_SNAPTHREAD = 0x4
CONTEXT_FULL = 0x10000B  # CONTEXT_AMD64 | CONTROL | INTEGER | FLOATING_POINT
IMAGE_FILE_MACHINE_AMD64 = 0x8664


class THREADENTRY32(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ThreadID', wintypes.DWORD),
                ('th32OwnerProcessID', wintypes.DWORD), ('tpBasePri', wintypes.LONG),
                ('tpDeltaPri', wintypes.LONG), ('dwFlags', wintypes.DWORD)]


class CONTEXT(ctypes.Structure):
    # x64 CONTEXT, 1232 bytes; only the fields StackWalk64 needs are named
    _fields_ = [('P1Home', ctypes.c_uint64 * 6), ('ContextFlags', wintypes.DWORD), ('MxCsr', wintypes.DWORD),
                ('SegCs', wintypes.WORD), ('SegDs', wintypes.WORD), ('SegEs', wintypes.WORD),
                ('SegFs', wintypes.WORD), ('SegGs', wintypes.WORD), ('SegSs', wintypes.WORD),
                ('EFlags', wintypes.DWORD), ('Dr', ctypes.c_uint64 * 6), ('Rax', ctypes.c_uint64),
                ('Rcx', ctypes.c_uint64), ('Rdx', ctypes.c_uint64), ('Rbx', ctypes.c_uint64),
                ('Rsp', ctypes.c_uint64), ('Rbp', ctypes.c_uint64), ('Rsi', ctypes.c_uint64),
                ('Rdi', ctypes.c_uint64), ('R8_15', ctypes.c_uint64 * 8), ('Rip', ctypes.c_uint64),
                ('Rest', ctypes.c_byte * (1232 - 256))]


class ADDRESS64(ctypes.Structure):
    _fields_ = [('Offset', ctypes.c_uint64), ('Segment', wintypes.WORD), ('Mode', wintypes.DWORD)]


class STACKFRAME64(ctypes.Structure):
    _fields_ = [('AddrPC', ADDRESS64), ('AddrReturn', ADDRESS64), ('AddrFrame', ADDRESS64),
                ('AddrStack', ADDRESS64), ('AddrBStore', ADDRESS64), ('FuncTableEntry', ctypes.c_void_p),
                ('Params', ctypes.c_uint64 * 4), ('Far', wintypes.BOOL), ('Virtual', wintypes.BOOL),
                ('Reserved', ctypes.c_uint64 * 3), ('KdHelp', ctypes.c_byte * 112)]


class SYMBOL_INFO(ctypes.Structure):
    _fields_ = [('SizeOfStruct', wintypes.ULONG), ('TypeIndex', wintypes.ULONG), ('Reserved', ctypes.c_uint64 * 2),
                ('Index', wintypes.ULONG), ('Size', wintypes.ULONG), ('ModBase', ctypes.c_uint64),
                ('Flags', wintypes.ULONG), ('Value', ctypes.c_uint64), ('Address', ctypes.c_uint64),
                ('Register', wintypes.ULONG), ('Scope', wintypes.ULONG), ('Tag', wintypes.ULONG),
                ('NameLen', wintypes.ULONG), ('MaxNameLen', wintypes.ULONG), ('Name', ctypes.c_char * 512)]


class IMAGEHLP_MODULE64(ctypes.Structure):
    _fields_ = [('SizeOfStruct', wintypes.DWORD), ('BaseOfImage', ctypes.c_uint64), ('ImageSize', wintypes.DWORD),
                ('TimeDateStamp', wintypes.DWORD), ('CheckSum', wintypes.DWORD), ('NumSyms', wintypes.DWORD),
                ('SymType', wintypes.DWORD), ('ModuleName', ctypes.c_char * 32), ('ImageName', ctypes.c_char * 256),
                ('LoadedImageName', ctypes.c_char * 256), ('Rest', ctypes.c_byte * 2048)]


k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenThread.restype = wintypes.HANDLE
k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
dbghelp.SymFunctionTableAccess64.restype = ctypes.c_void_p
dbghelp.SymFunctionTableAccess64.argtypes = [wintypes.HANDLE, ctypes.c_uint64]
dbghelp.SymGetModuleBase64.restype = ctypes.c_uint64
dbghelp.SymGetModuleBase64.argtypes = [wintypes.HANDLE, ctypes.c_uint64]
dbghelp.SymInitialize.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.BOOL]
dbghelp.SymFromAddr.argtypes = [wintypes.HANDLE, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(SYMBOL_INFO)]
dbghelp.SymGetModuleInfo64.argtypes = [wintypes.HANDLE, ctypes.c_uint64, ctypes.POINTER(IMAGEHLP_MODULE64)]
FTA = ctypes.WINFUNCTYPE(ctypes.c_void_p, wintypes.HANDLE, ctypes.c_uint64)
GMB = ctypes.WINFUNCTYPE(ctypes.c_uint64, wintypes.HANDLE, ctypes.c_uint64)
dbghelp.StackWalk64.argtypes = [wintypes.DWORD, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(STACKFRAME64),
                                ctypes.c_void_p, ctypes.c_void_p, FTA, GMB, ctypes.c_void_p]


def find_pid(arg):
    if arg.isdigit():
        return int(arg)
    out = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {arg}', '/FO', 'CSV', '/NH'],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == arg.lower():
            return int(parts[1])
    sys.exit(f'no process {arg}')


def threads(pid):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    te = THREADENTRY32()
    te.dwSize = ctypes.sizeof(te)
    ok = k32.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            yield te.th32ThreadID
        ok = k32.Thread32Next(snap, ctypes.byref(te))
    k32.CloseHandle(snap)


def describe(hproc, addr):
    sym = SYMBOL_INFO()
    sym.SizeOfStruct = 88  # sizeof(SYMBOL_INFO) in C, Name[1] included
    sym.MaxNameLen = 511
    disp = ctypes.c_uint64()
    mod = IMAGEHLP_MODULE64()
    mod.SizeOfStruct = ctypes.sizeof(IMAGEHLP_MODULE64) - 2048 + 8  # IMAGEHLP_MODULE64 v1 size is enough
    mname = ''
    if dbghelp.SymGetModuleInfo64(hproc, addr, ctypes.byref(mod)):
        mname = mod.ModuleName.decode(errors='replace')
    if dbghelp.SymFromAddr(hproc, addr, ctypes.byref(disp), ctypes.byref(sym)):
        return f'{mname}!{sym.Name.decode(errors="replace")}+{disp.value:#x}'
    return f'{mname}+{addr - mod.BaseOfImage:#x}' if mname else f'{addr:#x}'


def main():
    pid = find_pid(sys.argv[1])
    max_frames = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    hproc = k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not hproc:
        sys.exit(f'OpenProcess failed: {ctypes.get_last_error()}')
    dbghelp.SymSetOptions(0x2 | 0x10 | 0x200)  # UNDNAME | LOAD_LINES | DEFERRED_LOADS
    if not dbghelp.SymInitialize(hproc, None, True):
        sys.exit(f'SymInitialize failed: {ctypes.get_last_error()}')
    fta = FTA(lambda h, a: dbghelp.SymFunctionTableAccess64(h, a))
    gmb = GMB(lambda h, a: dbghelp.SymGetModuleBase64(h, a))
    for tid in threads(pid):
        ht = k32.OpenThread(THREAD_ALL_ACCESS, False, tid)
        if not ht:
            continue
        buf = ctypes.create_string_buffer(ctypes.sizeof(CONTEXT) + 16)
        ctx = CONTEXT.from_address((ctypes.addressof(buf) + 15) & ~15)
        ctx.ContextFlags = CONTEXT_FULL
        k32.SuspendThread(ht)
        try:
            if not k32.GetThreadContext(ht, ctypes.byref(ctx)):
                continue
            frame = STACKFRAME64()
            frame.AddrPC.Offset, frame.AddrPC.Mode = ctx.Rip, 3
            frame.AddrFrame.Offset, frame.AddrFrame.Mode = ctx.Rbp, 3
            frame.AddrStack.Offset, frame.AddrStack.Mode = ctx.Rsp, 3
            lines = []
            for _ in range(max_frames):
                if not dbghelp.StackWalk64(IMAGE_FILE_MACHINE_AMD64, hproc, ht, ctypes.byref(frame),
                                           ctypes.addressof(ctx), None, fta, gmb, None):
                    break
                if not frame.AddrPC.Offset:
                    break
                lines.append('    ' + describe(hproc, frame.AddrPC.Offset))
        finally:
            k32.ResumeThread(ht)
            k32.CloseHandle(ht)
        print(f'thread {tid}:')
        print('\n'.join(lines))
    dbghelp.SymCleanup(hproc)


if __name__ == '__main__':
    main()
