"""Notification-area restore control, using only the native Windows shell."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import queue
import uuid


class NotificationIcon:
    MESSAGE = 0x8000 + 39

    def __init__(self, root, identity: str, on_restore, on_menu):
        self.root = root
        self.on_restore, self.on_menu = on_restore, on_menu
        self._queue = queue.SimpleQueue()
        self._closed = False
        self._hwnd = 0
        self._old_proc = None
        self._tooltip = "Codex 用量伴侣 · 单击恢复窗口"
        self.available = False
        if os.name != "nt":
            return
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        lresult = ctypes.c_ssize_t
        self._proc_type = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HANDLE),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", GUID), ("hBalloonIcon", wintypes.HANDLE)]
        self._data_type = NOTIFYICONDATA
        self._guid = GUID.from_buffer_copy(uuid.uuid5(uuid.NAMESPACE_URL, identity).bytes_le)
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        self.user32.LoadIconW.restype = wintypes.HANDLE
        self.user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        self.user32.RegisterWindowMessageW.restype = wintypes.UINT
        self._taskbar_created = self.user32.RegisterWindowMessageW("TaskbarCreated")
        self.user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.CallWindowProcW.restype = lresult
        self.user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.DefWindowProcW.restype = lresult
        name = "SetWindowLongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "SetWindowLongW"
        self._set_proc = getattr(self.user32, name)
        self._set_proc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self._set_proc.restype = ctypes.c_ssize_t
        self.shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
        self.shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        self._callback = self._proc_type(self._wndproc)
        root.bind("<Map>", self._mapped, add="+")
        self._attach()
        self._timer = root.after(150, self._drain)

    def _data(self):
        data = self._data_type()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd, data.uID = self._hwnd, 1
        data.uFlags = 1 | 2 | 4 | 0x20
        data.uCallbackMessage = self.MESSAGE
        data.hIcon = self.user32.LoadIconW(None, ctypes.c_void_p(32516))
        data.szTip = self._tooltip[:127]
        data.guidItem = self._guid
        return data

    def _attach(self):
        if self._closed:
            return
        self.root.update_idletasks()
        inner = self.root.winfo_id()
        hwnd = self.user32.GetAncestor(inner, 2) or inner
        if hwnd != self._hwnd:
            self._restore_proc()
            self._hwnd = hwnd
            self._old_proc = self._set_proc(hwnd, -4, ctypes.cast(self._callback, ctypes.c_void_p).value)
        data = self._data()
        self.available = bool(self.shell32.Shell_NotifyIconW(0, ctypes.byref(data)))
        if not self.available:
            self.available = bool(self.shell32.Shell_NotifyIconW(1, ctypes.byref(data)))

    def _mapped(self, event):
        if event.widget is self.root and not self._closed:
            self._attach()

    def _wndproc(self, hwnd, message, wparam, lparam):
        if message == self.MESSAGE:
            code = lparam & 0xFFFF
            if code in (0x0202, 0x0203, 0x0400):
                self._queue.put("restore")
            elif code in (0x0205, 0x007B):
                self._queue.put("menu")
            return 0
        if message == self._taskbar_created:
            self._queue.put("readd")
        if self._old_proc:
            return self.user32.CallWindowProcW(self._old_proc, hwnd, message, wparam, lparam)
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _drain(self):
        if self._closed:
            return
        while not self._closed and not self._queue.empty():
            action = self._queue.get()
            if action == "restore":
                self.on_restore()
            elif action == "menu":
                self.on_menu()
            else:
                self._attach()
        if not self._closed:
            self._timer = self.root.after(150, self._drain)

    def _restore_proc(self):
        if self._hwnd and self._old_proc and self.user32.IsWindow(self._hwnd):
            self._set_proc(self._hwnd, -4, self._old_proc)
        self._old_proc = None

    def close(self):
        self._closed = True
        if os.name == "nt":
            self.root.after_cancel(self._timer)
            data = self._data()
            self.shell32.Shell_NotifyIconW(2, ctypes.byref(data))
            self._restore_proc()
