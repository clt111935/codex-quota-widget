from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quota_widget.settings import Settings, load_settings, save_settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "settings.json"

    def write_raw(self, value):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_missing_corrupt_and_non_object_use_defaults(self):
        self.assertEqual(load_settings(self.path), Settings())
        self.path.parent.mkdir()
        self.path.write_bytes(b"\xff\xfe{broken")
        self.assertEqual(load_settings(self.path), Settings())
        for value in ([], None, "settings", 42):
            with self.subTest(value=value):
                self.write_raw(value)
                self.assertEqual(load_settings(self.path), Settings())

    def test_round_trip_preserves_custom_preferences(self):
        expected = Settings(
            theme="forest", opacity=0.8, background_path="D:/图片/背景.png",
            background_dim=0.3, follow_codex=False, x=-1920, y=80,
            refresh_seconds=120, compact=True,
            codex_command="C:/Program Files/OpenAI/Codex/codex.exe",
        )
        save_settings(self.path, expected)
        self.assertEqual(load_settings(self.path), expected)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_deeply_nested_corrupt_file_uses_defaults(self):
        self.path.parent.mkdir()
        self.path.write_text("[" * 5000 + "]" * 5000, encoding="utf-8")
        self.assertEqual(load_settings(self.path), Settings())

    def test_hostile_types_and_non_finite_numbers_use_defaults(self):
        self.write_raw({
            "theme": ["forest"], "opacity": float("nan"),
            "background_path": {"path": "bad"}, "background_dim": True,
            "follow_codex": [], "x": True, "y": 2.8,
            "refresh_seconds": float("inf"), "compact": {},
            "unknown": "ignored",
            "codex_command": "bad\0path",
        })
        self.assertEqual(load_settings(self.path), Settings())

    def test_clamps_ranges_and_parses_boolean_strings(self):
        self.write_raw({
            "opacity": "9", "background_dim": -1, "refresh_seconds": "2",
            "follow_codex": "false", "compact": "YES", "x": "-500", "y": 100001,
        })
        actual = load_settings(self.path)
        self.assertEqual(actual.opacity, 1.0)
        self.assertEqual(actual.background_dim, 0.0)
        self.assertEqual(actual.refresh_seconds, 10)
        self.assertIs(actual.follow_codex, False)
        self.assertIs(actual.compact, True)
        self.assertEqual(actual.x, -500)
        self.assertIsNone(actual.y)

    def test_failed_replace_keeps_existing_file_and_removes_temp(self):
        save_settings(self.path, Settings(theme="ocean"))
        before = self.path.read_bytes()
        with patch("quota_widget.settings.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                save_settings(self.path, Settings(theme="forest"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_direct_settings_are_sanitized_on_save(self):
        save_settings(self.path, Settings(theme="unknown", opacity=-5, background_path="bad\0path"))
        actual = load_settings(self.path)
        self.assertEqual(actual.theme, "midnight")
        self.assertEqual(actual.opacity, 0.35)
        self.assertEqual(actual.background_path, "")


if __name__ == "__main__":
    unittest.main()
