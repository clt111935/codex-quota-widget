"""Validated, local-only preferences for the desktop quota widget."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


THEMES = ("midnight", "ocean", "forest")


@dataclass
class Settings:
    theme: str = "midnight"
    opacity: float = 0.96
    background_path: str = ""
    background_dim: float = 0.65
    follow_codex: bool = True
    x: int | None = None
    y: int | None = None
    refresh_seconds: int = 60
    compact: bool = False
    codex_command: str = ""


def _number(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(result):
        return default
    return min(high, max(low, result))


def _boolean(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "yes", "on", "1"):
            return True
        if normalized in ("false", "no", "off", "0"):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return default


def _coordinate(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or not value.lstrip("+-").isdigit():
            return None
    if not isinstance(value, (int, str)):
        return None
    try:
        result = int(value)
    except (ValueError, OverflowError):
        return None
    return result if -100_000 <= result <= 100_000 else None


def _validated(raw: Any) -> Settings:
    defaults = Settings()
    if not isinstance(raw, dict):
        return defaults
    theme = raw.get("theme", defaults.theme)
    if not isinstance(theme, str) or theme not in THEMES:
        theme = defaults.theme
    background = raw.get("background_path", "")
    if not isinstance(background, str) or len(background) > 32_768 or "\0" in background:
        background = ""
    command = raw.get("codex_command", "")
    if not isinstance(command, str) or len(command) > 32_768 or "\0" in command:
        command = ""
    return Settings(
        theme=theme,
        opacity=_number(raw.get("opacity"), defaults.opacity, 0.35, 1.0),
        background_path=background,
        background_dim=_number(raw.get("background_dim"), defaults.background_dim, 0.0, 1.0),
        follow_codex=_boolean(raw.get("follow_codex"), defaults.follow_codex),
        x=_coordinate(raw.get("x")),
        y=_coordinate(raw.get("y")),
        refresh_seconds=int(_number(raw.get("refresh_seconds"), defaults.refresh_seconds, 10, 3600)),
        compact=_boolean(raw.get("compact"), defaults.compact),
        codex_command=command,
    )


def load_settings(path: Path) -> Settings:
    """Return safe defaults for a missing, malformed, or unreadable file."""
    try:
        # Preferences are small. Avoid parsing an accidentally selected large file.
        if path.stat().st_size > 256 * 1024:
            return Settings()
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError, RecursionError):
        return Settings()
    return _validated(raw)


def save_settings(path: Path, settings: Settings) -> None:
    """Atomically replace the supplied local preferences file.

    Only the caller-provided path is used; this module never writes to a home
    directory or to the installed Codex application.
    """
    path = Path(path)
    safe = _validated({field.name: getattr(settings, field.name, None) for field in fields(Settings)})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(asdict(safe), handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
