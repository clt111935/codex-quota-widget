"""Native window lifecycle notifications, with a slow recovery scan."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time


class ClientWindowMonitor:
    """Cache the Codex HWND; idle callers never repeatedly enumerate windows.

    WinEvent callbacks run on the Tk thread that installed them. A 30-second
    safety scan covers missed OS notifications without account or process polls.
    """
    INTERESTING = {0x8000, 0x8001, 0x8002, 0x8003, 3, 0x16, 0x17}

    def __init__(self, finder, *, clock=time.monotonic, recovery_seconds=30, native=None):
        self.finder = finder
        self.clock = clock
        self.recovery_seconds = recovery_seconds
        self._target = 0
        self._dirty = True
        self._last_scan = float("-inf")
        self._hooks = []
        self._closed = False
        self.scan_count = 0
        self.native = native
        if self.native is None and os.name == "nt":
            self.native = ctypes.WinDLL("user32", use_last_error=True)
            self.native.IsWindow.argtypes = [wintypes.HWND]
            self.native.IsWindow.restype = wintypes.BOOL
            self.native.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            self.native.GetClassNameW.restype = ctypes.c_int
            self._callback_type = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                                    ctypes.c_long, ctypes.c_long, wintypes.DWORD, wintypes.DWORD)
            self.native.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                                   self._callback_type, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
            self.native.SetWinEventHook.restype = wintypes.HANDLE
            self.native.UnhookWinEvent.argtypes = [wintypes.HANDLE]
            self.native.UnhookWinEvent.restype = wintypes.BOOL
            self._callback = self._callback_type(self._event)
            for first, last in [(0x8000, 0x8003), (3, 3), (0x16, 0x17)]:
                hook = self.native.SetWinEventHook(first, last, None, self._callback, 0, 0, 2)
                if hook:
                    self._hooks.append(hook)

    @property
    def hooks_active(self):
        return len(self._hooks) == 3

    def _event(self, hook, event, hwnd, object_id, child_id, thread_id, event_time):
        if self._closed or not hwnd or object_id != 0 or child_id != 0 or event not in self.INTERESTING:
            return
        if hwnd == self._target:
            if event == 0x8001:
                self._target = 0
            self._dirty = True
            return
        if event in (0x8000, 0x8002, 3):
            name = ctypes.create_unicode_buffer(256)
            if self.native.GetClassNameW(hwnd, name, len(name)) and name.value.startswith("Chrome_WidgetWin"):
                self._dirty = True

    def invalidate(self):
        self._dirty = True

    def target(self):
        if self._closed:
            return 0
        now = self.clock()
        if self._target and self.native is not None and not self.native.IsWindow(self._target):
            self._dirty = True
        if self._dirty or now - self._last_scan >= self.recovery_seconds:
            self._target = self.finder()
            self._last_scan = now
            self._dirty = False
            self.scan_count += 1
        return self._target

    def close(self):
        self._closed = True
        for hook in self._hooks:
            self.native.UnhookWinEvent(hook)
        self._hooks.clear()
