"""
SDVX Discord Rich Presence — launcher for SOUND VOLTEX (spice2x + Asphyxia).

Launches the EA service (Asphyxia CORE) and the game (spice2x), then shows a
live Discord Rich Presence: current state (Menu / Song Select / Playing /
Result), song title + jacket, difficulty + level, the carded-in player's name
and their VolForce.

How the data is sourced
-----------------------
- State / song / jacket : parsed from the game's stdout log.
- Difficulty + level    : read live from soundvoltex.dll memory (spice2x never
                          logs it). The offset is located once with --find-diff
                          and persisted to the config.
- Player name           : read live from memory; the per-account offset is
                          discovered automatically from the Asphyxia profile
                          names and cached in the config (name_offsets).
- VolForce              : computed from the Asphyxia savedata for the account
                          that is currently carded-in.

Runs on Windows (native) and Linux (game under Wine/Proton; memory is read via
/proc). See the README for setup, prerequisites, CLI flags and credits.
"""

import json
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import ctypes
import ctypes.wintypes

from pypresence import Presence

# ─────────────────────────────────────────────────────────────────────────────
# FIXED CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

CLIENT_ID               = "896595145959551036"
GAME_EXECUTABLE         = "spice64.exe"
__version__             = "1.1.1"                       # bump per release/tag
GITHUB_REPO             = "JofoxTheCat/SDVX7-Launcher"
IMG_MENU                = "sdvx_nabla"
CONFIG_FILE             = "sdvx_rpc_config.json"
JACKET_STANDARD         = "https://jackets.ryu7w7.xyz/sdvx"
PLAYER_NAME_OFFSET_HINT = 0x11FE8E1

# ── SpiceAPI ────────────────────────────────────────────────────────────────
SPICE_DLL          = "soundvoltex.dll"   # module the offsets are relative to
SPICE_DEFAULT_PORT = 1337
SPICE_HOST         = "127.0.0.1"

# Difficulty index byte (0-5) → short name.  Index 3 is the per-song variant
# (INF/GRV/HVN/VVD/XCD) and is resolved against music_db at read time.
_SPICE_DIFF_BY_INDEX = {0: "NOV", 1: "ADV", 2: "EXH", 4: "MXM", 5: "ULT"}

_DIFF_BY_INDEX = _SPICE_DIFF_BY_INDEX
_VARIANT_NAMES = {
    "infinite": "INF", "gravity": "GRV",
    "heaven":   "HVN", "vivid":   "VVD", "exceed": "XCD",
}

# Regexes for "suspicious" log lines in debug mode.
# Using word boundaries + explicit extensions avoids false positives like
# "result" triggering on "ult" or "XCd…" triggering on "xcd".
_DBG_DIFF_RX = re.compile(
    r'(?:novice|advanced|exhaust|infinite|gravity|heaven|vivid|exceed|'
    r'maximum|ultimate|nov|adv|exh|mxm|ult|grv|hvn|vvd|xcd|'
    r'difnum|difficulty|chart)'
    r'|\.(?:ksh|vox|2dx|bin)',
    re.IGNORECASE
)

# ─────────────────────────────────────────────────────────────────────────────
# DEBUG HELPER
# ─────────────────────────────────────────────────────────────────────────────

_DEBUG: bool = "--debug" in sys.argv   # also set by wizard

_DBG_COLORS = {
    "LOG ": "\033[90m",                    # dim gray   – raw log lines
    "DIFF": "\033[96m",                    # cyan       – diff match
    "MISS": "\033[93m",                    # yellow     – no pattern match
    "GAUG": "\033[95m",                    # magenta    – gauge match
    "STAT": "\033[94m",                    # blue       – state transition
    "RPC ": "\033[38;2;88;101;242m",       # blurple    – Discord payload
    "SPCE": "\033[92m",                    # green      – SpiceAPI events
}

def _dbg(category: str, message: str) -> None:
    if not _DEBUG:
        return
    color = _DBG_COLORS.get(category, "\033[0m")
    print(f"{color}[DBG {category}]\033[0m {message}", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENT CONFIG
# ─────────────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _update_config(**kwargs) -> None:
    data = _load_config()
    data.update(kwargs)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass




# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM + LINUX MEMORY BACKEND (game running under Wine/Proton)
# ─────────────────────────────────────────────────────────────────────────────
# On Linux the PE game runs inside a native Wine process; soundvoltex.dll is
# mmap'd into it and shows up in /proc/<pid>/maps, and its memory is readable
# via /proc/<pid>/mem (needs ptrace permission – we launch the game ourselves,
# so default yama ptrace_scope=1 normally suffices; otherwise run privileged or
# set /proc/sys/kernel/yama/ptrace_scope to 0).

_IS_WINDOWS = sys.platform == "win32"
_IS_LINUX   = sys.platform.startswith("linux")
_PTRACE_HINT_SHOWN = False


def _linux_ptrace_hint() -> None:
    global _PTRACE_HINT_SHOWN
    if not _PTRACE_HINT_SHOWN:
        _PTRACE_HINT_SHOWN = True
        print("[WARN] Memory read failed. On Linux this usually means ptrace is "
              "restricted.\n       Try: sudo sysctl -w kernel.yama.ptrace_scope=0 "
              "(or run the launcher with sudo).")


def _linux_find_pid(exe_name: str) -> int | None:
    target = exe_name.lower()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if target in cmd.lower():          # e.g. "… Z:\\…\\spice64.exe …"
            return int(entry)
    return None


def _linux_find_game_pid() -> int | None:
    """Under Wine the real game runs as its OWN process (not the `wine` wrapper
    we spawned, and the wrapper may be named soda/wine64/preloader). The reliable
    signal is the process that has soundvoltex.dll mapped; fall back to a
    spice64.exe comm/cmdline match."""
    fallback = None
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        # definitive: the process that mapped the game module
        try:
            with open(f"/proc/{entry}/maps", errors="replace") as fh:
                if "soundvoltex.dll" in fh.read().lower():
                    return int(entry)
        except OSError:
            pass
        # hint: name/cmdline mentions spice64.exe
        if fallback is None:
            try:
                comm = open(f"/proc/{entry}/comm").read().strip().lower()
            except OSError:
                comm = ""
            if comm == GAME_EXECUTABLE.lower():
                fallback = int(entry)
            else:
                try:
                    with open(f"/proc/{entry}/cmdline", "rb") as fh:
                        if GAME_EXECUTABLE.lower() in fh.read().decode(
                                "utf-8", "replace").lower():
                            fallback = int(entry)
                except OSError:
                    pass
    return fallback


def _linux_module_maps(pid: int, module_name: str) -> tuple[int, int]:
    """(base, size) of the loaded PE module from /proc/<pid>/maps."""
    target = module_name.lower()
    lo, hi = None, 0
    try:
        with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 6:
                    continue
                if not parts[-1].lower().endswith(target):
                    continue
                a, b = parts[0].split("-")
                a, b = int(a, 16), int(b, 16)
                lo = a if lo is None or a < lo else lo
                hi = b if b > hi else hi
    except OSError:
        return 0, 0
    return (lo, hi - lo) if lo is not None else (0, 0)


def _linux_read_mem(pid: int, address: int, size: int) -> bytes:
    try:
        with open(f"/proc/{pid}/mem", "rb", 0) as fh:
            fh.seek(address)
            return fh.read(size)
    except (OSError, ValueError, OverflowError):
        return b""


def _linux_read_full_module(pid: int, base: int, size: int) -> bytes | None:
    """Read every readable segment of [base, base+size) via /proc/pid/mem,
    zero-filling gaps so file offsets stay aligned to RVAs."""
    out = bytearray(size)
    lo, hi = base, base + size
    segs: list[tuple[int, int]] = []
    try:
        with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 6 or "r" not in parts[1]:
                    continue
                a, b = parts[0].split("-")
                a, b = int(a, 16), int(b, 16)
                if b <= lo or a >= hi:
                    continue
                segs.append((max(a, lo), min(b, hi)))
    except OSError:
        return None
    try:
        fh = open(f"/proc/{pid}/mem", "rb", 0)
    except OSError:
        _linux_ptrace_hint()
        return None
    ok = False
    try:
        for a, b in segs:
            try:
                fh.seek(a)
                data = fh.read(b - a)
            except (OSError, ValueError, OverflowError):
                data = b""
            if data:
                out[a - base:a - base + len(data)] = data
                ok = True
    finally:
        fh.close()
    if not ok:
        _linux_ptrace_hint()
    return bytes(out)


# ─────────────────────────────────────────────────────────────────────────────
# WINDOWS MEMORY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_TH32CS_SNAPMODULE   = 0x00000008
_TH32CS_SNAPMODULE32 = 0x00000010
_PROCESS_VM_READ     = 0x0010


class _MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize",        ctypes.wintypes.DWORD),
        ("th32ModuleID",  ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("GlblcntUsage",  ctypes.wintypes.DWORD),
        ("ProccntUsage",  ctypes.wintypes.DWORD),
        ("modBaseAddr",   ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize",   ctypes.wintypes.DWORD),
        ("hModule",       ctypes.wintypes.HMODULE),
        ("szModule",      ctypes.c_char * 256),
        ("szExePath",     ctypes.c_char * 260),
    ]


def _get_module_info(pid: int, module_name: str) -> tuple[int, int]:
    if _IS_LINUX:
        return _linux_module_maps(pid, module_name)
    k32  = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(
        _TH32CS_SNAPMODULE | _TH32CS_SNAPMODULE32, pid)
    if snap == ctypes.wintypes.HANDLE(-1).value:
        return 0, 0
    me = _MODULEENTRY32()
    me.dwSize = ctypes.sizeof(_MODULEENTRY32)
    base = size = 0
    target = module_name.lower()
    try:
        if k32.Module32First(snap, ctypes.byref(me)):
            while True:
                if me.szModule.decode("utf-8", errors="replace").lower() == target:
                    base = ctypes.cast(me.modBaseAddr, ctypes.c_void_p).value or 0
                    size = me.modBaseSize
                    break
                if not k32.Module32Next(snap, ctypes.byref(me)):
                    break
    finally:
        k32.CloseHandle(snap)
    return base, size


def _get_module_base(pid: int, module_name: str) -> int:
    base, _ = _get_module_info(pid, module_name)
    return base


def _read_memory(pid: int, address: int, size: int) -> bytes:
    if _IS_LINUX:
        return _linux_read_mem(pid, address, size)
    k32    = ctypes.windll.kernel32
    handle = k32.OpenProcess(_PROCESS_VM_READ, False, pid)
    if not handle:
        return b""
    buf  = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    k32.ReadProcessMemory(handle, ctypes.c_void_p(address),
                          buf, size, ctypes.byref(read))
    k32.CloseHandle(handle)
    return bytes(buf)[: read.value]


def _read_cstr(pid: int, address: int, max_len: int = 16) -> bytes:
    raw = _read_memory(pid, address, max_len)
    null_pos = raw.find(b"\x00")
    return raw[:null_pos] if null_pos != -1 else raw


# ── process lookup + full-module read (for the differential offset finder) ───
_TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize",              ctypes.wintypes.DWORD),
        ("cntUsage",            ctypes.wintypes.DWORD),
        ("th32ProcessID",       ctypes.wintypes.DWORD),
        ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID",        ctypes.wintypes.DWORD),
        ("cntThreads",          ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase",      ctypes.c_long),
        ("dwFlags",             ctypes.wintypes.DWORD),
        ("szExeFile",           ctypes.c_char * 260),
    ]


def _find_pid_by_name(exe_name: str) -> int | None:
    if _IS_LINUX:
        return _linux_find_pid(exe_name)
    k32  = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == ctypes.wintypes.HANDLE(-1).value:
        return None
    pe = _PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(_PROCESSENTRY32)
    target = exe_name.lower()
    pid    = None
    try:
        if k32.Process32First(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.decode("utf-8", "replace").lower() == target:
                    pid = pe.th32ProcessID
                    break
                if not k32.Process32Next(snap, ctypes.byref(pe)):
                    break
    finally:
        k32.CloseHandle(snap)
    return pid


def _read_full_module(pid: int, base: int, size: int) -> bytes | None:
    """Read an entire loaded module image into a bytes object (admin)."""
    if _IS_LINUX:
        return _linux_read_full_module(pid, base, size)
    k32    = ctypes.windll.kernel32
    handle = k32.OpenProcess(_PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    out   = bytearray(size)
    mv    = memoryview(out)
    CHUNK = 0x100000
    done  = 0
    try:
        while done < size:
            to_read = min(CHUNK, size - done)
            buf     = ctypes.create_string_buffer(to_read)
            n       = ctypes.c_size_t(0)
            k32.ReadProcessMemory(handle, ctypes.c_void_p(base + done),
                                  buf, to_read, ctypes.byref(n))
            got = n.value
            if got:
                mv[done:done + got] = buf.raw[:got]
            done += to_read
    finally:
        k32.CloseHandle(handle)
    return bytes(out)


# ─────────────────────────────────────────────────────────────────────────────
# PLAYER-NAME OFFSET DISCOVERY
# ─────────────────────────────────────────────────────────────────────────────

_discovered_offset: int | None = None
_offset_lock = threading.Lock()


def _scan_dll_for_name(pid: int, name: str) -> int | None:
    base, dll_size = _get_module_info(pid, "soundvoltex.dll")
    if not base or not dll_size:
        return None
    try:
        needle = name.encode("shift_jis")
    except Exception:
        needle = name.encode("utf-8")
    if not needle:
        return None

    if _IS_LINUX:
        data = _read_full_module(pid, base, dll_size)
        if not data:
            return None
        i = data.find(needle)
        while i != -1:
            if i + len(needle) < len(data) and data[i + len(needle)] == 0:
                return i                       # null-terminated occurrence
            i = data.find(needle, i + 1)
        return None

    CHUNK   = 0x80000
    overlap = len(needle) - 1
    k32     = ctypes.windll.kernel32
    handle  = k32.OpenProcess(_PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    try:
        scanned   = 0
        prev_tail = b""
        while scanned < dll_size:
            to_read    = min(CHUNK, dll_size - scanned)
            buf        = ctypes.create_string_buffer(to_read)
            n_read     = ctypes.c_size_t(0)
            k32.ReadProcessMemory(handle, ctypes.c_void_p(base + scanned),
                                  buf, to_read, ctypes.byref(n_read))
            chunk_bytes = prev_tail + bytes(buf)[: n_read.value]
            pos = chunk_bytes.find(needle)
            if pos != -1:
                abs_off = scanned - len(prev_tail) + pos
                term    = _read_memory(pid, base + abs_off + len(needle), 1)
                if term == b"\x00":
                    return abs_off
            prev_tail = chunk_bytes[-overlap:] if overlap > 0 else b""
            scanned  += to_read
    finally:
        k32.CloseHandle(handle)
    return None


def _name_scanner_thread(pid: int, fallback_name: str) -> None:
    global _discovered_offset
    deadline = time.time() + 180
    attempt  = 0
    first    = True
    while time.time() < deadline:
        time.sleep(20 if first else 15)
        first   = False
        attempt += 1
        off = _scan_dll_for_name(pid, fallback_name)
        if off is not None:
            with _offset_lock:
                _discovered_offset = off
            _update_config(player_name_offset=off, player_name=fallback_name)
            print(f"\r[INFO] Player-name offset found "
                  f"(attempt {attempt}): 0x{off:X} – saved.            ")
            return
    print("\r[WARN] Name scan timed out – hint offset remains active.   ")


def read_player_name(pid: int) -> str:
    with _offset_lock:
        offset = _discovered_offset
    if offset is None:
        offset = PLAYER_NAME_OFFSET_HINT
    if not offset:
        return ""
    try:
        base = _get_module_base(pid, "soundvoltex.dll")
        if not base:
            return ""
        raw = _read_cstr(pid, base + offset)
        if not raw:
            return ""
        try:
            name = raw.decode("shift_jis").strip()
        except Exception:
            name = raw.decode("utf-8", errors="replace").strip()
        if not name or name in ("...", "GUEST", "NONAME"):
            return ""
        return name
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# SERVER AUTO-LAUNCH
# ─────────────────────────────────────────────────────────────────────────────

def _start_server(exe_path: str, wait_sec: int = 3) -> "subprocess.Popen | None":
    if not exe_path:
        return None
    exe_path = exe_path.strip('"').strip("'")
    if not os.path.exists(exe_path):
        print(f"[WARN] Server executable not found: {exe_path}")
        return None
    try:
        # CREATE_NEW_PROCESS_GROUP detaches Asphyxia from the launcher's console
        # signal group, so a Ctrl-C in our console can't kill it. Our own
        # shutdown still works (terminate() uses TerminateProcess, not signals).
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        cmd = [exe_path]
        env = None
        if _IS_LINUX and exe_path.lower().endswith(".exe"):
            cmd = ["wine"] + cmd          # Windows Asphyxia build under Wine
            wp = _load_config().get("wine_prefix")
            if wp:
                env = {**os.environ, "WINEPREFIX": os.path.expanduser(wp)}
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=os.path.dirname(exe_path) or None,
            creationflags=flags,
            start_new_session=_IS_LINUX,  # POSIX: own session → isolated from SIGINT
            env=env,
        )
        print(f"[INFO] Server started (PID {proc.pid}): "
              f"{os.path.basename(exe_path)}")
        print(f"[INFO] Waiting {wait_sec} s for server to initialize…")
        time.sleep(wait_sec)
        if proc.poll() is not None:
            print("[WARN] Server process already exited – check the path.")
            return None
        return proc
    except Exception as exc:
        print(f"[WARN] Failed to start server: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP WIZARD
# ─────────────────────────────────────────────────────────────────────────────

def run_setup_wizard() -> tuple[str, str, str, str, int, bool]:
    """
    Returns (fallback_player_name, jacket_base_url, ea_url, server_exe,
             spice_port, debug)
    """
    global _DEBUG
    cfg = _load_config()

    B   = "\033[1m"
    DIM = "\033[90m"
    CY  = "\033[96m"
    GR  = "\033[92m"
    YL  = "\033[93m"
    R   = "\033[0m"
    SEP = DIM + "─" * 60 + R

    def _ask_debug() -> bool:
        global _DEBUG
        dflt = "Y" if _DEBUG else "N"
        ans  = input(f"  Enable debug output? {DIM}(Y/N) [{dflt}]{R}: ") \
                   .strip().upper() or dflt
        _DEBUG = ans == "Y"
        return _DEBUG

    # ── Quick-start: if the config is complete, offer to start immediately ───
    # player_name is OPTIONAL now (auto-detected), and jacket_url only matters
    # for the online/local jacket modes – so don't gate the quick-start on them.
    required = ["asphyxia_url", "jacket_mode", "spice_api_port"]
    if cfg.get("jacket_mode") in ("O", "L"):
        required.append("jacket_url")
    if all(cfg.get(k) not in (None, "") for k in required):
        hb_min = cfg.get("heartbeat_min", 2.0)
        hb_max = cfg.get("heartbeat_max", 15.0)
        print(SEP)
        print(f"  {B}SDVX Rich Presence{R}  {DIM}— saved config found{R}")
        who = cfg.get("player_name") or "auto-detect"
        print(f"    {GR}✓{R} {who}  ·  port {cfg['spice_api_port']}"
              f"  ·  heartbeat {hb_min:g}-{hb_max:g}s")
        print(f"\n    {CY}[Enter]{R} Start now with saved settings")
        print(f"    {CY}[D]{R}     Start now with debug output on")
        print(f"    {CY}[C/O]{R}   Open the config wizard")
        choice = input(f"\n  Choice {DIM}[Enter]{R}: ").strip().upper()
        if choice not in ("C", "O"):
            _DEBUG = (choice == "D")    # D = start in debug, Enter = instant start
            print(SEP + "\n")
            jk = "" if cfg.get("jacket_mode") == "N" \
                 else cfg.get("jacket_url", JACKET_STANDARD)
            return (cfg.get("player_name", ""), jk,
                    cfg.get("asphyxia_url", ""), cfg.get("asphyxia_exe", ""),
                    int(cfg.get("spice_api_port", SPICE_DEFAULT_PORT)), _DEBUG)
        # else fall through to the full wizard below

    print(SEP)
    print(f"  {B}Quick Setup{R}  {DIM}— press Enter to keep the value in [brackets]{R}")
    saved_off = cfg.get("player_name_offset")
    if saved_off:
        print(f"  {DIM}  Player-name offset saved (0x{saved_off:X}) – no re-scan needed.{R}")
    print(SEP + "\n")

    # ── 1. Fallback player name ──────────────────────────────────────────────
    cur_name = cfg.get("player_name", "")
    hint     = DIM + f"[{cur_name or 'empty'}]" + R
    raw_inp  = input(f"  {B}Fallback player name{R} {hint}: ").strip()
    fallback = raw_inp if raw_inp else cur_name

    if saved_off:
        print(f"  {DIM}  ↳ Saved offset will be used directly – no DLL scan.{R}")
    elif fallback:
        print(f"  {DIM}  ↳ DLL scanned for '{fallback}' after card insert.{R}")
    else:
        print(f"  {YL}  ↳ No name set – hint offset only.{R}")

    # ── 2. Jacket image source ───────────────────────────────────────────────
    cur_mode = cfg.get("jacket_mode", "S")
    cur_url  = cfg.get("jacket_url",  JACKET_STANDARD)

    print(f"\n  {B}Jacket image source{R}")
    print(f"    {CY}[S]{R} Standard  →  {DIM}{JACKET_STANDARD}{R}")
    print(f"    {CY}[O]{R} Online    →  custom HTTPS URL")
    print(f"    {CY}[L]{R} Local     →  local server URL  "
          f"{DIM}(e.g. http://localhost:8080){R}")
    print(f"    {CY}[N]{R} None      →  disable jacket images\n")

    jacket      = ""
    jacket_mode = cur_mode
    while True:
        choice = input(
            f"  Choice {DIM}(S/O/L/N) [{cur_mode}]{R}: "
        ).strip().upper() or cur_mode
        if choice == "S":
            jacket = JACKET_STANDARD; jacket_mode = "S"; break
        elif choice in ("O", "L"):
            label   = "Online URL" if choice == "O" else "Local server URL"
            default = f" {DIM}[{cur_url}]{R}" if cur_url else ""
            entered = input(f"    {label}{default}: ").strip().rstrip("/")
            jacket  = entered if entered else cur_url
            jacket_mode = choice
            if jacket:
                break
            print(f"    {DIM}↳ Cannot be empty – try N to disable.{R}")
        elif choice == "N":
            jacket = ""; jacket_mode = "N"; break
        else:
            print(f"  {DIM}↳ Please type S, O, L, or N.{R}")

    # ── 3. EA Service (always asked) ─────────────────────────────────────────
    print(f"\n  {B}EA Service / Network server{R}  "
          f"{DIM}(always asked – path is remembered){R}")
    print(f"    {CY}[A]{R} Asphyxia   →  auto-launch exe, pass URL via -url")
    print(f"    {CY}[C]{R} Custom URL  →  any EA service URL, no auto-launch")
    print(f"    {CY}[K]{R} KONAMI      →  no override  {DIM}(use spicecfg setting){R}")
    print(f"    {CY}[N]{R} Offline     →  no network\n")

    ea_url = server_exe = ""
    while True:
        srv = input(f"  Choice {DIM}(A/C/K/N) [A]{R}: ").strip().upper() or "A"
        if srv == "A":
            saved_exe = cfg.get("asphyxia_exe", "")
            hint_exe  = f" {DIM}[{saved_exe}]{R}" if saved_exe else ""
            raw_exe   = input(
                f"    Path to Asphyxia executable{hint_exe}: "
            ).strip().strip('"').strip("'")
            server_exe = raw_exe if raw_exe else saved_exe

            saved_au = cfg.get("asphyxia_url", "http://localhost:8083")
            hint_u   = f" {DIM}[{saved_au}]{R}"
            raw_u    = input(f"    Asphyxia URL{hint_u}: ").strip().rstrip("/")
            ea_url   = (raw_u if raw_u else saved_au).rstrip("/") + "/"
            _update_config(asphyxia_exe=server_exe, asphyxia_url=ea_url)
            break
        elif srv == "C":
            saved_c = cfg.get("custom_ea_url", "")
            hint_c  = f" {DIM}[{saved_c}]{R}" if saved_c else ""
            raw_c   = input(f"    Custom EA URL{hint_c}: ").strip().rstrip("/")
            ea_url  = (raw_c if raw_c else saved_c).rstrip("/") + "/"
            # Custom servers require a PCBID, passed to spice2x via -p. Remember
            # it per server URL so it is only asked the first time.
            pcbid_map   = dict(cfg.get("pcbid_by_url", {}))
            saved_pcbid = pcbid_map.get(ea_url, "")
            hint_p = f" {DIM}[{saved_pcbid}]{R}" if saved_pcbid else ""
            raw_p  = input(f"    PCBID for this server "
                           f"{DIM}(spice2x -p){R}{hint_p}: ").strip()
            pcbid  = raw_p if raw_p else saved_pcbid
            if pcbid:
                pcbid_map[ea_url] = pcbid
                print(f"  {DIM}  ↳ PCBID stored for {ea_url}{R}")
            else:
                print(f"  {YL}  ↳ No PCBID set – a custom server will likely "
                      f"reject the connection.{R}")
            # Ryunet card id (NFC cid) for this server → VolForce via the API.
            cid_map     = dict(cfg.get("ryunet_cid_by_url", {}))
            saved_cid   = cid_map.get(ea_url, "") or cfg.get("ryunet_cid", "")
            hint_cid    = f" {DIM}[{saved_cid}]{R}" if saved_cid else ""
            raw_cid     = input(f"    Ryunet card id "
                                f"{DIM}(NFC cid, for VolForce){R}{hint_cid}: ").strip()
            cid_val     = raw_cid if raw_cid else saved_cid
            if cid_val:
                cid_map[ea_url] = cid_val
                print(f"  {DIM}  ↳ Ryunet card id stored for {ea_url}{R}")
            _update_config(custom_ea_url=ea_url, pcbid_by_url=pcbid_map,
                           ryunet_cid_by_url=cid_map)
            break
        elif srv in ("K", "N"):
            ea_url = ""; break
        else:
            print(f"  {DIM}↳ Please type A, C, K, or N.{R}")

    # ── 4. SpiceAPI port ─────────────────────────────────────────────────────
    print(f"\n  {B}SpiceAPI port{R}  "
          f"{DIM}reads difficulty / gauge from game memory in real time{R}")
    cur_port = cfg.get("spice_api_port", SPICE_DEFAULT_PORT)
    raw_port = input(
        f"  TCP port {DIM}[{cur_port}]{R}: "
    ).strip()
    try:
        spice_port = int(raw_port) if raw_port else int(cur_port)
    except ValueError:
        spice_port = SPICE_DEFAULT_PORT
        print(f"  {YL}  ↳ Not a number – falling back to {SPICE_DEFAULT_PORT}.{R}")
    _update_config(spice_api_port=spice_port)

    # ── 5. Heartbeat timing ───────────────────────────────────────────────────
    print(f"\n  {B}Heartbeat{R}  "
          f"{DIM}min = fastest update spacing, max = re-assert interval (s){R}")
    cur_min = cfg.get("heartbeat_min", 2.0)
    cur_max = cfg.get("heartbeat_max", 15.0)
    def _ask_float(label, default):
        raw = input(f"  {label} {DIM}[{default:g}]{R}: ").strip()
        try:
            return float(raw) if raw else float(default)
        except ValueError:
            print(f"  {YL}  ↳ Not a number – keeping {default:g}.{R}")
            return float(default)
    hb_min = _ask_float("Min interval", cur_min)
    hb_max = _ask_float("Max interval", cur_max)
    if hb_max < hb_min:
        hb_max = hb_min
    _update_config(heartbeat_min=hb_min, heartbeat_max=hb_max)

    # ── 6. Render options ─────────────────────────────────────────────────────
    print(f"\n  {B}What should be shown on Discord?{R}")
    def _ask_yn(label: str, key: str, default: bool) -> bool:
        d = "Y" if cfg.get(key, default) else "N"
        ans = input(f"  {label}? {DIM}(Y/N) [{d}]{R}: ").strip().upper() or d
        return ans == "Y"
    show_vf   = _ask_yn("Show VolForce (VF: xx.yyy)",          "show_vf",   True)
    show_diff = _ask_yn("Show difficulty + level (EXH 18.9)",  "show_diff", True)
    _update_config(show_vf=show_vf, show_diff=show_diff)

    # ── 7. Debug mode ───────────────────────────────────────────────────────────
    print(f"\n  {B}Debug mode{R}  "
          f"{DIM}prints matched log lines & Discord payload to console{R}")
    debug = _ask_debug()

    # Persist non-offset choices
    _update_config(player_name=fallback, jacket_mode=jacket_mode,
                   jacket_url=jacket)

    # ── Summary ───────────────────────────────────────────────────────────────
    ea_display  = ea_url or "(no override)"
    srv_display = os.path.basename(server_exe) if server_exe else "(none)"
    dbg_display = f"{YL}ON{R}" if debug else f"{DIM}OFF{R}"
    print(f"\n  {GR}✓{R}  Name    : {B}{fallback or '(none)'}{R}")
    print(f"  {GR}✓{R}  Jackets : {B}{jacket or 'disabled'}{R}")
    print(f"  {GR}✓{R}  EA URL  : {B}{ea_display}{R}")
    print(f"  {GR}✓{R}  Server  : {B}{srv_display}{R}")
    print(f"  {GR}✓{R}  API port: {B}{spice_port}{R}")
    print(f"  {GR}✓{R}  Debug   : {dbg_display}\n")
    print(SEP + "\n")

    return fallback, jacket, ea_url, server_exe, spice_port, debug


# ─────────────────────────────────────────────────────────────────────────────
# RATE-LIMITED RPC WRAPPER
# ─────────────────────────────────────────────────────────────────────────────

class RateLimitedRPC:
    def __init__(self, client_id: str,
                 hb_min: float = 2.0, hb_max: float = 15.0) -> None:
        self._rpc      = Presence(client_id)
        self._snapshot = None             # () -> dict | None  (full package)
        self._min      = max(0.5, float(hb_min))         # min secs between sends
        self._max      = max(self._min, float(hb_max))   # heartbeat re-assert
        self._sent: dict | None = None
        self._last_t   = 0.0
        threading.Thread(target=self._worker, daemon=True).start()

    def connect(self) -> None:
        self._rpc.connect()

    def set_snapshot(self, fn) -> None:
        """fn() returns the COMPLETE presence package for the current state."""
        self._snapshot = fn

    def _worker(self) -> None:
        # The pusher polls the CURRENT state every tick and sends the whole
        # package whenever it differs from what was last sent, so a stale
        # snapshot can never get stuck. It also re-asserts every _max seconds
        # (heartbeat) to recover from updates Discord silently drops.
        while True:
            time.sleep(0.25)
            fn = self._snapshot
            if fn is None:
                continue
            try:
                pkg = fn()
            except Exception:
                continue
            if not pkg:
                continue
            now     = time.monotonic()
            changed = pkg != self._sent
            due_hb  = (now - self._last_t) >= self._max
            if not (changed or due_hb):
                continue
            if now - self._last_t < self._min:
                continue                      # respect the minimum spacing
            try:
                self._rpc.update(**pkg)
                self._sent   = pkg
                self._last_t = now
                if _DEBUG and changed:
                    _dbg("RPC ", f"-> Discord: {pkg.get('state')!r} | "
                                 f"{pkg.get('details')!r}")
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# SONG DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_level(raw: str) -> str:
    try:
        v = float(raw)
    except ValueError:
        return raw
    # Only scale if stored as a plain integer in ×10 format ("189" → 18.9).
    # Float strings already have the correct value ("18.9" → 18.9 direct).
    if "." not in raw and v > 20:
        v /= 10.0
    return f"{v:.1f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def _find_music_db() -> str | None:
    """Locate the music_db XML across Windows/Linux layouts. Collects every
    candidate (known relative paths + a depth-limited recursive scan) and picks
    the LARGEST one, since partial/test dbs (e.g. 44 songs) are tiny compared to
    the full db (~2000+ songs). An explicit config path always wins."""
    cfg = _load_config()
    cfg_path = cfg.get("music_db_path")
    if cfg_path and os.path.exists(cfg_path):
        return cfg_path

    candidates: set[str] = set()
    rels = [
        "data_mods/omnimix/others/music_db.merged.xml",
        "data_mods/omnimix/others/music_db.xml",
        "data/others/music_db.xml",
        "others/music_db.xml",
        "music_db.merged.xml",
        "music_db.xml",
    ]
    # anchor roots: CWD, parent, and the configured game folder (if any)
    roots = [".", ".."]
    gexe = cfg.get("game_exe_path")
    game_dir = os.path.dirname(gexe) if gexe else ""
    if game_dir:
        roots.append(game_dir)
        for rel in rels:
            p = os.path.join(game_dir, rel)
            if os.path.exists(p):
                candidates.add(os.path.abspath(p))
    for rel in rels:
        if os.path.exists(rel):
            candidates.add(os.path.abspath(rel))

    for root in roots:
        if not os.path.isdir(root):
            continue
        base_depth = os.path.abspath(root).count(os.sep)
        for dirpath, dirs, files in os.walk(root):
            if os.path.abspath(dirpath).count(os.sep) - base_depth > 6:
                dirs[:] = []
                continue
            for fn in files:
                low = fn.lower()
                if low.startswith("music_db") and low.endswith(".xml"):
                    candidates.add(os.path.abspath(os.path.join(dirpath, fn)))

    if not candidates:
        return None
    # largest file = most complete db (partial/omnimix dbs are much smaller)
    return max(candidates, key=lambda p: (os.path.getsize(p) if os.path.exists(p) else 0))


def load_song_map() -> tuple[dict, dict]:
    xml_path = _find_music_db()
    if not xml_path:
        return {}, {}

    content = ""
    for enc in ("cp932", "shift_jis", "utf-8"):
        try:
            with open(xml_path, "r", encoding=enc, errors="ignore") as fh:
                content = fh.read()
            if "<music" in content:
                break
        except Exception:
            continue

    title_map: dict[int, str]  = {}
    diff_map:  dict[int, dict] = {}

    rx_block  = re.compile(r'<music id="(\d+)">(.*?)</music>', re.DOTALL)
    rx_title  = re.compile(r'<title_name>(.*?)</title_name>')
    rx_diff_b = re.compile(
        r'<(novice|advanced|exhaust|'
        r'infinite|gravity|heaven|vivid|exceed|'
        r'maximum|ultimate)>(.*?)</\1>',
        re.DOTALL
    )
    # Nabla may use <difnum>, <rating>, or <level> for the numeric chart level.
    # Cover all three so the parser works regardless of music_db variant.
    rx_difnum = re.compile(
        r'<(?:difnum|rating|level)\b[^>]*>(\d+(?:\.\d+)?)</(?:difnum|rating|level)>',
        re.IGNORECASE
    )

    # --dump-db: print raw XML of first 5 songs so you can see the actual format
    if "--dump-db" in sys.argv:
        print(f"\n\033[93m[DUMP-DB] First 5 <music> entries from {xml_path}:\033[0m\n")
        for i, mb in enumerate(rx_block.finditer(content)):
            if i >= 5:
                break
            raw = mb.group(0)
            print(f"--- Entry {i + 1} (id={mb.group(1)}) ---")
            print(raw[:700] + (" …(truncated)" if len(raw) > 700 else ""))
            print()
        sys.exit(0)

    _TAG_SHORT = {
        "novice": "NOV", "advanced": "ADV", "exhaust":  "EXH",
        "maximum": "MXM", "ultimate": "ULT",
        **{k: v for k, v in _VARIANT_NAMES.items()},
    }
    _FIXED = {"NOV", "ADV", "EXH", "MXM", "ULT"}

    for mb in rx_block.finditer(content):
        try:
            sid  = int(mb.group(1))
            body = mb.group(2)
        except Exception:
            continue
        t = rx_title.search(body)
        if t:
            title_map[sid] = t.group(1).strip()
        diffs: dict = {}
        variant_info: tuple | None = None
        for md in rx_diff_b.finditer(body):
            tag   = md.group(1)
            dn    = rx_difnum.search(md.group(2))
            if not dn:
                continue
            lvl   = _fmt_level(dn.group(1))
            short = _TAG_SHORT.get(tag)
            if short in _FIXED:
                diffs[short] = lvl
            elif tag in _VARIANT_NAMES:
                variant_info = (_VARIANT_NAMES[tag], lvl)
        diffs["variant"] = variant_info
        diff_map[sid] = diffs

    return title_map, diff_map


# ─────────────────────────────────────────────────────────────────────────────
# BANNER
# ─────────────────────────────────────────────────────────────────────────────

def print_logo() -> None:
    os.system("cls" if os.name == "nt" else "clear")
    C = "\033[92m"; M = "\033[97m"; P = "\033[38;2;88;101;242m"; R = "\033[0m"
    print(f"   {C}____                  {M}__                      {R}")
    print(f"  {C}/ __/__  __ _____  {M}___/ /                      {R}")
    print(f" {C}_\\ \\/ _ \\/ // / _ \\{M}/ _  /                        {R}")
    print(f"{C}/___/\\___/\\_,_/_//_/{M}\\_,_/                        {R}")
    print(f"           {C}_    __{M}      ____                     {R}")
    print(f"          {C}| | / /__{M}  / / /______ __              {R}")
    print(f"          {C}| |/ / _ \\{M}/ / __/ -_) \\ /              {R}")
    print(f"          {C}|___/\\___/{M}_/\\__/\\__/_\\_\\              {R}")
    print(f"{P}   ___  _                          __  ___  _____ {R}")
    print(f"{P}  / _ \\(_)__ _______  _______/ / / _ \\/ _ \\/ ___/ {R}")
    print(f"{P} / // / (_-</ __/ _ \\/ __/ _  / / , _/ ___/ /__   {R}")
    print(f"{P}/____/_/___/\\__/\\___/_/  \\_,_/ /_/|_/_/   \\___/   {R}")
    print(f"\n              {P}[ Active ]{R}\n")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _safe(text: str) -> str:
    return text if len(text) >= 2 else text + " "

def _large_image(state: str, jacket_path: str, jacket_base: str) -> str:
    if state in ("Playing", "Results") and jacket_path and jacket_base:
        return f"{jacket_base}/{jacket_path}"
    return IMG_MENU

def _build_details(play_mode: str, active_event: str,
                   diff: str, level: str) -> str:
    mode  = active_event or play_mode or ""
    chart = f"{diff} {level}".strip() if (diff or level) else ""
    if mode and chart:
        return f"{mode}  ·  {chart}"
    return mode or chart or "SDVX"

def _build_small_text(player: str) -> str:
    return player.strip() or "SDVX"


# ─────────────────────────────────────────────────────────────────────────────
# DIFFICULTY LOG DETECTION
# ─────────────────────────────────────────────────────────────────────────────

_RX_CHART_FILE = re.compile(
    r'music/(\d+)_[^/]+/\1_(\d)\.(?:ksh|vox|2dx|xml|bin|gz)',
    re.IGNORECASE
)
_RX_DIFF_KW = re.compile(
    r'\b(NOV(?:ICE)?|ADV(?:ANCED)?|EXH(?:AUST)?|'
    r'INF(?:INITE)?|GRV|HVN|VVD|XCD|MXM|ULT(?:IMATE)?)\b',
    re.IGNORECASE
)
_DIFF_KW_MAP = {
    "NOVICE": "NOV", "NOV": "NOV", "ADVANCED": "ADV", "ADV": "ADV",
    "EXHAUST": "EXH", "EXH": "EXH", "INFINITE": "INF", "INF": "INF",
    "GRV": "GRV", "HVN": "HVN", "VVD": "VVD", "XCD": "XCD", "MXM": "MXM",
    "ULTIMATE": "ULT", "ULT": "ULT",
}


def _detect_diff(line: str, variant_name: str) -> str | None:
    m = _RX_CHART_FILE.search(line)
    if m:
        idx = int(m.group(2)) if m.group(2).isdigit() else -1
        if idx in _DIFF_BY_INDEX:
            return _DIFF_BY_INDEX[idx]
        if idx == 3:
            return variant_name or "INF"
    m = _RX_DIFF_KW.search(line)
    if m:
        return _DIFF_KW_MAP.get(m.group(1).upper())
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SPICEAPI CLIENT  (TCP + JSON + stateful RC4)
# ─────────────────────────────────────────────────────────────────────────────

class _RC4:
    """Stateful RC4 stream cipher.

    crypt() advances the internal keystream, so a *single* instance encrypts
    the request and then decrypts the response on the same connection – the
    keystream flows continuously across both directions, exactly as the
    spice2x server expects. Encryption and decryption are the same XOR op.
    """

    def __init__(self, key: bytes) -> None:
        S = list(range(256))
        j = 0
        klen = len(key)
        for i in range(256):
            j = (j + S[i] + key[i % klen]) & 0xFF
            S[i], S[j] = S[j], S[i]
        self._S = S
        self._i = 0
        self._j = 0

    def crypt(self, data: bytes) -> bytes:
        S, i, j = self._S, self._i, self._j
        out = bytearray(len(data))
        for k in range(len(data)):
            i = (i + 1) & 0xFF
            j = (j + S[i]) & 0xFF
            S[i], S[j] = S[j], S[i]
            out[k] = data[k] ^ S[(S[i] + S[j]) & 0xFF]
        self._i, self._j = i, j
        return bytes(out)


class SpiceAPI:
    """Minimal spice2x API client.

    One TCP connection, one stateful RC4 cipher. On any socket/decrypt error
    the connection is dropped and transparently re-established (a fresh
    connection resets the keystream on both ends, so we never desync).
    """

    def __init__(self, host: str, port: int, password: str,
                 timeout: float = 4.0) -> None:
        self.host     = host
        self.port     = port
        self.password = password
        self.timeout  = timeout
        self._sock: socket.socket | None = None
        self._cipher: _RC4 | None        = None
        self._raw    = b""               # undecrypted leftover bytes
        self._id     = 0
        self._lock   = threading.Lock()  # one in-flight request at a time

    # ── connection lifecycle ────────────────────────────────────────────────
    def connect(self) -> None:
        self.close()
        s = socket.create_connection((self.host, self.port), self.timeout)
        s.settimeout(self.timeout)
        self._sock   = s
        self._raw    = b""
        self._cipher = _RC4(self.password.encode("utf-8")) if self.password else None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock   = None
        self._cipher = None
        self._raw    = b""

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # ── low-level framing ───────────────────────────────────────────────────
    def _send(self, module: str, function: str, params: list) -> int:
        self._id += 1
        payload = {"id": self._id, "module": module,
                   "function": function, "params": params}
        raw = (json.dumps(payload, separators=(",", ":")) + "\x00").encode("utf-8")
        if self._cipher:
            raw = self._cipher.crypt(raw)
        self._sock.sendall(raw)
        return self._id

    def _recv(self) -> dict:
        # Decrypt incrementally until the *plaintext* NUL terminator appears.
        plain = bytearray()
        while True:
            while self._raw:
                b = self._raw[:1]
                self._raw = self._raw[1:]
                if self._cipher:
                    b = self._cipher.crypt(b)
                if b == b"\x00":
                    return json.loads(plain.decode("utf-8", errors="replace"))
                plain.extend(b)
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("SpiceAPI connection closed by server")
            self._raw += chunk

    def request(self, module: str, function: str, params: list) -> dict:
        """Send one request, return the parsed response dict.

        Auto-reconnects once on failure. Raises on a second failure so the
        caller can back off.
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self.connect()
                    sent_id = self._send(module, function, params)
                    resp    = self._recv()
                    if resp.get("id") != sent_id:
                        raise ConnectionError(
                            f"id mismatch (sent {sent_id}, got {resp.get('id')})")
                    return resp
                except Exception:
                    self.close()
                    if attempt == 2:
                        raise
            return {}

    # ── high-level helpers ──────────────────────────────────────────────────
    def info_launcher(self) -> dict | None:
        """Health check – returns launcher info if the API is up & password OK."""
        try:
            resp = self.request("info", "launcher", [])
            if resp.get("errors"):
                return None
            data = resp.get("data")
            return data if isinstance(data, dict) else {"data": data}
        except Exception:
            return None

    def memory_read(self, dll: str, offset: int, size: int) -> bytes | None:
        """memory.read → raw bytes (None on error). Requires a password."""
        try:
            resp = self.request("memory", "read", [dll, int(offset), int(size)])
        except Exception:
            return None
        if resp.get("errors"):
            return None
        hexstr = _extract_hex(resp.get("data"))
        if hexstr is None:
            return None
        try:
            return bytes.fromhex(hexstr)
        except ValueError:
            return None


def _extract_hex(data) -> str | None:
    """Find the first even-length hex string anywhere in a memory.read result.

    spice2x has shipped the read payload as ["aabb.."], [{"data":"aabb.."}],
    {"data":"aabb.."} across versions – walk all of them defensively.
    """
    def walk(node):
        if isinstance(node, str):
            s = node.strip()
            if len(s) >= 2 and len(s) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", s):
                return s
            return None
        if isinstance(node, dict):
            for v in node.values():
                r = walk(v)
                if r:
                    return r
        if isinstance(node, (list, tuple)):
            for v in node:
                r = walk(v)
                if r:
                    return r
        return None
    return walk(data)


# ─────────────────────────────────────────────────────────────────────────────
# SPICEAPI DIFF / GAUGE  – SHARED STATE + DISCOVERY + POLLING
# ─────────────────────────────────────────────────────────────────────────────

class _SpiceState:
    """Thread-safe bridge between the log loop (main thread) and the Spice
    thread. The log loop publishes the current song id; the Spice thread
    publishes the difficulty / gauge it reads from memory."""

    def __init__(self) -> None:
        self.lock     = threading.Lock()
        self.song_id  = 0     # written by main loop
        self.playing  = False # written by main loop (only poll while relevant)
        self.diff     = ""    # written by spice thread
        self.diff_idx = -1
        self.alive    = False # spice thread is connected & polling

    def set_song(self, sid: int, playing: bool) -> None:
        with self.lock:
            self.song_id = sid
            self.playing = playing

    def get_song(self) -> tuple[int, bool]:
        with self.lock:
            return self.song_id, self.playing


def _spice_window(api: SpiceAPI, center: int, half: int) -> tuple[int, bytes] | None:
    """Read `2*half` bytes centred on `center`. Returns (start_offset, data)."""
    start = max(0, center - half)
    data  = api.memory_read(SPICE_DLL, start, half * 2)
    if data is None or len(data) < half * 2:
        return None
    return start, data


def _spice_thread(api: SpiceAPI, state: _SpiceState, name_offset: int,
                  on_update, game_pid: int = 0,
                  force_discovery: bool = False) -> None:
    """Connect to SpiceAPI, (auto-)discover the diff offset, then poll
    diff/gauge every 500 ms and call on_update() whenever they change.

    Discovery heuristic (per project handover):
      * read a 256-byte window around the player-name offset each poll
      * a byte that is STABLE within one song but CHANGES between songs and
        holds a value in 0..5 behaves like a difficulty index
      * a candidate offset that wins 3 such song-to-song transitions is
        confirmed and persisted
    The gauge byte changes far less often (players keep one gauge), so gauge
    is read from whatever offset the user confirms via --spice-dump / config;
    it is NOT auto-discovered.
    """
    HALF      = 128
    cfg       = _load_config()
    diff_off  = None if force_discovery else cfg.get("spice_diff_offset")
    # byte→slot encoding learned by --find-diff (empty → assume standard 0-5)
    diff_map  = {int(k): int(v) for k, v in cfg.get("spice_diff_map", {}).items()}

    # ── 1. wait for the API to come up (game just launched) ──────────────────
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            api.connect()
            if api.info_launcher() is not None:
                break
        except Exception:
            pass
        api.close()
        time.sleep(2.0)
    else:
        print("\r[WARN] SpiceAPI never became reachable – diff/gauge disabled.")
        return

    with state.lock:
        state.alive = True
    _dbg("SPCE", f"connected to {SPICE_HOST}:{api.port} – memory polling live")
    print(f"\r[INFO] SpiceAPI connected (port {api.port}).                     ")

    # discovery bookkeeping
    votes: dict[int, int]            = {}
    cur_song                          = 0
    song_stable: dict[int, int] | None = None  # pos → value, stable this song
    prev_song_stable: dict[int, int]  = {}
    last_diff = ""
    dll_base  = 0
    last_raw  = -1

    def _resolve_diff(idx: int) -> str:
        if diff_map:
            if idx not in diff_map:
                return ""          # encoding known, this value never mapped
            slot = diff_map[idx]
        else:
            slot = idx             # no map → assume standard 0-5 encoding
        if slot in _SPICE_DIFF_BY_INDEX:
            return _SPICE_DIFF_BY_INDEX[slot]
        if slot == 3:
            return "INF"   # variant – caller maps to the song's real variant
        return ""

    while True:
        time.sleep(0.5)
        sid, chart_active = state.get_song()

        # ── confirmed offset → read it, but ONLY while a chart is actually
        #    loaded. The difficulty is not written during song-select, so any
        #    value read there is stale and must be ignored (diff = unknown). ───
        if diff_off is not None:
            if not chart_active:
                if last_diff != "":          # leaving play → clear the display
                    last_diff = ""
                    last_raw  = -1
                    with state.lock:
                        state.diff_idx = -1
                        state.diff     = ""
                    on_update()
                continue
            # Read the SAME way the player name & --find-diff do: virtual
            # address = module base + offset (NOT SpiceAPI file offsets).
            if dll_base == 0:
                dll_base = _get_module_base(game_pid, SPICE_DLL)
                if dll_base == 0:
                    continue
            raw = _read_memory(game_pid, dll_base + diff_off, 1)
            if not raw:
                if last_raw != -2:
                    last_raw = -2
                    _dbg("SPCE", f"read FAILED @ base+0x{diff_off:X} "
                                 f"(pid {game_pid}) – admin? offset?")
                dll_base = 0          # force base re-resolve next tick
                continue
            idx = raw[0]
            if idx != last_raw:       # diagnostic: show the raw byte value
                last_raw = idx
                _dbg("SPCE", f"raw byte @0x{diff_off:X} = {idx} (0x{idx:02X})")
            diff = _resolve_diff(idx)
            if diff != last_diff:
                last_diff = diff
                with state.lock:
                    state.diff_idx = idx
                    state.diff     = diff
                _dbg("SPCE", f"diff_idx={idx} → {diff!r}")
                on_update()
            continue

        # ── (legacy) auto-discovery window around the player name ────────────
        # Kept as a fallback, but the reliable path is the interactive
        # --find-diff scan; this ±128-byte window rarely covers the value.
        with _offset_lock:
            center = _discovered_offset or name_offset
        win = _spice_window(api, center, HALF)
        if win is None:
            continue
        start, data = win
        if not chart_active and sid == 0:
            continue

        if sid != cur_song:
            # song changed – compare stable bytes of prev vs (soon) new song
            if song_stable and prev_song_stable:
                for pos, val in song_stable.items():
                    if pos in prev_song_stable and prev_song_stable[pos] != val \
                            and 0 <= val <= 5 and 0 <= prev_song_stable[pos] <= 5:
                        votes[pos] = votes.get(pos, 0) + 1
                        _dbg("SPCE",
                             f"discovery: offset 0x{start + pos:X} changed "
                             f"{prev_song_stable[pos]}→{val} "
                             f"(votes={votes[pos]})")
                # confirm a winner
                winner = next((p for p, v in votes.items() if v >= 3), None)
                if winner is not None:
                    diff_off = start + winner
                    _update_config(spice_diff_offset=diff_off)
                    print(f"\r[INFO] Diff offset discovered: 0x{diff_off:X} "
                          f"– saved.                       ")
                    _dbg("SPCE", f"CONFIRMED diff offset 0x{diff_off:X}")
            prev_song_stable = song_stable or prev_song_stable
            song_stable      = {i: data[i] for i in range(len(data))}
            cur_song         = sid
        else:
            # same song – keep only bytes that stayed constant
            if song_stable is not None:
                song_stable = {i: v for i, v in song_stable.items()
                               if i < len(data) and data[i] == v}


# ─────────────────────────────────────────────────────────────────────────────
# SPICE-DUMP  (manual reverse-engineering helper:  python sdvx_rpc.py --spice-dump)
# ─────────────────────────────────────────────────────────────────────────────

def _spice_dump_mode() -> None:
    """Connect to a *running* spiced game and live-print a memory window so you
    can eyeball which byte changes when you switch difficulty / gauge in-game.

    Requires the game to already be running with -api/-apipass. Pass the port
    and password the game was started with:
        python sdvx_rpc.py --spice-dump --port 1337 --pass <password>
        python sdvx_rpc.py --spice-dump --offset 0x11F0291 --span 256
    """
    def _arg(flag, default=None):
        if flag in sys.argv:
            i = sys.argv.index(flag)
            if i + 1 < len(sys.argv):
                return sys.argv[i + 1]
        return default

    cfg     = _load_config()
    port    = int(_arg("--port", cfg.get("spice_api_port", SPICE_DEFAULT_PORT)))
    pw      = _arg("--pass", "")
    center  = int(_arg("--offset", str(cfg.get("player_name_offset")
                                        or PLAYER_NAME_OFFSET_HINT)), 0)
    span    = int(_arg("--span", "256"))

    if not pw:
        print("[ERROR] --spice-dump needs the session password the game was "
              "started with:  --pass <password>")
        return

    api = SpiceAPI(SPICE_HOST, port, pw)
    try:
        api.connect()
    except Exception as exc:
        print(f"[ERROR] Could not connect to SpiceAPI on port {port}: {exc}")
        return
    if api.info_launcher() is None:
        print("[ERROR] Connected but info.launcher failed – wrong password?")
        return

    start = max(0, center - span // 2)
    print(f"[INFO] Dumping {span} bytes @ 0x{start:X} every 0.5 s. "
          f"Switch diff/gauge in-game and watch the columns. Ctrl-C to stop.\n")
    prev: bytes | None = None
    try:
        while True:
            data = api.memory_read(SPICE_DLL, start, span)
            if data:
                line_parts = []
                for i, b in enumerate(data):
                    changed = prev is not None and i < len(prev) and prev[i] != b
                    cell = f"{b:02X}"
                    line_parts.append(f"\033[93m{cell}\033[0m" if changed else cell)
                # 32 bytes per row, prefixed with the absolute offset
                for row in range(0, len(line_parts), 32):
                    off = start + row
                    print(f"0x{off:08X}  " + " ".join(line_parts[row:row + 32]))
                print("-" * 60)
                prev = data
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[INFO] Stopped.")
    finally:
        api.close()


# ─────────────────────────────────────────────────────────────────────────────
# OFFSET FINDER  (differential scan:  python sdvx_rpc.py --find-diff)
# ─────────────────────────────────────────────────────────────────────────────

_FIND_DIFF_INDEX = {
    "NOV": 0, "ADV": 1, "EXH": 2,
    "INF": 3, "GRV": 3, "HVN": 3, "VVD": 3, "XCD": 3,   # all variants = index 3
    "MXM": 4, "ULT": 5,
}


def _find_value_mode(target: str | None = None, pid: int | None = None) -> None:
    """Encoding-agnostic differential scan (à la Cheat Engine "unknown value")
    to locate the difficulty byte inside soundvoltex.dll.

    Called standalone (pid derived from argv) or inline from the first-run
    setup (an already-known game pid passed in).
    """
    target = "diff"
    word, valid = "difficulty", set(_FIND_DIFF_INDEX)
    CAP = 31   # plausible ceiling for a small index/enum byte

    # identity(label): the canonical difficulty slot (0-5) the label maps to.
    def ident(lbl: str):
        return _FIND_DIFF_INDEX[lbl]

    if pid is None:
        pid = _find_pid_by_name(GAME_EXECUTABLE)
    if not pid:
        print(f"[ERROR] {GAME_EXECUTABLE} is not running. Start the game first.")
        return
    base, size = _get_module_info(pid, SPICE_DLL)
    if not base or not size:
        print(f"[ERROR] {SPICE_DLL} not loaded yet (insert a card / enter "
              f"music-select first).  PID was {pid}.")
        return

    print(f"[INFO] Differential scan of {SPICE_DLL} in PID {pid} "
          f"({size/1_048_576:.1f} MB) for the {word} byte.")
    print( "[INFO] Run AS ADMIN.  Workflow:")
    print(f"       • Round 1: set a {word}, type its label, and DON'T touch")
    print( "         anything – two readings are taken ~1.5 s apart.")
    print(f"       • Then switch the {word} in-game and type the new label.")
    print( "       • Revisit earlier values too. 'done' to finish, 'q' to quit.")
    print(f"       Valid labels: {', '.join(sorted(valid))}\n")

    phase   = "baseline"                   # baseline → tracking
    cands: dict[int, dict] = {}            # offset -> {identity: byte_value}
    snapA = snapB = None                   # baseline stable reference
    base_id = None
    rounds  = 0

    while True:
        try:
            lbl = input(f"  current {word} in-game: ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if lbl in ("Q", "QUIT"):
            return
        if lbl in ("DONE", ""):
            break
        if lbl not in valid:
            print(f"    ↳ unknown label – pick one of: {', '.join(sorted(valid))}")
            continue
        rounds += 1

        if phase == "baseline":
            # ── two readings while the value is held → stability reference ──
            snapA = _read_full_module(pid, base, size)
            if not snapA:
                print("    ↳ memory read failed (Windows: run as admin; Linux: ptrace permission).")
                rounds -= 1; continue
            print("    capturing baseline… (do not change the difficulty yet)")
            time.sleep(1.5)
            snapB = _read_full_module(pid, base, size)
            if not snapB:
                print("    ↳ memory read failed (Windows: run as admin; Linux: ptrace permission).")
                rounds -= 1; continue
            base_id = ident(lbl)
            phase   = "tracking"
            print(f"    baseline captured for {lbl}. Now switch the {word} "
                  f"in-game and type the new label.")
            continue

        # ── tracking: every later round ──────────────────────────────────────
        buf = _read_full_module(pid, base, size)
        if not buf:
            print("    ↳ memory read failed (Windows: run as admin; Linux: ptrace permission).")
            rounds -= 1; continue
        this_id = ident(lbl)

        if not cands:   # first transition → build the candidate set
            if this_id == base_id:
                print(f"    ↳ that's still the baseline value – switch to a "
                      f"DIFFERENT {word} first.")
                rounds -= 1; continue
            # fast: only inspect bytes that differ from the stable reference
            xor = int.from_bytes(snapB, "little") ^ int.from_bytes(buf, "little")
            xb  = xor.to_bytes(size, "little")
            for mo in re.finditer(rb"[^\x00]", xb):
                i = mo.start()
                a, b, c = snapA[i], snapB[i], buf[i]
                if a == b and a <= CAP and c <= CAP:
                    cands[i] = {base_id: b, this_id: c}
            snapA = snapB = None   # free ~37 MB
        else:           # intersect / extend the learned mapping
            keep = {}
            for i, rec in cands.items():
                c = buf[i]
                if c > CAP:
                    continue
                if this_id in rec:
                    if rec[this_id] != c:
                        continue
                else:
                    if c in rec.values():     # not injective → reject
                        continue
                    rec = {**rec, this_id: c}
                keep[i] = rec
            cands = keep

        n = len(cands)
        print(f"    round {rounds}: {n} candidate offset(s) remain")
        if 0 < n <= 16:
            for i in sorted(cands):
                print(f"      0x{i:X}   map={cands[i]}")
        if n == 0:
            print(f"\n[RESULT] No byte in {SPICE_DLL} tracks the {word} as a")
            print( "         stable small value on this screen. It may live behind")
            print( "         a pointer / on the heap. Re-run --find-diff and switch")
            print( "         between more distinct difficulties, or set")
            print( "         \"spice_diff_offset\" manually in the config if known.")
            return

    if not cands:
        print("[INFO] Need at least one switch between two different values.")
        return

    # winner = the offset with the richest consistent mapping
    off = max(cands, key=lambda i: len(cands[i]))
    learned = cands[off]
    if len(cands) > 1:
        print(f"\n[INFO] {len(cands)} candidates remain; using 0x{off:X}. "
              "Re-run with more distinct values to disambiguate.")

    # learned maps byte_value → slot (int). Persist only when it's NOT the
    # trivial identity map (0→0,1→1,…), which the live reader assumes by default.
    value_to_slot = {str(v): k for k, v in learned.items()}
    is_identity = all(int(k) == v for k, v in value_to_slot.items())
    if is_identity:
        _update_config(spice_diff_offset=off)
        print(f"\n[RESULT] Difficulty offset = 0x{off:X}  → saved "
              f"(spice_diff_offset).")
    else:
        _update_config(spice_diff_offset=off, spice_diff_map=value_to_slot)
        print(f"\n[RESULT] Difficulty offset = 0x{off:X}  → saved "
              f"(spice_diff_offset).")
        print(f"         Learned byte→slot encoding: {value_to_slot} "
              f"(spice_diff_map).")
    print("         Restart the launcher – diff is now read live.")


def _first_run_offset_setup(game_pid: int) -> None:
    """On first run (no diff offset saved yet) offer to find it now – either
    guided (the differential scanner, inline) or by typing it in manually."""
    cfg = _load_config()
    if cfg.get("spice_diff_offset") is not None:
        return   # already configured

    B   = "\033[1m"; DIM = "\033[90m"; CY = "\033[96m"
    YL  = "\033[93m"; R = "\033[0m"
    print(f"\n  {B}Difficulty / Level detection is not set up yet.{R}")
    print(f"    {CY}[A]{R} Automatic  – guided step-by-step scan (recommended)")
    print(f"    {CY}[M]{R} Manual     – type a known offset yourself")
    print(f"    {CY}[S]{R} Skip       – start without difficulty/level for now")
    choice = input(f"  Choice {DIM}(A/M/S) [A]{R}: ").strip().upper() or "A"

    if choice == "S":
        print(f"  {DIM}↳ Skipped. Run  python sdvx_rpc.py --find-diff  later.{R}")
        return

    if choice == "M":
        raw = input("    Difficulty offset (hex, e.g. 0x11DDE18): ").strip()
        try:
            off = int(raw, 0)
        except ValueError:
            print(f"  {YL}↳ Not a valid hex offset – skipped.{R}")
            return
        _update_config(spice_diff_offset=off)
        print(f"  {DIM}↳ Saved 0x{off:X}. If labels look shifted, add a "
              f"spice_diff_map to the config.{R}")
        return

    # Automatic guided scan – needs the game to be IN a chart.
    print(f"\n  {B}Guided difficulty scan{R}")
    print( "    1. Get in-game and START PLAYING any chart (the difficulty is")
    print( "       only written to memory while a chart is actually running).")
    print( "    2. Come back here and follow the prompts.")
    input(f"  {DIM}Press Enter once you are playing a chart…{R}")
    _find_value_mode(target="diff", pid=game_pid)


# ─────────────────────────────────────────────────────────────────────────────
# VOLFORCE  (computed from the Asphyxia savedata + music_db levels)
# ─────────────────────────────────────────────────────────────────────────────
# Formula (sdvx.org compendium, NABLA): per chart
#   single = floor( Level * (Score/10,000,000) * GradeCoeff * ClearCoeff * 20 )
# then VolForce = sum(best 50 singles) / 1000.  NABLA uses DECIMAL levels and a
# buffed UC coefficient (1.06). Level comes from music_db; score/clear from the
# savedata. If a record already stores a per-chart VF (NABLA does), that value
# is preferred since it matches what the game shows.

_VF_GRADE = [(9_900_000, 1.05), (9_800_000, 1.02), (9_700_000, 1.00),
             (9_500_000, 0.97), (9_300_000, 0.94), (9_000_000, 0.91),
             (8_700_000, 0.88), (7_500_000, 0.85), (6_500_000, 0.82),
             (0, 0.80)]
# clear-mark code → coefficient. Standard SDVX marks; overridable via config
# "vf_clear_coeff". (1 played/crash, 2 effective, 3 excessive, 4 UC, 5 PUC;
#  some NABLA forks add a maxxive code – adjust if --vf-test shows a mismatch.)
_VF_CLEAR = {1: 0.50, 2: 1.00, 3: 1.02, 4: 1.06, 5: 1.10, 6: 1.04}
_VF_TYPE_DIFF = {0: "NOV", 1: "ADV", 2: "EXH", 3: "variant", 4: "MXM", 5: "ULT"}


def _vf_grade_coeff(score: int) -> float:
    for thr, c in _VF_GRADE:
        if score >= thr:
            return c
    return 0.80


def _vf_level(diff_map: dict, mid, typ) -> float:
    song = diff_map.get(mid, {})
    if typ == 3:
        v = song.get("variant")
        lv = v[1] if v else ""
    else:
        lv = song.get(_VF_TYPE_DIFF.get(typ, ""), "")
    try:
        return float(lv)
    except (TypeError, ValueError):
        return 0.0


def _vf_record_value(rec: dict, diff_map: dict, clear_map: dict) -> int:
    """Per-chart VF as an integer (×1000). Prefers the stored per-chart VF
    field, which the game already stores as an integer ×1000 (e.g. 314 =
    0.314). Only falls back to the formula when no stored field exists."""
    for k in ("vf", "volforce", "force"):
        if k in rec and isinstance(rec[k], (int, float)):
            v = rec[k]
            # int → already ×1000 (314, 50, …); float < 50 → a 0.314-style value
            return round(v * 1000) if isinstance(v, float) and 0 < v < 50 else int(v)
    score = rec.get("score")
    if not isinstance(score, int):
        return 0
    lvl = _vf_level(diff_map, rec.get("mid", rec.get("music_id")), rec.get("type"))
    if lvl <= 0:
        return 0
    cc = clear_map.get(rec.get("clear"), 1.00)
    return int(lvl * (score / 10_000_000) * _vf_grade_coeff(score) * cc * 20)


def _looks_like_score(rec: dict) -> bool:
    return (isinstance(rec.get("score"), int)
            and isinstance(rec.get("type"), int) and 0 <= rec["type"] <= 5
            and ("mid" in rec or "music_id" in rec))


def compute_volforce(savedata_path: str, diff_map: dict,
                     clear_map: dict | None = None,
                     refid: str | None = None,
                     collect_samples: int = 0):
    """Returns total VolForce (float) or None. With collect_samples>0 returns
    (vf, [sample score records]) for diagnostics."""
    clear_map = clear_map or _VF_CLEAR
    try:
        with open(savedata_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return (None, []) if collect_samples else None

    best: dict[tuple, int] = {}
    samples: list = []
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("$$deleted") or not _looks_like_score(rec):
            continue
        if refid is not None and rec.get("__refid") != refid:
            continue        # only this account's scores
        if len(samples) < collect_samples:
            samples.append(rec)
        key = (rec.get("mid", rec.get("music_id")), rec.get("type"))
        vf  = _vf_record_value(rec, diff_map, clear_map)
        if vf > best.get(key, -1):
            best[key] = vf
    if not best:
        return (None, samples) if collect_samples else None
    total = sum(sorted(best.values(), reverse=True)[:50]) / 1000.0
    return (total, samples) if collect_samples else total


def _active_profile(savedata_path: str):
    """(name, refid) of the most recently updated profile in the savedata –
    i.e. the account currently being played. (None, None) if unavailable."""
    try:
        with open(savedata_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return (None, None)
    best, best_t = (None, None), -1.0
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("collection") != "profile" or rec.get("$$deleted"):
            continue
        ts = rec.get("updatedAt", {})
        ts = ts.get("$$date", 0) if isinstance(ts, dict) else 0
        if rec.get("name") and ts >= best_t:
            best_t, best = ts, (rec.get("name"), rec.get("__refid"))
    return best


def _refid_for_name(savedata_path: str, name: str):
    """refid of the profile whose name matches `name` (newest if several).
    Bridges the LIVE in-memory name to the savedata account for VolForce."""
    if not name:
        return None
    try:
        with open(savedata_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    best, best_t = None, -1.0
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("collection") != "profile" or rec.get("$$deleted"):
            continue
        if rec.get("name") == name:
            ts = rec.get("updatedAt", {})
            ts = ts.get("$$date", 0) if isinstance(ts, dict) else 0
            if ts >= best_t:
                best_t, best = ts, rec.get("__refid")
    return best


def _all_profiles(savedata_path: str) -> dict:
    """{name: refid} for every account in the savedata (newest refid per name)."""
    out: dict[str, str] = {}
    try:
        with open(savedata_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    seen_ts: dict[str, float] = {}
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("collection") != "profile" or rec.get("$$deleted"):
            continue
        nm = rec.get("name")
        if not nm:
            continue
        ts = rec.get("updatedAt", {})
        ts = ts.get("$$date", 0) if isinstance(ts, dict) else 0
        if ts >= seen_ts.get(nm, -1):
            seen_ts[nm] = ts
            out[nm] = rec.get("__refid")
    return out


def _read_name_at(pid: int, offset: int) -> str:
    """Decode the player-name C-string at base+offset (or '' if not a name)."""
    base = _get_module_base(pid, "soundvoltex.dll")
    if not base:
        return ""
    raw = _read_cstr(pid, base + offset)
    if not raw:
        return ""
    try:
        nm = raw.decode("shift_jis").strip()
    except Exception:
        nm = raw.decode("utf-8", errors="replace").strip()
    return nm if nm and nm not in ("...", "GUEST", "NONAME") else ""


def _resolve_active_player(pid: int, savedata_path, name_offsets: dict):
    """Determine the carded-in account WITHOUT rescanning each time.

    Names live at a SEPARATE offset per account, so we keep a {name: offset}
    map. On each call we (1) read the known offsets and, if exactly one holds
    its own account name, return it instantly; otherwise (2) for any account
    name from the savedata that isn't mapped yet, scan once and remember its
    offset. Returns (name, refid) or (None, None).
    """
    profiles = _all_profiles(savedata_path) if savedata_path else {}

    # (1) fast path – read the offsets we already know
    present = [n for n, off in name_offsets.items()
               if isinstance(off, int) and _read_name_at(pid, off) == n]
    if len(present) == 1:
        n = present[0]
        return n, profiles.get(n)

    # (2) discover: read mapped names; scan unmapped ones (once each)
    saved = False
    for nm in profiles:
        off = name_offsets.get(nm)
        if isinstance(off, int):
            if _read_name_at(pid, off) == nm:
                return nm, profiles.get(nm)
            continue                       # mapped but not in memory now → skip
        found = _scan_dll_for_name(pid, nm)
        if found is not None:
            name_offsets[nm] = found
            saved = True
            _update_config(name_offsets=dict(name_offsets))
            return nm, profiles.get(nm)
    if saved:
        _update_config(name_offsets=dict(name_offsets))

    # (3) last resort – the single-hint live read
    nm = read_player_name(pid)
    return (nm, profiles.get(nm)) if nm else (None, None)


def _locate_savedata(cfg: dict) -> str | None:
    """Find the Asphyxia SDVX savedata file from config or near the exe."""
    explicit = cfg.get("asphyxia_savedata")
    if explicit and os.path.exists(explicit):
        return explicit
    exe = cfg.get("asphyxia_exe", "")
    base = os.path.dirname(exe) if exe else "."
    import glob
    cands = (glob.glob(os.path.join(base, "savedata", "*sdvx*.db"))
             + glob.glob(os.path.join(base, "savedata", "savedata.db"))
             + glob.glob(os.path.join(base, "savedata.db"))
             + glob.glob(os.path.join(base, "**", "*sdvx*@*.db"), recursive=True))
    return cands[0] if cands else None


def _format_vf(vf) -> str:
    return f"{vf:.3f}" if isinstance(vf, (int, float)) else ""


# ─────────────────────────────────────────────────────────────────────────────
# RYUNET (Ryu7w7 network) – rich-presence lookup by card id
# ─────────────────────────────────────────────────────────────────────────────
# Endpoint (provided by the Ryunet developer):
#   GET https://x.ryu7w7.xyz/api/rp/lookup?cid=<card_id>
# Same idea as the Asphyxia path: resolve the active account, then pull its
# rich-presence data (name, and possibly VolForce / live status) for that id –
# only the *source* differs (a network API instead of the local savedata).
# The exact response shape + auth are confirmed via --ryunet-test before this is
# wired into the live loop.

RYUNET_API_DEFAULT = "https://x.ryu7w7.xyz/api/rp/lookup"
# The endpoint serves JSON to browser-like clients but 404s unknown user agents
# (edge/anti-bot filtering), so we present standard browser headers.
_RYUNET_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
_RYUNET_NAME_KEYS  = ("name", "dname", "player", "playername", "pname", "djname")
_RYUNET_VF_KEYS    = ("vf", "volforce", "volforce_value", "vforce", "force")


def _ryunet_lookup(cid: str, base_url: str = RYUNET_API_DEFAULT,
                   timeout: float = 4.0, headers: dict | None = None):
    """GET the Ryunet RP lookup for a card id.

    Returns (status, payload): payload is the parsed JSON (dict/list) when the
    body is JSON, otherwise the raw text. status is the HTTP code, or None on a
    network/transport error (payload then holds the error string). HTTPError
    bodies (4xx/5xx) are returned too, so error messages stay visible.
    """
    url = f"{base_url}?{urllib.parse.urlencode({'cid': cid})}"
    req = urllib.request.Request(url, headers=headers or _RYUNET_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, body = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                       # URLError, timeout, DNS, …
        return None, f"{type(e).__name__}: {e}"
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body


def _ryunet_extract(payload):
    """Best-effort (name, vf) extraction from a Ryunet JSON payload, tolerating
    a wrapper object ({"data": {...}} etc.). Tuned once the real shape is known."""
    if not isinstance(payload, dict):
        return None, None
    flat = dict(payload)
    for wrap in ("data", "profile", "result", "rp", "player"):
        if isinstance(payload.get(wrap), dict):
            flat.update(payload[wrap])
    name = next((flat[k] for k in _RYUNET_NAME_KEYS if flat.get(k)), None)
    vf   = next((flat[k] for k in _RYUNET_VF_KEYS if k in flat), None)
    return name, vf


def _ryunet_vf_string(cid: str):
    """Fetch + format the VolForce for a card id via the Ryunet API.
    Returns a string like '5.572' or None on any failure. The API ships VF
    scaled ×1000 (e.g. 5572 = 5.572)."""
    status, payload = _ryunet_lookup(cid)
    if status == 200:
        _, vf = _ryunet_extract(payload)
        if isinstance(vf, (int, float)):
            return _format_vf(vf / 1000.0 if vf > 50 else float(vf))
    return None


def _ryunet_test_mode() -> None:
    """python sdvx_rpc.py --ryunet-test --cid <card_id>

    Probe the Ryunet endpoint and print the raw + parsed response so we can
    confirm the contract (field names, auth, live status) from your machine,
    where a valid Ryunet card id is available."""
    cfg  = _load_config()
    base = cfg.get("ryunet_api", RYUNET_API_DEFAULT)
    cid  = ""
    if "--cid" in sys.argv:
        i = sys.argv.index("--cid")
        if i + 1 < len(sys.argv):
            cid = sys.argv[i + 1]
    cid = cid or cfg.get("ryunet_cid", "")
    if not cid:
        print("[ERROR] No card id. Usage: python sdvx_rpc.py --ryunet-test "
              "--cid <card_id>   (or set \"ryunet_cid\" in the config).")
        return
    print(f"[RYUNET] GET {base}?cid={cid}")
    status, payload = _ryunet_lookup(cid, base)
    print(f"[RYUNET] HTTP status: {status}")
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, indent=2, ensure_ascii=False)[:4000])
        nm, vf = _ryunet_extract(payload)
        print(f"\n[RYUNET] guessed → name={nm!r}  vf={vf!r}")
        if nm is None:
            print("[RYUNET] (name field not auto-detected – send me the JSON "
                  "above so I can map the right field.)")
    else:
        print("[RYUNET] body (not JSON):")
        print(str(payload)[:4000])


def _vf_test_mode() -> None:
    """python sdvx_rpc.py --vf-test  → verify VolForce + dump sample records."""
    cfg = _load_config()
    title_map, diff_map = load_song_map()
    path = _locate_savedata(cfg)
    if not path:
        print("[ERROR] savedata not found. Set \"asphyxia_savedata\" in the config "
              "to the full path of your sdvx@asphyxia .db file.")
        return
    print(f"[INFO] Reading savedata: {path}")
    name, refid = _active_profile(path)
    if name:
        print(f"[INFO] Active account (newest profile): {name}  ({refid})")
    vf, samples = compute_volforce(path, diff_map, refid=refid, collect_samples=3)
    if vf is None:
        print("[WARN] No score records recognised. Sample lines below – tell me "
              "the field names so I can adjust the parser:")
    else:
        print(f"[RESULT] VolForce for {name or 'active account'} = "
              f"{_format_vf(vf)}  (compare with in-game).")
    # also list every account's VF, so multi-profile setups are easy to verify
    seen = set()
    for ln in open(path, encoding="utf-8", errors="replace").read().splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("collection") == "profile" and r.get("__refid") not in seen \
                and r.get("name"):
            seen.add(r.get("__refid"))
            v = compute_volforce(path, diff_map, refid=r.get("__refid"))
            print(f"    • {r.get('name'):<12} {r.get('__refid')}  VF: {_format_vf(v)}")
    for i, rec in enumerate(samples, 1):
        keys = {k: rec[k] for k in list(rec)[:12]}
        print(f"  sample {i}: {keys}")


def _find_name_mode() -> None:
    """python sdvx_rpc.py --find-name  → (re)locate the player-name offset.
    Run it while carded-in as the account whose name you want to track. Lists
    every matching location so duplicate copies are visible."""
    cfg  = _load_config()
    name = cfg.get("player_name") or input("  Your exact in-game name: ").strip()
    if not name:
        print("[ERROR] No name given.")
        return
    pid = _find_pid_by_name(GAME_EXECUTABLE)
    if not pid:
        print(f"[ERROR] {GAME_EXECUTABLE} is not running.")
        return
    base, size = _get_module_info(pid, "soundvoltex.dll")
    if not base or not size:
        print("[ERROR] soundvoltex.dll not loaded yet (card in / enter My Room).")
        return
    data = _read_full_module(pid, base, size)
    if not data:
        print("[ERROR] Memory read failed – run as Administrator.")
        return
    try:
        needle = name.encode("shift_jis")
    except Exception:
        needle = name.encode("utf-8")
    offs, i = [], data.find(needle)
    while i != -1:
        if i + len(needle) < len(data) and data[i + len(needle)] == 0:
            offs.append(i)               # null-terminated occurrence
        i = data.find(needle, i + 1)
    if not offs:
        print(f"[RESULT] '{name}' not found. Be carded-in (My Room) and retry, "
              f"or check the spelling.")
        return
    print(f"[RESULT] '{name}' found at {len(offs)} location(s):")
    for o in offs:
        print(f"    0x{o:X}")
    _update_config(player_name_offset=offs[0], player_name=name)
    print(f"[INFO] Saved player_name_offset = 0x{offs[0]:X}.")
    if len(offs) > 1:
        print("[NOTE] Several copies exist. If the name doesn't switch when you")
        print("       change accounts, set player_name_offset to another value")
        print("       from the list above and restart.")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# UPDATE CHECK (GitHub) – offline-safe, never blocks the launch
# ─────────────────────────────────────────────────────────────────────────────

def _parse_ver(s: str) -> tuple:
    s = s.strip().lstrip("vV")
    return tuple(int(p) for p in re.split(r"[.\-_]", s) if p.isdigit())


def _check_for_update(timeout: float = 3.0) -> None:
    """Compare the local __version__ with the latest GitHub release tag. Prints a
    notice and offers to open the download page if newer. Any failure (no
    internet, rate limit, no release) is swallowed so the launcher still runs."""
    api = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(api, headers={
        "User-Agent": "sdvx-rpc-update-check",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return                                   # offline / rate-limited → skip
    tag = data.get("tag_name") or ""
    url = data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases"
    if not tag:
        return
    try:
        newer = _parse_ver(tag) > _parse_ver(__version__)
    except Exception:
        newer = tag.lstrip("vV") != __version__
    if not newer:
        return
    print(f"\n[UPDATE] New version available: {tag}  (you have v{__version__})")
    print(f"[UPDATE] {url}")
    try:
        ans = input("[UPDATE] Open the download page now? (Y/N) [N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if ans == "y":
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            print(f"[UPDATE] Open manually: {url}")


def main() -> None:
    if "--spice-dump" in sys.argv:
        _spice_dump_mode()
        return
    if "--find-diff" in sys.argv:
        _find_value_mode()
        return
    if "--vf-test" in sys.argv:
        _vf_test_mode()
        return
    if "--find-name" in sys.argv:
        _find_name_mode()
        return
    if "--ryunet-test" in sys.argv:
        _ryunet_test_mode()
        return

    print_logo()
    _check_for_update()

    fallback_name, jacket_base_url, ea_url, server_exe, spice_port, _ = \
        run_setup_wizard()

    game_exe = _load_config().get("game_exe_path") or GAME_EXECUTABLE
    if not os.path.exists(game_exe):
        print(f"[ERROR] {game_exe} not found. Set \"game_exe_path\" in the config "
              f"to the full path of spice64.exe, or run from the game folder.")
        input("Press Enter to exit…")
        return
    game_dir = os.path.dirname(os.path.abspath(game_exe))

    title_map, diff_map = load_song_map()
    _db = _find_music_db()
    if title_map:
        songs_with_levels = sum(
            1 for d in diff_map.values()
            if any(d.get(k) for k in ("NOV", "ADV", "EXH", "MXM", "ULT"))
               or d.get("variant")
        )
        print(f"[INFO] Loaded {len(title_map)} songs "
              f"({songs_with_levels} with level data) from {_db}.")
        if songs_with_levels == 0:
            print("[WARN] Level data is EMPTY – the <difnum>/<rating>/<level> tag")
            print("       in your music_db.xml does not match any known format.")
            print("       Run with --dump-db to inspect the raw XML structure.")
    else:
        print("[WARN] music_db not found – song IDs will be shown instead of "
              "names, and no level. Set \"music_db_path\" to the full path of your")
        print("       music_db.xml (e.g. <game>/data/others/music_db.xml or the "
              "omnimix .merged.xml).")

    if _DEBUG:
        print("\033[93m[DEBUG MODE ON]\033[0m  Logging diff / gauge / RPC events to console.\n")

    # ── Discord ──────────────────────────────────────────────────────────────
    rpc: RateLimitedRPC | None = None
    try:
        _cfg   = _load_config()
        hb_min = float(_cfg.get("heartbeat_min", 2.0))
        hb_max = float(_cfg.get("heartbeat_max", 15.0))
        rpc = RateLimitedRPC(CLIENT_ID, hb_min, hb_max)
        rpc.connect()
        print(f"[INFO] Connected to Discord. (heartbeat {hb_min:g}-{hb_max:g}s)")
    except Exception as exc:
        print(f"[WARN] Discord not available ({exc}). Continuing without RPC.")

    # ── Start EA server BEFORE the game ──────────────────────────────────────
    server_proc = _start_server(server_exe) if server_exe else None

    # ── Launch game (with -url flag for EA service) ───────────────────────────
    game_args = [game_exe]
    if ea_url:
        game_args += ["-url", ea_url]   # Correct Spice2x flag (not -ea)
        print(f"[INFO] Passing EA URL to game: {ea_url}")

    # Custom servers need a PCBID (spice2x -p). It's matched to the server URL
    # and stored in the config (asked once in the wizard's Custom-URL step).
    _pcbid = _load_config().get("pcbid_by_url", {}).get(ea_url)
    if _pcbid:
        game_args += ["-p", _pcbid]
        print(f"[INFO] Passing PCBID to game (-p {_pcbid}).")

    # ── SpiceAPI: fresh session password, enable API + memory access ──────────
    # The password is generated per session and never written to disk; the
    # memory module only works when a password is set.
    spice_password = secrets.token_hex(8)
    game_args += ["-api", str(spice_port), "-apipass", spice_password]
    print(f"[INFO] SpiceAPI enabled on port {spice_port} "
          f"(session password generated).")

    # On Linux the game is a Windows binary → run it through Wine/Proton.
    # Configurable via "launch_prefix" (e.g. ["wine"] or a Proton wrapper) and
    # "wine_prefix" (WINEPREFIX path, e.g. "/home/user/.wine_sdvx").
    _game_env = None
    if _IS_LINUX:
        _cfg = _load_config()
        prefix = _cfg.get("launch_prefix") or ["wine"]
        game_args = list(prefix) + game_args
        print(f"[INFO] Linux: launching via {' '.join(prefix)}.")
        wp = _cfg.get("wine_prefix")
        if wp:
            _game_env = {**os.environ, "WINEPREFIX": os.path.expanduser(wp)}
            print(f"[INFO] Using WINEPREFIX={wp}")

    try:
        process = subprocess.Popen(
            game_args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,  bufsize=1,
            universal_newlines=True, encoding="cp932", errors="replace",
            env=_game_env,
            cwd=game_dir or None,
        )
    except Exception as exc:
        print(f"[ERROR] Failed to launch {GAME_EXECUTABLE}: {exc}")
        if server_proc:
            server_proc.terminate()
        input()
        return

    game_pid = process.pid
    print(f"[INFO] Game launched (PID {game_pid}).")

    # ── Drain the game's stdout in a dedicated thread from here on. Without
    #    this, any blocking prompt below (or an idle main loop) would stop the
    #    pipe being read, the OS buffer would fill, and the GAME would freeze. ─
    _log_q: "queue.Queue" = queue.Queue()

    def _stdout_reader() -> None:
        try:
            for ln in process.stdout:
                _log_q.put(ln)
        except Exception:
            pass
        _log_q.put(None)        # sentinel: stream closed

    threading.Thread(target=_stdout_reader, daemon=True).start()

    # ── Linux/Wine: the pid we just spawned is the `wine` wrapper, not the game.
    #    Find the real spice64.exe process (the one memory reads target). ──
    if _IS_LINUX:
        print("[INFO] Linux: locating the spice64.exe process under Wine…")
        for _ in range(60):
            gp = _linux_find_game_pid()
            if gp:
                game_pid = gp
                break
            if process.poll() is not None:
                break
            time.sleep(1)
        if game_pid == process.pid:
            print("[WARN] Could not find the spice64.exe process – memory reads "
                  "(diff/name) may not work until it appears.")
        else:
            print(f"[INFO] Game process pid = {game_pid}.")

    # ── Player-name scanner / saved offset ───────────────────────────────────
    saved_cfg    = _load_config()
    saved_offset = saved_cfg.get("player_name_offset")
    if saved_offset:
        with _offset_lock:
            _discovered_offset = saved_offset
        print(f"[INFO] Using saved player-name offset 0x{saved_offset:X}.")
    elif fallback_name:
        threading.Thread(
            target=_name_scanner_thread,
            args=(game_pid, fallback_name),
            daemon=True,
        ).start()
        print(f"[INFO] DLL scanner active – searching for '{fallback_name}'.")
    else:
        print(f"[INFO] No name / offset – using hint 0x{PLAYER_NAME_OFFSET_HINT:X}.")

    # ── Runtime state ─────────────────────────────────────────────────────────
    current_song        = "..."
    current_jacket_path = ""
    current_state       = "Menu"
    play_mode           = ""
    active_event        = ""
    sub_menu            = ""
    current_diff        = ""
    current_level       = ""
    current_song_id     = 0
    current_vf          = ""           # VolForce string, e.g. "19.234"
    session_start       = time.time()
    song_start          = session_start
    player_name         = fallback_name or "GUEST"
    _last_name_check    = 0.0
    logged_in           = False        # True only once carded-in (MyRoom seen)
    _render_cfg         = _load_config()
    show_vf             = _render_cfg.get("show_vf", True)
    show_diff           = _render_cfg.get("show_diff", True)

    rx_song           = re.compile(r"music/(\d+)_")
    _MUSIC_PREFIX     = "Loading /data/music/"
    _MUSIC_PREFIX_LEN = len(_MUSIC_PREFIX)
    _PLAY_TRIGGERS    = (
        "in ALTERNATIVE_GAME_SCENE", "in MEGAMIX_GAME_SCENE",
        "in MEGAMIX_BATTLE",         "in BATTLE_GAME_SCENE",
        "in AUTOMATION_GAME_SCENE",  "in ARENA_GAME_SCENE",
        "game_bg/",
    )

    def _variant_for(sid: int) -> str:
        v = diff_map.get(sid, {}).get("variant")
        return v[0] if v else "INF"

    def _level_for(sid: int, diff: str) -> str:
        song = diff_map.get(sid, {})
        if diff in song:
            return song[diff]
        v = song.get("variant")
        if v and v[0] == diff:
            return v[1]
        return ""

    _vf_cfg       = _load_config()
    _vf_path      = _locate_savedata(_vf_cfg)
    _vf_clear_map = {int(k): v for k, v in
                     _vf_cfg.get("vf_clear_coeff", {}).items()} or _VF_CLEAR
    active_refid  = None
    _ryunet_cid   = (_vf_cfg.get("ryunet_cid_by_url", {}).get(ea_url)
                     or _vf_cfg.get("ryunet_cid"))     # custom-server VF source
    _last_ryunet  = 0.0
    # per-account name offsets ({name: offset}); migrate any single offset
    name_offsets  = {str(k): int(v) for k, v in
                     _vf_cfg.get("name_offsets", {}).items()}
    if _vf_cfg.get("player_name") and _vf_cfg.get("player_name_offset") \
            and _vf_cfg["player_name"] not in name_offsets:
        name_offsets[_vf_cfg["player_name"]] = int(_vf_cfg["player_name_offset"])

    def _refresh_account() -> None:
        """Resolve the carded-in account and update the display name + VolForce.
        Name is read live from memory. VolForce comes from the Asphyxia savedata,
        or — if a Ryunet card id is configured (custom server) — from the Ryunet
        API. Only meaningful once logged in (My Room reached)."""
        nonlocal current_vf, player_name, active_refid, _last_ryunet
        if not logged_in:
            player_name  = "GUEST"
            current_vf   = ""
            active_refid = None
            return
        name, refid = _resolve_active_player(game_pid, _vf_path, name_offsets)
        if _DEBUG:
            _dbg("NAME", f"active = {name!r}  (known offsets: "
                         f"{ {k: hex(v) for k, v in name_offsets.items()} })")
        if name:
            player_name = name
            if refid:
                active_refid = refid
        elif _vf_path:                             # fallback: newest profile
            n2, r2 = _active_profile(_vf_path)
            if n2:
                player_name, active_refid = n2, r2

        if _ryunet_cid:                            # custom server → VF from API
            now = time.time()
            if now - _last_ryunet >= 60.0 or not current_vf:
                _last_ryunet = now
                vf = _ryunet_vf_string(_ryunet_cid)
                if vf is not None:
                    current_vf = vf
        elif _vf_path and active_refid:            # local Asphyxia savedata
            try:
                vf = compute_volforce(_vf_path, diff_map, _vf_clear_map,
                                      refid=active_refid)
                current_vf = _format_vf(vf)
            except Exception:
                pass

    _refresh_vf = _refresh_account   # keep the existing call-site name working

    if _ryunet_cid:
        print(f"[INFO] VolForce from Ryunet API (cid {_ryunet_cid}); "
              f"name appears once you're carded-in (My Room).")
    elif _vf_path:
        print(f"[INFO] VolForce ready (from {os.path.basename(_vf_path)}); "
              f"name + VF appear once you're carded-in (My Room).")
    else:
        print("[INFO] VolForce disabled – no savedata and no Ryunet card id. "
              "Name is read live from memory once carded-in.")

    def build_package() -> dict | None:
        """The COMPLETE Discord package for the current state. Pure snapshot,
        no side effects, so the pusher thread can poll it safely."""
        large_img = _large_image(current_state, current_jacket_path, jacket_base_url)
        # diff/level only when enabled
        details   = _build_details(play_mode, active_event,
                                   current_diff if show_diff else "",
                                   current_level if show_diff else "")
        small_txt = _build_small_text(player_name)
        vf_suffix = f"  •  VF: {current_vf}" if (show_vf and current_vf) else ""

        if current_state == "Playing":
            song_txt = (f"Playing: {current_song}"
                        if current_song not in ("...", "Browsing...")
                        else "Loading...")
            return dict(state=song_txt, details=details, large_image=large_img,
                        large_text=_safe(current_song),
                        small_image=IMG_MENU,
                        small_text=_safe(small_txt), start=int(session_start))
        if current_state == "Selecting":
            sel_txt = (f"Selecting: {current_song}"
                       if current_song not in ("...", "Browsing...")
                       else "Choosing Song")
            det = details + vf_suffix
            return dict(state=sel_txt, details=det,
                        large_image=IMG_MENU,
                        large_text=_safe(player_name),
                        small_image=None, small_text=None,
                        start=int(session_start))
        if current_state == "Results":
            return dict(state=f"Result: {current_song}", details=details,
                        large_image=large_img, large_text=_safe(current_song),
                        small_image=IMG_MENU,
                        small_text=_safe(small_txt),
                        start=int(song_start))
        if current_state == "TotalResults":
            return dict(state="Session Results", details=_safe(play_mode or "SDVX"),
                        large_image=IMG_MENU, large_text=_safe(player_name),
                        start=int(session_start))
        if current_state == "MyRoom":
            det = _safe(play_mode or "SDVX") + vf_suffix
            return dict(state="My Room", details=det,
                        large_image=IMG_MENU,
                        large_text=_safe(player_name),
                        small_image=None,
                        start=int(session_start))
        return dict(state=_safe(sub_menu or "In Menu"),
                    details=_safe(play_mode or "SDVX"),
                    large_image=IMG_MENU, large_text=_safe(player_name),
                    start=int(session_start))

    def push() -> None:
        # Sending is handled by the pusher thread (it polls build_package);
        # here we only emit the debug line when a change is detected.
        if _DEBUG:
            details = _build_details(play_mode, active_event,
                                     current_diff, current_level)
            _dbg("RPC ", f"[{current_state}] song={current_song!r}  "
                         f"diff={current_diff!r} lv={current_level!r}  "
                         f"details={details!r}")

    if rpc is not None:
        rpc.set_snapshot(build_package)
    push()

    # ── First run: difficulty/level offset setup (typed prompts, no Ctrl-C).
    #    The player NAME no longer needs a guided scan – it is resolved
    #    automatically at My Room from the Asphyxia profile names. ──
    _first_run_offset_setup(game_pid)

    # ── SpiceAPI: real-time difficulty / gauge from game memory ───────────────
    # spice2x never logs diff/gauge, so we read them straight out of
    # soundvoltex.dll. The Spice thread polls every 500 ms and calls
    # _on_spice_update() whenever the values change. (current_diff / _level /
    # _gauge are also touched by the log loop; under the GIL the overlap is
    # benign for a cosmetic presence display and self-corrects on the next push.)
    spice_state = _SpiceState()
    spice_api   = SpiceAPI(SPICE_HOST, spice_port, spice_password)

    def _on_spice_update() -> None:
        nonlocal current_diff, current_level
        with spice_state.lock:
            idx   = spice_state.diff_idx
            sdiff = spice_state.diff
        # index 3 = the per-song variant slot → resolve via music_db
        if sdiff and idx == 3:
            new_diff = _variant_for(current_song_id)
        else:
            new_diff = sdiff                       # "" when cleared (not playing)
        new_level = _level_for(current_song_id, new_diff) if new_diff else ""
        if new_diff != current_diff or new_level != current_level:
            current_diff  = new_diff
            current_level = new_level
            push()

    spice_name_off = saved_offset or PLAYER_NAME_OFFSET_HINT
    threading.Thread(
        target=_spice_thread,
        args=(spice_api, spice_state, spice_name_off, _on_spice_update),
        kwargs={"game_pid": game_pid,
                "force_discovery": "--discover" in sys.argv},
        daemon=True,
    ).start()
    saved_diff_off = _load_config().get("spice_diff_offset")
    if saved_diff_off and "--discover" not in sys.argv:
        print(f"[INFO] Using saved diff offset 0x{saved_diff_off:X} "
              f"(SpiceAPI memory poll).")
    else:
        print("[INFO] SpiceAPI diff-offset discovery armed "
              "(play a few different songs to confirm it).")

    # ── Log reader loop ───────────────────────────────────────────────────────
    try:
        while True:
            try:
                raw = _log_q.get(timeout=0.3)
            except queue.Empty:
                raw = ""                       # idle tick → still refresh below
            if raw is None:                    # stream-closed sentinel
                break
            if not raw and process.poll() is not None and _log_q.empty():
                break
            line = raw.strip() if raw else ""

            # ── Periodic account refresh (every 5 s, even while idle): the live
            #    name (from memory) + VF follow whichever account is carded-in. ─
            now = time.time()
            if now - _last_name_check >= 5.0:
                _last_name_check = now
                before = (player_name, current_vf)
                _refresh_account()
                if (player_name, current_vf) != before:
                    push()

            if not line:
                continue                       # idle tick done

            # ── Debug: flag lines that contain diff / gauge keywords ──────────
            if _DEBUG:
                if _DBG_DIFF_RX.search(line):
                    _dbg("LOG ", line[:180])

            # 1. Play Mode
            if "ea3_report_posev" in line and "/coin/kfc_game_s_" in line:
                for kw, mode in (
                    ("light",         "Light Start"),
                    ("standard_plus", "Normal Start"),
                    ("standard",      "Normal Start"),
                    ("premium",       "Premium Time"),
                    ("blaster",       "Blaster Start"),
                    ("paradise",      "Paradise Start"),
                    ("arena",         "Arena Battle"),
                    ("megamix",       "Megamix Battle"),
                ):
                    if kw in line:
                        play_mode = mode
                        break

            # (Gauge is no longer guessed from the log – spice2x doesn't log
            #  it. It comes exclusively from the memory offset while playing.)

            # 3. Hexa Diver
            if "LoadingIFS" in line and "hexa_diver" in line and "blue" in line:
                if active_event != "Hexa Diver":
                    active_event  = "Hexa Diver"
                    current_song  = "Browsing..."
                    current_state = "Selecting"
                    push()
            if "LoadingIFS" in line and "ver06/ms_sel" in line:
                if active_event == "Hexa Diver":
                    active_event = ""

            # 4. Song + Jacket
            if _MUSIC_PREFIX in line and ".png" in line:
                m = rx_song.search(line)
                if m:
                    sid_str = m.group(1)
                    is_bg   = "_b.png" in line
                    png_pos = line.find(".png")

                    if is_bg or current_state == "Playing" or active_event == "Hexa Diver":
                        p_start = line.find(_MUSIC_PREFIX) + _MUSIC_PREFIX_LEN
                        current_jacket_path = line[p_start: png_pos + 4]

                    if is_bg or current_state == "Playing":
                        try:
                            sid  = int(sid_str)
                            name = title_map.get(sid, str(sid))
                            changed = (name != current_song
                                       or current_state == "Playing")
                            if sid != current_song_id:
                                current_song_id = sid
                                # Diff/level come ONLY from the memory offset
                                # while playing. On a new song, drop any stale
                                # value instead of guessing the highest diff.
                                current_diff  = ""
                                current_level = ""
                            if changed:
                                current_song = name
                                _dbg("DIFF",
                                     f"song #{sid} = {name!r}  "
                                     f"diff={current_diff!r} lv={current_level!r}")
                                push()
                        except Exception:
                            pass

            # (Difficulty is no longer guessed from the log – spice2x doesn't
            #  log it. It comes exclusively from the memory offset while playing.)

            # 5. State transitions
            if "in MUSICSELECT" in line:
                if active_event == "Hexa Diver" and "ms_sel" in line:
                    active_event = ""
                if current_state != "Selecting":
                    current_state = "Selecting"
                    if current_song == "..." and active_event != "Hexa Diver":
                        current_song = "Browsing..."
                    _dbg("STAT", f"→ Selecting")
                    push()

            if current_state == "Selecting":
                pass  # diff is unknown during select – shown only while playing

            if any(t in line for t in _PLAY_TRIGGERS):
                if current_state != "Playing":
                    current_state = "Playing"
                    song_start    = time.time()
                    _dbg("STAT", f"→ Playing")
                    push()

            if "in RESULT_SCENE" in line:
                if current_state != "Results":
                    current_state = "Results"
                    _refresh_vf()          # a new score may change VolForce
                    _dbg("STAT", f"→ Results")
                    push()

            if "in T_RESULT_SCENE" in line:
                if current_state != "TotalResults":
                    current_state = "TotalResults"
                    sub_menu      = ""
                    _dbg("STAT", f"→ TotalResults")
                    push()

            if "in MYROOM_SCENE" in line or "MY_ROOM" in line:
                if current_state != "MyRoom":
                    current_state = "MyRoom"
                    sub_menu      = "My Room"
                    logged_in     = True       # carded-in → name is valid now
                    _refresh_account()         # read the live name/VF fresh
                    _last_name_check = time.time()
                    _dbg("STAT", f"→ MyRoom")
                    push()

            if ("in GAMEOVER"          in line
                    or "in CARD_OUT_SCENE" in line
                    or "in TITLEDEMO"      in line):
                if current_state != "Menu":
                    current_state       = "Menu"
                    play_mode           = ""
                    active_event        = ""
                    sub_menu            = ""
                    current_jacket_path = ""
                    current_diff        = ""
                    current_level       = ""
                    current_song_id     = 0
                    session_start       = time.time()
                    logged_in           = False     # carded-out → no valid name
                    player_name         = "GUEST"
                    current_vf          = ""
                    _dbg("STAT", f"→ Menu (card-out / game-over)")
                    push()

            if current_state == "Menu" and "in " in line:
                new_sub = ""
                if "GENERATOR"        in line: new_sub = "Card Generator"
                elif "SKILL_ANALYZER" in line: new_sub = "Skill Analyzer"
                elif "CREW_SELECT"    in line: new_sub = "Crew Selection"
                elif "CREW"           in line: new_sub = "Crew Selection"
                if new_sub and new_sub != sub_menu:
                    sub_menu = new_sub
                    push()

            # Tell the SpiceAPI thread when a chart is actually loaded. The
            # difficulty offset is only valid during play (and the results
            # screen right after) – not during song-select.
            spice_state.set_song(
                current_song_id,
                current_state in ("Playing", "Results"),
            )

    finally:
        spice_api.close()
        if server_proc and server_proc.poll() is None:
            # Give Asphyxia a moment to flush any pending save (card-out, score
            # write, …) before we stop it. Try a graceful stop first, then kill.
            print("\n[INFO] Stopping Asphyxia in 5 s (letting saves finish)…")
            time.sleep(5)
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except Exception:
                server_proc.kill()
            print("[INFO] Server process terminated.")


if __name__ == "__main__":
    main()
