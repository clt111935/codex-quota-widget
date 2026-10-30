"""Per-user Task Scheduler registration and named-mutex/event instance control."""
from __future__ import annotations

import ctypes
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


class StartupRegistration:
    KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    VALUE = "CodexQuotaWidget"  # Legacy entry; migrate only this project's exact command.

    def __init__(self, script_path: Path):
        self.script_path = script_path.resolve()
        identity = getpass.getuser().casefold() + "|" + str(self.script_path).casefold()
        self.task_name = "CodexQuotaWidget-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        self._cached_status = None

    @property
    def pythonw_path(self) -> Path:
        return Path(sys.executable).with_name("pythonw.exe")

    @property
    def arguments(self) -> str:
        return '"' + str(self.script_path) + '" --background'

    @property
    def command(self) -> str:
        return subprocess.list2cmdline([str(self.pythonw_path)]) + " " + self.arguments

    def _task(self, operation: str) -> dict:
        if os.name != "nt":
            if operation == "Status":
                return {"enabled": False, "installed": False, "mechanism": "unsupported"}
            raise OSError("自动启动目前仅支持 Windows。")
        script = Path(__file__).resolve().parent.parent / "Startup-Task.ps1"
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        try:
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                 "-Operation", operation, "-TaskName", self.task_name, "-ScriptPath", str(self.script_path),
                 "-PythonwPath", str(self.pythonw_path)],
                capture_output=True, text=True, encoding="utf-8-sig", errors="replace", timeout=25,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            status = json.loads(result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise OSError("无法连接 Windows 任务计划程序；请查看项目运行日志。") from error
        if result.returncode or "error" in status:
            code = str(status.get("hresult", "unknown"))
            error = OSError(f"当前用户计划任务操作失败（{code}）；未请求管理员权限。")
            if len(code) == 8 and all(char in "0123456789abcdefABCDEF" for char in code):
                error.winerror = int(code, 16)
            raise error
        self._cached_status = status
        return status

    def status(self, *, refresh=True) -> dict:
        if refresh or self._cached_status is None:
            return self._task("Status")
        return dict(self._cached_status)

    def is_enabled(self) -> bool:
        return bool(self.status(refresh=False).get("enabled"))

    def _remove_legacy(self):
        if os.name != "nt":
            return
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
                value, kind = winreg.QueryValueEx(key, self.VALUE)
                expected = subprocess.list2cmdline([str(self.pythonw_path), str(self.script_path)])
                if kind == winreg.REG_SZ and isinstance(value, str) and value.casefold() == expected.casefold():
                    winreg.DeleteValue(key, self.VALUE)
        except FileNotFoundError:
            pass

    def enable(self) -> None:
        if not self.script_path.is_file() or not self.pythonw_path.is_file():
            raise OSError("项目入口或 pythonw.exe 不存在，未安装自动启动任务。")
        self._task("Enable")
        self._remove_legacy()
        # Preserve only this non-secret path, never credentials or the whole environment.
        context_path = self.script_path.parent / ".local" / "startup-context.json"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(json.dumps({"codex_home": os.environ.get("CODEX_HOME")}), encoding="utf-8")

    def disable(self) -> None:
        self._task("Disable")
        self._remove_legacy()

    def run(self) -> None:
        self._task("Run")

    def restore_environment(self):
        if os.environ.get("CODEX_HOME"):
            return
        path = self.script_path.parent / ".local" / "startup-context.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = data.get("codex_home")
            if isinstance(value, str) and value:
                os.environ["CODEX_HOME"] = value
        except (OSError, ValueError, AttributeError):
            pass


class SingleInstance:
    ACTIONS = ("restore", "stop", "reload_startup", "restart_managed")

    def __init__(self, config_path: Path):
        identity = str(config_path.resolve()).casefold().encode("utf-8")
        self.name = "Local\\CodexQuotaWidget_" + hashlib.sha256(identity).hexdigest()[:24]
        self._handle = None
        self._events = {}
        self._kernel32 = None

    def _bind(self):
        if self._kernel32 is not None:
            return
        from ctypes import wintypes
        k = self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.OpenMutexW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = wintypes.HANDLE
        k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.OpenEventW.restype = wintypes.HANDLE
        k.SetEvent.argtypes = [wintypes.HANDLE]
        k.SetEvent.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        self._bind()
        ctypes.set_last_error(0)
        handle = self._kernel32.CreateMutexW(None, False, self.name)
        error = ctypes.get_last_error()
        if not handle:
            raise OSError(error, "无法创建面板实例锁。")
        if error == 183:
            self._kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        try:
            for action in self.ACTIONS:
                event = self._kernel32.CreateEventW(None, False, False, self.name + "_" + action)
                if not event:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._events[action] = event
        except OSError:
            self.release()
            raise
        return True

    def running(self) -> bool:
        if os.name != "nt":
            return False
        self._bind()
        handle = self._kernel32.OpenMutexW(0x00100000, False, self.name)
        if not handle:
            return False
        self._kernel32.CloseHandle(handle)
        return True

    def signal(self, action: str, *, wait=0.0) -> bool:
        if action not in self.ACTIONS:
            raise ValueError("Unknown instance command")
        if os.name != "nt":
            return False
        self._bind()
        deadline = time.monotonic() + wait
        while True:
            handle = self._kernel32.OpenEventW(0x0002, False, self.name + "_" + action)
            if handle:
                try:
                    return bool(self._kernel32.SetEvent(handle))
                finally:
                    self._kernel32.CloseHandle(handle)
            if time.monotonic() >= deadline:
                return False
            time.sleep(.025)

    def pending(self) -> list[str]:
        return [action for action, handle in self._events.items()
                if self._kernel32.WaitForSingleObject(handle, 0) == 0]

    def release(self) -> None:
        for handle in self._events.values():
            self._kernel32.CloseHandle(handle)
        self._events.clear()
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
