from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import re
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quota_widget.app import QuotaApp
from quota_widget.settings import Settings, load_settings
from quota_widget.windows import WindowFollower, sidebar_geometry


class FakeRoot:
    def __init__(self, x=0, y=0, width=420, height=504, requested_size=None):
        self.x, self.y = x, y
        self.width, self.height = width, height
        self.requested_size = requested_size or (width, height)
        self.after = Mock()

    def winfo_x(self): return self.x
    def winfo_y(self): return self.y
    def winfo_width(self): return self.width
    def winfo_reqwidth(self): return self.requested_size[0]
    def winfo_height(self): return self.height
    def winfo_reqheight(self): return self.requested_size[1]
    def update_idletasks(self): pass
    def state(self): return "normal"

    def geometry(self, value):
        match = re.fullmatch(r"\+(-?\d+)\+(-?\d+)", value)
        self.x, self.y = map(int, match.groups())


class FakeDesktop:
    def __init__(self, root):
        self.root = root
        self.rect = (100, 100, 1100, 900)
        self.client_rect = (0, 0, 1000, 800)
        self.client_origin = (100, 100)
        self.minimized = False
        self.target_visible = True
        self.widget_visible = True
        self.client_rect_valid = True
        self.screen_origin_valid = True
        self.ShowWindow = Mock(side_effect=self._show_window)
        self.positions = []

    def IsIconic(self, target): return self.minimized
    def IsWindowVisible(self, target):
        return self.widget_visible if target == 99 else self.target_visible

    def _show_window(self, hwnd, mode):
        self.widget_visible = mode != 0
        return True

    def GetWindowRect(self, target, rect_pointer):
        rect = rect_pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = self.rect
        return True

    def GetClientRect(self, target, rect_pointer):
        rect = rect_pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = self.client_rect
        return self.client_rect_valid

    def ClientToScreen(self, target, point_pointer):
        point = point_pointer._obj
        point.x += self.client_origin[0]
        point.y += self.client_origin[1]
        return self.screen_origin_valid

    def GetSystemMetrics(self, index):
        return {76: -1920, 77: -1080, 78: 6000, 79: 4000}[index]

    def SetWindowPos(self, hwnd, after, x, y, width, height, flags):
        self.root.x, self.root.y = x, y
        self.positions.append((hwnd, after, x, y, width, height, flags))
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


class AppFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = QuotaApp.__new__(QuotaApp)
        self.app.root = FakeRoot()
        self.app.settings = Settings(sidebar_dock=False)
        self.app.config_path = Path(temporary.name) / "settings.json"
        self.app.follower = make_follower(self.app.root)
        self.app.thumbnail_follower = Mock()
        self.app.thumbnail = Mock()
        self.app.hover_controller = Mock()
        self.app._closed = False
        self.app._drag_start = None
        self.app._drag_position = None
        self.app._last_click = None
        self.app.startup = None
        self.app.launch_linked = False
        self.app._client_present = False
        self.app.window_monitor = None
        self.app._user_hidden = False
        self.app._temporary_independent = False
        self.app._restore_hold_until = 0
        self.app.refresh = Mock()
        self.app.actions = []
        self.app.draw = Mock()
        self.app._clamp_position = lambda x, y: (x, y)

    def event(self, x, y):
        return SimpleNamespace(x=45, y=30, x_root=x, y_root=y)


class FollowDraggingTests(AppFixture):
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


class LaunchLinkTests(AppFixture):
    def test_missing_client_hides_in_both_window_modes(self):
        follower = self.app.follower
        follower._find_target.return_value = 0
        for following in (True, False):
            with self.subTest(following=following):
                follower._hidden = False
                follower._show.reset_mock()
                follower.update(following, linked=True)
                self.assertFalse(follower.client_available)
                self.assertTrue(follower._hidden)
                follower._show.assert_not_called()

    def test_client_reopen_restores_panel_and_minimize_hides_it(self):
        follower = self.app.follower
        follower._find_target.return_value = 0
        follower.update(True, linked=True)
        follower._find_target.return_value = 17
        follower.update(True, linked=True)
        self.assertTrue(follower.client_available)
        follower._show.assert_called_once()
        follower._show.reset_mock()
        follower._hidden = False
        follower._user32.minimized = True
        follower.update(True, linked=True)
        self.assertTrue(follower._hidden)
        follower._show.assert_not_called()

    def test_manual_mode_stays_visible_without_client(self):
        follower = self.app.follower
        follower._find_target.return_value = 0
        follower.update(True, linked=False)
        follower._show.assert_called_once()

    def test_refresh_occurs_on_each_client_launch_not_while_waiting(self):
        app = self.app
        app.startup = SimpleNamespace(is_enabled=lambda: True)
        app.launch_linked = True
        app.follower._find_target.return_value = 0
        app._follow_tick()
        app.refresh.assert_not_called()
        app.follower._find_target.return_value = 17
        app._follow_tick()
        app._follow_tick()
        app.refresh.assert_called_once()
        app.follower._find_target.return_value = 0
        app._follow_tick()
        app.follower._find_target.return_value = 17
        app._follow_tick()
        self.assertEqual(app.refresh.call_count, 2)

    def test_periodic_refresh_is_paused_without_client(self):
        app = self.app
        app.demo = False
        app.busy = False
        app.last_attempt = 0
        app.results = queue.Queue()
        app.launch_linked = True
        app.follower.client_available = False
        app._tick()
        app.refresh.assert_not_called()
        app.draw.assert_not_called()
        app.follower.client_available = True
        app._tick()
        app.refresh.assert_called_once()
        app.draw.assert_called_once()


class SidebarGeometryTests(unittest.TestCase):
    def test_thumbnail_and_detail_share_bottom_with_eight_dp_gap(self):
        client = (100, 100, 1100, 900)
        mini = sidebar_geometry(client, (40, 156))
        detail = sidebar_geometry(client, (420, 504), anchor="sidebar-detail")
        self.assertEqual(mini, (106, 654))
        self.assertEqual(detail, (154, 306))
        self.assertEqual(mini[1] + 156, detail[1] + 504)
        self.assertEqual(detail[0] - (mini[0] + 40), 8)
        self.assertEqual(client[3] - (mini[1] + 156), 90)

    def test_dpi_scales_sidebar_center_gap_and_avatar_reservation(self):
        client = (100, 100, 1600, 1300)
        mini = sidebar_geometry(client, (60, 234), 1.5)
        detail = sidebar_geometry(client, (630, 756), 1.5, "sidebar-detail")
        self.assertEqual(mini, (109, 931))
        self.assertEqual(detail, (181, 409))
        self.assertEqual(detail[0] - (mini[0] + 60), 12)
        self.assertEqual(client[3] - (mini[1] + 234), 135)

    def test_measured_codex_rail_fits_thumbnail_above_both_profile_buttons(self):
        # Actual client capture: 78px rail at 150% DPI; upper profile button is
        # centered 106px above the bottom and has a 36px diameter.
        client = (0, 0, 2560, 1368)
        mini = sidebar_geometry(client, (60, 234), 1.5)
        detail = sidebar_geometry(client, (420, 504), 1.5, "sidebar-detail")
        self.assertEqual(mini, (9, 999))
        self.assertEqual(detail, (81, 729))
        self.assertGreaterEqual(mini[0], 0)
        self.assertLessEqual(mini[0] + 60, 78)
        upper_button_top = client[3] - 106 - 18
        self.assertEqual(upper_button_top - (mini[1] + 234), 11)

    def test_negative_monitor_coordinates_are_preserved(self):
        desktop = (-1920, -1080, 3840, 2160)
        client = (-1800, -900, -500, 100)
        self.assertEqual(sidebar_geometry(client, (40, 156), desktop_rect=desktop), (-1794, -146))
        self.assertEqual(
            sidebar_geometry(client, (420, 504), anchor="sidebar-detail", desktop_rect=desktop),
            (-1746, -494),
        )

    def test_short_client_keeps_thumbnail_reachable_and_popup_on_desktop(self):
        client = (10, 20, 210, 220)
        self.assertEqual(sidebar_geometry(client, (40, 156)), (16, 20))
        self.assertEqual(
            sidebar_geometry(client, (200, 220), anchor="sidebar-detail", desktop_rect=(0, 0, 1920, 1080)),
            (64, 0),
        )
        self.assertEqual(
            sidebar_geometry(client, (420, 504), anchor="sidebar-detail", desktop_rect=(0, 0, 320, 240)),
            (0, 0),
        )

    def test_detail_is_clamped_at_right_edge_without_changing_size(self):
        self.assertEqual(
            sidebar_geometry((1800, 0, 1920, 1080), (420, 504), anchor="sidebar-detail",
                             desktop_rect=(0, 0, 1920, 1080)),
            (1500, 486),
        )


class SidebarVisibilityTests(unittest.TestCase):
    def follower(self, *, width=40, height=156, requested_size=(420, 504)):
        return make_follower(FakeRoot(width=width, height=height, requested_size=requested_size))

    def test_client_coordinates_and_actual_size_override_old_panel_request(self):
        follower = self.follower()
        follower._user32.rect = (90, 70, 1110, 910)
        follower.update(True, anchor="sidebar")
        self.assertEqual((follower.root.x, follower.root.y), (106, 654))
        self.assertTrue(follower.client_visible)

    def test_hidden_detail_still_tracks_client_sets_owner_and_never_shows(self):
        follower = self.follower(width=420, height=504)
        follower.update(True, linked=True, anchor="sidebar-detail", visible=False)
        follower._configure.assert_called_once_with(True, 17)
        follower._show.assert_not_called()
        self.assertEqual((follower.root.x, follower.root.y), (154, 306))
        self.assertTrue(follower.client_available)
        self.assertTrue(follower.client_visible)
        self.assertTrue(follower._hidden)
        self.assertFalse(follower.native_visible)
        self.assertTrue(all(call.args[1] == 0 for call in follower._user32.ShowWindow.call_args_list))
        follower._user32.client_origin = (400, 250)
        follower.update(True, linked=True, anchor="sidebar-detail", visible=False)
        self.assertEqual((follower.root.x, follower.root.y), (454, 456))
        # SWP_NOACTIVATE is mandatory; SWP_SHOWWINDOW must never appear.
        self.assertTrue(all(position[-1] & 0x10 for position in follower._user32.positions))
        self.assertFalse(any(position[-1] & 0x40 for position in follower._user32.positions))

    def test_hover_can_show_then_hide_without_requesting_activation(self):
        follower = self.follower(width=420, height=504)
        follower._show = Mock(wraps=WindowFollower._show.__get__(follower))
        follower.update(True, anchor="sidebar-detail", visible=False)
        follower.update(True, anchor="sidebar-detail", visible=True)
        self.assertTrue(follower.native_visible)
        follower.update(True, anchor="sidebar-detail", visible=False)
        self.assertFalse(follower.native_visible)
        self.assertEqual([call.args[1] for call in follower._user32.ShowWindow.call_args_list], [0, 4, 0])

    def test_missing_hidden_and_minimized_clients_hide_both_panels(self):
        for anchor in ("sidebar", "sidebar-detail"):
            for state in ("missing", "hidden", "minimized"):
                with self.subTest(anchor=anchor, state=state):
                    follower = self.follower()
                    if state == "missing":
                        follower._find_target.return_value = 0
                    elif state == "hidden":
                        follower._user32.target_visible = False
                    else:
                        follower._user32.minimized = True
                    follower.update(True, linked=True, anchor=anchor)
                    follower._show.assert_not_called()
                    self.assertFalse(follower.client_visible)
                    self.assertFalse(follower.native_visible)
                    self.assertEqual(follower.client_available, state != "missing")

    def test_dpi_is_taken_from_client_and_old_windows_fall_back_to_one(self):
        follower = self.follower(width=60, height=234)
        follower._get_dpi = lambda _target: 144
        follower.update(True, anchor="sidebar")
        self.assertEqual(follower.dpi_scale, 1.5)
        self.assertEqual((follower.root.x, follower.root.y), (109, 531))
        follower._get_dpi = None
        follower.update(True, anchor="sidebar")
        self.assertEqual(follower.dpi_scale, 1.0)

    def test_invalid_client_rect_never_displays_an_unanchored_popup(self):
        for failure in ("client_rect_valid", "screen_origin_valid"):
            with self.subTest(failure=failure):
                follower = self.follower()
                setattr(follower._user32, failure, False)
                follower.update(True, anchor="sidebar")
                follower._show.assert_not_called()
                self.assertFalse(follower.client_visible)


if __name__ == "__main__":
    unittest.main()
