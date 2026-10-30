"""Round the native window, including custom backgrounds and translucent edges."""
from __future__ import annotations

import sys
import tkinter as tk


class RoundedWindow:
    def __init__(self, root: tk.Tk, radius: int = 20):
        self.root = root
        self.radius = max(0, int(radius))
        self._last_shape = None
        self._pending = None
        if sys.platform != "win32":
            return

        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self._user32.GetAncestor.restype = wintypes.HWND
        self._user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user32.GetWindowRect.restype = wintypes.BOOL
        self._user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        self._user32.SetWindowRgn.restype = ctypes.c_int
        self._gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        self._gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
        self._gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        self._gdi32.DeleteObject.restype = wintypes.BOOL
        root.bind("<Configure>", self._queue, add="+")
        root.bind("<Map>", self._mapped, add="+")
        self._queue()

    def _mapped(self, event):
        if event.widget is self.root:
            # Tk may replace its native wrapper after withdrawing and remapping.
            self._last_shape = None
            self._queue()

    def _queue(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        if self._pending is None:
            self._pending = self.root.after_idle(self._apply)

    def _apply(self):
        self._pending = None
        try:
            inner = self.root.winfo_id()
            hwnd = self._user32.GetAncestor(inner, 2) or inner
            rect = self._wintypes.RECT()
            if not self._user32.GetWindowRect(hwnd, self._ctypes.byref(rect)):
                return
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width < 2 or height < 2:
                return
            radius = min(self.radius, width // 2, height // 2)
            shape = (hwnd, width, height, radius)
            if shape == self._last_shape:
                return
            region = self._gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
            if not region:
                return
            if self._user32.SetWindowRgn(hwnd, region, True):
                # On success Windows owns the region and releases it when replaced.
                self._last_shape = shape
            else:
                self._gdi32.DeleteObject(region)
        except tk.TclError:
            pass  # The window may have been closed before this idle callback.
