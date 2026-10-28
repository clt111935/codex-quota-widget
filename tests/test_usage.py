from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from quota_widget.usage import UsageError, UsageService, _RpcClient, parse_rate_limits


def window(used=20, minutes=300, reset=1800000000):
    return {"usedPercent": used, "windowDurationMins": minutes, "resetsAt": reset}


def result(primary=None, secondary=None):
    return {"rateLimits": {"limitId": "codex", "primary": primary, "secondary": secondary, "planType": "plus"}}


class FakeProcess:
    def __init__(self, messages):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))
        self.sent = ""
        self.terminated = False
        self.killed = False
        self.waits = 0

    def wait(self, timeout):
        self.waits += 1
        if self.stdin.closed:
            return 0
        raise subprocess.TimeoutExpired("fake", timeout)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class ParseTests(unittest.TestCase):
    def test_camel_case_multi_bucket_prefers_codex(self):
        payload = result(window(90), window(95, 10080))
        payload["rateLimitsByLimitId"] = {
            "codex_other": {"primary": window(99), "secondary": None},
            "codex": {"limitId": "codex", "primary": window(25), "secondary": window(40, 10080), "planType": "pro"},
        }
        snapshot = parse_rate_limits(payload, captured_at=100, source="test")
        self.assertEqual(snapshot.five_hour.remaining_percent, 75)
        self.assertEqual(snapshot.weekly.remaining_percent, 60)
        self.assertEqual(snapshot.plan_type, "pro")
        self.assertEqual(snapshot.limit_id, "codex")

    def test_snake_case_and_swapped_windows_use_duration(self):
        payload = {
            "primary": {"used_percent": 42, "window_minutes": 10080, "resets_at": 1800000000},
            "secondary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": 1800000100},
            "plan_type": "plus",
        }
        snapshot = parse_rate_limits(payload, captured_at=100, source="local")
        self.assertEqual(snapshot.weekly.used_percent, 42)
        self.assertEqual(snapshot.five_hour.used_percent, 12.5)

    def test_missing_or_unrelated_window_is_not_reported_as_full(self):
        snapshot = parse_rate_limits(result(window(9, 60)), captured_at=100, source="test")
        self.assertIsNone(snapshot.five_hour)
        self.assertIsNone(snapshot.weekly)
        only_week = parse_rate_limits(result(None, window(20, 10080)), captured_at=100, source="test")
        self.assertIsNone(only_week.five_hour)
        self.assertEqual(only_week.weekly.remaining_percent, 80)

    def test_expired_reset_keeps_reported_usage(self):
        snapshot = parse_rate_limits(result(window(100, reset=1)), captured_at=100, source="test")
        self.assertEqual(snapshot.five_hour.resets_at, 1)
        self.assertEqual(snapshot.five_hour.remaining_percent, 0)

    def test_non_finite_values_are_unavailable_and_percent_is_bounded(self):
        snapshot = parse_rate_limits(result(window(float("nan")), window(150, 10080)), captured_at=100, source="test")
        self.assertIsNone(snapshot.five_hour)
        self.assertEqual(snapshot.weekly.remaining_percent, 0)
        self.assertIsNone(parse_rate_limits(result(window(True)), captured_at=100, source="test").five_hour)

    def test_empty_payload_is_diagnostic(self):
        with self.assertRaisesRegex(UsageError, "暂未提供"):
            parse_rate_limits({}, captured_at=100, source="test")


class RpcTests(unittest.TestCase):
    def test_live_protocol_is_read_only_and_closes_process(self):
        process = FakeProcess([
            {"method": "account/updated", "params": {"planType": "plus"}},
            {"id": 1, "result": {"userAgent": "fake"}},
            {"id": 2, "result": result(window(25), window(50, 10080))},
        ])
        # Keep the written stream available after the service closes it.
        class CaptureInput(io.StringIO):
            def close(self):
                process.sent = self.getvalue()
                super().close()
        process.stdin = CaptureInput()
        service = UsageService(Path("fixture-home"), "fixture-codex")
        with patch.object(service, "_find_command", return_value="fixture-codex"), patch("quota_widget.usage.subprocess.Popen", return_value=process) as spawn:
            snapshot = service.read_snapshot()
        self.assertEqual(snapshot.source, "Codex 实时接口")
        self.assertEqual(snapshot.five_hour.remaining_percent, 75)
        sent = [json.loads(line) for line in process.sent.splitlines()]
        self.assertEqual([message["method"] for message in sent], ["initialize", "initialized", "account/rateLimits/read"])
        self.assertEqual(spawn.call_args.args[0], ["fixture-codex", "app-server"])
        self.assertEqual(spawn.call_args.kwargs["env"]["CODEX_HOME"], "fixture-home")
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertGreaterEqual(process.waits, 1)

    def test_rpc_errors_do_not_expose_raw_upstream_details(self):
        process = FakeProcess([{"id": 1, "error": {"code": -32601, "message": "Method not found secret@example.com"}}])
        client = _RpcClient(process)
        with self.assertRaises(UsageError) as failure:
            client.request(1, "initialize", {})
        self.assertIn("不支持", str(failure.exception))
        self.assertNotIn("secret@example.com", str(failure.exception))
        UsageService._stop_process(process)

    def test_disconnected_process_returns_prompt_error(self):
        process = FakeProcess([])
        client = _RpcClient(process)
        with self.assertRaisesRegex(UsageError, "退出"):
            client.request(1, "initialize", {})
        UsageService._stop_process(process)

    def test_timeout_is_bounded(self):
        process = FakeProcess([])
        with patch.object(_RpcClient, "_read", return_value=None):
            client = _RpcClient(process)
        with patch.object(client, "TIMEOUT", 0.01):
            with self.assertRaisesRegex(UsageError, "超时"):
                client.request(1, "initialize", {})
        UsageService._stop_process(process)

    def test_only_read_only_rpc_methods_are_allowed(self):
        process = FakeProcess([])
        client = _RpcClient(process)
        with self.assertRaises(ValueError):
            client.request(2, "turn/start", {})
        UsageService._stop_process(process)


class DiscoveryTests(unittest.TestCase):
    def test_windows_desktop_binary_is_discovered_without_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary)
            old = local / "OpenAI" / "Codex" / "bin" / "old" / "codex.exe"
            new = local / "OpenAI" / "Codex" / "bin" / "new" / "codex.exe"
            for index, path in enumerate((old, new)):
                path.parent.mkdir(parents=True)
                path.touch()
                os.utime(path, (1800000000 + index, 1800000000 + index))
            with patch("quota_widget.usage.shutil.which", return_value=None), patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
                self.assertEqual(UsageService()._find_command(), str(new))

    def test_invalid_explicit_command_has_clear_diagnostic(self):
        with patch("quota_widget.usage.shutil.which", return_value=None):
            with self.assertRaisesRegex(UsageError, "路径无效"):
                UsageService(codex_command="missing-codex-test.exe")._find_command()


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.sessions = self.home / "sessions" / "2026" / "10" / "28"
        self.sessions.mkdir(parents=True)
        self.service = UsageService(self.home)

    def tearDown(self):
        self.temporary.cleanup()

    def event(self, timestamp="2026-10-28T00:00:00Z", used=20):
        return {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": used, "window_minutes": 300, "resets_at": 1800000000},
                "secondary": {"used_percent": 30, "window_minutes": 10080, "resets_at": 1800000000},
                "plan_type": "plus",
            }},
        }

    def write(self, name, events):
        path = self.sessions / name
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        return path

    def test_fallback_preserves_event_time_and_warns_about_freshness(self):
        path = self.write("rollout.jsonl", [self.event()])
        os.utime(path, (1900000000, 1900000000))
        with patch.object(self.service, "_read_live", side_effect=UsageError("实时接口不可用")):
            snapshot = self.service.read_snapshot()
        expected = datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp()
        self.assertEqual(snapshot.captured_at, expected)
        self.assertEqual(snapshot.source, "本地会话快照")
        self.assertEqual(snapshot.five_hour.remaining_percent, 80)
        self.assertIn("可能滞后", self.service.last_warning)

    def test_newest_event_wins_even_if_file_mtime_is_older(self):
        old = self.write("a.jsonl", [self.event("2026-10-27T00:00:00Z", 90)])
        new = self.write("b.jsonl", [self.event("2026-10-28T00:00:00Z", 10)])
        os.utime(old, (1900000000, 1900000000))
        os.utime(new, (1800000000, 1800000000))
        self.assertEqual(self.service._read_local().five_hour.remaining_percent, 90)

    def test_malformed_conversation_and_undated_events_are_ignored(self):
        valid = self.event()
        undated = self.event(None, 99)
        fake_conversation = {"type": "response_item", "payload": {"type": "token_count", "rate_limits": valid["payload"]["rate_limits"]}}
        path = self.write("rollout.jsonl", [fake_conversation, valid, undated])
        with path.open("ab") as stream:
            stream.write(b'{"rate_limits": malformed\n')
        self.assertEqual(self.service._read_local().five_hour.used_percent, 20)

    def test_tail_scan_ignores_truncated_prefix_and_reads_final_record(self):
        path = self.write("rollout.jsonl", [self.event(used=90)])
        with path.open("ab") as stream:
            stream.write(b'x' * 10000 + b'\n')
            stream.write(json.dumps(self.event("2026-10-28T01:00:00Z", 10)).encode() + b'\n')
        with patch.object(self.service, "MAX_TAIL_BYTES", 2048):
            self.assertEqual(self.service._read_local().five_hour.used_percent, 10)

    def test_only_recent_file_count_is_read(self):
        for index in range(5):
            path = self.write(f"{index}.jsonl", [self.event(used=index)])
            os.utime(path, (1800000000 + index, 1800000000 + index))
        with patch.object(self.service, "MAX_SESSION_FILES", 2):
            self.assertEqual([path.name for path in self.service._session_files()], ["4.jsonl", "3.jsonl"])

    def test_missing_sources_report_both_causes(self):
        with patch.object(self.service, "_read_live", side_effect=UsageError("未找到 Codex CLI")):
            with self.assertRaises(UsageError) as failure:
                self.service.read_snapshot()
        self.assertIn("未找到 Codex CLI", str(failure.exception))
        self.assertIn("本地用量记录", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
