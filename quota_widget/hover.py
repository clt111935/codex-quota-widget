"""Delayed hover expansion, independent of Tk widgets and window geometry.

Call ``poll`` from the UI loop every 80--100 ms. The caller supplies a pointer
check covering both the thumbnail and expanded panel, plus a ``hold_open``
check for settings or connection dialogs. Only the supplied callbacks show or
hide windows; the controller never creates or destroys a widget.
"""

from __future__ import annotations

from typing import Callable, Protocol


class Scheduler(Protocol):
    def after(self, milliseconds: int, callback: Callable[[], None]) -> object: ...

    def after_cancel(self, timer_id: object) -> None: ...


class HoverController:
    """Keep a panel expanded while its hover surfaces or dialogs are active.

    ``close`` immediately collapses the panel but permits a later hover to open
    it again. ``set_enabled(False)`` also collapses it and suspends hover.
    ``dispose`` permanently stops timers without calling window callbacks, so
    it is safe to use while the containing UI is being destroyed.
    """

    def __init__(
        self,
        scheduler: Scheduler,
        on_open: Callable[[], None],
        on_close: Callable[[], None],
        contains_pointer: Callable[[], bool],
        hold_open: Callable[[], bool] = lambda: False,
        *,
        open_delay_ms: int = 120,
        close_delay_ms: int = 280,
    ):
        self._scheduler = scheduler
        self._on_open = on_open
        self._on_close = on_close
        self._contains_pointer = contains_pointer
        self._hold_open = hold_open
        self._open_delay_ms = max(0, int(open_delay_ms))
        self._close_delay_ms = max(0, int(close_delay_ms))
        self._enabled = True
        self._opened = False
        self._disposed = False
        self._open_timer: object | None = None
        self._close_timer: object | None = None
        self._open_token: object | None = None
        self._close_token: object | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def opened(self) -> bool:
        return self._opened

    @property
    def disposed(self) -> bool:
        return self._disposed

    def _inside(self) -> bool:
        return bool(self._hold_open() or self._contains_pointer())

    def _cancel(self, opening: bool) -> None:
        if opening:
            timer = self._open_timer
            self._open_timer = self._open_token = None
        else:
            timer = self._close_timer
            self._close_timer = self._close_token = None
        if timer is not None:
            try:
                self._scheduler.after_cancel(timer)
            except Exception:
                # Tk may already have removed the timer or destroyed its
                # interpreter. Invalidated tokens still make stale calls inert.
                pass

    def _cancel_all(self) -> None:
        self._cancel(True)
        self._cancel(False)

    def _schedule_open(self) -> None:
        if self._opened or self._open_token is not None:
            return
        token = self._open_token = object()

        def finish() -> None:
            if self._disposed or not self._enabled or self._open_token is not token:
                return
            self._open_timer = self._open_token = None
            if self._inside():
                self._opened = True
                self._on_open()

        self._open_timer = self._scheduler.after(self._open_delay_ms, finish)

    def _schedule_close(self) -> None:
        if not self._opened or self._close_token is not None:
            return
        token = self._close_token = object()

        def finish() -> None:
            if self._disposed or not self._enabled or self._close_token is not token:
                return
            self._close_timer = self._close_token = None
            if not self._inside():
                self._opened = False
                self._on_close()

        self._close_timer = self._scheduler.after(self._close_delay_ms, finish)

    def poll(self) -> bool:
        """Reconcile current pointer/dialog state and return whether expanded."""
        if self._disposed or not self._enabled:
            return self._opened
        if self._inside():
            self._cancel(False)
            self._schedule_open()
        else:
            self._cancel(True)
            self._schedule_close()
        return self._opened

    def enter(self) -> None:
        """Handle entry into either hover surface without restarting the delay."""
        if self._disposed or not self._enabled:
            return
        self._cancel(False)
        self._schedule_open()

    def leave(self) -> None:
        """Handle leaving a surface; another surface or dialog may still hold it."""
        self.poll()

    def open_now(self) -> None:
        """Expand immediately after a click, preserving later hover dismissal."""
        if self._disposed or not self._enabled:
            return
        self._cancel_all()
        if not self._opened:
            self._opened = True
            self._on_open()

    def close(self) -> None:
        """Immediately collapse, canceling pending transitions."""
        if self._disposed:
            return
        self._cancel_all()
        if self._opened:
            self._opened = False
            self._on_close()

    def set_enabled(self, enabled: bool) -> None:
        if self._disposed:
            return
        self._enabled = bool(enabled)
        if self._enabled:
            self.poll()
        else:
            self.close()

    def dispose(self) -> None:
        """Permanently cancel work; never access windows during UI teardown."""
        if self._disposed:
            return
        self._disposed = True
        self._enabled = self._opened = False
        self._cancel_all()
