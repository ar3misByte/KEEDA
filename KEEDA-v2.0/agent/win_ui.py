"""Drive KiCad's editor windows on Windows: Save, Revert, "has unsaved edits?".

Why this exists: eeschema (and, on some machines, pcbnew) exposes no usable
live API. The only way to make another user's change appear in an open editor
is to write the file and ask the editor to reload it - exactly what the user
would do with File > Revert. We do that by posting the menu command to the
window. This does NOT need the window to be focused, does not send keystrokes
and does not move the mouse.

Verified on KiCad 10.0.6 / Windows 11:
  * both editors have File > Save and File > Revert as ordinary Win32 menu items;
  * Revert always shows a "Confirmation" dialog with a "Yes" button, which we
    click; the schematic then reloads from disk;
  * an editor with unsaved changes shows a leading '*' in its title.

Menu items are found by their TEXT (menu ids change between KiCad versions).
On other platforms or non-English KiCad UIs every function returns False/None
and the caller falls back to telling the user to reload by hand.
"""
from __future__ import annotations

import logging
import sys
import time

log = logging.getLogger("kicadlive.winui")

AVAILABLE = sys.platform == "win32"

WM_COMMAND = 0x0111
BM_CLICK = 0x00F5
MF_BYPOSITION = 0x400

SUFFIX = {"sch": "Schematic Editor", "pcb": "PCB Editor"}

if AVAILABLE:
    import ctypes
    from ctypes import wintypes

    _u = ctypes.windll.user32
    _u.GetMenu.restype = ctypes.c_void_p
    _u.GetMenu.argtypes = [wintypes.HWND]
    _u.GetSubMenu.restype = ctypes.c_void_p
    _u.GetSubMenu.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _u.GetMenuItemCount.argtypes = [ctypes.c_void_p]
    _u.GetMenuItemID.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _u.GetMenuStringW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR,
                                  ctypes.c_int, wintypes.UINT]
    _ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _text(hwnd) -> str:
    n = _u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(128)
    _u.GetClassNameW(hwnd, buf, 128)
    return buf.value


def _pid(hwnd) -> int:
    pid = wintypes.DWORD()
    _u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _top_windows() -> list[int]:
    found: list[int] = []

    def cb(hwnd, _):
        if _u.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    _u.EnumWindows(_ENUM(cb), 0)
    return found


class Editor:
    """One open KiCad editor window (schematic or PCB)."""

    def __init__(self, hwnd: int, kind: str):
        self.hwnd = hwnd
        self.kind = kind

    @property
    def title(self) -> str:
        return _text(self.hwnd)

    def alive(self) -> bool:
        return bool(_u.IsWindow(self.hwnd))

    def is_modified(self) -> bool:
        """True when the editor holds edits that are not on disk yet."""
        return self.title.lstrip().startswith("*")

    def dialogs(self) -> list[int]:
        """Open modal dialogs belonging to this editor's process."""
        pid = _pid(self.hwnd)
        return [h for h in _top_windows()
                if h != self.hwnd and _pid(h) == pid and _class(h) == "#32770"]

    def blocked(self) -> bool:
        return bool(self.dialogs())

    # -------------------------------------------------------------- commands

    def _file_menu_id(self, *names: str) -> int | None:
        menubar = _u.GetMenu(self.hwnd)
        if not menubar:
            return None
        file_menu = _u.GetSubMenu(menubar, 0)
        if not file_menu:
            return None
        wanted = {n.lower() for n in names}
        for i in range(_u.GetMenuItemCount(file_menu)):
            buf = ctypes.create_unicode_buffer(256)
            _u.GetMenuStringW(file_menu, i, buf, 256, MF_BYPOSITION)
            label = buf.value.split("\t")[0].replace("&", "").strip().lower()
            if label in wanted:
                item = _u.GetMenuItemID(file_menu, i)
                return item if item > 0 else None
        return None

    def _post(self, item_id: int) -> None:
        _u.PostMessageW(self.hwnd, WM_COMMAND, item_id, 0)

    def save(self, wait: float = 4.0) -> bool:
        """File > Save. True once the '*' has cleared."""
        if self.blocked():
            return False
        item = self._file_menu_id("Save")
        if item is None:
            return False
        self._post(item)
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(0.15)
            if not self.alive():
                return False
            if self.blocked():
                return False        # e.g. a "convert format?" prompt: leave it to the user
            if not self.is_modified():
                return True
        return not self.is_modified()

    def revert(self, wait: float = 5.0) -> bool:
        """File > Revert, confirming the prompt. Discards unsaved edits, so callers
        must check `is_modified()` first."""
        if self.blocked():
            return False
        item = self._file_menu_id("Revert")
        if item is None:
            return False
        self._post(item)
        deadline = time.time() + wait
        clicked = False
        while time.time() < deadline:
            time.sleep(0.15)
            for dialog in self.dialogs():
                if self._click_yes(dialog):
                    clicked = True
            if clicked and not self.dialogs():
                time.sleep(0.5)
                return True
        return clicked

    @staticmethod
    def _click_yes(dialog: int) -> bool:
        buttons: list[tuple[int, str]] = []

        def cb(hwnd, _):
            if _class(hwnd) == "Button":
                buttons.append((hwnd, _text(hwnd).replace("&", "").strip().lower()))
            return True
        _u.EnumChildWindows(dialog, _ENUM(cb), 0)
        for hwnd, label in buttons:
            if label in ("yes", "ok"):
                _u.PostMessageW(hwnd, BM_CLICK, 0, 0)
                return True
        return False


def find_editor(kind: str, stem: str = "") -> Editor | None:
    """Locate the open editor window of `kind` ('sch' or 'pcb').

    With several editors open, prefer the one whose title mentions `stem`
    (the file name without extension).
    """
    if not AVAILABLE:
        return None
    suffix = SUFFIX[kind].lower()
    candidates = []
    for hwnd in _top_windows():
        title = _text(hwnd).lower()
        if title.rstrip().endswith(suffix) and _class(hwnd) != "#32770":
            candidates.append((hwnd, title))
    if not candidates:
        return None
    for hwnd, title in candidates:
        if stem and stem.lower() in title:
            return Editor(hwnd, kind)
    return Editor(candidates[0][0], kind) if len(candidates) == 1 else None
