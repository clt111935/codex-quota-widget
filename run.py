"""Launch, install, inspect, and restore the current-user quota widget."""
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
    parser.add_argument("--demo", action="store_true", help="显示演示数据，不读取账户")
    parser.add_argument("--check", action="store_true", help="读取真实额度 JSON，不显示窗口")
    parser.add_argument("--screenshot", type=Path, help="保存面板截图并退出")
    parser.add_argument("--config", type=Path, help="指定独立配置（不管理登录启动任务）")
    parser.add_argument("--background", action="store_true", help="由 Windows 计划任务运行的独立后台实例")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    startup_options = parser.add_mutually_exclusive_group()
    startup_options.add_argument("--enable-startup", action="store_true", help="安装当前用户的登录计划任务并立即运行")
    startup_options.add_argument("--disable-startup", action="store_true", help="删除本项目的登录计划任务，保留当前窗口")
    startup_options.add_argument("--startup-status", action="store_true", help="显示启动任务、进程及窗口状态")
    window_options = parser.add_mutually_exclusive_group()
    window_options.add_argument("--restore", action="store_true", help="唤回已有窗口；未运行时启动")
    window_options.add_argument("--stop", action="store_true", help="退出当前实例，保留自动启动配置")
    args = parser.parse_args()
    if args.worker and not args.background:
        parser.error("内部界面进程必须由后台守护启动。")
    if args.background and (args.demo or args.check or args.screenshot or args.config):
        parser.error("后台守护仅用于正常配置，不能与演示、诊断、截图或独立配置组合。")
    if (args.demo or args.check or args.screenshot or args.config or args.background) and (
            args.enable_startup or args.disable_startup):
        parser.error("安装或关闭登录启动任务时不能指定演示、诊断、截图、独立配置或后台参数。")
    from quota_widget.startup import SingleInstance, StartupRegistration
    from quota_widget.diagnostics import RuntimeLog, runtime_paths
    from quota_widget.supervisor import ProcessSupervisor, supervisor_config
    config = args.config or PROJECT_ROOT / ".local" / ("demo-settings.json" if args.demo else "settings.json")
    lock = SingleInstance(config)
    startup = StartupRegistration(PROJECT_ROOT / "run.py")
    if args.startup_status:
        status = startup.status()
        status["instance_running"] = lock.running()
        status["supervisor_running"] = SingleInstance(supervisor_config(config)).running()
        log_path, status_path = runtime_paths(config)
        status["log_file"] = str(log_path)
        status["supervisor_log_file"] = str(runtime_paths(supervisor_config(config))[0])
        try:
            runtime = json.loads(status_path.read_text(encoding="utf-8"))
            status["runtime"] = {key: runtime[key] for key in ("pid", "managed", "state", "updated_at") if key in runtime}
        except (OSError, ValueError, TypeError):
            status["runtime"] = None
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    if args.check:
        from quota_widget.usage import UsageService
        from quota_widget.settings import load_settings
        try:
            snapshot = UsageService(codex_command=load_settings(config).codex_command or None).read_snapshot()
            print(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2))
            return 0
        except Exception as error:
            print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
            return 1

    log = RuntimeLog(supervisor_config(config) if args.background and not args.worker else config)
    app = None
    try:
        if args.disable_startup:
            startup.disable()
            lock.signal("reload_startup")
            log.event("startup_disabled", task_name=startup.task_name)
            print(json.dumps({"enabled": False}))
            return 0
        if args.stop:
            sent = lock.signal("stop", wait=.5)
            sent = SingleInstance(supervisor_config(config)).signal("stop") or sent
            log.event("stop_requested")
            print(json.dumps({"stop_requested": sent}))
            return 0
        if args.enable_startup:
            startup.enable()
            log.event("startup_enabled", task_name=startup.task_name)
            if lock.running():
                if not lock.signal("restart_managed", wait=1):
                    raise RuntimeError("LegacyInstanceNeedsUpgrade")
            else:
                startup.run()
            print(json.dumps({"enabled": True, "mechanism": "scheduled_task", "task_name": startup.task_name}))
            return 0

        normal_profile = not args.demo and not args.screenshot and not args.config
        registration = startup if normal_profile else None
        if args.background:
            startup.restore_environment()
            if not args.worker:
                guardian = SingleInstance(supervisor_config(config))
                if not guardian.acquire():
                    log.event("duplicate_supervisor_ignored")
                    return 0
                try:
                    command = [str(startup.pythonw_path), str(PROJECT_ROOT / "run.py"), "--background", "--worker"]
                    return ProcessSupervisor(command, guardian, lock, log).run()
                finally:
                    guardian.release()
        # A user launch wakes the existing instance, or asks Task Scheduler to
        # start it. The managed GUI is never a child of Codex's integrated terminal.
        if normal_profile and not args.background and startup.is_enabled():
            if lock.signal("restore", wait=.5):
                log.event("existing_instance_restored")
            elif not lock.running():
                startup.run()
                SingleInstance(supervisor_config(config)).signal("restore", wait=5)
                log.event("managed_start_requested")
            else:
                raise RuntimeError("LegacyInstanceNeedsUpgrade")
            return 0
        if not lock.acquire():
            if not args.background:
                lock.signal("restore", wait=1)
            log.event("duplicate_instance_ignored", managed=args.background)
            return 0
        if os.name == "nt":
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                pass
        import tkinter as tk
        from quota_widget.app import QuotaApp
        root = tk.Tk()
        root.tk.call("tk", "scaling", 96 / 72)
        app = QuotaApp(root, config, demo=args.demo, startup=registration,
                       control=lock if normal_profile else None, managed=args.background, runtime_log=log)
        def callback_failure(kind, error, tb):
            log.failure("gui_callback_failed", error)
            app._exit_code = 1
            app.close()
        root.report_callback_exception = callback_failure
        log.event("instance_started", pid=os.getpid(), managed=args.background)
        log.state(app._runtime_state(), managed=args.background)
        if args.screenshot:
            root.attributes("-alpha", 1.0)
            def capture():
                try:
                    app.capture(args.screenshot.resolve())
                finally:
                    app.close()
            root.after(1400, capture)
        if args.restore:
            root.after(350, app.restore_window)
        root.mainloop()
        if not app._closed:
            # Native owner-window teardown must not be mistaken for an explicit
            # user exit. Let the supervisor recreate the GUI in this case.
            log.event("gui_loop_ended_unexpectedly")
            app._exit_code = 1
            app.close()
        result = app._exit_code
    except Exception as error:
        log.failure("startup_failed", error)
        if app is not None and not app._closed:
            app.close()
        if not args.background:
            print(json.dumps({"error_type": type(error).__name__, "log_file": str(log.log_path)}), file=sys.stderr)
        return 1
    finally:
        lock.release()
        restart = bool(app and app._restart_task)
        log.close()
    if restart:
        startup.run()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
