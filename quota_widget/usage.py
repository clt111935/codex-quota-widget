"""Read Codex quota without starting a conversation or reading credential files.

The documented app-server account endpoint is the primary source. If that is
unavailable, a bounded tail of recent session logs supplies an explicitly dated
snapshot. Session contents and raw RPC responses are never logged or persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import heapq
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, Mapping


@dataclass(frozen=True)
class QuotaWindow:
    used_percent: float
    window_minutes: int
    resets_at: int | None

    @property
    def remaining_percent(self) -> float:
        return 100.0 - self.used_percent


@dataclass(frozen=True)
class QuotaSnapshot:
    five_hour: QuotaWindow | None
    weekly: QuotaWindow | None
    plan_type: str | None
    captured_at: float
    source: str
    limit_id: str = "codex"


class UsageError(Exception):
    """A user-safe explanation of an unavailable quota source."""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _get(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _window(value: Any) -> QuotaWindow | None:
    if not isinstance(value, Mapping):
        return None
    used = _number(_get(value, "usedPercent", "used_percent"))
    minutes = _number(_get(value, "windowDurationMins", "window_minutes", "window_duration_mins"))
    if used is None or minutes is None or minutes <= 0 or not minutes.is_integer():
        return None
    reset = _number(_get(value, "resetsAt", "resets_at"))
    # An expired reset is retained: only the server can confirm a reset quota.
    resets_at = int(reset) if reset is not None and reset > 0 else None
    return QuotaWindow(min(100.0, max(0.0, used)), int(minutes), resets_at)


def parse_rate_limits(
    payload: Mapping[str, Any], *, captured_at: float, source: str
) -> QuotaSnapshot:
    """Normalize current app-server fields and older session-log fields.

    Classify windows by their reported duration, never by primary/secondary
    position. A missing or differently sized window remains unavailable.
    """
    if not isinstance(payload, Mapping):
        raise UsageError("Codex 返回的用量数据格式不受支持。")
    multi = _get(payload, "rateLimitsByLimitId", "rate_limits_by_limit_id")
    default = _get(payload, "rateLimits", "rate_limits")
    bucket: Mapping[str, Any] | None = None
    bucket_id = "codex"
    if isinstance(multi, Mapping) and isinstance(multi.get("codex"), Mapping):
        bucket = multi["codex"]
    elif isinstance(default, Mapping):
        bucket = default
    elif "primary" in payload or "secondary" in payload:
        bucket = payload
    elif isinstance(multi, Mapping):
        for key in sorted(multi):
            if isinstance(multi[key], Mapping):
                bucket, bucket_id = multi[key], str(key)
                break
    if bucket is None or not any(key in bucket for key in ("primary", "secondary")):
        raise UsageError("Codex 暂未提供账户用量窗口；请确认已使用 ChatGPT 账户登录。")
    bucket_id = str(_get(bucket, "limitId", "limit_id") or bucket_id)
    windows = [_window(bucket.get(key)) for key in ("primary", "secondary")]
    five_hour = next((window for window in windows if window and window.window_minutes == 300), None)
    weekly = next((window for window in windows if window and window.window_minutes == 10080), None)
    plan = _get(bucket, "planType", "plan_type") or _get(payload, "planType", "plan_type")
    return QuotaSnapshot(
        five_hour=five_hour,
        weekly=weekly,
        plan_type=plan if isinstance(plan, str) else None,
        captured_at=captured_at,
        source=source,
        limit_id=bucket_id,
    )


def _rpc_error(value: Any) -> UsageError:
    # Do not show raw upstream messages: they can include account information.
    details = value if isinstance(value, Mapping) else {}
    message = str(details.get("message", "")).lower()
    code = details.get("code")
    if code == -32601 or "method not found" in message:
        return UsageError("当前 Codex CLI 不支持读取用量，请更新 Codex 客户端。")
    if any(word in message for word in ("unauthorized", "not authenticated", "not logged", "login required", "401")):
        return UsageError("Codex 登录状态不可用，请先在 Codex 客户端登录。")
    if "api key" in message or "chatgpt" in message:
        return UsageError("用量接口需要受支持的账户登录；API Key 模式不提供此额度。")
    if code == 429 or "429" in message or "rate limit" in message:
        return UsageError("Codex 用量接口暂时限流，请稍后重试。")
    suffix = f"（代码 {code}）" if isinstance(code, int) and not isinstance(code, bool) else ""
    return UsageError(f"Codex 用量接口返回错误{suffix}。")


class _RpcClient:
    """Small bounded JSONL client; this client only allows two RPC methods."""

    TIMEOUT = 12.0
    MAX_LINE_BYTES = 1024 * 1024

    def __init__(self, process: subprocess.Popen[str]):
        self.process = process
        self.responses: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=8)
        self.reader = threading.Thread(target=self._read, name="quota-rpc-reader", daemon=True)
        self.reader.start()

    def _enqueue(self, message: dict[str, Any] | None) -> None:
        try:
            self.responses.put_nowait(message)
        except queue.Full:
            pass

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                line = self.process.stdout.readline(self.MAX_LINE_BYTES)
                if not line:
                    break
                if not line.endswith("\n"):
                    # Discard the rest of an oversized line without parsing it.
                    while line and not line.endswith("\n"):
                        line = self.process.stdout.readline(self.MAX_LINE_BYTES)
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(message, dict) and message.get("id") in (1, 2) and ("result" in message or "error" in message):
                    self._enqueue(message)
        except (OSError, ValueError):
            pass
        finally:
            self._enqueue(None)

    def send(self, message: Mapping[str, Any]) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise UsageError("无法与 Codex 用量进程通信。") from exc

    def request(self, request_id: int, method: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if method not in ("initialize", "account/rateLimits/read"):
            raise ValueError("Only initialization and read-only quota requests are allowed")
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        deadline = time.monotonic() + self.TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageError("读取 Codex 实时用量超时，请检查网络后重试。")
            try:
                response = self.responses.get(timeout=remaining)
            except queue.Empty:
                raise UsageError("读取 Codex 实时用量超时，请检查网络后重试。") from None
            if response is None:
                raise UsageError("Codex 用量进程在返回结果前退出。")
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise _rpc_error(response["error"])
            result = response.get("result")
            if not isinstance(result, Mapping):
                raise UsageError("Codex 返回的用量数据格式不受支持。")
            return result


def _timestamp(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric if numeric > 0 else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Do not invent a timezone for old or malformed records.
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


class UsageService:
    MAX_SESSION_FILES = 24
    MAX_TAIL_BYTES = 512 * 1024
    MAX_SCAN_ENTRIES = 20000

    def __init__(self, codex_home: Path | None = None, codex_command: str | None = None):
        configured_home = os.environ.get("CODEX_HOME")
        self.codex_home = Path(codex_home or configured_home or (Path.home() / ".codex")).expanduser()
        self.codex_command = codex_command
        self.last_warning: str | None = None

    def _find_command(self) -> str:
        if self.codex_command:
            configured = Path(self.codex_command).expanduser()
            if configured.is_file():
                return str(configured.resolve())
            resolved = shutil.which(self.codex_command)
            if resolved:
                return resolved
            raise UsageError("设置中的 Codex CLI 路径无效，请选择 codex.exe。")
        executable = shutil.which("codex.exe") or shutil.which("codex")
        if executable and Path(executable).suffix.lower() not in (".cmd", ".bat", ".ps1"):
            return executable
        local = os.environ.get("LOCALAPPDATA")
        if local:
            root = Path(local) / "OpenAI" / "Codex" / "bin"
            try:
                candidates = list(root.glob("*/codex.exe"))
                candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
                if candidates:
                    return str(candidates[0])
            except OSError:
                pass
        if executable and Path(executable).suffix.lower() in (".cmd", ".bat"):
            # npm's shim has a bundled native executable alongside it.
            npm_root = Path(executable).parent / "node_modules" / "@openai"
            try:
                candidates = list(npm_root.glob("codex*/vendor/*/codex/codex.exe"))
                if candidates:
                    return str(candidates[0])
            except OSError:
                pass
        program_files = os.environ.get("ProgramFiles")
        if os.name == "nt" and program_files:
            # Store/MSIX installations may bundle the CLI inside app resources.
            # Access to WindowsApps can be denied; that simply leaves this
            # optional discovery path unavailable.
            root = Path(program_files) / "WindowsApps"
            try:
                candidates = list(root.glob("OpenAI.Codex_*/app/resources/codex.exe"))
                candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
                if candidates:
                    return str(candidates[0])
            except OSError:
                pass
        raise UsageError("未找到 Codex CLI，请在设置中指定 codex.exe 路径。")

    def _read_live(self) -> QuotaSnapshot:
        command = self._find_command()
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.codex_home)
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                [command, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                **options,
            )
        except OSError as exc:
            raise UsageError("无法启动 Codex CLI，请检查 codex.exe 路径。") from exc
        client: _RpcClient | None = None
        try:
            client = _RpcClient(process)
            client.request(1, "initialize", {"clientInfo": {"name": "codex_quota_widget", "title": "Codex Quota Widget", "version": "0.1.0"}})
            client.send({"method": "initialized", "params": {}})
            payload = client.request(2, "account/rateLimits/read")
            return parse_rate_limits(payload, captured_at=time.time(), source="Codex 实时接口")
        finally:
            self._stop_process(process)
            if client is not None:
                client.reader.join(timeout=0.25)

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=1.0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            try:
                process.terminate()
                process.wait(timeout=1.0)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
        finally:
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except (OSError, ValueError):
                    pass

    def _session_files(self) -> list[Path]:
        root = self.codex_home / "sessions"
        if not root.is_dir():
            return []
        candidates: list[tuple[int, str]] = []
        visited = 0
        # Date folders sort newest first, and enumeration has a hard upper bound.
        for directory, subdirectories, files in os.walk(root, followlinks=False):
            subdirectories.sort(reverse=True)
            for name in files:
                visited += 1
                if visited > self.MAX_SCAN_ENTRIES:
                    return [Path(item[1]) for item in sorted(candidates, reverse=True)]
                if not name.endswith(".jsonl"):
                    continue
                path = Path(directory) / name
                try:
                    if path.is_symlink():
                        continue
                    item = (path.stat().st_mtime_ns, str(path))
                except OSError:
                    continue
                if len(candidates) < self.MAX_SESSION_FILES:
                    heapq.heappush(candidates, item)
                elif item > candidates[0]:
                    heapq.heapreplace(candidates, item)
        return [Path(item[1]) for item in sorted(candidates, reverse=True)]

    def _read_local(self) -> QuotaSnapshot:
        files = self._session_files()
        if not files:
            raise UsageError("没有可用的本地用量记录；请先在 Codex 中完成一次对话。")
        latest: QuotaSnapshot | None = None
        for path in files:
            try:
                with path.open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    size = stream.tell()
                    offset = max(0, size - self.MAX_TAIL_BYTES)
                    stream.seek(offset)
                    if offset:
                        stream.readline(self.MAX_TAIL_BYTES)
                    tail = stream.read(self.MAX_TAIL_BYTES)
            except OSError:
                continue
            for line in tail.splitlines():
                # Ignore conversation lines before JSON parsing.
                if b'"rate_limits"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, Mapping) or event.get("type") != "event_msg":
                    continue
                payload = event.get("payload")
                if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
                    continue
                captured_at = _timestamp(event.get("timestamp"))
                limits = payload.get("rate_limits")
                if captured_at is None or not isinstance(limits, Mapping):
                    continue
                try:
                    snapshot = parse_rate_limits(limits, captured_at=captured_at, source="本地会话快照")
                except UsageError:
                    continue
                if latest is None or snapshot.captured_at > latest.captured_at:
                    latest = snapshot
        if latest is None:
            raise UsageError("最近的本地会话中没有带有效时间戳的用量快照。")
        return latest

    def read_snapshot(self) -> QuotaSnapshot:
        self.last_warning = None
        try:
            return self._read_live()
        except UsageError as live_error:
            try:
                snapshot = self._read_local()
            except UsageError as local_error:
                raise UsageError(f"{live_error}\n{local_error}") from None
            self.last_warning = f"实时读取未成功：{live_error} 当前显示本地记录，数值可能滞后。"
            return snapshot
