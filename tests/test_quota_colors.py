from __future__ import annotations

from copy import deepcopy
import queue
import unittest
from unittest.mock import Mock, patch

from quota_widget.app import QuotaApp, THEMES
from quota_widget.quota_colors import QuotaColorAnimator, quota_color
from quota_widget.settings import Settings
from quota_widget.usage import QuotaSnapshot, QuotaWindow


VALUES = (100, 77, 61, 60, 45, 31, 30, 25, 16, 15, 10, 1, 0)
EXPECTED = {
    "five_hour": ("#82dfeb",) * 3 + ("#57B9B0",) * 3 + ("#F4B45F",) * 3 + ("#F06570",) * 4,
    "weekly": ("#a6c7ff",) * 3 + ("#B6A4E8",) * 3 + ("#E9A16B",) * 3 + ("#F06570",) * 4,
}


class Scheduler:
    def __init__(self):
        self.now = 0
        self.next_id = 0
        self.pending = {}

    def after(self, milliseconds, callback):
        self.next_id += 1
        self.pending[self.next_id] = (self.now + milliseconds, callback)
        return self.next_id

    def after_cancel(self, identifier):
        self.pending.pop(identifier, None)

    def clock(self):
        return self.now / 1000

    def advance(self, milliseconds):
        deadline = self.now + milliseconds
        while self.pending:
            identifier, (due, callback) = min(self.pending.items(), key=lambda item: (item[1][0], item[0]))
            if due > deadline:
                break
            self.now = due
            self.pending.pop(identifier)
            callback()
        self.now = deadline


class ColorRuleTests(unittest.TestCase):
    def test_all_requested_percentages_for_both_periods(self):
        for period, original in (("five_hour", "#82dfeb"), ("weekly", "#a6c7ff")):
            for value, expected in zip(VALUES, EXPECTED[period]):
                with self.subTest(period=period, remaining=value):
                    self.assertEqual(quota_color(value, period, original, "#8fb2bd"), expected)

    def test_fractional_values_use_raw_inclusive_boundaries(self):
        for ceiling, below, above in ((15, "#F06570", "#F4B45F"),
                                      (30, "#F4B45F", "#57B9B0"), (60, "#57B9B0", "#82dfeb")):
            for value, expected in ((ceiling - .0001, below), (ceiling, below), (ceiling + .0001, above)):
                with self.subTest(remaining=value):
                    self.assertEqual(quota_color(value, "five_hour", "#82dfeb", "#8fb2bd"), expected)

    def test_every_theme_keeps_its_exact_original_colors(self):
        originals = {"midnight": ("#b5c9ff", "#c5b8f8"), "ocean": ("#82dfeb", "#a6c7ff"),
                     "forest": ("#b8dfad", "#e0d5a2")}
        for name, (first, second) in originals.items():
            theme = THEMES[name]
            self.assertEqual((theme["accent"], theme["second"]), (first, second))
            for period, color in (("five_hour", first), ("weekly", second)):
                for value in (61, 77, 100):
                    self.assertEqual(quota_color(value, period, color, theme["muted"]), color)

    def test_unavailable_or_invalid_data_never_becomes_zero_or_red(self):
        for value in (None, float("nan"), float("inf"), -float("inf"), True, False, -.1, 100.1, "bad", 10**500):
            for period in EXPECTED:
                with self.subTest(period=period, value=value):
                    self.assertEqual(quota_color(value, period, "#82dfeb", "#8fb2bd"), "#8fb2bd")


class AnimationTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = Scheduler()
        self.frames = []
        self.animator = QuotaColorAnimator(self.scheduler, self.frames.append, clock=self.scheduler.clock)

    def test_initial_and_unchanged_colors_have_no_recurring_timer(self):
        for _ in range(100):
            colors = self.animator.set_targets({"five_hour": "#82dfeb", "weekly": "#a6c7ff"})
        self.assertEqual(colors, {"five_hour": "#82dfeb", "weekly": "#a6c7ff"})
        self.assertFalse(self.scheduler.pending)
        self.assertFalse(self.frames)

    def test_transition_is_smooth_and_finishes_in_about_300ms(self):
        self.animator.set_targets({"five_hour": "#82dfeb"})
        self.animator.set_targets({"five_hour": "#F06570"})
        self.scheduler.advance(150)
        halfway = self.frames[-1]["five_hour"]
        self.assertNotIn(halfway, ("#82dfeb", "#F06570"))
        self.scheduler.advance(151)
        self.assertEqual(self.frames[-1]["five_hour"], "#F06570")
        self.assertFalse(self.scheduler.pending)
        count = len(self.frames)
        self.scheduler.advance(30000)
        self.assertEqual(len(self.frames), count)

    def test_repeated_redraw_does_not_restart_transition(self):
        self.animator.set_targets({"five_hour": "#82dfeb"})
        self.animator.set_targets({"five_hour": "#57B9B0"})
        for _ in range(30):
            self.scheduler.advance(10)
            self.animator.set_targets({"five_hour": "#57B9B0"})
        self.scheduler.advance(1)
        self.assertEqual(self.frames[-1]["five_hour"], "#57B9B0")
        self.assertFalse(self.scheduler.pending)

    def test_mid_animation_retarget_does_not_jump_and_uses_one_timer(self):
        self.animator.set_targets({"five_hour": "#82dfeb"})
        self.animator.set_targets({"five_hour": "#57B9B0"})
        self.scheduler.advance(100)
        before = self.animator.set_targets({"five_hour": "#57B9B0"})
        after = self.animator.set_targets({"five_hour": "#F06570"})
        self.assertEqual(before, after)
        self.assertEqual(len(self.scheduler.pending), 1)
        self.scheduler.advance(301)
        self.assertEqual(self.frames[-1]["five_hour"], "#F06570")

    def test_periods_animate_independently_and_can_recover(self):
        self.animator.set_targets({"five_hour": "#82dfeb", "weekly": "#a6c7ff"})
        self.animator.set_targets({"five_hour": "#F06570", "weekly": "#a6c7ff"})
        self.scheduler.advance(301)
        self.assertTrue(all(frame["weekly"] == "#a6c7ff" for frame in self.frames))
        self.animator.set_targets({"five_hour": "#82dfeb", "weekly": "#B6A4E8"})
        self.scheduler.advance(301)
        self.assertEqual(self.frames[-1], {"five_hour": "#82dfeb", "weekly": "#B6A4E8"})

    def test_shutdown_cancels_pending_frames_and_stale_callbacks(self):
        self.animator.set_targets({"five_hour": "#82dfeb"})
        self.animator.set_targets({"five_hour": "#F06570"})
        stale = next(iter(self.scheduler.pending.values()))[1]
        self.animator.dispose()
        stale()
        self.animator.set_targets({"five_hour": "#57B9B0"})
        self.assertFalse(self.scheduler.pending)
        self.assertFalse(self.frames)


class Canvas:
    def __init__(self):
        self.items = {}
        self.next_id = 0

    def add(self, kind, coordinates, options):
        self.next_id += 1
        self.items[self.next_id] = {"kind": kind, "coords": coordinates, **options}
        return self.next_id

    def create_text(self, *coordinates, **options): return self.add("text", coordinates, options)
    def create_line(self, *coordinates, **options): return self.add("line", coordinates, options)
    def create_polygon(self, *coordinates, **options): return self.add("polygon", coordinates, options)
    def create_arc(self, *coordinates, **options): return self.add("arc", coordinates, options)
    def create_oval(self, *coordinates, **options): return self.add("oval", coordinates, options)
    def configure(self, **options): pass
    def delete(self, identifier): self.items.clear()
    def tagged(self, tag): return [item for item in self.items.values() if item.get("tags") == tag]
    def itemconfigure(self, tag, **options):
        for item in self.tagged(tag):
            item.update(options)


class QuotaRenderingTests(unittest.TestCase):
    def setUp(self):
        self.now = 2_000_000_000
        clock = patch("quota_widget.app.time.time", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.app = QuotaApp.__new__(QuotaApp)
        app = self.app
        app.root = Scheduler()
        app.canvas = Canvas()
        app.quota_colors = QuotaColorAnimator(app.root, app._apply_quota_colors, clock=app.root.clock)
        app._closed = False
        app.settings = Settings(theme="ocean", follow_codex=False, sidebar_dock=False)
        app.demo = True
        app.thumbnail = Mock()
        app.follower = Mock(dpi_scale=1)
        app.snapshot = None
        app.error = app._background_warning = ""
        app.busy = False
        app._background_photo = None
        app._make_background = Mock()
        app.actions = []
        app._temporary_independent = False
        app.launch_linked = False
        app.last_attempt = 0
        app.results = queue.Queue()
        for name in ("refresh", "open_settings", "request_close", "toggle_follow", "open_connection"):
            setattr(app, name, Mock())

    def snapshot(self, first, second, *, expired=False):
        def window(value, minutes):
            return None if value is None else QuotaWindow(100 - value, minutes, self.now + (-1 if expired else 3600))
        return QuotaSnapshot(window(first, 300), window(second, 10080), "plus", self.now, "Codex 实时接口")

    def assert_group(self, period, expected, *, remaining, compact=False):
        group = self.app.canvas.tagged("quota-color-" + period)
        self.assertTrue(group)
        self.assertTrue(all(item["fill"] == expected for item in group), group)
        texts = [item for item in group if item["kind"] == "text"]
        if compact:
            self.assertEqual(len(texts), 1)
            self.assertTrue(texts[0]["text"].endswith("%"))
        else:
            self.assertEqual(len(texts), 2)
            self.assertEqual(texts[1]["text"], "%")
        bars = [item for item in group if item["kind"] == "polygon"]
        self.assertEqual(len(bars), int(remaining > 0))
        if bars:
            points = bars[0]["coords"][0]
            width = max(points[::2]) - min(points[::2])
            self.assertAlmostEqual(width, max(3, (172 if compact else 334) * remaining / 100))

    def test_full_and_compact_views_cover_all_thresholds_down_and_back_up(self):
        for compact in (False, True):
            self.app.settings.compact = compact
            pairs = list(zip(VALUES, reversed(VALUES)))
            for first, second in pairs + list(reversed(pairs)):
                with self.subTest(compact=compact, first=first, second=second):
                    self.app.snapshot = self.snapshot(first, second)
                    self.app.draw()
                    self.app.root.advance(301)
                    self.assert_group("five_hour", EXPECTED["five_hour"][VALUES.index(first)], remaining=first, compact=compact)
                    self.assert_group("weekly", EXPECTED["weekly"][VALUES.index(second)], remaining=second, compact=compact)

    def test_every_animation_frame_keeps_digits_suffix_and_fill_identical(self):
        app = self.app
        app.snapshot = self.snapshot(77, 77)
        app.draw()
        app.snapshot = self.snapshot(45, 25)
        app.draw()
        before = deepcopy(app.canvas.items)
        frames = []
        def check_frame(colors):
            app._apply_quota_colors(colors)
            for period in ("five_hour", "weekly"):
                self.assert_group(period, colors[period], remaining=45 if period == "five_hour" else 25)
            frames.append(colors)
        app.quota_colors.on_frame = check_frame
        app.root.advance(301)
        self.assertGreater(len(frames), 10)
        self.assertEqual(frames[-1], {"five_hour": "#57B9B0", "weekly": "#E9A16B"})
        after = deepcopy(app.canvas.items)
        for item in (*before.values(), *after.values()):
            if str(item.get("tags", "")).startswith("quota-color-"):
                item.pop("fill")
        self.assertEqual(before, after, "Animation changed geometry, fonts, tracks, labels or countdowns")

    def test_missing_window_stays_placeholder_without_a_red_or_zero_value(self):
        for compact in (False, True):
            self.app.settings.compact = compact
            self.app.snapshot = self.snapshot(10, 25)
            self.app.draw()
            self.app.root.advance(301)
            self.app.snapshot = self.snapshot(None, 25)
            self.app.draw()
            self.assertFalse(self.app.canvas.tagged("quota-color-five_hour"))
            self.assert_group("weekly", "#E9A16B", remaining=25, compact=compact)
            items = self.app.canvas.items.values()
            self.assertTrue(any(item.get("text") == "—" and item["fill"] == THEMES["ocean"]["muted"] for item in items))

    def test_failed_refresh_keeps_existing_balance_color_and_countdown(self):
        app = self.app
        app.snapshot = previous = self.snapshot(45, 25)
        app.draw()
        app.results.put((None, "暂时无法读取额度"))
        app._tick()
        self.assertIs(app.snapshot, previous)
        self.assert_group("five_hour", "#57B9B0", remaining=45)
        self.assert_group("weekly", "#E9A16B", remaining=25)
        self.assertTrue(any(item.get("text") == "01:00:00 后重置" for item in app.canvas.items.values()))
        app.refresh.assert_not_called()

    def test_expired_snapshot_keeps_history_labels_and_does_not_invent_reset(self):
        self.app.snapshot = self.snapshot(10, 25, expired=True)
        self.app.draw()
        self.assert_group("five_hour", "#F06570", remaining=10)
        self.assert_group("weekly", "#E9A16B", remaining=25)
        texts = [item["text"] for item in self.app.canvas.items.values() if item["kind"] == "text"]
        self.assertEqual(texts.count("上次剩余"), 2)
        self.assertEqual(texts.count("已到重置时间 · 等待刷新"), 2)
