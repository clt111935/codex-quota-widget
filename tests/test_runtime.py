from __future__ import annotations

import ctypes
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from quota_widget.diagnostics import RuntimeLog
from quota_widget.watcher import ClientWindowMonitor


class Native:
    def __init__(self):
        self.valid = True
    def IsWindow(self, hwnd): return self.valid
    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = "Chrome_WidgetWin_1"
        return len(buffer.value)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.native = Native()
        self.finder = Mock(return_value=123)
        self.monitor = ClientWindowMonitor(self.finder, native=self.native, clock=lambda: self.now)

    def test_idle_ticks_do_not_enumerate_windows(self):
        for _ in range(1000):
            self.assertEqual(self.monitor.target(), 123)
        self.finder.assert_called_once()

    def test_destroy_and_recreate_reconcile_immediately(self):
        self.monitor.target()
        self.finder.return_value = 0
        self.monitor._event(0, 0x8001, 123, 0, 0, 0, 0)
        self.assertEqual(self.monitor.target(), 0)
        self.finder.return_value = 456
        self.monitor._event(0, 0x8002, 456, 0, 0, 0, 0)
        self.assertEqual(self.monitor.target(), 456)

    def test_accessibility_child_events_do_not_trigger_scans(self):
        self.monitor.target()
        for _ in range(100):
            self.monitor._event(0, 0x8002, 999, -4, 7, 0, 0)
            self.monitor.target()
        self.finder.assert_called_once()

    def test_slow_safety_scan_recovers_missed_notifications(self):
        self.monitor.target()
        self.finder.return_value = 456
        self.now = 29
        self.assertEqual(self.monitor.target(), 123)
        self.now = 30
        self.assertEqual(self.monitor.target(), 456)

    def test_close_makes_stale_events_inert(self):
        self.monitor.close()
        self.monitor._event(0, 0x8002, 123, 0, 0, 0, 0)
        self.assertEqual(self.monitor.target(), 0)
        self.finder.assert_not_called()


class LogTests(unittest.TestCase):
    def test_logs_never_include_exception_text_or_unapproved_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            log = RuntimeLog(Path(directory) / "settings.json")
            try:
                try:
                    raise ValueError("Bearer secret-example API_KEY=secret-example")
                except ValueError as error:
                    log.failure("operation_failed", error)
                log.event("safe_event", token="secret-example", pid=123)
                error = OSError("token=secret-example")
                error.winerror = 5
                log.failure("os_operation_failed", error)
                log.state("waiting_for_codex", managed=True)
                log.state("waiting_for_codex", managed=True)
            finally:
                log.close()
            text = log.log_path.read_text(encoding="utf-8")
            self.assertNotIn("secret-example", text)
            self.assertIn('"winerror": 5', text)
            self.assertEqual(text.count('"event": "state_changed"'), 1)
            state = json.loads(log.status_path.read_text(encoding="utf-8"))
            self.assertTrue(state["managed"])
            self.assertEqual(state["state"], "waiting_for_codex")
