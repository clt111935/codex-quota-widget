"""Launch the Codex quota companion, or print a read-only quota diagnostic."""
from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex 用量伴侣 · Windows 桌面悬浮面板")
    parser.add_argument("--demo", action="store_true", help="仅显示演示数据，不读取账户")
    parser.add_argument("--check", action="store_true", help="读取真实额度并输出 JSON，不显示窗口")
    parser.add_argument("--screenshot", type=Path, help="启动后保存面板截图并退出（建议与 --demo 一起使用）")
    parser.add_argument("--config", type=Path, help="指定本地设置文件")
    args = parser.parse_args()
    config = args.config or PROJECT_ROOT / ".local" / ("demo-settings.json" if args.demo else "settings.json")
    if args.check:
        from quota_widget.usage import UsageService
        from quota_widget.settings import load_settings
        try:
            snapshot = UsageService(codex_command=load_settings(config).codex_command or None).read_snapshot()
            print(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1

    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    import tkinter as tk
    from quota_widget.app import QuotaApp
    root = tk.Tk()
    # Canvas coordinates are pixels; make point-sized fonts predictable at Windows DPI.
    root.tk.call("tk", "scaling", 96 / 72)
    app = QuotaApp(root, config, demo=args.demo)
    if args.screenshot:
        # Keep exported previews independent of whatever happens to be behind the panel.
        root.attributes("-alpha", 1.0)
        def take_screenshot():
            try:
                app.capture(args.screenshot.resolve())
            finally:
                app.close()
        root.after(1400, take_screenshot)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback
        # Visible launches get a traceback; pythonw launches also leave a local diagnostic.
        error_path = PROJECT_ROOT / ".local" / "startup-error.log"
        error_path.parent.mkdir(parents=True, exist_ok=True)
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, f"面板启动失败，诊断已保存：\n{error_path}", "Codex 用量伴侣", 16)
        raise
