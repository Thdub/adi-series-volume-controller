"""
rme_app.py
-----------
ADI Series Volume Controller - controls RME ADI-2 output volumes from wheels,
remotes or any hotkey, via MIDI SysEx. Tray app, settings in config.ini.

Needs sendmidi.exe / receivemidi.exe (next to the app, or paths set in
config.ini). No administrator rights needed (the keyboard hook just can't
see keys while a window running as administrator has focus).
Broadlink IR (optional): `pip install broadlink` when running as a script.
"""

import configparser
import ctypes
import difflib
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from ctypes import wintypes

TITLE = "ADI Series Volume Controller"

# ============================================================================
# PATHS
# ============================================================================

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

APP_DIR = app_dir()
CONFIG_PATH = os.path.join(APP_DIR, "config.ini")
TRAY_ICON_PATH = os.path.join(APP_DIR, "tray.ico")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# ============================================================================
# WIN32 (ctypes)
# ============================================================================

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
VK_ESCAPE = 0x1B
VK_MENU_MASK = 0xE8      # unassigned VK - same "menu mask key" trick as AutoHotkey
KEYEVENTF_KEYUP = 0x2

VK_TO_MOD = {
    0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
    0x12: "alt", 0xA4: "alt", 0xA5: "alt",
    0x10: "shift", 0xA0: "shift", 0xA1: "shift",
    0x5B: "win", 0x5C: "win",
}
MODIFIER_VKS = set(VK_TO_MOD)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


LowLevelKeyboardProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WNDPROCTYPE = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROCTYPE),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _proto(fn, restype, *argtypes):
    """Explicit types for every Win32 call: avoids 64-bit pointer truncation."""
    fn.restype = restype
    fn.argtypes = list(argtypes)


W = wintypes
_proto(user32.SetWindowsHookExW, W.HHOOK, ctypes.c_int, LowLevelKeyboardProc, W.HINSTANCE, W.DWORD)
_proto(user32.CallNextHookEx, W.LPARAM, W.HHOOK, ctypes.c_int, W.WPARAM, W.LPARAM)
_proto(user32.UnhookWindowsHookEx, W.BOOL, W.HHOOK)
_proto(user32.GetMessageW, W.BOOL, ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
_proto(user32.TranslateMessage, W.BOOL, ctypes.POINTER(W.MSG))
_proto(user32.DispatchMessageW, ctypes.c_long, ctypes.POINTER(W.MSG))
_proto(user32.RegisterClassW, W.ATOM, ctypes.POINTER(WNDCLASS))
_proto(user32.CreateWindowExW, W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
_proto(user32.DefWindowProcW, ctypes.c_long, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
_proto(user32.DestroyWindow, W.BOOL, W.HWND)
_proto(user32.PostQuitMessage, None, ctypes.c_int)
_proto(user32.PostMessageW, W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
_proto(user32.CreatePopupMenu, W.HMENU)
_proto(user32.AppendMenuW, W.BOOL, W.HMENU, W.UINT, ctypes.c_size_t, W.LPCWSTR)
_proto(user32.DestroyMenu, W.BOOL, W.HMENU)
_proto(user32.TrackPopupMenu, W.BOOL, W.HMENU, W.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, ctypes.c_void_p)
_proto(user32.GetCursorPos, W.BOOL, ctypes.POINTER(W.POINT))
_proto(user32.SetForegroundWindow, W.BOOL, W.HWND)
_proto(user32.LoadIconW, W.HICON, W.HINSTANCE, ctypes.c_void_p)
_proto(user32.LoadImageW, W.HANDLE, W.HINSTANCE, W.LPCWSTR, W.UINT, ctypes.c_int, ctypes.c_int, W.UINT)
_proto(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
_proto(user32.keybd_event, None, W.BYTE, W.BYTE, W.DWORD, ctypes.c_size_t)
_proto(user32.MessageBoxW, ctypes.c_int, W.HWND, W.LPCWSTR, W.LPCWSTR, W.UINT)
_proto(user32.ShowWindow, W.BOOL, W.HWND, ctypes.c_int)
_proto(user32.GetParent, W.HWND, W.HWND)
_proto(user32.GetSystemMenu, W.HMENU, W.HWND, W.BOOL)
_proto(user32.DeleteMenu, W.BOOL, W.HMENU, W.UINT, W.UINT)
_proto(shell32.Shell_NotifyIconW, W.BOOL, W.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
_proto(kernel32.GetModuleHandleW, W.HMODULE, W.LPCWSTR)
_proto(kernel32.CreateMutexW, W.HANDLE, ctypes.c_void_p, W.BOOL, W.LPCWSTR)
_proto(kernel32.CreateJobObjectW, W.HANDLE, ctypes.c_void_p, W.LPCWSTR)
_proto(kernel32.SetInformationJobObject, W.BOOL, W.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
_proto(kernel32.AssignProcessToJobObject, W.BOOL, W.HANDLE, W.HANDLE)
_proto(kernel32.GetCurrentProcess, W.HANDLE)
_proto(kernel32.GetConsoleWindow, W.HWND)
_proto(kernel32.GetConsoleProcessList, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32)
_proto(kernel32.AllocConsole, W.BOOL)

# ============================================================================
# HOTKEYS - names, parsing, formatting
# ============================================================================

SLOT_NAMES = ("left", "right", "click", "click_left", "click_right", "long_click")
BASE_SLOTS = ("left", "right", "click")
BASE_KEYS = tuple(f"controller1_{n}" for n in BASE_SLOTS)   # required: Controller 1 left/right/click
HOTKEY_KEYS = (tuple(f"controller1_{n}" for n in SLOT_NAMES)
               + tuple(f"controller2_{n}" for n in SLOT_NAMES[:5]))   # long click: Controller 1 only
SLOT_LABELS = {"left": "LEFT", "right": "RIGHT", "click": "CLICK", "click_left": "CLICK+LEFT",
               "click_right": "CLICK+RIGHT", "long_click": "LONG CLICK"}
MODIFIER_NAMES = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
                  "win": "win", "windows": "win"}


def hotkey_section(key):
    return "Split Mode Hotkeys" if key.startswith("controller2") else "Hotkeys"


def slot_label(key):
    controller, name = key.split("_", 1)
    return f"{SLOT_LABELS[name]} (Controller {controller[-1]})"


def key_name_to_vk(name):
    """'8' / 'K' (digit or letter), 'F20', or 'VK38' (hex) -> VK code."""
    n = name.strip().upper()
    if len(n) == 1 and n.isascii() and n.isalnum():
        return ord(n)                      # VK codes of 0-9 / A-Z are the characters
    m = re.fullmatch(r"F(\d{1,2})", n)
    if m and 1 <= int(m.group(1)) <= 24:
        return 0x6F + int(m.group(1))      # F1 = 0x70 ... F24 = 0x87
    m = re.fullmatch(r"VK([0-9A-F]{1,2})", n)
    if m:
        return int(m.group(1), 16)
    return None


def parse_hotkey(text):
    """'alt+8', 'ctrl+alt+K', 'F20', 'alt+VK38' -> (frozenset(mods), vk), or None."""
    text = text.strip()
    if not text:
        return None
    parts = [p.strip() for p in text.split("+")]
    if not all(parts):
        return None
    mods = set()
    for part in parts[:-1]:
        mod = MODIFIER_NAMES.get(part.lower())
        if mod is None:
            return None
        mods.add(mod)
    vk = key_name_to_vk(parts[-1])
    if vk is None or vk in MODIFIER_VKS:
        return None
    return (frozenset(mods), vk)


def format_hotkey(mods, vk):
    """Written by Set Hotkeys: modifiers in a fixed order + hex VK code."""
    prefix = "".join(f"{m}+" for m in ("ctrl", "alt", "shift", "win") if m in mods)
    return f"{prefix}VK{vk:02X}"


# ============================================================================
# CONFIG - the app owns the layout, the user owns the values
# ============================================================================
# Reading tolerates any spacing. Whenever the app writes config.ini it renders
# the whole file from DEFAULT_CONFIG with the current values, so order,
# comments and alignment are always the reference ones. A file with an error
# or an unknown key is never rewritten: a value could be lost.

# Reference layout - also the default config (no hotkeys, Broadlink off,
# console hidden), created when config.ini is missing.
DEFAULT_CONFIG = r'''; =============================== CONFIG ====================================
; ADI Series Volume Controller - edit values while the app is closed, or use tray menu > Reload config.
; Format: key = value   (no quotes; units are given in the comments)
; Only the values are yours: the app restores this layout (order, comments, spacing).

[Device]
device_id = 72                  ; 71 -> ADI-2 DAC, 72 -> ADI-2 Pro, 73 -> ADI-2/4 Pro SE

[Mode]
default_output = 1/2            ; 1/2 -> Output 1/2 | 3/4 -> Output 3/4
split_mode = false              ; false -> single/cloned controller | true -> Controller 1 on default_output, Controller 2 on second output
; Note: In split_mode, each controller is linked to its own output, click toggles Mute instead of switching selected output,
; long click - if available - will swap/invert assigned output between controllers.
; You will need to set different hotkeys for your second controller. Toggle it any time from the tray menu.

[Controls]
default_db_out12 = -41.5        ; value in dB
default_db_out34 = -30.0        ; value in dB
danger_db_threshold = -6.0      ; at/above this (louder), any move resets to default instead of applying it
step_normal_db = 0.1            ; normal steps (value in dB)
fast_threshold_ms = 180         ; ticks closer together than this = fast spin (value in ms)
step_fast_db = 0.5              ; fast spin steps (value in dB)
step_click_db = 1.5             ; click + rotation (value in dB)

[Hotkeys]
; Controller 1. Set with tray menu > Set Hotkeys..., or by hand: [modifiers+]key
; eg: alt+8, ctrl+alt+K, F20, alt+VK38 (VK = Windows virtual-key code, hex).
; left, right and click are required - extended actions can be left empty.

controller1_left =                  ; left -> volume down (-0.1dB default)
controller1_right =                 ; right -> volume up (+0.1dB default)
controller1_click =                 ; click -> switch Output 1/2 and 3/4 (in default mode) or mute output (in split mode)

; Extended actions for controllers supporting more than 3 input states (ignore the values below if unused)
controller1_click_left =            ; click+left -> coarse step down (-1.5dB default)
controller1_click_right =           ; click+right -> coarse step up (+1.5dB default)
controller1_long_click =            ; long click -> mute or swap controllers depending on split_mode

[Split Mode Hotkeys]
; Controller 2
controller2_left =                        ; left -> volume down (-0.1dB default)
controller2_right =                       ; right -> volume up (+0.1dB default)
controller2_click =                       ; click -> mute selected output
; Extended actions for controllers supporting more than 3 input states (ignore the values below if unused)
controller2_click_left =                  ; click+left -> coarse step down (-1.5dB default)
controller2_click_right =                 ; click+right -> coarse step up (+1.5dB default)

[Background Resync]
; Catches drift from changes made outside this app, e.g. the front-panel encoder or the ADI-2 Remote app.
resync_interval_minutes = 30    ; 0 = disable

[Midi]
send_midi_exe = sendmidi.exe          ; in the app folder, or a full path
receive_midi_exe = receivemidi.exe    ; in the app folder, or a full path
; Retrieve your exact port name by running this command: sendmidi.exe list
midi_port_name = ADI-2 Pro Midi Port 1

[Broadlink IR]
; Optional, syncs the LCD "volume select" indicator.
; Requires a Broadlink IR hub (the VOL Push codes are built in, nothing to learn).
; Set use_broadlink to false to skip entirely (no paths or hardware infos needed then).
use_broadlink = false

; Install: pip install broadlink
; Run discovery: python -c "import broadlink; [print(f'Device: {d.devtype:#06x} | IP: {d.host[0]} | MAC: {d.mac.hex()}') for d in broadlink.discover()]"
broadlink_type = 0x649b
broadlink_host = 
broadlink_mac = 

[Debug]
show_console = false            ; true -> show log window
'''


def _parse_config_text(text, source="config.ini"):
    """-> {(section, key): value as typed}. Raises configparser.Error."""
    parser = configparser.ConfigParser(inline_comment_prefixes=(";",), interpolation=None)
    parser.optionxform = str
    parser.read_string(text, source=source)
    return {(s, k): v.strip() for s in parser.sections() for k, v in parser[s].items()}


TEMPLATE_DEFAULTS = _parse_config_text(DEFAULT_CONFIG, source="DEFAULT_CONFIG")


TEMPLATE_KEYS = {}   # section -> settings, from the reference layout
for _section, _key in TEMPLATE_DEFAULTS:
    TEMPLATE_KEYS.setdefault(_section, []).append(_key)


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _find_line(text, section, key=None):
    """Line number of a [section] header (key=None) or of a setting in it."""
    current = None
    for number, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            current = s[1:-1].strip()
            if key is None and current == section:
                return number
        elif key is not None and current == section and _line_key(s) == key:
            return number
    return None


def _at(number):
    return f"Line {number}    " if number else ""


def describe_parse_error(e):
    if isinstance(e, configparser.DuplicateOptionError):
        return [f"{_at(e.lineno)}{e.option}  \u2192  appears twice"]
    if isinstance(e, configparser.DuplicateSectionError):
        return [f"{_at(e.lineno)}[{e.section}]  \u2192  appears twice"]
    if isinstance(e, configparser.MissingSectionHeaderError):
        return [f"{_at(e.lineno)}{e.line.strip()}  \u2192  text before the first [section]"]
    if isinstance(e, configparser.ParsingError):
        lines = []
        for number, line in e.errors:
            if line[:1] in "'\"" and line[-1:] == line[:1]:
                line = line[1:-1]
            lines.append(f"{_at(number)}{line.replace(chr(92) + 'n', '').strip()}  \u2192  not a setting")
        return lines
    return [str(e)]


def describe_unknown(unknown, text):
    """One line per unknown setting, with the likely fix when it is obvious."""
    lines, bad_sections = [], []
    for section, key in unknown:
        if section not in TEMPLATE_KEYS:              # misnamed section: one line for it
            if section not in bad_sections:
                bad_sections.append(section)
                match = difflib.get_close_matches(section, list(TEMPLATE_KEYS), n=1, cutoff=0.6)
                hint = f"unknown section, did you mean [{match[0]}]?" if match else "unknown section"
                lines.append(f"{_at(_find_line(text, section))}[{section}]  \u2192  {hint}")
            continue
        home = [s for s, keys in TEMPLATE_KEYS.items() if key in keys]
        if home:
            hint = f"belongs in the [{home[0]}] section"
        else:
            match = difflib.get_close_matches(key, TEMPLATE_KEYS[section], n=1, cutoff=0.6)
            hint = f"did you mean {match[0]}?" if match else "unknown setting"
        lines.append(f"{_at(_find_line(text, section, key))}{key}  \u2192  {hint}")
    return lines


def read_config_file(path):
    """-> (values, problems). problems: readable lines (with line numbers) for
    anything that isn't a known setting - stray text, duplicate, unknown or
    misplaced setting. The values are only trusted when problems is empty."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return {}, [f"{os.path.basename(path)} can't be opened ({e})"]
    try:
        values = _parse_config_text(text)
    except configparser.Error as e:
        return {}, describe_parse_error(e)
    unknown = [(s, k) for (s, k) in values if (s, k) not in TEMPLATE_DEFAULTS]
    return ({sk: v for sk, v in values.items() if sk in TEMPLATE_DEFAULTS},
            describe_unknown(unknown, text))


class ConfigValueError(ValueError):
    """An invalid value, located by section/key so it can be shown with its line."""

    def __init__(self, section, key, reason):
        super().__init__(f"{key}: {reason}")
        self.section, self.key, self.reason = section, key, reason


class ConfigErrors(ValueError):
    """Every invalid value found in one pass."""

    def __init__(self, errors):
        super().__init__("; ".join(str(e) for e in errors))
        self.errors = errors


def describe_value_errors(e, values, path=None):
    errors = e.errors if isinstance(e, ConfigErrors) else [e]
    text = _read_text(path or CONFIG_PATH)
    lines = []
    for err in errors:
        if not isinstance(err, ConfigValueError):
            lines.append(str(err))
            continue
        raw = values.get((err.section, err.key), TEMPLATE_DEFAULTS.get((err.section, err.key), ""))
        setting = f"{err.key} = {raw}" if raw else err.key
        lines.append(f"{_at(_find_line(text, err.section, err.key))}{setting}  \u2192  {err.reason}")
    return lines


def check_values(values, path=None):
    """Problem lines for every invalid value, without applying anything."""
    try:
        App.__new__(App).apply_config(values)
    except ValueError as e:
        return describe_value_errors(e, values, path)
    return []


def _line_number(problem):
    m = re.match(r"Line (\d+)", problem)
    return int(m.group(1)) if m else 0


def errors_text(problems, name="config.ini", limit=3):
    """'config.ini contains errors' + the first ones (by line) + '+ N more errors'."""
    problems = sorted(problems, key=_line_number)
    shown, more = problems[:limit], len(problems) - limit
    text = (f"{name} contains {'an error' if len(problems) == 1 else 'errors'}\n\n"
            + "\n".join(shown))
    if more > 0:
        text += f"\n+ {more} more error{'s' if more > 1 else ''}"
    return text


class Config:
    """Typed access with clear messages. A missing key = reference default."""

    def __init__(self, values):
        self.values = values

    def get(self, section, key):
        return self.values.get((section, key), TEMPLATE_DEFAULTS[(section, key)])

    def _convert(self, section, key, kind, expected):
        raw = self.get(section, key)
        try:
            return kind(raw)
        except ValueError:
            raise ConfigValueError(section, key, f"expected {expected}") from None

    def getfloat(self, section, key):
        return self._convert(section, key, float, "a number")

    def getint(self, section, key):
        return self._convert(section, key, int, "a whole number")

    def getboolean(self, section, key):
        raw = self.get(section, key)
        if raw.lower() in ("true", "yes", "on", "1"):
            return True
        if raw.lower() in ("false", "no", "off", "0"):
            return False
        raise ConfigValueError(section, key, "expected true or false")


def _line_key(stripped):
    if not stripped or stripped[0] in ";#[" or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def replace_value(line, value):
    """Swaps only the value of a 'key = value   ; comment' line: key, spacing
    and the inline comment (kept at the same column) are untouched."""
    eq = line.index("=")
    prefix, rest = line[:eq + 1], line[eq + 1:]
    if rest[:1] in (" ", "\t"):
        prefix, rest = prefix + rest[0], rest[1:]
    new = prefix + value
    m = re.search(r"(?:^|(?<=\s));", rest)          # same rule as configparser
    if not m:
        return new
    column = len(prefix) + m.start()
    return new + " " * max(1, column - len(new)) + rest[m.start():]


def render_config(values):
    """The whole file: reference layout + values (missing = default)."""
    out, section = [], None
    for line in DEFAULT_CONFIG.split("\n"):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
        else:
            key = _line_key(stripped)
            if key is not None:
                line = replace_value(line, values.get((section, key), TEMPLATE_DEFAULTS[(section, key)]))
        out.append(line)
    return "\n".join(out)


def write_text_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)   # never a half-written config


RESTORED_NOTE = "Last working config restored (faulty config.ini saved as config.ini.bak)."


def backup_config():
    """Keeps the current file as config.ini.bak before the app replaces it."""
    try:
        shutil.copyfile(CONFIG_PATH, CONFIG_PATH + ".bak")
    except OSError:
        pass


def save_config_values(updates, fallback):
    """Set Hotkeys / Split mode: values of the file + updates, in the reference
    layout. fallback = settings in use, used instead of the file if it is
    missing or broken (the broken file is kept as .bak). Returns a note for
    the user when that happened, else None."""
    note = None
    if os.path.isfile(CONFIG_PATH):
        values, problems = read_config_file(CONFIG_PATH)
        if problems:
            backup_config()
            values, note = dict(fallback), f"{errors_text(problems)}\n\n{RESTORED_NOTE}"
    else:
        values = dict(fallback)
    values.update(updates)
    write_text_atomic(CONFIG_PATH, render_config(values))
    return note


def tidy_config(values):
    """Same values, reference layout: restores missing lines, drops extra
    blank/comment lines, realigns. Only for a file without error or unknown
    key. Returns the missing lines it restored (with their default value)."""
    restored = [f"[{s}] {k} = {d}" if d else f"[{s}] {k} (empty)"
                for (s, k), d in TEMPLATE_DEFAULTS.items() if (s, k) not in values]
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        return []
    text = render_config(values)
    if text != current:
        write_text_atomic(CONFIG_PATH, text)
        print("[Config] config.ini tidied (layout restored, values unchanged)")
    return restored


# ============================================================================
# SYSEX ENCODE / DECODE (formula verified byte-for-byte vs RME's examples)
# ============================================================================

def encode_channel_param(address, idx, val):
    v = val & 0xFFF
    b1 = ((address & 0x0F) << 3) | ((idx >> 2) & 0x07)
    b2 = ((idx & 0x03) << 5) | (((v >> 11) & 0x01) << 4) | ((v >> 7) & 0x0F)
    b3 = v & 0x7F
    return b1, b2, b3


def decode_channel_container(b1, b2, b3):
    address = (b1 >> 3) & 0x0F
    idx = ((b1 & 0x07) << 2) | ((b2 >> 5) & 0x03)
    v = (((b2 >> 4) & 0x01) << 11) | ((b2 & 0x0F) << 7) | (b3 & 0x7F)
    if v & 0x800:
        v -= 4096
    return address, idx, v


# ============================================================================
# PROTOCOL CONSTANTS
# ============================================================================

# RME - same across the ADI-2 family for shared SysEx functions (volume/mute).
ADDR_OUT12 = 3
ADDR_OUT34 = 9
IDX_VOLUME = 12
IDX_MUTE = 15

# Broadlink IR - VOL Push (LCD output-select indicator), generated
# mathematically from the RME IR command tables + NEC protocol (not a
# physical scan - identical for anyone with the same model).
# https://github.com/Thdub/generate_rme_broadlink_ir
BROADLINK_VOL_PUSH = {
    "71": "26004800000128941212123712121212123712121212121212121212123712121237123712121212121212371237123712121237121212121237121212121212123712121237123712000521",
    "72": "26004800000128941212123712121212123712121212121212121212123712121237123712121212121212371237123712121237121212371237121212121212123712121237121212000521",
    "73": "26004800000128941212123712121212123712121212121212121212123712121237123712121212121212371237123712121237123712121237121212121212123712121212123712000521",
}

# ============================================================================
# APPLICATION
# ============================================================================

class App:
    def __init__(self, values):
        self.apply_config(values)
        self.log_hotkeys()
        self.state = {
            "active_output": self.default_output,
            "db_out12": round(self.default_db_out12 * 10),
            "db_out34": round(self.default_db_out34 * 10),
            "muted_out12": False,
            "muted_out34": False,
            "last_tick_controller1": 0,
            "last_tick_controller2": 0,
        }
        self.tasks = queue.Queue()   # every action runs on one worker thread, in order
        self.hwnd = None             # tray window
        self.wizard_active = False   # Set Hotkeys window open: hotkeys disabled
        self.capture_queue = None    # set while Set Hotkeys waits for a key

    # ---------------------------------------------------------------- config

    def apply_config(self, values):
        """Validates and applies config values. Every invalid value is collected
        and raised together (ConfigErrors): the caller then keeps / restores
        the last working settings."""
        c = Config(values)
        errors = []

        def read(getter, section, key, fallback):
            try:
                return getter(section, key)
            except ConfigValueError as e:
                errors.append(e)
                return fallback

        def check(ok, section, key, reason):
            if not ok:
                errors.append(ConfigValueError(section, key, reason))

        self.device_id = c.get("Device", "device_id")
        valid_id = re.fullmatch(r"[0-9A-Fa-f]{2}", self.device_id)
        check(valid_id, "Device", "device_id", "expected 71, 72 or 73")
        self.device_id_int = int(self.device_id, 16) if valid_id else 0

        self.default_output = c.get("Mode", "default_output")
        check(self.default_output in ("1/2", "3/4"), "Mode", "default_output", "expected 1/2 or 3/4")
        self.split_mode = read(c.getboolean, "Mode", "split_mode", False)

        self.default_db_out12 = read(c.getfloat, "Controls", "default_db_out12", 0.0)
        self.default_db_out34 = read(c.getfloat, "Controls", "default_db_out34", 0.0)
        self.danger_db_threshold = read(c.getfloat, "Controls", "danger_db_threshold", 0.0)
        self.step_normal_db = read(c.getfloat, "Controls", "step_normal_db", 0.0)
        self.fast_threshold_ms = read(c.getint, "Controls", "fast_threshold_ms", 0)
        self.step_fast_db = read(c.getfloat, "Controls", "step_fast_db", 0.0)
        self.step_click_db = read(c.getfloat, "Controls", "step_click_db", 0.0)

        self.resync_interval_minutes = read(c.getint, "Background Resync", "resync_interval_minutes", 0)
        check(self.resync_interval_minutes >= 0, "Background Resync", "resync_interval_minutes", "expected 0 or more")

        send_raw = c.get("Midi", "send_midi_exe")
        receive_raw = c.get("Midi", "receive_midi_exe")
        self.send_midi_exe = send_raw if os.path.isabs(send_raw) else os.path.join(APP_DIR, send_raw)
        self.receive_midi_exe = receive_raw if os.path.isabs(receive_raw) else os.path.join(APP_DIR, receive_raw)
        self.midi_port_name = c.get("Midi", "midi_port_name")
        check(self.midi_port_name, "Midi", "midi_port_name", "must not be empty")

        self.use_broadlink = read(c.getboolean, "Broadlink IR", "use_broadlink", False)
        self.broadlink_type = c.get("Broadlink IR", "broadlink_type")
        self.broadlink_host = c.get("Broadlink IR", "broadlink_host")
        self.broadlink_mac = c.get("Broadlink IR", "broadlink_mac")
        if self.use_broadlink:
            check(re.fullmatch(r"(0x)?[0-9A-Fa-f]+", self.broadlink_type), "Broadlink IR", "broadlink_type",
                  "expected a hex code, eg 0x649b")
            check(self.broadlink_host, "Broadlink IR", "broadlink_host", "needed when use_broadlink = true")
            check(re.fullmatch(r"[0-9A-Fa-f]{12}", self.broadlink_mac), "Broadlink IR", "broadlink_mac",
                  "expected 12 hex digits")
        self.broadlink_vol_select_code = BROADLINK_VOL_PUSH.get(self.device_id, BROADLINK_VOL_PUSH["72"])

        self.show_console = read(c.getboolean, "Debug", "show_console", False)
        if errors:
            raise ConfigErrors(errors)

        self.danger_x10 = round(self.danger_db_threshold * 10)
        self.step_normal_x10 = round(self.step_normal_db * 10)
        self.step_fast_x10 = round(self.step_fast_db * 10)
        self.step_click_x10 = round(self.step_click_db * 10)
        self.hotkeys_raw = {k: c.get(hotkey_section(k), k) for k in HOTKEY_KEYS}
        self.hotkeys = {k: parse_hotkey(v) for k, v in self.hotkeys_raw.items()}
        self.values = dict(values)

    def log_hotkeys(self):
        for k in HOTKEY_KEYS:
            print(f"[Config] {k} = '{self.hotkeys_raw[k]}' -> {self.hotkeys[k]}")

    def hotkeys_unset(self):
        return not any(self.hotkeys_raw[k] for k in BASE_KEYS)

    def missing_base_hotkeys(self):
        """Controller 1 left/right/click are mandatory (empty or invalid = missing)."""
        return [k for k in BASE_KEYS if self.hotkeys[k] is None]

    def hotkey_issues(self):
        issues = [f"{k} is empty - Controller 1 needs left, right and click"
                  for k in BASE_KEYS if not self.hotkeys_raw[k]]
        for key, raw in self.hotkeys_raw.items():
            if raw and self.hotkeys[key] is None:
                issues.append(f"{key} = {raw} is not a valid hotkey (ignored)")
        for controller in ("controller1", "controller2"):
            seen = {}
            for key, parsed in self.hotkeys.items():
                if key.startswith(controller) and parsed:
                    if parsed in seen:
                        issues.append(f"{key} uses the same key as {seen[parsed]} ({self.hotkeys_raw[key]})")
                    else:
                        seen[parsed] = key
        if self.split_mode:
            controller1 = {p: k for k, p in self.hotkeys.items() if k.startswith("controller1") and p}
            for key, parsed in self.hotkeys.items():
                if key.startswith("controller2") and parsed in controller1:
                    issues.append(f"{key} uses the same key as {controller1[parsed]} "
                                  "- split mode needs different keys on each controller")
            if not any(self.hotkeys[k] for k in ("controller2_left", "controller2_right", "controller2_click")):
                issues.append("split mode is on, but Controller 2 has no hotkeys")
        return issues

    def reload_config(self, explicit=False):
        """Worker task: tray > Reload config, or after Set Hotkeys. An unreadable
        file, an unknown key, an invalid value or a missing Controller 1 base
        hotkey is refused: the last working config is restored."""
        if not os.path.isfile(CONFIG_PATH):
            write_text_atomic(CONFIG_PATH, render_config(self.values))
            warn_once("config.ini was missing - recreated from the settings in use.", MB_ICONINFORMATION)
            return
        values, problems = read_config_file(CONFIG_PATH)
        problems += check_values(values) if values else []
        if problems:
            return self.restore_config_file(errors_text(problems))
        backup = dict(self.__dict__)
        self.apply_config(values)          # validated just above
        if self.missing_base_hotkeys():
            issues = self.hotkey_issues()
            self.__dict__.update(backup)
            return self.restore_config_file("Hotkey issues in config.ini:\n\n"
                                            + "\n".join(f"- {i}" for i in issues))
        self.log_hotkeys()
        print("[Config] Reloaded")
        self.notify_ui()
        report_config(self, tidy_config(values), reloaded=explicit)

    def restore_config_file(self, problem):
        """Refused reload: the file gets the settings in use back, so app and
        file stay identical. The user's version is kept as config.ini.bak."""
        backup_config()
        write_text_atomic(CONFIG_PATH, render_config(self.values))
        print("[Config] Refused - last working config restored")
        warn_once(f"{problem}\n\n{RESTORED_NOTE}")

    # ------------------------------------------------------------- MIDI I/O

    def send_channel_param(self, address, idx, val):
        b1, b2, b3 = encode_channel_param(address, idx, val)
        hexstr = f"{b1:02X} {b2:02X} {b3:02X}"
        print(f"[MIDI] send addr={address} idx={idx} val={val} -> {hexstr}")
        subprocess.run(
            [self.send_midi_exe, "dev", self.midi_port_name, "system-exclusive", "hex",
             "00", "20", "0D", self.device_id, "02", *hexstr.split()],
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
        )

    def sync_all_from_device(self):
        """One 'send all settings' request, short listen window, then the
        listening port is closed: the device stops broadcasting."""
        print("[MIDI] Syncing from device...")
        lines = []
        try:
            proc = subprocess.Popen(
                [self.receive_midi_exe, "dev", self.midi_port_name],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, creationflags=NO_WINDOW,
            )
        except Exception as e:
            print(f"[MIDI] Could not start receivemidi.exe: {e}")
            return
        threading.Thread(target=lambda: lines.extend(proc.stdout), daemon=True).start()

        time.sleep(0.1)
        subprocess.run(
            [self.send_midi_exe, "dev", self.midi_port_name, "system-exclusive", "hex",
             "00", "20", "0D", self.device_id, "03", "09"],
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
        )
        time.sleep(1.2)

        proc.terminate()   # never leave the port open - force-kill if needed
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)

        tokens = []
        for tok in "".join(lines).split():
            if len(tok) == 2:
                try:
                    tokens.append(int(tok, 16))
                except ValueError:
                    pass

        i = 0
        while i <= len(tokens) - 5:
            if tokens[i:i + 4] == [0x00, 0x20, 0x0D, self.device_id_int] and tokens[i + 4] in (0x01, 0x02):
                p = i + 5
                while p + 2 < len(tokens):
                    b1, b2, b3 = tokens[p], tokens[p + 1], tokens[p + 2]
                    if ((b1 >> 3) & 0x0F) != 12:
                        addr, idx, val = decode_channel_container(b1, b2, b3)
                        if addr == ADDR_OUT12 and idx == IDX_VOLUME:
                            self.state["db_out12"] = val
                        elif addr == ADDR_OUT12 and idx == IDX_MUTE:
                            self.state["muted_out12"] = (val != 0)
                        elif addr == ADDR_OUT34 and idx == IDX_VOLUME:
                            self.state["db_out34"] = val
                        elif addr == ADDR_OUT34 and idx == IDX_MUTE:
                            self.state["muted_out34"] = (val != 0)
                    p += 3
            i += 1
        print(f"[MIDI] Sync done. State: {self.state}")

    def send_broadlink_volume_select(self):
        if not self.use_broadlink:
            return
        try:
            import broadlink
            from broadlink.const import DEFAULT_PORT
            dev = broadlink.gendevice(int(self.broadlink_type, 16),
                                      (self.broadlink_host, DEFAULT_PORT),
                                      bytearray.fromhex(self.broadlink_mac))
            dev.auth()
            dev.send_data(bytearray.fromhex(self.broadlink_vol_select_code))
            print("[Broadlink] IR sent")
        except Exception as e:
            print(f"[Broadlink] Error: {e}")

    # ---------------------------------------------------------------- logic

    def get_controller2_output(self):
        if not self.split_mode:
            return self.state["active_output"]
        return "3/4" if self.state["active_output"] == "1/2" else "1/2"

    def apply_volume_step(self, target_output, direction, step_x10):
        out12 = target_output == "1/2"
        db_key = "db_out12" if out12 else "db_out34"
        muted_key = "muted_out12" if out12 else "muted_out34"
        addr = ADDR_OUT12 if out12 else ADDR_OUT34
        default_db = round((self.default_db_out12 if out12 else self.default_db_out34) * 10)

        if self.state[muted_key]:   # changing the volume unmutes that output
            self.state[muted_key] = False
            self.send_channel_param(addr, IDX_MUTE, 0)

        current = self.state[db_key]
        # At/above the danger threshold, any move resets to default: a quick
        # flick in either direction gets back to a safe level.
        new_val = default_db if current >= self.danger_x10 else current + direction * step_x10
        self.state[db_key] = new_val
        self.send_channel_param(addr, IDX_VOLUME, new_val)

    # now_ms = time of the key press, taken in the hook (not when the queued
    # action runs), so fast-spin detection stays accurate.

    def adjust_volume(self, direction, now_ms):
        delta = now_ms - self.state["last_tick_controller1"]
        self.state["last_tick_controller1"] = now_ms
        step = self.step_fast_x10 if 0 < delta < self.fast_threshold_ms else self.step_normal_x10
        self.apply_volume_step(self.state["active_output"], direction, step)

    def adjust_volume_coarse(self, direction, now_ms):
        self.state["last_tick_controller1"] = now_ms
        self.apply_volume_step(self.state["active_output"], direction, self.step_click_x10)

    def adjust_volume_controller2(self, direction, now_ms):
        delta = now_ms - self.state["last_tick_controller2"]
        self.state["last_tick_controller2"] = now_ms
        step = self.step_fast_x10 if 0 < delta < self.fast_threshold_ms else self.step_normal_x10
        self.apply_volume_step(self.get_controller2_output(), direction, step)

    def adjust_volume_coarse_controller2(self, direction, now_ms):
        self.state["last_tick_controller2"] = now_ms
        self.apply_volume_step(self.get_controller2_output(), direction, self.step_click_x10)

    def toggle_mute_for_output(self, target_output):
        muted_key = "muted_out12" if target_output == "1/2" else "muted_out34"
        addr = ADDR_OUT12 if target_output == "1/2" else ADDR_OUT34
        self.state[muted_key] = not self.state[muted_key]
        self.send_channel_param(addr, IDX_MUTE, 1 if self.state[muted_key] else 0)

    def toggle_mute(self):
        self.toggle_mute_for_output(self.state["active_output"])

    def single_click_controller2(self):
        self.toggle_mute_for_output(self.get_controller2_output())

    def switch_output(self):
        self.state["active_output"] = "3/4" if self.state["active_output"] == "1/2" else "1/2"
        self.notify_ui()
        self.send_broadlink_volume_select()

    def single_click(self):
        if self.split_mode:
            self.toggle_mute()
        else:
            self.switch_output()

    def long_click(self):
        if self.split_mode:
            self.switch_output()   # swaps the outputs of Controller 1 and 2
        else:
            self.toggle_mute()

    def realign_lcd(self):
        """Tray action: one VOL Push, app state untouched (fixes LCD drift)."""
        print("[Broadlink] Realign LCD")
        self.send_broadlink_volume_select()

    # ------------------------------------------------------------ worker / UI

    def worker_loop(self):
        """Runs every queued action in order, off the hook thread: keeps the
        hook fast and avoids races between hotkeys, sync and resync."""
        while True:
            task = self.tasks.get()
            if task is None:
                return
            try:
                task()
            except Exception as e:
                print(f"[Worker] Error: {e}")

    def tooltip_text(self):
        if self.split_mode:
            return (f"{TITLE} - Controller 1: {self.state['active_output']}"
                    f" | Controller 2: {self.get_controller2_output()}")
        return f"{TITLE} - Active: {self.state['active_output']}"

    def notify_ui(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_UPDATE_TIP, 0, 0)

    # --------------------------------------------------------------- hotkeys

    def handle_key(self, mods, vk, now_ms):
        """Called from the hook: match only, queue the action, return fast.
        True if the key is one of ours (so the hook blocks it)."""
        for key, parsed in self.hotkeys.items():
            if parsed is not None and parsed == (mods, vk):
                self.tasks.put(lambda: self.run_action(key, mods, vk, now_ms))
                return True
        return False

    def run_action(self, key, mods, vk, now_ms):
        print(f"[Hotkey] {key} ({format_hotkey(mods, vk)})")
        actions = {
            "controller1_left": lambda: self.adjust_volume(-1, now_ms),
            "controller1_right": lambda: self.adjust_volume(1, now_ms),
            "controller1_click": self.single_click,
            "controller1_click_left": lambda: self.adjust_volume_coarse(-1, now_ms),
            "controller1_click_right": lambda: self.adjust_volume_coarse(1, now_ms),
            "controller1_long_click": self.long_click,
            "controller2_left": lambda: self.adjust_volume_controller2(-1, now_ms),
            "controller2_right": lambda: self.adjust_volume_controller2(1, now_ms),
            "controller2_click": self.single_click_controller2,
            "controller2_click_left": lambda: self.adjust_volume_coarse_controller2(-1, now_ms),
            "controller2_click_right": lambda: self.adjust_volume_coarse_controller2(1, now_ms),
        }
        actions[key]()


# ============================================================================
# KEYBOARD HOOK
# ============================================================================

APP = None
mod_state = {"ctrl": False, "alt": False, "shift": False, "win": False}
suppressed_vks = set()   # keys we blocked: their key-up is blocked too
keys_down = set()        # to tell a real press from key auto-repeat


def mask_menu_key():
    """Blocking an Alt/Win combo leaves a lone Alt/Win press behind, which
    would open the menu bar / Start menu on release. A dummy unassigned key
    press in between cancels that."""
    user32.keybd_event(VK_MENU_MASK, 0, 0, 0)
    user32.keybd_event(VK_MENU_MASK, 0, KEYEVENTF_KEYUP, 0)


def _block(vk, mods):
    suppressed_vks.add(vk)
    if mods & {"alt", "win"}:
        mask_menu_key()
    return 1   # never reaches the foreground app (DAW...)


def hook_proc(nCode, wParam, lParam):
    # Must stay fast: Windows silently removes low-level hooks that take too
    # long, so actions are queued to the worker thread instead of run here.
    if nCode >= 0 and APP is not None:
        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = kb.vkCode
        is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)

        if vk == VK_MENU_MASK:
            pass                                   # our own dummy key: ignore
        elif vk in MODIFIER_VKS:
            mod_state[VK_TO_MOD[vk]] = is_down
        elif is_down:
            repeat = vk in keys_down
            keys_down.add(vk)
            mods = frozenset(m for m in ("ctrl", "alt", "shift", "win") if mod_state[m])
            capture = APP.capture_queue
            if capture is not None:                # Set Hotkeys is waiting for a key
                if not repeat:
                    capture.put("ESC" if vk == VK_ESCAPE and not mods else (mods, vk))
                return _block(vk, mods)
            if not APP.wizard_active and APP.handle_key(mods, vk, time.monotonic() * 1000):
                return _block(vk, mods)
        else:
            keys_down.discard(vk)
            if vk in suppressed_vks:
                suppressed_vks.discard(vk)
                return 1

    return user32.CallNextHookEx(None, nCode, wParam, lParam)


_HOOK_PTR = LowLevelKeyboardProc(hook_proc)


def install_hook():
    hook_id = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _HOOK_PTR, kernel32.GetModuleHandleW(None), 0)
    if not hook_id:
        raise ctypes.WinError(ctypes.get_last_error())
    return hook_id


# ============================================================================
# MESSAGES
# ============================================================================

MB_OK = 0x0
MB_RETRYCANCEL = 0x5
MB_YESNO = 0x4
MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30
MB_ICONINFORMATION = 0x40
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000
IDRETRY = 4
IDYES = 6


def message_box(text, flags=MB_ICONWARNING):
    return user32.MessageBoxW(None, text, TITLE, flags | MB_SETFOREGROUND | MB_TOPMOST)


_warning_open = threading.Lock()


def warn_once(text, flags=MB_ICONWARNING, on_yes=None):
    """Non-blocking box (own thread), one at a time: no pile-up."""
    if not _warning_open.acquire(blocking=False):
        return

    def show():
        try:
            if message_box(text, flags) == IDYES and on_yes:
                on_yes()
        finally:
            _warning_open.release()
    threading.Thread(target=show, daemon=True).start()


def warn_hotkey_issues(app, issues):
    warn_once("Hotkey issues in config.ini:\n\n" + "\n".join(f"- {i}" for i in issues)
              + "\n\nRun Set Hotkeys now?", MB_YESNO | MB_ICONWARNING, on_yes=lambda: start_wizard(app))


def report_config(app, restored=(), reloaded=False, check_hotkeys=True):
    """One box summing up what needs attention after config.ini was loaded."""
    parts = ["config.ini reloaded."] if reloaded else []
    if restored:
        parts.append("Missing lines restored with their default value:\n"
                     + "\n".join(f"- {r}" for r in restored))
    issues = app.hotkey_issues() if check_hotkeys else []
    if issues:
        parts.append("Hotkey issues:\n" + "\n".join(f"- {i}" for i in issues))
        warn_once("\n\n".join(parts) + "\n\nRun Set Hotkeys now?", MB_YESNO | MB_ICONWARNING,
                  on_yes=lambda: start_wizard(app))
    elif parts:
        warn_once("\n\n".join(parts), MB_ICONINFORMATION)


# ============================================================================
# UI STYLE - one look for every window: shades of gray, native fonts
# ============================================================================
# Two sizes, two weights, Consolas for codes. Follows the Windows dark mode.

PALETTES = {
    "light": {"bg": "#FAFAFA", "fg": "#1F1F1F", "fg2": "#6B6B6B", "hover": "#EDEDED"},
    "dark": {"bg": "#202020", "fg": "#EDEDED", "fg2": "#9A9A9A", "hover": "#2D2D2D"},
}


def windows_dark_mode():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


class Style:
    """Palette, fonts and the few widgets every window is made of."""

    def __init__(self, root):
        import tkinter as tk
        import tkinter.font as tkfont
        self.tk = tk
        families = set(tkfont.families(root))

        def first(*names, default=None):
            return next((n for n in names if n in families), default)
        base = first("Segoe UI Variable Text", "Segoe UI",
                     default=tkfont.nametofont("TkDefaultFont").actual("family"))
        strong = first("Segoe UI Variable Text Semibold", "Segoe UI Semibold")
        code = first("Consolas", default=tkfont.nametofont("TkFixedFont").actual("family"))
        dark = windows_dark_mode()
        self.c = PALETTES["dark" if dark else "light"]
        self.fonts = {
            "body": (base, 10),
            "note": (base, 10),
            "strong": (strong, 10) if strong else (base, 10, "bold"),
            "main": (strong, 13) if strong else (base, 13, "bold"),
            "code": (code, 10),
        }
        root.configure(bg=self.c["bg"])
        if dark:
            _dark_title_bar(root)

    def frame(self, parent, **options):
        return self.tk.Frame(parent, bg=self.c["bg"], **options)

    def label(self, parent, text="", kind="body"):
        fg = self.c["fg2"] if kind == "note" else self.c["fg"]
        return self.tk.Label(parent, text=text, font=self.fonts[kind], fg=fg, bg=self.c["bg"],
                             justify="left", anchor="w", wraplength=560)

    def rich(self, parent, text, kind="note"):
        """Multi-line text where *word* is shown in italics (setting names)."""
        box = self.frame(parent)
        font = self.fonts[kind]
        italic = (font[0], font[1], "italic")
        fg = self.c["fg2"] if kind == "note" else self.c["fg"]
        for line in text.split("\n"):
            row = self.frame(box)
            row.pack(anchor="w")
            for i, part in enumerate(re.split(r"\*([^*]+)\*", line)):
                if part:
                    self.tk.Label(row, text=part, font=italic if i % 2 else font, fg=fg, bg=self.c["bg"],
                                  padx=0, bd=0).pack(side="left")
        return box

    def option(self, parent, title, precision, command):
        """Clickable row: title, and its precision below. Light gray on hover."""
        row = self.frame(parent, cursor="hand2", padx=10, pady=6)
        row.pack(fill="x", pady=1)
        parts = [row, self.label(row, f"\u203a  {title}", "strong")]
        parts[1].pack(fill="x")
        if precision:
            parts.append(self.label(row, f"    {precision}", "note"))
            parts[2].pack(fill="x")
        self._clickable(parts, command)

    def link(self, parent, text, command):
        """Small flat text button (footer: Skip, Cancel, Close)."""
        label = self.label(parent, text, "note")
        label.config(padx=10, pady=4, cursor="hand2")
        self._clickable([label], command)
        return label

    def _clickable(self, widgets, command):
        def paint(color):
            for widget in widgets:
                widget.config(bg=color)
        for widget in widgets:
            widget.bind("<Enter>", lambda _e: paint(self.c["hover"]))
            widget.bind("<Leave>", lambda _e: paint(self.c["bg"]))
            widget.bind("<Button-1>", lambda _e: command())


def _dark_title_bar(root):
    """Dark title bar to match (Windows 10 20H1+ / 11); ignored elsewhere."""
    try:
        root.update_idletasks()
        hwnd = user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        ctypes.WinDLL("dwmapi").DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def new_window(title):
    """-> (root, style, content frame) - the shared frame of every window."""
    import tkinter as tk
    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    ui = Style(root)
    content = ui.frame(root, padx=26, pady=22)
    content.pack(fill="both", expand=True)
    return root, ui, content


def ask_choice(text, choices):
    """Problem text, then one clickable row per option (title + precision).
    choices: [(title, precision), ...]. Returns the chosen title (the last
    one if the window is closed). tkinter is only loaded when needed, so a
    normal startup stays as fast as before."""
    titles = [title for title, _ in choices]
    try:
        import tkinter  # noqa: F401
    except ImportError:                    # fallback: Retry / Cancel box
        ok = message_box(text + "\n\nRetry, or Cancel to quit.", MB_RETRYCANCEL | MB_ICONERROR) == IDRETRY
        return titles[0] if ok else titles[-1]

    result = {"choice": titles[-1]}
    root, ui, content = new_window(TITLE)
    heading, _, details = text.partition("\n\n")
    ui.label(content, heading, "main").pack(anchor="w")
    if details:
        kind = "code" if details.startswith("Line") else "body"
        ui.label(content, details, kind).pack(anchor="w", pady=(10, 0))
    options = ui.frame(content)
    options.pack(fill="x", pady=(18, 0))

    def pick(title):
        result["choice"] = title
        root.destroy()
    for title, precision in choices:
        ui.option(options, title, precision, lambda t=title: pick(t))
    root.bind("<Escape>", lambda _e: root.destroy())
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.lift()
    root.focus_force()
    root.mainloop()
    return result["choice"]


def fatal(text):
    """Blocking error box (the compiled app has no console), then exit."""
    print(f"[Startup] ERROR: {text}")
    message_box(text, MB_ICONERROR)
    sys.exit(1)


# ============================================================================
# SET HOTKEYS (wizard window - tkinter, runs on its own thread)
# ============================================================================

SLOT_TEXT = {
    "left": ("Turn LEFT", "volume down"),
    "right": ("Turn RIGHT", "volume up"),
    "click": ("CLICK", "switch output / mute"),
    "click_left": ("CLICK + turn LEFT", "coarse step down"),
    "click_right": ("CLICK + turn RIGHT", "coarse step up"),
    "long_click": ("LONG CLICK", "mute / swap controllers"),
}


class Wizard:
    """Nothing is written to config.ini until the last step. Cancel (or Esc
    while waiting for a key) leaves everything untouched."""

    def __init__(self, app, first_launch=False):
        self.app = app
        self.first_launch = first_launch   # no hotkeys yet: leaving means quitting the app
        self.stage = "controller1"         # controller1 / split / controller2 / done
        self.result = {}      # "controller1_left" -> "alt+VK31" ("" = unused)
        self.used = {}        # code -> hotkey key, for duplicate checks
        self.controller, self.slots, self.index = 1, [], 0
        self.accepting = False
        self.polling = False

        root, ui, content = new_window(f"{TITLE} - Set Hotkeys")
        self.root, self.ui = root, ui
        root.minsize(540, 0)
        root.protocol("WM_DELETE_WINDOW", self.escape)
        self.lbl_head = ui.label(content, kind="note")
        self.lbl_main = ui.label(content, kind="main")
        self.sub = ui.frame(content)
        self.status = ui.frame(content)
        self.lbl_symbol = ui.label(self.status, kind="main")
        self.lbl_status = ui.label(self.status, kind="code")
        self.lbl_status.pack(side="left")
        self.lbl_head.pack(anchor="w")
        self.lbl_main.pack(anchor="w", pady=(4, 0))
        self.options = ui.frame(content)
        self.footer = ui.frame(content)

    def run(self):
        if not self.first_launch and not self.app.hotkeys_unset():
            self.ask_keep_or_restart()
        else:
            self.ask_actions(1)
        self.root.mainloop()

    # ---------------------------------------------------------------- screens

    def escape_link(self):
        """Controller 1 in progress: Cancel (or Quit on first launch) - nothing saved.
        Controller 2 in progress: Controller 1 is kept, Controller 2 skipped."""
        if self.stage == "controller1":
            return ("Quit" if self.first_launch else "Cancel", self.cancel)
        if self.stage == "controller2":
            return ("Skip Controller 2", self.skip_controller2)
        return None

    def escape(self):
        """Esc while waiting for a key, or the window's close button."""
        if self.stage == "controller1":
            self.cancel()
        elif self.stage in ("split", "controller2"):
            self.skip_controller2()
        else:
            self.close()

    def screen(self, head, main, sub="", status="", options=(), links=(), cancel=True, strong=False):
        self.lbl_head.config(text=head)
        self.lbl_head.pack_forget()        # no header line on question screens
        if head:
            self.lbl_head.pack(anchor="w", before=self.lbl_main)
        self.lbl_main.config(text=main)
        for widget in self.sub.winfo_children() + self.options.winfo_children() + self.footer.winfo_children():
            widget.destroy()
        for widget in (self.sub, self.status, self.options, self.footer):
            widget.pack_forget()           # empty parts take no room (tk frames don't shrink by themselves)
        if sub:
            self.ui.rich(self.sub, sub).pack(anchor="w")
            self.sub.pack(anchor="w", pady=(2, 0))
        if status:
            self.status.pack(anchor="w", pady=(14, 0))
            self.set_status(status, strong)
        if options:
            self.options.pack(fill="x", pady=(14, 0))
            for title, precision, command in options:
                self.ui.option(self.options, title, precision, command)
        escape = self.escape_link() if cancel else None
        items = list(links) + ([escape] if escape else [])
        if items:
            self.footer.pack(fill="x", pady=(18, 0))
        for text, command in reversed(items):
            self.ui.link(self.footer, text, command).pack(side="right")

    def set_status(self, text, strong=False, symbol=""):
        """symbol: a big \u2713 / \u2715 in front of the text, so success and error
        are told apart at a glance."""
        self.lbl_symbol.pack_forget()
        if symbol:
            self.lbl_symbol.config(text=symbol)
            self.lbl_symbol.pack(side="left", padx=(0, 10), before=self.lbl_status)
        self.lbl_status.config(text=text, fg=self.ui.c["fg"] if strong else self.ui.c["fg2"])

    def ask_keep_or_restart(self):
        """Controller 1 already has hotkeys from a previous run: offer to keep
        them and only set up Controller 2, instead of starting the whole
        wizard over."""
        self.stage = "controller1"
        self.screen("", "Controller 1 already has assigned hotkeys",
                    options=[("Set up second set of controls only", "keep Controller 1 hotkeys",
                              self.keep_controller1),
                             ("Start over", "reset any assigned hotkeys", lambda: self.ask_actions(1))])

    def keep_controller1(self):
        """Nothing about Controller 1 is touched or re-saved: its current
        hotkeys are only loaded into self.used, so Controller 2 can't
        duplicate them."""
        for key in HOTKEY_KEYS:
            if key.startswith("controller1"):
                parsed = self.app.hotkeys[key]
                if parsed is not None:
                    self.used[format_hotkey(*parsed)] = key
        self.ask_actions(2)

    def ask_actions(self, controller):
        self.stop_capture()
        self.stage = f"controller{controller}"
        extended = "also click+left, click+right, long click"
        if controller == 2:
            extended = "also click+left, click+right  (long click is shared: only 5 to set)"
        self.screen("", f"How many actions does Controller {controller} have?",
                    options=[("3 actions", "left, right, click", lambda: self.begin(controller, False)),
                             ("6 actions", extended, lambda: self.begin(controller, True))])

    def begin(self, controller, extended):
        names = list(SLOT_NAMES if controller == 1 else SLOT_NAMES[:5])   # no long click on Controller 2
        if not extended:
            for name in names[3:]:
                self.result[f"controller{controller}_{name}"] = ""
            names = names[:3]
        if controller == 1:
            self.used = {}
        self.controller, self.slots, self.index = controller, names, 0
        self.next_slot()

    def current_key(self):
        return f"controller{self.controller}_{self.slots[self.index]}"

    def next_slot(self):
        if self.index >= len(self.slots):
            self.stop_capture()
            if self.controller == 1:
                self.ask_split()
            else:
                self.finish(split=True)
            return
        name = self.slots[self.index]
        main, sub = SLOT_TEXT[name]
        optional = name not in BASE_SLOTS           # left / right / click can't be skipped
        esc = {"controller1": "Esc to quit" if self.first_launch else "Esc to cancel",
               "controller2": "Esc to skip Controller 2"}[self.stage]
        self.screen(f"Controller {self.controller}  \u00b7  step {self.index + 1} of {len(self.slots)}",
                    main, sub, f"waiting for input...   ({esc})",
                    links=[("Skip", self.skip)] if optional else [])
        self.start_capture()

    def ask_split(self):
        self.stage = "split"
        self.screen("", "Set up a second controller and enable split mode?",
                    "In split mode, each controller is linked to its own output,\n"
                    "Controller 1 being assigned to *default_output*.", cancel=False,
                    options=[("No", "one set of controls only", lambda: self.finish(split=False)),
                             ("Yes", "set up second controller", lambda: self.ask_actions(2))])

    def finish(self, split):
        self.stop_capture()
        self.stage = "done"
        updates = {(hotkey_section(k), k): v for k, v in self.result.items()}
        updates[("Mode", "split_mode")] = "true" if split else "false"
        try:
            note = save_config_values(updates, fallback=self.app.values)
        except OSError as e:
            self.screen("Error", "Could not save", str(e), links=[("Close", self.close)], cancel=False)
            return
        if note:
            warn_once(note)
        self.app.tasks.put(self.app.reload_config)
        summary = []
        for n in (1, 2):
            keys = [k for k in HOTKEY_KEYS if k in self.result and k.startswith(f"controller{n}")]
            if keys:
                summary.append(("\n" if summary else "") + f"Controller {n}")
                summary += [f"  {SLOT_LABELS[k.split('_', 1)[1]]:<14}{self.result[k] or 'unused'}" for k in keys]
        self.screen("Done", "Saved - hotkeys are active", f"Split mode {'on' if split else 'off'}",
                    "\n".join(summary), links=[("Close", self.close)], cancel=False, strong=True)

    # ---------------------------------------------------------------- capture

    def start_capture(self):
        if self.app.capture_queue is None:
            self.app.capture_queue = queue.Queue()
        self.drain()
        self.accepting = True
        if not self.polling:
            self.polling = True
            self.root.after(30, self.poll)

    def stop_capture(self):
        self.accepting = False
        self.app.capture_queue = None

    def drain(self):
        q = self.app.capture_queue
        while q is not None:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def poll(self):
        q = self.app.capture_queue
        if q is None:
            self.polling = False
            return
        while True:
            try:
                event = q.get_nowait()
            except queue.Empty:
                break
            if not self.accepting:
                continue                   # e.g. extra wheel ticks during the confirmation
            if event == "ESC":
                self.escape()
                return
            self.captured(format_hotkey(*event))
        self.root.after(30, self.poll)

    def captured(self, code):
        if code in self.used:
            self.set_status(f"Already used   {code} is {slot_label(self.used[code])}", True, "\u2715")
            return
        key = self.current_key()
        self.result[key] = code
        self.used[code] = key
        self.accepting = False
        self.set_status(f"Detected   {code}", True, "\u2713")
        self.root.after(700, self.advance)

    def advance(self):
        self.drain()
        self.index += 1
        self.next_slot()

    def skip(self):
        if not self.accepting or self.slots[self.index] in BASE_SLOTS:
            return
        self.result[self.current_key()] = ""
        self.accepting = False
        self.index += 1
        self.next_slot()

    def skip_controller2(self):
        """Keeps Controller 1 (already complete), drops what was captured for Controller 2."""
        self.result = {k: v for k, v in self.result.items() if not k.startswith("controller2")}
        self.finish(split=False)

    def cancel(self):
        """Nothing saved. On first launch there are no hotkeys: the app quits."""
        self.stop_capture()
        self.root.destroy()
        if self.first_launch and self.app.hwnd:
            user32.PostMessageW(self.app.hwnd, WM_COMMAND, ID_QUIT, 0)

    def close(self):
        self.stop_capture()
        self.root.destroy()


def start_wizard(app, first_launch=False):
    if app.wizard_active:
        return
    app.wizard_active = True
    threading.Thread(target=_wizard_thread, args=(app, first_launch), daemon=True).start()


def _wizard_thread(app, first_launch):
    try:
        Wizard(app, first_launch).run()
    except ImportError:
        message_box("Set Hotkeys needs tkinter, which is missing from this Python install.\n\n"
                    "Edit config.ini by hand instead (tray menu > Open config.ini).")
    except Exception as e:
        message_box(f"Set Hotkeys failed:\n\n{e}")
    finally:
        app.capture_queue = None           # never leave the keyboard captured
        app.wizard_active = False


# ============================================================================
# TRAY (Set Hotkeys, Split mode, Swap controllers, Reload / Save as / Load config, Realign LCD, Quit)
# ============================================================================

WM_NULL = 0x0000
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_UPDATE_TIP = WM_APP + 2
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202

ID_SET_HOTKEYS, ID_SPLIT, ID_SWAP, ID_RELOAD, ID_SAVE_AS, ID_LOAD, ID_REALIGN, ID_QUIT = range(1001, 1009)

MF_STRING = 0x0
MF_GRAYED = 0x1
MF_CHECKED = 0x8
MF_SEPARATOR = 0x800

NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4
NIM_ADD = 0x0
NIM_MODIFY = 0x1
NIM_DELETE = 0x2
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
SM_CXSMICON = 49
SM_CYSMICON = 50

TRAY = {}   # keeps the icon data and window proc alive (avoid GC)


CONFIGS_DIR = os.path.join(APP_DIR, "configs")
_file_dialog_open = threading.Lock()


def _file_dialog(kind):
    """Native Windows 'Save as' / 'Open' dialog. -> chosen path, or ''."""
    import tkinter as tk
    from tkinter import filedialog
    os.makedirs(CONFIGS_DIR, exist_ok=True)
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        common = dict(parent=root, initialdir=CONFIGS_DIR, filetypes=[("Config files", "*.ini")],
                      defaultextension=".ini")
        if kind == "save":
            return filedialog.asksaveasfilename(title="Save config as", initialfile="my setup.ini", **common)
        return filedialog.askopenfilename(title="Load config", **common)
    finally:
        root.destroy()


def run_file_dialog(action):
    """Save / Load run on their own thread (the tray stays responsive), one at a time."""
    if not _file_dialog_open.acquire(blocking=False):
        return

    def body():
        try:
            action()
        except Exception as e:
            warn_once(str(e))
        finally:
            _file_dialog_open.release()
    threading.Thread(target=body, daemon=True).start()


def save_config_as(app):
    """Saves the settings in use (always valid) under a name of your choice."""
    path = _file_dialog("save")
    if path:
        write_text_atomic(path, render_config(app.values))
        print(f"[Config] Saved as {path}")


def load_config(app):
    """Loads a saved config: checked like at startup. If valid, config.ini is
    kept as config.ini.bak and replaced; otherwise nothing changes."""
    path = _file_dialog("open")
    if not path:
        return
    name = os.path.basename(path)
    values, problems = read_config_file(path)
    problems += check_values(values, path) if values else []
    if not problems:
        probe = App.__new__(App)
        probe.apply_config(values)
        problems = [f"{k}  \u2192  empty or invalid (left, right and click are required)"
                    for k in probe.missing_base_hotkeys()]
    if problems:
        warn_once(errors_text(problems, name) + "\n\nNothing loaded - config.ini unchanged.")
        return
    backup_config()
    write_text_atomic(CONFIG_PATH, render_config(values))
    app.tasks.put(app.reload_config)
    print(f"[Config] Loaded {path}")
    warn_once(f"{name} loaded  (previous config.ini saved as config.ini.bak)", MB_ICONINFORMATION)


def toggle_split_mode(app):
    new_value = not app.split_mode
    text = "true" if new_value else "false"
    try:
        note = save_config_values({("Mode", "split_mode"): text}, fallback=app.values)
    except OSError as e:
        warn_once(f"Split mode not changed:\n\n{e}")
        return
    if note:
        warn_once(note)
    app.split_mode = new_value
    app.values[("Mode", "split_mode")] = text
    app.notify_ui()
    print(f"[Mode] Split mode {'on' if new_value else 'off'}")
    if new_value:
        issues = app.hotkey_issues()
        if issues:
            warn_hotkey_issues(app, issues)


def open_config():
    """Default editor (startup config error), launched outside our job so it
    isn't closed when the app quits."""
    args = ["cmd", "/c", "start", "", CONFIG_PATH]
    for flags in (CREATE_BREAKAWAY_FROM_JOB | NO_WINDOW, NO_WINDOW):
        try:
            subprocess.Popen(args, creationflags=flags, stdin=subprocess.DEVNULL)
            return
        except OSError:
            continue
    warn_once(f"Could not open config.ini:\n\n{CONFIG_PATH}")


def make_wndproc(app):
    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            if lparam in (WM_RBUTTONUP, WM_LBUTTONUP):
                show_tray_menu(hwnd, app)
        elif msg == WM_UPDATE_TIP:
            nid = TRAY["nid"]
            nid.szTip = app.tooltip_text()
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        elif msg == WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == ID_SET_HOTKEYS:
                start_wizard(app)
            elif cmd == ID_SPLIT:
                toggle_split_mode(app)
            elif cmd == ID_SWAP:
                app.tasks.put(app.switch_output)   # same as a long click in split mode
            elif cmd == ID_RELOAD:
                app.tasks.put(lambda: app.reload_config(explicit=True))
            elif cmd == ID_SAVE_AS:
                run_file_dialog(lambda: save_config_as(app))
            elif cmd == ID_LOAD:
                run_file_dialog(lambda: load_config(app))
            elif cmd == ID_REALIGN:
                app.tasks.put(app.realign_lcd)
            elif cmd == ID_QUIT:
                user32.DestroyWindow(hwnd)
        elif msg == WM_DESTROY:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(TRAY["nid"]))
            user32.PostQuitMessage(0)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return wndproc


def show_tray_menu(hwnd, app):
    hmenu = user32.CreatePopupMenu()

    def add(flags, cmd, text):
        user32.AppendMenuW(hmenu, flags, cmd, text)

    add(MF_GRAYED if app.wizard_active else MF_STRING, ID_SET_HOTKEYS, "Set Hotkeys...")
    add(MF_CHECKED if app.split_mode else MF_STRING, ID_SPLIT, "Split mode")
    if app.split_mode:                     # only meaningful with two controllers
        add(MF_STRING, ID_SWAP, "Swap controllers")
    add(MF_SEPARATOR, 0, None)
    busy = MF_GRAYED if app.wizard_active else MF_STRING
    add(busy, ID_RELOAD, "Reload config")
    add(busy, ID_SAVE_AS, "Save config as...")
    add(busy, ID_LOAD, "Load config...")
    add(MF_SEPARATOR, 0, None)
    if app.use_broadlink:
        add(MF_STRING, ID_REALIGN, "Realign LCD")
        add(MF_SEPARATOR, 0, None)
    add(MF_STRING, ID_QUIT, "Quit")

    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.SetForegroundWindow(hwnd)
    user32.TrackPopupMenu(hmenu, 0x0, pt.x, pt.y, 0, hwnd, None)
    user32.PostMessageW(hwnd, WM_NULL, 0, 0)   # lets the menu close properly
    user32.DestroyMenu(hmenu)


def load_tray_icon():
    """tray.ico next to the app, at the system's small-icon size (sharp at
    any DPI scaling). Falls back to the generic Windows icon if the file
    is missing or fails to load - the tray still works either way."""
    if os.path.isfile(TRAY_ICON_PATH):
        cx = user32.GetSystemMetrics(SM_CXSMICON)
        cy = user32.GetSystemMetrics(SM_CYSMICON)
        hicon = user32.LoadImageW(None, TRAY_ICON_PATH, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
        if hicon:
            return hicon
    return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))


def create_tray_window(app):
    hinst = kernel32.GetModuleHandleW(None)
    class_name = "ADISeriesVolumeController"
    wndproc_c = WNDPROCTYPE(make_wndproc(app))
    TRAY["wndproc"] = wndproc_c

    wc = WNDCLASS()
    wc.lpfnWndProc = wndproc_c
    wc.hInstance = hinst
    wc.lpszClassName = class_name
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, class_name, TITLE, 0, 0, 0, 0, 0, None, None, hinst, None)
    app.hwnd = hwnd

    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAYICON
    nid.hIcon = load_tray_icon()
    nid.szTip = app.tooltip_text()
    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
    TRAY["nid"] = nid
    return hwnd


# ============================================================================
# STARTUP SAFETY
# ============================================================================

ERROR_ALREADY_EXISTS = 183
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x800          # lets "Open config.ini" outlive the app
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
SW_HIDE = 0
SC_CLOSE = 0xF060
MF_BYCOMMAND = 0x0
_INSTANCE_MUTEX = None
_JOB = None


def enable_dpi_awareness():
    """Sharp (not blurry) Set Hotkeys window on high-DPI screens."""
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def ensure_single_instance():
    """A second copy would install a second hook: every tick applied twice."""
    global _INSTANCE_MUTEX
    ctypes.set_last_error(0)
    _INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, "Local\\ADISeriesVolumeController")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        fatal(f"{TITLE} is already running (see the tray icon).")


def kill_children_on_exit():
    """Puts the app in a Windows job: if it dies in any way (crash, killed,
    PC shutdown), Windows kills its child processes too - so an in-progress
    receivemidi.exe can never survive, keep the port open and make the
    device broadcast forever."""
    global _JOB
    _JOB = kernel32.CreateJobObjectW(None, None)
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                                             | JOB_OBJECT_LIMIT_BREAKAWAY_OK)
    ok = (_JOB and kernel32.SetInformationJobObject(
              _JOB, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info))
          and kernel32.AssignProcessToJobObject(_JOB, kernel32.GetCurrentProcess()))
    if not ok:
        print("[Startup] WARNING: could not set up child-process cleanup")


def _protect_console(hwnd):
    """Closing a console window kills the app under Windows: remove its close
    button and ignore Ctrl+C - quit from the tray menu instead."""
    menu = user32.GetSystemMenu(hwnd, False)
    if menu:
        user32.DeleteMenu(menu, SC_CLOSE, MF_BYCOMMAND)
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def setup_console(show):
    """show_console = true: log window. false: none.
    Run as rme_app.pyw (pythonw) or compiled: no console unless requested.
    A console shared with other processes (terminal, py.exe launcher) is
    never touched."""
    hwnd = kernel32.GetConsoleWindow()
    if hwnd:
        pids = (ctypes.c_uint32 * 8)()
        if kernel32.GetConsoleProcessList(pids, 8) > 1:
            return                         # shared terminal: leave it alone
        if show:
            _protect_console(hwnd)
        else:
            user32.ShowWindow(hwnd, SW_HIDE)
    elif show and kernel32.AllocConsole():  # compiled app: create one on demand
        sys.stdout = sys.stderr = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
        _protect_console(kernel32.GetConsoleWindow())


def list_midi_ports(tool):
    try:
        out = subprocess.run([tool, "list"], capture_output=True, text=True, timeout=5,
                             stdin=subprocess.DEVNULL, creationflags=NO_WINDOW).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def check_tools_and_port(app):
    """-> (problem text, Retry precision), or None if the MIDI tools and port
    are OK. Partial port match, like sendmidi/receivemidi (Windows may list
    '2- ADI-2 Pro ...')."""
    missing = [p for p in (app.send_midi_exe, app.receive_midi_exe) if not os.path.isfile(p)]
    if missing:
        names = " and ".join(os.path.basename(p) for p in missing)
        it, its = ("them", "their") if len(missing) > 1 else ("it", "its")
        return (f"{names} not found\n\nExpected at:\n" + "\n".join(missing)
                + f"\nPlace {it} next to the app, or set {its} path in config.ini.",
                f"(after placing {names} or fixing config.ini)")
    wanted = app.midi_port_name.lower()
    outputs = list_midi_ports(app.send_midi_exe)
    inputs = list_midi_ports(app.receive_midi_exe)
    if any(wanted in p.lower() for p in outputs) and any(wanted in p.lower() for p in inputs):
        return None
    return (f'MIDI port "{app.midi_port_name}" not found\n\n'
            "Is the ADI-2 connected and powered on?\n"
            f"Available ports:  {', '.join(outputs) or 'none found'}",
            "(after connecting the ADI-2 or fixing config.ini)")


def load_at_startup():
    """-> app. Never runs with a broken config and never quits without
    asking: on a problem, Retry (after fixing), Restore original (config
    problems only - the old file is kept as config.ini.bak), or Quit.
    config.ini is opened in the editor when it is the problem."""
    editor_opened = False
    while True:
        if not os.path.isfile(CONFIG_PATH):
            write_text_atomic(CONFIG_PATH, DEFAULT_CONFIG)
            print("[Startup] config.ini created from the default layout")
        values, problems = read_config_file(CONFIG_PATH)
        problems += check_values(values) if values else []
        if problems:
            if not editor_opened:
                open_config()
                editor_opened = True
            choice = ask_choice(errors_text(problems), [
                ("Retry", "(after fixing and saving config.ini)"),
                ("Restore original config.ini", "(faulty config.ini will be saved as config.ini.bak)"),
                ("Quit", "")])
        else:
            app = App(values)
            port_problem = check_tools_and_port(app)
            if port_problem is None:
                return app
            text, precision = port_problem
            choice = ask_choice(text, [("Retry", precision), ("Quit", "")])
        if choice == "Restore original config.ini":
            backup_config()
            write_text_atomic(CONFIG_PATH, DEFAULT_CONFIG)
            print("[Startup] config.ini restored to the original (previous file: config.ini.bak)")
        elif choice != "Retry":
            sys.exit(1)


def resync_loop(app):
    while True:
        time.sleep(max(app.resync_interval_minutes, 1) * 60)
        if app.resync_interval_minutes > 0:
            app.tasks.put(app.sync_all_from_device)


# ============================================================================
# MAIN
# ============================================================================

def main():
    global APP

    # Compiled --windowed build (or rme_app.pyw): no console attached, so
    # Python hands us stdout/stderr = None. Every print() below would then
    # crash the app on first use (including fatal()'s own print) - give
    # them a harmless sink instead. Real logging (show_console=true) is
    # set up right after, in setup_console().
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    enable_dpi_awareness()
    ensure_single_instance()
    kill_children_on_exit()

    app = load_at_startup()
    APP = app
    setup_console(app.show_console)
    print(f"[Startup] App dir: {APP_DIR}")
    restored = tidy_config(app.values)

    worker = threading.Thread(target=app.worker_loop, daemon=True)
    worker.start()
    create_tray_window(app)
    try:
        hook_id = install_hook()
    except OSError as e:
        fatal(f"Could not install the keyboard hook:\n\n{e}")
    print("[Startup] Hook installed, tray icon created. Listening for hotkeys...")

    app.tasks.put(app.sync_all_from_device)
    if app.resync_interval_minutes > 0:
        threading.Thread(target=resync_loop, args=(app,), daemon=True).start()

    if app.hotkeys_unset():
        start_wizard(app, first_launch=True)
        report_config(app, restored, check_hotkeys=False)
    else:
        report_config(app, restored)

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    user32.UnhookWindowsHookEx(hook_id)
    app.tasks.put(None)
    worker.join(timeout=10)
    print("[Shutdown] Clean exit.")


if __name__ == "__main__":
    main()
