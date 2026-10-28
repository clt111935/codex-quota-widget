"""A native Windows owner relationship; no changes to the Codex installation.

The widget stays above its Codex owner, follows moves, and hides while Codex is
minimized. It is never made globally topmost while follow mode is enabled.
"""

from __future__ import annotations

import sys
from typing import Any


def desktop_bounds(root: Any) -> tuple[int, int, int, int]:
    """Return virtual desktop bounds, including monitors left/above primary."""
    if sys.platform == "win32":
        import ctypes
        user32 = ctypes.windll.user32
        return tuple(user32.GetSystemMetrics(index) for index in (76, 77, 78, 79))
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


class WindowFollower:
    """Follow the desktop application from an overrideredirect Tk root.

    Call ``update(enabled)`` from the Tk event loop. Offsets are measured from
    the target's upper-right corner: x = right - widget_width + dx, y = top + dy.
    ``remember_position`` can be called after dragging to preserve that offset.
    """

    DEFAULT_OFFSET = (-18, 68)

    def __init__(self, root: Any):
        self.root = root
        self._supported = sys.platform == "win32"
        self._hwnd = 0
        self._target = 0
        self._owner = 0
        self._hidden = False
        self._mode: bool | None = None
        self._offset = self.DEFAULT_OFFSET
        self._last_rect: tuple[int, int, int, int] | None = None
        if self._supported:
            self._bind_win32()

    def _bind_win32(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        user32 = self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        signatures = {
            "EnumWindows": ([self._enum_callback, wintypes.LPARAM], wintypes.BOOL),
            "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
            "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(user32, name)
            function.argtypes, function.restype = arguments, result
        # LONG_PTR is pointer-sized on 64-bit Windows, unlike ctypes.c_long.
        get_name = "GetWindowLongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "GetWindowLongW"
        set_name = "SetWindowLongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "SetWindowLongW"
        self._get_long = getattr(user32, get_name)
        self._get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self._get_long.restype = ctypes.c_ssize_t
        self._set_long = getattr(user32, set_name)
        self._set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self._set_long.restype = ctypes.c_ssize_t
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def _own_hwnd(self) -> int:
        if not self._hwnd or not self._user32.IsWindow(self._hwnd):
            self.root.update_idletasks()
            # winfo_id is often the inner Tk child, so always select GA_ROOT.
            inner = self.root.winfo_id()
            self._hwnd = int(self._user32.GetAncestor(inner, 2) or inner)
        return self._hwnd

    def _process_path(self, hwnd: int) -> str:
        pid = self._wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid))
        process = self._kernel32.OpenProcess(0x1000, False, pid.value)
        if not process:
            return ""
        try:
            buffer = self._ctypes.create_unicode_buffer(32768)
            length = self._wintypes.DWORD(len(buffer))
            if self._kernel32.QueryFullProcessImageNameW(process, 0, buffer, self._ctypes.byref(length)):
                return buffer.value
            return ""
        finally:
            self._kernel32.CloseHandle(process)

    @staticmethod
    def _is_codex_process(path: str) -> bool:
        normalized = path.replace("/", "\\").casefold()
        basename = normalized.rsplit("\\", 1)[-1]
        if basename == "codex.exe":
            return True
        # Current Microsoft Store builds use ChatGPT.exe inside OpenAI.Codex.
        return basename == "chatgpt.exe" and (
            "openai.codex" in normalized or "\\openai\\codex\\" in normalized
        )

    def _find_target(self) -> int:
        candidates: list[tuple[int, int]] = []
        process_cache: dict[str, bool] = {}

        @self._enum_callback
        def visit(hwnd: int, _parameter: int) -> bool:
            try:
                if hwnd == self._hwnd or not self._user32.IsWindowVisible(hwnd):
                    return True
                class_name = self._ctypes.create_unicode_buffer(256)
                self._user32.GetClassNameW(hwnd, class_name, len(class_name))
                # This excludes the CLI console and browser tabs by process path.
                if not class_name.value.startswith("Chrome_WidgetWin"):
                    return True
                title = self._ctypes.create_unicode_buffer(1024)
                self._user32.GetWindowTextW(hwnd, title, len(title))
                if not any(name in title.value.casefold() for name in ("codex", "chatgpt")):
                    return True
                path = self._process_path(hwnd)
                if path not in process_cache:
                    process_cache[path] = self._is_codex_process(path)
                if not process_cache[path]:
                    return True
                rect = self._wintypes.RECT()
                if self._user32.GetWindowRect(hwnd, self._ctypes.byref(rect)):
                    area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
                    # Prefer the main window over small secondary dialogs.
                    candidates.append((area, int(hwnd)))
            except (OSError, ValueError):
                pass  # A window can disappear while EnumWindows is running.
            return True

        self._user32.EnumWindows(visit, 0)
        return max(candidates)[1] if candidates else 0

    def _configure(self, enabled: bool, owner: int) -> None:
        hwnd = self._own_hwnd()
        if self._mode == enabled and self._owner == owner:
            return
        # GWLP_HWNDPARENT changes the owner of a top-level window, not its parent.
        self._set_long(hwnd, -8, owner)
        self._owner = owner
        extended = self._get_long(hwnd, -20)
        extended = (extended | 0x80) & ~0x40000  # TOOLWINDOW, clear APPWINDOW.
        extended = extended | 0x08000000 if enabled else extended & ~0x08000000
        self._set_long(hwnd, -20, extended)
        # HWND_NOTOPMOST while following, HWND_TOPMOST only in independent mode.
        self._user32.SetWindowPos(hwnd, -2 if enabled else -1, 0, 0, 0, 0, 0x13)
        self._mode = enabled

    def _show(self) -> None:
        if self._hidden or not self._user32.IsWindowVisible(self._own_hwnd()):
            self._user32.ShowWindow(self._own_hwnd(), 4)  # SW_SHOWNOACTIVATE.
            self._hidden = False

    def set_offset(self, dx: int, dy: int) -> None:
        self._offset = (int(dx), int(dy))

    def reset_offset(self) -> None:
        self._offset = self.DEFAULT_OFFSET

    def remember_position(self) -> None:
        """Remember the current root position after a user drag in follow mode."""
        if self._last_rect is not None:
            left, top, right, bottom = self._last_rect
            width = max(self.root.winfo_width(), self.root.winfo_reqwidth())
            self._offset = (self.root.winfo_x() - (right - width), self.root.winfo_y() - top)

    def set_offset_from_position(self, x: int, y: int) -> None:
        """Preserve a drag position without waiting for Tk's geometry update."""
        if self._last_rect is not None:
            width = max(self.root.winfo_width(), self.root.winfo_reqwidth())
            self._offset = (int(x) - (self._last_rect[2] - width), int(y) - self._last_rect[1])

    def update(self, enabled: bool) -> str:
        if not self._supported:
            try:
                self.root.attributes("-topmost", not enabled)
            except Exception:
                pass
            return "当前系统不支持窗口跟随" if enabled else "独立悬浮窗"
        try:
            self._own_hwnd()
            if not enabled:
                self._target = 0
                self._last_rect = None
                self._configure(False, 0)
                self._show()
                return "独立悬浮窗"
            target = self._find_target()
            self._target = target
            self._configure(True, target)
            if not target:
                self._last_rect = None
                self._show()
                return "等待 Codex 桌面客户端"
            if self._user32.IsIconic(target):
                self._user32.ShowWindow(self._hwnd, 0)
                self._hidden = True
                return "Codex 已最小化"
            rect = self._wintypes.RECT()
            if not self._user32.GetWindowRect(target, self._ctypes.byref(rect)):
                self._show()
                return "等待 Codex 桌面客户端"
            self._last_rect = (rect.left, rect.top, rect.right, rect.bottom)
            self.root.update_idletasks()
            width = max(self.root.winfo_width(), self.root.winfo_reqwidth())
            height = max(self.root.winfo_height(), self.root.winfo_reqheight())
            x = rect.right - width + self._offset[0]
            y = rect.top + self._offset[1]
            # Keep the widget reachable when the owner is partly off-screen.
            screen_x, screen_y = self._user32.GetSystemMetrics(76), self._user32.GetSystemMetrics(77)
            screen_w, screen_h = self._user32.GetSystemMetrics(78), self._user32.GetSystemMetrics(79)
            x = max(screen_x, min(x, screen_x + max(0, screen_w - width)))
            y = max(screen_y, min(y, screen_y + max(0, screen_h - height)))
            self._user32.SetWindowPos(self._hwnd, 0, x, y, 0, 0, 0x15)
            self._show()
            return "已跟随 Codex"
        except (OSError, RuntimeError):
            return "窗口跟随暂不可用"

    def detach(self) -> None:
        """Release the native owner relationship and show an independent widget."""
        if self._supported:
            try:
                self._configure(False, 0)
                self._show()
            except (OSError, RuntimeError):
                pass
        self._target = 0
        self._last_rect = None
