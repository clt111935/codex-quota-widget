from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quota_widget.app import QuotaApp
from quota_widget.settings import Settings, load_settings
from quota_widget.windows import WindowFollower


class FakeRoot:
    def __init__(self, x=0, y=0):
        self.x, self.y = x, y
        self.after = Mock()

    def winfo_x(self): return self.x
    def winfo_y(self): return self.y
    def winfo_width(self): return 420
    def winfo_reqwidth(self): return 420
    def winfo_height(self): return 504
    def winfo_reqheight(self): return 504
    def update_idletasks(self): pass

    def geometry(self, value):
        match = re.fullmatch(r"\+(-?\d+)\+(-?\d+)", value)
        self.x, self.y = map(int, match.groups())


class FakeDesktop:
    def __init__(self, root):
        self.root = root
        self.rect = (100, 100, 1100, 900)

    def IsIconic(self, target): return False

    def GetWindowRect(self, target, rect_pointer):
        rect = rect_pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = self.rect
        return True

    def GetSystemMetrics(self, index):
        return {76: -1920, 77: -1080, 78: 6000, 79: 4000}[index]

    def SetWindowPos(self, hwnd, after, x, y, width, height, flags):
        self.root.x, self.root.y = x, y
        return True


def make_follower(root):
    with patch.object(WindowFollower, "_bind_win32"):
        follower = WindowFollower(root)
    follower._supported = True
    follower._ctypes = ctypes
    follower._wintypes = wintypes
    follower._user32 = FakeDesktop(root)
    follower._own_hwnd = Mock(return_value=99)
    follower._find_target = Mock(return_value=17)
    follower._configure = Mock()
    follower._show = Mock()
    follower.detach = Mock(wraps=follower.detach)
    return follower


class FollowDraggingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = QuotaApp.__new__(QuotaApp)
        self.app.root = FakeRoot()
        self.app.settings = Settings()
        self.app.config_path = Path(temporary.name) / "settings.json"
        self.app.follower = make_follower(self.app.root)
        self.app._closed = False
        self.app._drag_start = None
        self.app._drag_position = None
        self.app._last_click = None
        self.app.actions = []
        self.app.draw = Mock()
        self.app._clamp_position = lambda x, y: (x, y)

    def event(self, x, y):
        return SimpleNamespace(x=45, y=30, x_root=x, y_root=y)

    def test_drag_keeps_following_and_tracks_owner_after_release(self):
        app = self.app
        app.follower.update(True)
        start = (app.root.x, app.root.y)
        app._press(self.event(700, 190))
        app._drag(self.event(350, 350))
        dragged = (start[0]-350, start[1]+160)
        self.assertEqual((app.root.x, app.root.y), dragged)
        self.assertTrue(app.settings.follow_codex)
        app.follower.detach.assert_not_called()
        # A periodic update in the middle of a drag must not snap the panel back.
        with patch.object(app.follower, "update", wraps=app.follower.update) as update:
            app._follow_tick()
            update.assert_not_called()
        app._release(self.event(350, 350))
        app.follower._user32.rect = (400, 250, 1400, 1050)
        app._follow_tick()
        self.assertEqual((app.root.x, app.root.y), (dragged[0]+300, dragged[1]+150))
        self.assertTrue(app.settings.follow_codex)
        # The chosen relative position also survives a restart.
        saved = load_settings(app.config_path)
        self.assertTrue(saved.follow_codex)
        restored = make_follower(FakeRoot())
        restored.set_offset(saved.follow_offset_x, saved.follow_offset_y)
        restored._user32.rect = app.follower._user32.rect
        restored.update(True)
        self.assertEqual((restored.root.x, restored.root.y), (app.root.x, app.root.y))

    def test_only_manual_toggle_detaches_and_reenable_keeps_current_position(self):
        app = self.app
        app.follower.update(True)
        app.toggle_follow()
        self.assertFalse(app.settings.follow_codex)
        app.follower.detach.assert_called_once()
        app.root.x, app.root.y = -500, 300
        app.toggle_follow()
        self.assertTrue(app.settings.follow_codex)
        self.assertEqual((app.root.x, app.root.y), (-500, 300))

    def test_drag_before_codex_opens_is_adopted_when_owner_appears(self):
        app = self.app
        app.follower._find_target.return_value = 0
        app.root.x, app.root.y = 600, 120
        app._press(self.event(640, 150))
        app._drag(self.event(280, 420))
        app._release(self.event(280, 420))
        self.assertIsNone(app.follower.offset)
        app._follow_tick()
        self.assertEqual((app.root.x, app.root.y), (240, 390))
        app.follower._find_target.return_value = 17
        app._follow_tick()
        self.assertEqual((app.root.x, app.root.y), (240, 390))
        self.assertTrue(app.settings.follow_codex)
        saved = load_settings(app.config_path)
        self.assertIsNotNone(saved.follow_offset_x)
        self.assertIsNotNone(saved.follow_offset_y)

    def test_returning_pointer_to_press_point_is_still_a_drag(self):
        app = self.app
        app.follower.update(True)
        app._press(self.event(700, 190))
        app._drag(self.event(800, 290))
        app._drag(self.event(700, 190))
        action = Mock()
        app.actions = [((0, 0, 100, 60), action)]
        app._release(self.event(700, 190))
        action.assert_not_called()
        self.assertTrue(app.settings.follow_codex)


if __name__ == "__main__":
    unittest.main()
