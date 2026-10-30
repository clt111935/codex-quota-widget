from __future__ import annotations

import unittest

from quota_widget.hover import HoverController


class FakeScheduler:
    def __init__(self):
        self.now = 0
        self.next_id = 0
        self.pending = {}
        self.history = {}
        self.cancel_fails = False

    def after(self, milliseconds, callback):
        self.next_id += 1
        self.pending[self.next_id] = (self.now + milliseconds, callback)
        self.history[self.next_id] = callback
        return self.next_id

    def after_cancel(self, timer_id):
        if self.cancel_fails:
            raise RuntimeError("scheduler already destroyed")
        self.pending.pop(timer_id, None)

    def advance(self, milliseconds):
        deadline = self.now + milliseconds
        while self.pending:
            timer_id, (due, callback) = min(self.pending.items(), key=lambda item: (item[1][0], item[0]))
            if due > deadline:
                break
            self.now = due
            self.pending.pop(timer_id)
            callback()
        self.now = deadline


class HoverTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = FakeScheduler()
        self.inside = False
        self.dialog_open = False
        self.events = []
        self.controller = HoverController(
            self.scheduler,
            lambda: self.events.append("open"),
            lambda: self.events.append("close"),
            lambda: self.inside,
            lambda: self.dialog_open,
        )

    def open_panel(self):
        self.inside = True
        self.controller.enter()
        self.scheduler.advance(120)
        self.assertTrue(self.controller.opened)

    def test_repeated_polls_do_not_restart_open_delay(self):
        self.inside = True
        self.assertFalse(self.controller.poll())
        self.scheduler.advance(80)
        self.assertFalse(self.controller.poll())
        self.scheduler.advance(39)
        self.assertEqual(self.events, [])
        self.scheduler.advance(1)
        self.assertTrue(self.controller.poll())
        self.scheduler.advance(1000)
        self.assertEqual(self.events, ["open"])

    def test_quick_enter_and_leave_never_opens(self):
        self.inside = True
        self.controller.enter()
        self.scheduler.advance(60)
        self.inside = False
        self.controller.leave()
        self.scheduler.advance(1000)
        self.assertFalse(self.controller.opened)
        self.assertEqual(self.events, [])
        self.assertEqual(self.scheduler.pending, {})

    def test_crossing_gap_and_returning_does_not_flicker(self):
        self.open_panel()
        for _ in range(3):
            self.inside = False
            self.controller.leave()
            self.scheduler.advance(200)
            self.inside = True
            self.controller.enter()
            self.scheduler.advance(200)
        self.assertEqual(self.events, ["open"])
        self.inside = False
        self.controller.leave()
        self.scheduler.advance(279)
        self.assertTrue(self.controller.opened)
        self.scheduler.advance(1)
        self.assertFalse(self.controller.opened)
        self.assertEqual(self.events, ["open", "close"])

    def test_open_callback_rechecks_pointer_without_another_poll(self):
        self.inside = True
        self.controller.enter()
        self.inside = False
        self.scheduler.advance(120)
        self.assertEqual(self.events, [])
        self.assertFalse(self.controller.opened)

    def test_close_callback_rechecks_returned_pointer(self):
        self.open_panel()
        self.inside = False
        self.controller.leave()
        self.inside = True
        self.scheduler.advance(280)
        self.assertEqual(self.events, ["open"])
        self.assertTrue(self.controller.opened)

    def test_dialog_holds_panel_open_outside_hover_surfaces(self):
        self.open_panel()
        self.inside = False
        self.controller.leave()
        self.scheduler.advance(100)
        self.dialog_open = True
        self.controller.poll()
        self.scheduler.advance(1000)
        self.assertEqual(self.events, ["open"])
        self.dialog_open = False
        self.controller.poll()
        self.scheduler.advance(280)
        self.assertEqual(self.events, ["open", "close"])

    def test_close_callback_rechecks_new_dialog_without_poll(self):
        self.open_panel()
        self.inside = False
        self.controller.leave()
        self.dialog_open = True
        self.scheduler.advance(280)
        self.assertTrue(self.controller.opened)
        self.assertEqual(self.events, ["open"])

    def test_disabling_cancels_pending_open_and_reenable_can_open(self):
        self.inside = True
        self.controller.enter()
        stale_open = self.scheduler.history[self.scheduler.next_id]
        self.controller.set_enabled(False)
        stale_open()
        self.controller.enter()
        self.controller.leave()
        self.controller.poll()
        self.scheduler.advance(1000)
        self.assertEqual(self.events, [])
        self.assertFalse(self.controller.enabled)
        self.assertEqual(self.scheduler.pending, {})
        self.controller.set_enabled(True)
        self.scheduler.advance(120)
        self.assertEqual(self.events, ["open"])

    def test_disabling_expanded_panel_closes_once_and_cancels_delay(self):
        self.open_panel()
        self.inside = False
        self.controller.leave()
        stale_close = self.scheduler.history[self.scheduler.next_id]
        self.controller.set_enabled(False)
        self.controller.set_enabled(False)
        stale_close()
        self.scheduler.advance(1000)
        self.assertEqual(self.events, ["open", "close"])
        self.assertFalse(self.controller.opened)
        self.assertEqual(self.scheduler.pending, {})

    def test_close_cancels_pending_open_but_permits_later_hover(self):
        self.inside = True
        self.controller.enter()
        stale_open = self.scheduler.history[self.scheduler.next_id]
        self.controller.close()
        stale_open()
        self.scheduler.advance(200)
        self.assertEqual(self.events, [])
        self.assertTrue(self.controller.enabled)
        self.controller.enter()
        self.scheduler.advance(120)
        self.assertEqual(self.events, ["open"])
        self.controller.close()
        self.controller.close()
        self.assertEqual(self.events, ["open", "close"])

    def test_click_then_immediate_leave_closes_after_grace_period(self):
        self.inside = True
        self.controller.enter()
        stale_open = self.scheduler.history[self.scheduler.next_id]
        self.controller.open_now()
        self.assertTrue(self.controller.opened)
        self.assertEqual(self.events, ["open"])
        self.inside = False
        self.controller.leave()
        stale_open()
        self.scheduler.advance(279)
        self.assertEqual(self.events, ["open"])
        self.scheduler.advance(1)
        self.assertEqual(self.events, ["open", "close"])
        self.assertFalse(self.controller.opened)

    def test_click_while_expanded_cancels_pending_close_without_reopening(self):
        self.open_panel()
        self.inside = False
        self.controller.leave()
        stale_close = self.scheduler.history[self.scheduler.next_id]
        self.controller.open_now()
        stale_close()
        self.scheduler.advance(280)
        self.assertTrue(self.controller.opened)
        self.assertEqual(self.events, ["open"])
        self.assertEqual(self.scheduler.pending, {})

    def test_click_is_ignored_while_disabled_or_disposed(self):
        self.controller.set_enabled(False)
        self.controller.open_now()
        self.controller.set_enabled(True)
        self.controller.dispose()
        self.controller.open_now()
        self.assertEqual(self.events, [])
        self.assertFalse(self.controller.opened)

    def test_dispose_cancels_open_and_ignores_all_future_calls(self):
        self.inside = True
        self.controller.enter()
        stale_open = self.scheduler.history[self.scheduler.next_id]
        self.controller.dispose()
        self.controller.dispose()
        self.controller.set_enabled(True)
        self.controller.enter()
        self.controller.leave()
        self.controller.close()
        stale_open()
        self.scheduler.advance(1000)
        self.assertFalse(self.controller.poll())
        self.assertTrue(self.controller.disposed)
        self.assertFalse(self.controller.enabled)
        self.assertEqual(self.events, [])
        self.assertEqual(self.scheduler.pending, {})

    def test_dispose_does_not_call_windows_during_teardown(self):
        self.open_panel()
        self.inside = False
        self.controller.leave()
        stale_close = self.scheduler.history[self.scheduler.next_id]
        self.controller.dispose()
        stale_close()
        self.scheduler.advance(1000)
        self.assertEqual(self.events, ["open"])
        self.assertFalse(self.controller.opened)

    def test_failed_timer_cancel_still_invalidates_stale_callbacks(self):
        self.inside = True
        self.controller.enter()
        self.scheduler.cancel_fails = True
        self.controller.dispose()
        self.scheduler.advance(1000)
        self.assertEqual(self.events, [])
        self.assertFalse(self.controller.opened)

    def test_custom_delays_are_respected(self):
        controller = HoverController(
            self.scheduler, lambda: self.events.append("open"),
            lambda: self.events.append("close"), lambda: self.inside,
            open_delay_ms=10, close_delay_ms=20,
        )
        self.inside = True
        controller.poll()
        self.scheduler.advance(9)
        self.assertEqual(self.events, [])
        self.scheduler.advance(1)
        self.inside = False
        controller.poll()
        self.scheduler.advance(19)
        self.assertEqual(self.events, ["open"])
        self.scheduler.advance(1)
        self.assertEqual(self.events, ["open", "close"])


if __name__ == "__main__":
    unittest.main()
