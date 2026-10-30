from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from quota_widget.app import QuotaApp


class LifecycleTests(unittest.TestCase):
    def app(self):
        app = QuotaApp.__new__(QuotaApp)
        app._closed = False
        app._user_hidden = False
        app._temporary_independent = False
        app.control = Mock()
        app.launch_linked = True
        app.managed = True
        app.settings = SimpleNamespace(follow_codex=True, sidebar_dock=True)
        app.hover_controller = Mock()
        app._update_windows = Mock()
        app._log_event = Mock()
        app.close = Mock()
        app.restore_window = Mock(wraps=app.restore_window)
        app.root = Mock()
        app.runtime_log = Mock()
        app.window_monitor = Mock()
        app.follower = SimpleNamespace(_supported=False)
        return app

    def test_close_keeps_managed_application_alive(self):
        app = self.app()
        app.request_close()
        self.assertTrue(app._user_hidden)
        app.close.assert_not_called()
        app._update_windows.assert_called_once()

    def test_close_without_autostart_keeps_original_exit_behavior(self):
        app = self.app()
        app.launch_linked = False
        app.request_close()
        app.close.assert_called_once()

    def test_restore_without_codex_shows_temporary_independent_window(self):
        app = self.app()
        app._user_hidden = True
        app.window_monitor.target.return_value = 0
        app.restore_window()
        self.assertFalse(app._user_hidden)
        self.assertTrue(app._temporary_independent)
        app._update_windows.assert_called_once()
        app.hover_controller.open_now.assert_not_called()

    def test_restore_with_client_opens_details(self):
        app = self.app()
        app.window_monitor.target.return_value = 17
        app.restore_window()
        self.assertFalse(app._temporary_independent)
        app.hover_controller.open_now.assert_called_once()

    def test_tray_restore_works_while_codex_is_minimized(self):
        app = self.app()
        app.window_monitor.target.return_value = 17
        app.follower = SimpleNamespace(_supported=True, _user32=Mock())
        app.follower._user32.IsIconic.return_value = True
        app.restore_window()
        self.assertTrue(app._temporary_independent)
        app.hover_controller.open_now.assert_not_called()

    def test_startup_refresh_only_occurs_on_explicit_notification(self):
        app = self.app()
        app.startup = Mock()
        app.control.pending.return_value = []
        app._runtime_state = Mock(return_value="sidebar")
        for _ in range(100):
            app._runtime_tick()
        app.startup.status.assert_not_called()
        app.control.pending.return_value = ["reload_startup"]
        app.startup.status.return_value = {"enabled": False}
        app._runtime_tick()
        app.startup.status.assert_called_once()
        self.assertFalse(app.launch_linked)

    def test_stop_is_intentional_exit(self):
        app = self.app()
        app.control.pending.return_value = ["stop"]
        app._runtime_tick()
        app.close.assert_called_once()
        app.root.after.assert_not_called()
