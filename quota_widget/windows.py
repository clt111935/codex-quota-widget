"""A native Windows owner relationship; no changes to the Codex installation.

The widget stays above its Codex owner, follows moves, and hides while Codex is
minimized. It is never made globally topmost while follow mode is enabled.
"""

from __future__ import annotations

import sys
from typing import Any


SIDEBAR_WIDTH_DP = 52
SIDEBAR_THUMBNAIL_DP = (40, 156)
SIDEBAR_BOTTOM_GAP_DP = 90
SIDEBAR_DETAIL_GAP_DP = 8


def _clamp_to_desktop(
    x: int, y: int, size: tuple[int, int], desktop: tuple[int, int, int, int],
) -> tuple[int, int]:
    left, top, width, height = desktop
    return (
        max(left, min(x, left + max(0, width - size[0]))),
        max(top, min(y, top + max(0, height - size[1]))),
    )


def sidebar_geometry(
    client_rect: tuple[int, int, int, int],
    widget_size: tuple[int, int],
    scale: float = 1.0,
    anchor: str = "sidebar",
    desktop_rect: tuple[int, int, int, int] | None = None,
) -> tuple[int, int]:
    """Position a thumbnail or its detail panel in physical screen pixels.

    ``client_rect`` contains left/top/right/bottom screen coordinates. As with
    ``desktop_bounds``, ``desktop_rect`` contains x/y/width/height and supports
    monitors to the left of or above the primary monitor. A short client keeps
    its thumbnail at the top; the detail panel remains aligned to that thumbnail
    until the desktop boundary requires clipping its position.
    """
    if anchor not in ("sidebar", "sidebar-detail"):
        raise ValueError(f"Unknown sidebar anchor: {anchor}")
    if not 0 < scale < float("inf"):
        scale = 1.0
    left, top, right, bottom = client_rect
    width, height = widget_size
    rail_width = round(SIDEBAR_WIDTH_DP * scale)
    thumbnail_width = round(SIDEBAR_THUMBNAIL_DP[0] * scale)
    thumbnail_height = round(SIDEBAR_THUMBNAIL_DP[1] * scale)
    bottom_gap = round(SIDEBAR_BOTTOM_GAP_DP * scale)
    if anchor == "sidebar":
        x = left + round((rail_width - width) / 2)
        y = max(top, bottom - bottom_gap - height)
    else:
        thumbnail_x = left + round((rail_width - thumbnail_width) / 2)
        thumbnail_y = max(top, bottom - bottom_gap - thumbnail_height)
        x = thumbnail_x + thumbnail_width + round(SIDEBAR_DETAIL_GAP_DP * scale)
        y = thumbnail_y + thumbnail_height - height
    if desktop_rect is not None:
        x, y = _clamp_to_desktop(x, y, widget_size, desktop_rect)
    return x, y


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
        self._pending_position: tuple[int, int] | None = None
        self._last_rect: tuple[int, int, int, int] | None = None
        self.client_available = False
        self.client_visible = False
        self.dpi_scale = 1.0
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
            "GetClientRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "ClientToScreen": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
            "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(user32, name)
            function.argtypes, function.restype = arguments, result
        self._get_dpi = getattr(user32, "GetDpiForWindow", None)
        if self._get_dpi is not None:
            self._get_dpi.argtypes = [wintypes.HWND]
            self._get_dpi.restype = wintypes.UINT
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
                # The title can change during sign-in or when switching chats.
                # Match the application binary and native class instead.
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
            if self.root.state() == "withdrawn":
                mode, owner = self._mode, self._owner
                self.root.deiconify()
                self.root.update_idletasks()
                # Tk can replace its native wrapper when mapping a withdrawn root.
                self._hwnd = 0
                self._mode = None
                self._configure(bool(mode), owner)
            self._user32.ShowWindow(self._own_hwnd(), 4)  # SW_SHOWNOACTIVATE.
            self._hidden = False

    def _hide(self) -> None:
        if not self._hidden:
            self._user32.ShowWindow(self._own_hwnd(), 0)
            self._hidden = True

    @property
    def native_visible(self) -> bool:
        """Whether the widget is currently visible according to the window system."""
        if self._supported:
            try:
                return bool(self._user32.IsWindowVisible(self._own_hwnd()))
            except (OSError, RuntimeError):
                return False
        try:
            return bool(self.root.winfo_viewable())
        except (AttributeError, RuntimeError):
            return False

    def _dpi_for_target(self, target: int) -> float:
        get_dpi = getattr(self, "_get_dpi", None)
        if get_dpi is not None:
            try:
                dpi = get_dpi(target)
                if dpi:
                    return dpi / 96.0
            except OSError:
                pass
        return 1.0

    def _client_rect(self, target: int) -> tuple[int, int, int, int] | None:
        rect = self._wintypes.RECT()
        if not self._user32.GetClientRect(target, self._ctypes.byref(rect)):
            return None
        origin = self._wintypes.POINT(rect.left, rect.top)
        if not self._user32.ClientToScreen(target, self._ctypes.byref(origin)):
            return None
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return (origin.x, origin.y, origin.x + rect.right - rect.left,
                origin.y + rect.bottom - rect.top)

    def _desktop_rect(self) -> tuple[int, int, int, int]:
        return tuple(self._user32.GetSystemMetrics(index) for index in (76, 77, 78, 79))

    def _set_visibility(self, visible: bool) -> None:
        if visible:
            self._show()
        else:
            self._hide()

    def set_offset(self, dx: int, dy: int) -> None:
        self._offset = (int(dx), int(dy))
        self._pending_position = None

    @property
    def offset(self) -> tuple[int, int] | None:
        """Return the relative position, or None while waiting for the owner."""
        return self._offset if self._pending_position is None else None

    def reset_offset(self) -> None:
        self.set_offset(*self.DEFAULT_OFFSET)

    def remember_position(self) -> None:
        """Remember the current root position after a user drag in follow mode."""
        self.set_offset_from_position(self.root.winfo_x(), self.root.winfo_y())

    def set_offset_from_position(self, x: int, y: int) -> None:
        """Preserve a drag position without waiting for Tk's geometry update."""
        if self._last_rect is not None:
            width = max(1, self.root.winfo_width())
            self.set_offset(int(x) - (self._last_rect[2] - width), int(y) - self._last_rect[1])
        else:
            # A drag before Codex opens must also survive the first follow tick.
            self._pending_position = (int(x), int(y))

    def update(
        self, enabled: bool, *, linked: bool = False,
        anchor: str = "floating", visible: bool = True,
    ) -> str:
        if anchor not in ("floating", "sidebar", "sidebar-detail"):
            raise ValueError(f"Unknown window anchor: {anchor}")
        if not self._supported:
            try:
                self.root.attributes("-topmost", not enabled)
                if not visible:
                    self.root.withdraw()
            except Exception:
                pass
            return "当前系统不支持窗口跟随" if enabled else "独立悬浮窗"
        try:
            self._own_hwnd()
            target = self._find_target() if enabled or linked else 0
            self.client_available = bool(target)
            minimized = bool(target and self._user32.IsIconic(target))
            self.client_visible = bool(target and not minimized and self._user32.IsWindowVisible(target))
            self.dpi_scale = self._dpi_for_target(target) if target else 1.0
            self._target = target if enabled else 0
            # Even a hidden popup must have the correct native owner relationship.
            self._configure(enabled, target if enabled else 0)
            if linked and not target:
                self._last_rect = None
                self._hide()
                return "等待 Codex 启动"
            if target and (enabled or linked) and not self.client_visible:
                self._hide()
                return "Codex 已最小化" if minimized else "Codex 窗口已隐藏"
            if not enabled:
                self._last_rect = None
                self._set_visibility(visible)
                return "独立悬浮窗"
            if not target:
                self._last_rect = None
                self._set_visibility(visible)
                return "等待 Codex 桌面客户端"
            rect = self._wintypes.RECT()
            if not self._user32.GetWindowRect(target, self._ctypes.byref(rect)):
                self._set_visibility(visible and not linked and anchor == "floating")
                return "等待 Codex 桌面客户端"
            self._last_rect = (rect.left, rect.top, rect.right, rect.bottom)
            self.root.update_idletasks()
            # Requested geometry can belong to a much larger detail panel. The
            # current native size is authoritative for a collapsed thumbnail.
            size = (max(1, self.root.winfo_width()), max(1, self.root.winfo_height()))
            if anchor in ("sidebar", "sidebar-detail"):
                client_rect = self._client_rect(target)
                if client_rect is None:
                    self.client_visible = False
                    self._hide()
                    return "等待 Codex 客户区"
                x, y = sidebar_geometry(client_rect, size, self.dpi_scale, anchor, self._desktop_rect())
            else:
                if self._pending_position is not None:
                    self.set_offset_from_position(*self._pending_position)
                x = rect.right - size[0] + self._offset[0]
                y = rect.top + self._offset[1]
                x, y = _clamp_to_desktop(x, y, size, self._desktop_rect())
            self._set_visibility(visible)
            self._user32.SetWindowPos(self._own_hwnd(), 0, x, y, 0, 0, 0x15)
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
