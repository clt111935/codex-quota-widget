"""Tk desktop UI. Network and filesystem quota reads never run on the UI thread."""
from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageGrab, ImageOps, ImageTk
except ImportError:
    Image = None

from .settings import Settings, load_settings, save_settings
from .usage import QuotaSnapshot, QuotaWindow, UsageError, UsageService
from .windows import WindowFollower, desktop_bounds


THEMES = {
    "midnight": {"name": "月夜", "bg": "#10151e", "card": "#1a2230", "line": "#2c3748", "text": "#f3f5fa", "muted": "#93a1b5", "accent": "#b5c9ff", "second": "#c5b8f8", "glow": "#233b67"},
    "ocean": {"name": "深海", "bg": "#0d1d25", "card": "#142f39", "line": "#254852", "text": "#ecfaff", "muted": "#8fb2bd", "accent": "#82dfeb", "second": "#a6c7ff", "glow": "#164b5d"},
    "forest": {"name": "松林", "bg": "#131e1a", "card": "#20332a", "line": "#344e40", "text": "#f0f8ef", "muted": "#9aafa1", "accent": "#b8dfad", "second": "#e0d5a2", "glow": "#324c35"},
}


def countdown(resets_at: int | None, now: float | None = None) -> str:
    if resets_at is None:
        return "重置时间未提供"
    seconds = math.ceil(resets_at - (now if now is not None else time.time()))
    if seconds <= 0:
        return "已到重置时间 · 等待刷新"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days} 天 {hours:02d} 小时后重置"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d} 后重置"


def snapshot_age(captured_at: float) -> str:
    age = max(0, int(time.time() - captured_at))
    if age < 60:
        return "刚刚更新"
    if age < 3600:
        return f"{age // 60} 分钟前更新"
    if age < 86400:
        return f"{age // 3600} 小时前更新"
    return f"{age // 86400} 天前更新"


def demo_snapshot() -> QuotaSnapshot:
    now = time.time()
    return QuotaSnapshot(
        five_hour=QuotaWindow(24, 300, int(now + 8070)),
        weekly=QuotaWindow(42, 10080, int(now + 324000)),
        plan_type="plus", captured_at=now, source="演示数据",
    )


class QuotaApp:
    WIDTH = 420
    HEIGHT = 504
    COMPACT_HEIGHT = 246

    def __init__(self, root: tk.Tk, config_path: Path, *, demo: bool = False,
                 service: UsageService | None = None):
        self.root = root
        self.config_path = config_path
        self.settings = load_settings(config_path)
        self.demo = demo
        if demo:
            self.settings = replace(self.settings, follow_codex=False)
        self.service = service or UsageService(codex_command=self.settings.codex_command or None)
        self.snapshot: QuotaSnapshot | None = demo_snapshot() if demo else None
        self.error = ""
        self.busy = False
        self.last_attempt = 0.0
        self.results: queue.Queue = queue.Queue()
        self.actions: list[tuple[tuple[int, int, int, int], object]] = []
        self.settings_window: tk.Toplevel | None = None
        self._background_key = None
        self._background_photo = None
        self._background_warning = ""
        self._closed = False
        self._drag_start = None
        self._last_click = None
        self.follow_status = "独立悬浮"
        root.title("Codex 用量伴侣")
        root.overrideredirect(True)
        root.configure(bg=THEMES[self.settings.theme]["bg"])
        self.canvas = tk.Canvas(root, width=self.WIDTH, height=self.height,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        x = self.settings.x if self.settings.x is not None else max(0, root.winfo_screenwidth() - self.WIDTH - 42)
        y = self.settings.y if self.settings.y is not None else 100
        x, y = self._clamp_position(x, y)
        root.geometry(f"{self.WIDTH}x{self.height}+{x}+{y}")
        root.attributes("-alpha", self.settings.opacity)
        root.update_idletasks()
        self.follower = WindowFollower(root)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Button-3>", self._context_menu)
        root.bind("<Escape>", lambda _: self.close())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.draw()
        self.root.after(100, self._tick)
        self.root.after(200, self._follow_tick)
        if not demo:
            self.refresh()

    @property
    def height(self):
        return self.COMPACT_HEIGHT if self.settings.compact else self.HEIGHT

    def _persist(self):
        try:
            save_settings(self.config_path, self.settings)
        except OSError:
            self.error = "设置未能保存，请检查项目目录的写入权限"

    def _clamp_position(self, x, y, *, width=None, height=None):
        left, top, screen_w, screen_h = desktop_bounds(self.root)
        return (max(left, min(x, left+max(0,screen_w-(width or self.WIDTH)))),
                max(top, min(y, top+max(0,screen_h-(height or self.height)))))

    @staticmethod
    def _short_filename(path):
        if not path:
            return "未选择图片"
        name = Path(path).name
        return name if len(name) <= 28 else name[:16] + "…" + name[-10:]

    def _text(self, x, y, text, size=10, color=None, *, bold=False, anchor="nw", family="Microsoft YaHei UI", **kw):
        return self.canvas.create_text(x, y, text=text, fill=color or THEMES[self.settings.theme]["text"],
                                       font=(family, size, "bold" if bold else "normal"), anchor=anchor, **kw)

    def _rounded(self, x1, y1, x2, y2, fill, radius=16, outline=""):
        # Smooth polygons remain native and render correctly at Windows DPI settings.
        points = [x1+radius,y1,x2-radius,y1,x2,y1,x2,y1+radius,x2,y2-radius,x2,y2,
                  x2-radius,y2,x1+radius,y2,x1,y2,x1,y2-radius,x1,y1+radius,x1,y1]
        return self.canvas.create_polygon(points, smooth=True, splinesteps=24, fill=fill, outline=outline)

    def _button(self, bounds, label, callback, *, color=None, size=13):
        x1,y1,x2,y2 = bounds
        self._text((x1+x2)/2, (y1+y2)/2, label, size, color, anchor="center", family="Segoe UI Symbol")
        self.actions.append((bounds, callback))

    def _make_background(self):
        theme = THEMES[self.settings.theme]
        key = (self.settings.theme, self.settings.background_path, self.settings.background_dim, self.height)
        if key == self._background_key:
            return
        self._background_key = key
        self._background_warning = ""
        if Image is None:
            self._background_photo = None
            if self.settings.background_path:
                self._background_warning = "背景图片需要 Pillow，请参考说明安装"
            return
        base = Image.new("RGB", (self.WIDTH, self.height), theme["bg"])
        if self.settings.background_path:
            try:
                with Image.open(self.settings.background_path) as original:
                    original = ImageOps.exif_transpose(original)
                    backdrop = ImageOps.fit(original.convert("RGB"), base.size, method=Image.Resampling.LANCZOS)
                base = Image.blend(backdrop, base, self.settings.background_dim)
            except (OSError, ValueError, Image.DecompressionBombError):
                self._background_warning = "背景图片无法读取，已使用主题背景"
        else:
            glow = Image.new("RGB", base.size, theme["bg"])
            painter = ImageDraw.Draw(glow)
            painter.ellipse((180, -160, 600, 200), fill=theme["glow"])
            glow = glow.filter(ImageFilter.GaussianBlur(65))
            base = Image.blend(base, glow, .65)
        self._background_photo = ImageTk.PhotoImage(base, master=self.root)

    def draw(self):
        if self._closed:
            return
        c = self.canvas
        theme = THEMES[self.settings.theme]
        c.delete("all")
        c.configure(bg=theme["bg"], height=self.height)
        self.actions.clear()
        self._make_background()
        if self._background_photo:
            c.create_image(0, 0, image=self._background_photo, anchor="nw")
        c.create_rectangle(0, 0, self.WIDTH-1, self.height-1, outline=theme["line"])
        self._rounded(22, 23, 58, 59, theme["card"], 11)
        self._text(40, 41, "⌘", 20, theme["accent"], anchor="center", family="Segoe UI Symbol")
        self._text(70, 22, "Codex", 16, bold=True, family="Segoe UI")
        self._text(71, 49, "用 量 伴 侣", 8, theme["muted"])
        self._button((292,22,324,58), "↻", self.refresh, color=theme["accent"], size=19)
        self._button((329,22,361,58), "⚙", self.open_settings, color=theme["muted"], size=15)
        self._button((368,22,400,58), "×", self.close, color=theme["muted"], size=19)
        if self.settings.compact:
            self._draw_compact(theme)
            return
        c.create_line(24, 79, 396, 79, fill=theme["line"])
        self._text(24, 93, "订阅剩余额度", 10, theme["muted"])
        plan = (self.snapshot.plan_type or "CODEX").upper() if self.snapshot else "CODEX"
        if self.demo:
            plan = "DEMO · 演示"
        self._text(394, 95, plan[:24], 9, theme["accent"], anchor="ne", family="Segoe UI")
        self._draw_card(122, "5 小时", "短周期额度", self.snapshot.five_hour if self.snapshot else None, theme["accent"])
        self._draw_card(264, "一周", "每周额度", self.snapshot.weekly if self.snapshot else None, theme["second"])
        self._draw_source(420, theme)
        c.create_line(24, 454, 396, 454, fill=theme["line"])
        self._text(24, 475, self.follow_status if self.settings.follow_codex else "独立悬浮", 9, theme["muted"], anchor="w")
        self.actions.append(((18,460,240,494), self.toggle_follow))
        self._text(394, 475, "外观设置  ›", 9, theme["accent"], anchor="e")
        self.actions.append(((292,460,402,494), self.open_settings))

    def _draw_card(self, y, title, subtitle, window, accent):
        theme = THEMES[self.settings.theme]
        self._rounded(24, y, 396, y+128, theme["card"], 18, theme["line"])
        self._text(43, y+15, title, 11, bold=True)
        self._text(375, y+18, subtitle, 9, theme["muted"], anchor="ne")
        expired = window is not None and window.resets_at is not None and window.resets_at <= time.time()
        if window is None:
            self._text(42, y+39, "—", 32, theme["muted"], family="Segoe UI")
            self._text(113, y+59, "未提供", 10, theme["muted"])
            percent = 0
            label = "账户未返回此周期额度" if self.snapshot else ("正在读取账户额度…" if self.busy else "等待额度数据")
        else:
            percent = window.remaining_percent
            display = f"{percent:.0f}" if percent == int(percent) else f"{percent:.1f}"
            # Never round a tiny positive balance into an apparent 0% or 100%.
            if 0 < percent < .1:
                display = "<0.1"
            elif 99.9 < percent < 100:
                display = ">99.9"
            self._text(41, y+34, display, 35, accent if not expired else theme["muted"], bold=True, family="Segoe UI")
            suffix_x = 43 + len(display) * 24
            self._text(suffix_x, y+56, "%", 16, theme["muted"], family="Segoe UI")
            self._text(375, y+67, "上次剩余" if expired else "剩余", 10, theme["muted"], anchor="ne")
            label = countdown(window.resets_at)
        self._rounded(43, y+92, 377, y+97, theme["line"], 2)
        if percent > 0:
            self._rounded(43, y+92, 43+max(3,334*percent/100), y+97,
                          "#efb086" if percent <= 15 else accent, 2)
        self._text(43, y+106, label, 8, theme["muted"])

    def _draw_compact(self, theme):
        for x, title, window, accent in [
            (24,"5 小时",self.snapshot.five_hour if self.snapshot else None,theme["accent"]),
            (224,"一周",self.snapshot.weekly if self.snapshot else None,theme["second"]),
        ]:
            self._text(x, 84, title, 10, theme["muted"])
            value = f"{window.remaining_percent:g}%" if window else "—"
            self._text(x, 106, value, 27, accent, bold=True, family="Segoe UI")
            self._rounded(x, 155, x+172, 159, theme["line"], 2)
            if window and window.remaining_percent > 0:
                self._rounded(x, 155, x+max(3,172*window.remaining_percent/100), 159, accent, 2)
            self._text(x, 168, countdown(window.resets_at) if window else "未提供此周期", 8, theme["muted"])
        self._draw_source(209, theme)
        self.actions.append(((18,198,402,241), self.open_settings))

    def _draw_source(self, y, theme):
        if self.busy:
            text, dot = "正在同步额度…", theme["accent"]
        elif self.snapshot:
            is_stale = time.time() - self.snapshot.captured_at > 300
            text = self.snapshot.source + (" · 历史快照" if is_stale else "")
            if self.error and "快照" not in self.snapshot.source:
                text = "同步失败 · 保留上次数据"
            dot = "#e5b57a" if is_stale or self.error or "快照" in self.snapshot.source else "#96d1b0"
        else:
            text, dot = "暂未连接", "#e5b57a"
        self.canvas.create_oval(25,y+4,31,y+10,fill=dot,outline="")
        self._text(39, y, text, 9, theme["muted"])
        if self.snapshot:
            self._text(394, y+2, snapshot_age(self.snapshot.captured_at), 8, theme["muted"], anchor="ne")
        warning = self._background_warning or self.error
        if not self.settings.compact:
            self.actions.append(((18,y-4,402,y+34), self.open_connection))
        if warning and not self.settings.compact:
            warning = warning.replace("\n", " ")
            short = warning if len(warning) <= 33 else warning[:32] + "…"
            self._text(24, y+20, short, 8, "#e5b57a")
        elif not self.settings.compact:
            self._text(24, y+20, "演示模式 · 不读取账户" if self.demo else "额度以 Codex 账户返回的数据为准", 8, theme["muted"])

    def refresh(self):
        if self._closed or self.busy:
            return
        if self.demo:
            self.snapshot = demo_snapshot()
            self.draw()
            return
        self.busy = True
        self.last_attempt = time.monotonic()
        self.draw()

        def worker():
            try:
                snapshot = self.service.read_snapshot()
                self.results.put((snapshot, getattr(self.service, "last_warning", "")))
            except Exception as exc:
                # Provider messages contain no raw RPC responses or authentication data.
                self.results.put((None, str(exc) if isinstance(exc, UsageError) else "读取额度失败，请检查 Codex 登录状态"))
        threading.Thread(target=worker, name="quota-read", daemon=True).start()

    def _tick(self):
        if self._closed:
            return
        try:
            snapshot, error = self.results.get_nowait()
            self.busy = False
            self.error = error
            if snapshot:
                self.snapshot = snapshot
        except queue.Empty:
            pass
        if not self.demo and not self.busy and time.monotonic() - self.last_attempt >= self.settings.refresh_seconds:
            self.refresh()
        self.draw()
        self.root.after(1000, self._tick)

    def _follow_tick(self):
        if not self._closed:
            try:
                self.follow_status = self.follower.update(self.settings.follow_codex)
            except (OSError, tk.TclError):
                self.follow_status = "暂时无法跟随窗口"
            self.root.after(800, self._follow_tick)

    def toggle_follow(self):
        self.settings.follow_codex = not self.settings.follow_codex
        self._persist()
        self.draw()

    def _press(self, event):
        self._last_click = (event.x_root, event.y_root)
        if event.y < 77 and not any(x1 <= event.x <= x2 and y1 <= event.y <= y2 for (x1,y1,x2,y2),_ in self.actions):
            self._drag_start = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag(self, event):
        if self._drag_start:
            sx,sy,wx,wy = self._drag_start
            if abs(event.x_root-sx) + abs(event.y_root-sy) < 4:
                return
            if self.settings.follow_codex:
                self.settings.follow_codex = False
                self.follower.detach()
            x, y = self._clamp_position(wx+event.x_root-sx, wy+event.y_root-sy)
            self.root.geometry(f"+{x}+{y}")

    def _release(self, event):
        dragged = self._drag_start and self._last_click and abs(event.x_root-self._last_click[0])+abs(event.y_root-self._last_click[1]) > 4
        if dragged:
            self.settings.x, self.settings.y = self.root.winfo_x(), self.root.winfo_y()
            self._persist()
        self._drag_start = None
        if not dragged:
            for (x1,y1,x2,y2), action in self.actions:
                if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                    action()
                    break

    def _hover(self, event):
        clickable = any(x1 <= event.x <= x2 and y1 <= event.y <= y2 for (x1,y1,x2,y2),_ in self.actions)
        self.canvas.configure(cursor="hand2" if clickable else "fleur" if event.y < 77 else "")

    def _context_menu(self, event):
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="刷新额度", command=self.refresh)
        menu.add_command(label="外观与设置", command=self.open_settings)
        menu.add_command(label="数据与连接", command=self.open_connection)
        menu.add_command(label="切换精简模式", command=self.toggle_compact)
        menu.add_command(label="切换窗口跟随", command=self.toggle_follow)
        menu.add_separator()
        menu.add_command(label="退出", command=self.close)
        menu.tk_popup(event.x_root, event.y_root)

    def toggle_compact(self):
        self.settings.compact = not self.settings.compact
        self.root.geometry(f"{self.WIDTH}x{self.height}")
        self._persist()
        self.draw()

    def open_settings(self):
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        win = tk.Toplevel(self.root)
        self.settings_window = win
        win.title("外观与设置 · Codex 用量伴侣")
        win.resizable(False, False)
        win.transient(self.root)
        win.configure(bg="#141c28")
        sx, sy = self._clamp_position(self.root.winfo_x()-480, self.root.winfo_y(), width=470, height=644)
        win.geometry(f"470x644+{sx}+{sy}")
        style = ttk.Style(win)
        style.theme_use("clam")
        style.configure("Quota.TFrame", background="#141c28")
        style.configure("Quota.TLabel", background="#141c28", foreground="#e9edf7", font=("Microsoft YaHei UI", 10))
        style.configure("Hint.Quota.TLabel", foreground="#96a6bb", font=("Microsoft YaHei UI", 9))
        style.configure("Quota.TCheckbutton", background="#141c28", foreground="#e9edf7", font=("Microsoft YaHei UI", 10))
        style.map("Quota.TCheckbutton", background=[("active", "#223147")], foreground=[("active", "#ffffff")])
        style.configure("Quota.TButton", padding=(10,7), font=("Microsoft YaHei UI", 9))
        frame = ttk.Frame(win, padding=24, style="Quota.TFrame")
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="让余量，也有你的风格。", style="Quota.TLabel", font=("Microsoft YaHei UI",16,"bold")).pack(anchor="w", pady=(0,5))
        ttk.Label(frame, text="设置即时生效，自动保存在本地。", style="Hint.Quota.TLabel").pack(anchor="w", pady=(0,18))

        def heading(text):
            ttk.Label(frame, text=text, style="Quota.TLabel").pack(anchor="w", pady=(10,7))

        heading("配色主题")
        theme_row = ttk.Frame(frame, style="Quota.TFrame")
        theme_row.pack(fill="x")
        for key, theme in THEMES.items():
            ttk.Button(theme_row, text=f"●  {theme['name']}", style="Quota.TButton", command=lambda k=key: change("theme",k)).pack(side="left", padx=(0,10))
        heading("自定义背景")
        path_var = tk.StringVar(value=self._short_filename(self.settings.background_path))
        ttk.Label(frame, textvariable=path_var, style="Hint.Quota.TLabel", wraplength=410).pack(anchor="w")
        image_row = ttk.Frame(frame, style="Quota.TFrame")
        image_row.pack(fill="x", pady=(8,4))

        def choose_image():
            if Image is None:
                messagebox.showinfo("需要安装 Pillow", "在项目目录运行：\npython -m pip install -r requirements.txt", parent=win)
                return
            path = filedialog.askopenfilename(parent=win, title="选择背景图片", filetypes=[("背景图片", "*.png *.jpg *.jpeg *.webp *.bmp"), ("所有文件", "*.*")])
            if path:
                try:
                    with Image.open(path) as image:
                        image.verify()
                except (OSError, ValueError, Image.DecompressionBombError):
                    messagebox.showerror("图片无法读取", "请选择有效的 PNG、JPG、WebP 或 BMP 图片。", parent=win)
                    return
                path_var.set(self._short_filename(path))
                change("background_path",path)

        def clear_image():
            path_var.set("未选择图片")
            change("background_path", "")

        ttk.Button(image_row, text="选择图片…", command=choose_image, style="Quota.TButton").pack(side="left", padx=(0,10))
        ttk.Button(image_row, text="恢复主题背景", command=clear_image, style="Quota.TButton").pack(side="left")
        heading("背景遮罩（更高时文字更清楚）")
        dim_var = tk.DoubleVar(value=self.settings.background_dim)
        ttk.Scale(frame, from_=0.05, to=.95, variable=dim_var, command=lambda value: change("background_dim",round(float(value),2),persist=False)).pack(fill="x")
        heading("窗口不透明度")
        alpha_var = tk.DoubleVar(value=self.settings.opacity)
        ttk.Scale(frame, from_=.45, to=1, variable=alpha_var, command=lambda value: change("opacity",round(float(value),2),persist=False)).pack(fill="x")
        flags = ttk.Frame(frame, style="Quota.TFrame")
        flags.pack(fill="x", pady=(20,8))
        follow_var = tk.BooleanVar(value=self.settings.follow_codex)
        compact_var = tk.BooleanVar(value=self.settings.compact)
        ttk.Checkbutton(flags, text="跟随 Codex 窗口", variable=follow_var, style="Quota.TCheckbutton", command=lambda: change("follow_codex",follow_var.get())).pack(anchor="w", pady=3)
        ttk.Checkbutton(flags, text="精简面板", variable=compact_var, style="Quota.TCheckbutton", command=lambda: change("compact",compact_var.get())).pack(anchor="w", pady=3)
        interval_row = ttk.Frame(frame, style="Quota.TFrame")
        interval_row.pack(fill="x", pady=(8,12))
        ttk.Label(interval_row, text="自动刷新", style="Quota.TLabel").pack(side="left")
        interval_var = tk.StringVar(value=str(self.settings.refresh_seconds))
        picker = ttk.Combobox(interval_row, textvariable=interval_var, values=[30,60,120,300], width=6, state="readonly")
        picker.pack(side="left", padx=(16,8))
        ttk.Label(interval_row, text="秒", style="Quota.TLabel").pack(side="left")
        picker.bind("<<ComboboxSelected>>", lambda _: change("refresh_seconds",int(interval_var.get())))
        ttk.Label(frame, text="拖动面板顶部可自由摆放，并切换为独立悬浮。\n右键面板可刷新、切换精简模式或退出。", style="Hint.Quota.TLabel").pack(anchor="w", pady=(2,10))
        ttk.Button(frame, text="完成", style="Quota.TButton", command=lambda: close_settings()).pack(anchor="e")

        def change(key, value, persist=True):
            setattr(self.settings, key, value)
            if key == "opacity":
                self.root.attributes("-alpha", value)
            elif key == "compact":
                self.root.geometry(f"{self.WIDTH}x{self.height}")
            if persist:
                self._persist()
            self.draw()

        def close_settings():
            self._persist()
            self.settings_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_settings)
        win.bind("<Escape>", lambda _: close_settings())

    def open_connection(self):
        win = tk.Toplevel(self.root)
        win.title("数据与连接 · Codex 用量伴侣")
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(bg="#141c28")
        frame = tk.Frame(win, bg="#141c28", padx=24, pady=24)
        frame.pack(fill="both", expand=True)

        def label(text, *, muted=False, bold=False):
            widget = tk.Label(frame, text=text, bg="#141c28", fg="#98a8bc" if muted else "#edf2fa",
                              font=("Microsoft YaHei UI", 10, "bold" if bold else "normal"),
                              justify="left", anchor="w", wraplength=410)
            widget.pack(fill="x", pady=(0,10))
            return widget

        label("额度来源", bold=True)
        if self.snapshot:
            label(f"{self.snapshot.source} · {self.snapshot.limit_id}\n采集时间：{datetime.fromtimestamp(self.snapshot.captured_at):%Y-%m-%d %H:%M:%S}")
            for name, window in [("5 小时", self.snapshot.five_hour), ("一周", self.snapshot.weekly)]:
                reset = datetime.fromtimestamp(window.resets_at).strftime("%Y-%m-%d %H:%M:%S") if window and window.resets_at else "未提供"
                label(f"{name}重置时间：{reset}", muted=True)
        else:
            label("尚未取得额度数据", muted=True)
        if self.error:
            label(self.error, muted=True)
        label("Codex 命令行", bold=True)
        path_label = label(self.settings.codex_command or "自动查找桌面版附带的 CLI / PATH", muted=True)
        row = tk.Frame(frame, bg="#141c28")
        row.pack(fill="x", pady=(0,18))

        def set_command(path):
            self.settings.codex_command = path
            self.service.codex_command = path or None
            path_label.configure(text=path or "自动查找桌面版附带的 CLI / PATH")
            self._persist()
            self.refresh()

        def choose_command():
            path = filedialog.askopenfilename(parent=win, title="选择 Codex CLI", filetypes=[("Codex CLI", "codex.exe"), ("应用程序", "*.exe")])
            if path:
                set_command(path)

        ttk.Button(row, text="选择 codex.exe…", command=choose_command).pack(side="left", padx=(0,10))
        ttk.Button(row, text="恢复自动查找", command=lambda: set_command("")).pack(side="left")
        label("使用 Codex 管理的账户登录。CLI 与桌面客户端请保持相同账户；本地历史快照可能来自切换账户之前。", muted=True)
        ttk.Button(frame, text="关闭", command=win.destroy).pack(anchor="e")
        win.bind("<Escape>", lambda _: win.destroy())

    def capture(self, path: Path):
        if Image is None:
            raise RuntimeError("截图需要 Pillow")
        self.root.update_idletasks()
        self.root.lift()
        self.root.update()
        x,y = self.root.winfo_rootx(), self.root.winfo_rooty()
        path.parent.mkdir(parents=True, exist_ok=True)
        ImageGrab.grab(bbox=(x,y,x+self.WIDTH,y+self.height), all_screens=True).save(path)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if not self.settings.follow_codex:
            self.settings.x, self.settings.y = self.root.winfo_x(), self.root.winfo_y()
        self._persist()
        self.follower.detach()
        self.root.destroy()
