"""Bounded operational logs; no account data, credentials, or raw exceptions."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import tempfile
import traceback


def runtime_paths(config: Path) -> tuple[Path, Path]:
    prefix = "" if config.name == "settings.json" else config.stem + "."
    return config.parent / (prefix + "runtime.log"), config.parent / (prefix + "runtime.json")


class RuntimeLog:
    SAFE_FIELDS = {"pid", "managed", "state", "reason", "error_type", "task_name", "hooks_active", "frames", "exit_code", "winerror", "errno"}

    def __init__(self, config: Path):
        self.log_path, self.status_path = runtime_paths(config)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.Logger("quota-runtime-" + str(os.getpid()))
        self.handler = RotatingFileHandler(self.log_path, maxBytes=256 * 1024, backupCount=2, encoding="utf-8")
        self.logger.addHandler(self.handler)
        self.last_state = None

    def event(self, name: str, **fields):
        data = {"time": datetime.now(timezone.utc).isoformat(), "event": name}
        data.update({key: value for key, value in fields.items() if key in self.SAFE_FIELDS})
        self.logger.info(json.dumps(data, ensure_ascii=False))

    def failure(self, name: str, error: BaseException):
        frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                  for frame in traceback.extract_tb(error.__traceback__)[-8:]]
        codes = {key: getattr(error, key) for key in ("winerror", "errno")
                 if isinstance(getattr(error, key, None), int)}
        self.event(name, error_type=type(error).__name__, frames=frames, **codes)

    def state(self, state: str, *, managed: bool):
        if state == self.last_state:
            return
        self.last_state = state
        self.event("state_changed", state=state, pid=os.getpid(), managed=managed)
        data = {"pid": os.getpid(), "managed": managed, "state": state,
                "updated_at": datetime.now(timezone.utc).isoformat()}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.status_path.parent,
                                             prefix=".runtime-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False)
            os.replace(temporary, self.status_path)
        except OSError as error:
            self.failure("status_write_failed", error)
        finally:
            if temporary and temporary.exists():
                temporary.unlink(missing_ok=True)

    def close(self):
        self.handler.close()
        self.logger.removeHandler(self.handler)
