"""Shared quota thresholds and short, on-demand color transitions for Tk canvases."""
from __future__ import annotations

from dataclasses import dataclass
import math
import time


# Inclusive upper bounds: [0, 15], (15, 30], (30, 60], (60, 100].
# Compare the unrounded balance; presentation rounding never selects a level.
THRESHOLDS = (15, 30, 60)
LEVEL_COLORS = {
    "five_hour": ("#F06570", "#F4B45F", "#57B9B0"),
    "weekly": ("#F06570", "#E9A16B", "#B6A4E8"),
}


def quota_color(remaining: float | None, period: str, original_color: str, unavailable_color: str) -> str:
    """Keep each theme's exact original high-balance color; missing data is neutral."""
    palette = LEVEL_COLORS[period]
    if remaining is None or isinstance(remaining, bool):
        return unavailable_color
    try:
        value = float(remaining)
    except (TypeError, ValueError, OverflowError):
        return unavailable_color
    if not math.isfinite(value) or not 0 <= value <= 100:
        return unavailable_color
    for ceiling, color in zip(THRESHOLDS, palette):
        if value <= ceiling:
            return color
    return original_color


def _blend(start: str, end: str, amount: float) -> str:
    if amount <= 0:
        return start
    if amount >= 1:
        return end
    first = (int(start[index:index + 2], 16) for index in (1, 3, 5))
    last = (int(end[index:index + 2], 16) for index in (1, 3, 5))
    return "#" + "".join(f"{round(a + (b - a) * amount):02x}" for a, b in zip(first, last))


@dataclass
class _Transition:
    start: str
    target: str
    started: float

    def active(self, now: float, duration: float) -> bool:
        return self.start != self.target and now < self.started + duration

    def color(self, now: float, duration: float) -> str:
        if self.start == self.target:
            return self.target
        amount = max(0.0, min(1.0, (now - self.started) / duration))
        # Smoothstep eases both ends without overshooting a warning color.
        return _blend(self.start, self.target, amount * amount * (3 - 2 * amount))


class QuotaColorAnimator:
    """One timer and one sampled color per period, shared by number, %, and fill.

    ``set_targets`` is called when the existing UI redraws. Animation frames only
    recolor tagged items; they do not redraw layouts, refresh data, or loop when
    colors are unchanged. Retargeting starts from the current interpolated color.
    """
    DURATION = .300
    FRAME_MS = 16

    def __init__(self, scheduler, on_frame, *, clock=time.monotonic):
        self.scheduler = scheduler
        self.on_frame = on_frame
        self.clock = clock
        self._transitions: dict[str, _Transition] = {}
        self._timer = None
        self._token = None
        self._disposed = False

    def _colors(self, now):
        return {period: transition.color(now, self.DURATION)
                for period, transition in self._transitions.items()}

    def set_targets(self, targets: dict[str, str]) -> dict[str, str]:
        now = self.clock()
        if not self._disposed:
            for period, target in targets.items():
                previous = self._transitions.get(period)
                if previous is None:
                    self._transitions[period] = _Transition(target, target, now)
                elif target != previous.target:
                    self._transitions[period] = _Transition(previous.color(now, self.DURATION), target, now)
            self._schedule(now)
        return self._colors(now)

    def _schedule(self, now):
        if self._disposed or self._timer is not None:
            return
        remaining = [transition.started + self.DURATION - now for transition in self._transitions.values()
                     if transition.active(now, self.DURATION)]
        if not remaining:
            return
        delay = max(1, min(self.FRAME_MS, math.ceil(min(remaining) * 1000)))
        token = self._token = object()

        def frame():
            if self._disposed or self._token is not token:
                return
            self._timer = self._token = None
            current = self.clock()
            self.on_frame(self._colors(current))
            self._schedule(current)

        self._timer = self.scheduler.after(delay, frame)

    def dispose(self):
        self._disposed = True
        if self._timer is not None:
            try:
                self.scheduler.after_cancel(self._timer)
            except Exception:
                # The Tk interpreter may already be closing; invalidate the token.
                pass
        self._timer = self._token = None
