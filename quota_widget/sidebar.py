"""A small, non-activating quota thumbnail for the Codex navigation rail.

This view receives snapshots from its caller and never accesses an account or
starts a refresh. Its Toplevel starts withdrawn; the caller owns visibility,
position, and the relationship to the Codex desktop window.
"""
from __future__ import annotations

import math
from pathlib import Path
import sys
import time
import tkinter as tk
from typing import Callable, Mapping

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk
except ImportError:
    Image = None

from .rounded_window import RoundedWindow
from .settings import Settings
from .usage import QuotaSnapshot, QuotaWindow


_DEFAULT_PALETTE = {
    "bg": "#10151e", "card": "#1a2230", "line": "#2c3748",
    "text": "#f3f5fa", "muted": "#93a1b5", "accent": "#b5c9ff",
    "second": "#c5b8f8", "glow": "#233b67",
}


def _mix(first: str, second: str, amount: float) -> str:
    """Blend theme colors without introducing a Pillow dependency."""
    try:
        start = tuple(int(first[index:index + 2], 16) for index in (1, 3, 5))
        end = tuple(int(second[index:index + 2], 16) for index in (1, 3, 5))
        return "#" + "".join(f"{round(a * (1 - amount) + b * amount):02x}" for a, b in zip(start, end))
    except (ValueError, TypeError):
        return second


def _percent(window: QuotaWindow | None) -> tuple[str, float | None]:
    if window is None:
        return "—", None
    try:
        value = float(window.remaining_percent)
    except (TypeError, ValueError, OverflowError):
        return "—", None
    if not math.isfinite(value):
        return "—", None
    value = max(0.0, min(100.0, value))
    if 0 < value < 1:
        return "<1%", value
    if 99 < value < 100:
        return ">99%", value
    return f"{round(value):d}%", value


class SidebarThumbnail:
    WIDTH = 40
    HEIGHT = 156
    CORNER_RADIUS = 11
    STALE_SECONDS = 300

    def __init__(
        self,
        master: tk.Misc,
        settings: Settings,
        *,
        on_enter: Callable[[], object],
        on_leave: Callable[[], object],
        on_click: Callable[[tk.Event], object],
        on_context: Callable[[tk.Event], object],
    ):
        self.settings = settings
        self.size = (self.WIDTH, self.HEIGHT)
        self._closed = False
        self._background_key = None
        self._background_photo = None
        self._native = None
        self._scale = 1.0
        self.window = tk.Toplevel(master, takefocus=False)
        self.window.withdraw()
        self.window.title("Codex 用量")
        self.window.overrideredirect(True)
        self.window.configure(bg=_DEFAULT_PALETTE["bg"])
        self.window.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.window.focusmodel("passive")
        if sys.platform == "win32":
            self.window.attributes("-toolwindow", True)
        self.canvas = tk.Canvas(
            self.window, width=self.WIDTH, height=self.HEIGHT,
            highlightthickness=0, bd=0, takefocus=False, cursor="hand2",
            bg=_DEFAULT_PALETTE["bg"],
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Enter>", lambda _: on_enter())
        self.canvas.bind("<Leave>", lambda _: on_leave())
        self.canvas.bind("<Button-1>", on_click)
        self.canvas.bind("<Button-3>", on_context)
        self.window.bind("<Map>", self._avoid_activation, add="+")
        self.window.update_idletasks()
        self.rounded_window = RoundedWindow(self.window, self.CORNER_RADIUS)
        self._avoid_activation()
        self.draw(None)

    def _avoid_activation(self, event=None) -> None:
        """Keep a mouse click on the thumbnail from stealing editor focus."""
        if self._closed or sys.platform != "win32":
            return
        if event is not None and event.widget is not self.window:
            return
        try:
            if self._native is None:
                import ctypes
                from ctypes import wintypes

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
                user32.GetAncestor.restype = wintypes.HWND
                pointer_sized = ctypes.sizeof(ctypes.c_void_p) == 8
                get_long = getattr(user32, "GetWindowLongPtrW" if pointer_sized else "GetWindowLongW")
                set_long = getattr(user32, "SetWindowLongPtrW" if pointer_sized else "SetWindowLongW")
                get_long.argtypes = [wintypes.HWND, ctypes.c_int]
                get_long.restype = ctypes.c_ssize_t
                set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
                set_long.restype = ctypes.c_ssize_t
                self._native = user32, get_long, set_long
            user32, get_long, set_long = self._native
            inner = self.window.winfo_id()
            hwnd = user32.GetAncestor(inner, 2) or inner
            style = get_long(hwnd, -20)  # GWL_EXSTYLE
            # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW, without WS_EX_APPWINDOW.
            set_long(hwnd, -20, (style | 0x08000000 | 0x00000080) & ~0x00040000)
        except (OSError, AttributeError, tk.TclError):
            pass

    def _background(self, palette: Mapping[str, str]) -> None:
        path = self.settings.background_path
        dim = max(0.0, min(1.0, self.settings.background_dim))
        modified = None
        if path:
            try:
                modified = Path(path).stat().st_mtime_ns
            except OSError:
                pass
        key = (self.size, palette["bg"], palette["glow"], path, dim, modified)
        if key == self._background_key:
            return
        self._background_key = key
        self._background_photo = None
        if Image is None:
            return
        base = Image.new("RGB", self.size, palette["bg"])
        if path:
            try:
                with Image.open(path) as original:
                    picture = ImageOps.fit(
                        ImageOps.exif_transpose(original).convert("RGB"),
                        self.size, method=Image.Resampling.LANCZOS,
                    )
                base = Image.blend(picture, base, dim)
            except (OSError, ValueError, Image.DecompressionBombError):
                pass
        else:
            glow = Image.new("RGB", self.size, palette["bg"])
            painter = ImageDraw.Draw(glow)
            width, height = self.size
            painter.ellipse((width * .1, -height * .3, width * 2, height * .5), fill=palette["glow"])
            glow = glow.filter(ImageFilter.GaussianBlur(max(1, width * .45)))
            base = Image.blend(base, glow, .42)
        self._background_photo = ImageTk.PhotoImage(base, master=self.window)

    def _text(self, x, y, text, size, color, *, bold=False, family="Segoe UI"):
        # Negative font sizes are physical pixels; positive Tk point sizes would
        # multiply the OS DPI on top of the explicit per-monitor scale.
        return self.canvas.create_text(
            x * self._scale, y * self._scale,
            text=text, fill=color, anchor="center",
            font=(family, -max(1, round(size * self._scale)), "bold" if bold else "normal"),
        )

    def _line(self, coordinates, *, fill, width=1, **options):
        return self.canvas.create_line(
            *(coordinate * self._scale for coordinate in coordinates),
            fill=fill, width=max(.5, width * self._scale), **options,
        )

    def _group(self, label, window, y, color, palette, now, stale):
        text, remaining = _percent(window)
        expired = bool(window and window.resets_at is not None and window.resets_at <= now)
        uncertain = stale or expired
        value_color = _mix(palette["text"], palette["muted"], .65) if uncertain else palette["text"]
        bar_color = _mix(color, palette["bg"], .48) if uncertain else color
        if remaining is not None and remaining <= 10 and not uncertain:
            bar_color = "#e5b57a"
        self._text(20, y, label, 9, palette["muted"], bold=True)
        self._text(20, y + 18, text, 14 if len(text) <= 3 else 12, value_color, bold=True)
        self._line((8, y + 35, 32, y + 35), fill=_mix(palette["line"], palette["bg"], .2), width=3, capstyle=tk.ROUND)
        if remaining is not None and remaining > 0:
            self._line((8, y + 35, 8 + 24 * remaining / 100, y + 35), fill=bar_color, width=3, capstyle=tk.ROUND)
        return expired

    def draw(
        self,
        snapshot: QuotaSnapshot | None,
        *,
        busy: bool = False,
        error: bool = False,
        dpi_scale: float = 1.0,
        palette: Mapping[str, str] | None = None,
    ) -> None:
        if self._closed:
            return
        try:
            scale = float(dpi_scale)
        except (TypeError, ValueError, OverflowError):
            scale = 1.0
        if not math.isfinite(scale) or scale <= 0:
            scale = 1.0
        self._scale = scale
        self.size = (max(1, round(self.WIDTH * scale)), max(1, round(self.HEIGHT * scale)))
        theme = {**_DEFAULT_PALETTE, **(palette or {})}
        width, height = self.size
        self.window.geometry(f"{width}x{height}")
        self.window.attributes("-alpha", self.settings.opacity)
        self.rounded_window.radius = max(1, round(self.CORNER_RADIUS * scale))
        canvas = self.canvas
        canvas.delete("all")
        canvas.configure(width=width, height=height, bg=theme["bg"])
        self.window.configure(bg=theme["bg"])
        self._background(theme)
        if self._background_photo is not None:
            canvas.create_image(0, 0, image=self._background_photo, anchor="nw")

        # The native region clips both the backdrop and this fine outline.
        inset = .5 / scale
        x1, y1 = inset, inset
        x2, y2 = width / scale - inset, height / scale - inset
        r = self.CORNER_RADIUS
        border = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
                  x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
                  x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        canvas.create_polygon(
            *(coordinate * scale for coordinate in border), smooth=True,
            splinesteps=24, fill="", outline=_mix(theme["line"], theme["bg"], .23), width=1,
        )
        now = time.time()
        stale = bool(snapshot and now - snapshot.captured_at > self.STALE_SECONDS)
        five_hour = snapshot.five_hour if snapshot else None
        weekly = snapshot.weekly if snapshot else None
        expired = self._group("5h", five_hour, 42, theme["accent"], theme, now, stale)
        expired |= self._group("7d", weekly, 91, theme["second"], theme, now, stale)
        local = bool(snapshot and "快照" in snapshot.source)
        if error or stale or expired or local:
            dot = "#e5b57a"
        elif busy:
            dot = theme["accent"]
        elif five_hour is not None or weekly is not None:
            dot = "#96d1b0"
        else:
            dot = theme["muted"]
        canvas.create_oval(6 * scale, 13.5 * scale, 9 * scale, 16.5 * scale, fill=dot, outline="")
        self._text(25, 15, "余量", 9, theme["muted"], family="Microsoft YaHei UI")
        self._line((8, 28, 32, 28), fill=_mix(theme["line"], theme["bg"], .28))
        # Three small dots are a quiet affordance for the hover detail panel.
        for x in (15, 20, 25):
            radius = .8 * scale
            canvas.create_oval(x * scale - radius, 143 * scale - radius,
                               x * scale + radius, 143 * scale + radius,
                               fill=theme["muted"], outline="")

    def bounds(self) -> tuple[int, int, int, int]:
        left, top = self.window.winfo_rootx(), self.window.winfo_rooty()
        width, height = self.size
        return left, top, left + width, top + height

    def destroy(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = getattr(self.rounded_window, "_pending", None)
        if pending is not None:
            try:
                self.window.after_cancel(pending)
            except tk.TclError:
                pass
        self._background_photo = None
        try:
            self.window.destroy()
        except tk.TclError:
            pass
