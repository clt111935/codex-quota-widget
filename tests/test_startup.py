from __future__ import annotations

from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from quota_widget.startup import SingleInstance, StartupRegistration


class FakeRegistry:
    HKEY_CURRENT_USER = 1
    KEY_SET_VALUE = 2
    KEY_QUERY_VALUE = 4
    REG_SZ = 1

    def __init__(self):
        self.values = {"OtherApp": ("untouched", self.REG_SZ)}

    def OpenKey(self, *args): return nullcontext(self)
    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]
    def DeleteValue(self, key, name): del self.values[name]


class StartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="quota widget ")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.script = self.folder / "run.py"
        self.script.touch()
        (self.folder / "pythonw.exe").touch()
        self.registration = StartupRegistration(self.script)
        self.registry = FakeRegistry()
        self.scheduled = {"enabled": False, "installed": False}
        self.calls = []
        def task(operation):
            self.calls.append(operation)
            if operation == "Enable":
                self.scheduled.update(enabled=True, installed=True, mechanism="scheduled_task")
            elif operation == "Disable":
                self.scheduled.update(enabled=False, installed=False)
            self.registration._cached_status = dict(self.scheduled)
            return dict(self.scheduled)
        for context in [
            patch.dict(sys.modules, {"winreg": self.registry}),
            patch("quota_widget.startup.sys.executable", str(self.folder / "python.exe")),
            patch.object(self.registration, "_task", side_effect=task),
        ]:
            context.start()
            self.addCleanup(context.stop)

    def test_enable_migrates_only_exact_legacy_entry_and_disable_preserves_other_apps(self):
        old_command = subprocess.list2cmdline([str(self.folder / "pythonw.exe"), str(self.script)])
        self.registry.values[self.registration.VALUE] = (old_command, 1)
        self.registration.enable()
        self.assertTrue(self.registration.is_enabled())
        self.assertNotIn(self.registration.VALUE, self.registry.values)
        self.registration.disable()
        self.assertFalse(self.registration.is_enabled())
        self.assertEqual(self.registry.values, {"OtherApp": ("untouched", 1)})

    def test_does_not_remove_another_project_installation(self):
        self.registry.values[self.registration.VALUE] = ('"D:\\other\\run.py"', 1)
        self.registration.enable()
        self.registration.disable()
        self.assertIn(self.registration.VALUE, self.registry.values)

    def test_failed_task_installation_preserves_existing_startup(self):
        self.registry.values[self.registration.VALUE] = ("old value", 1)
        self.registration._task.side_effect = OSError("scheduler unavailable")
        with self.assertRaises(OSError):
            self.registration.enable()
        self.assertIn(self.registration.VALUE, self.registry.values)

    def test_missing_script_does_not_register_broken_task(self):
        self.script.unlink()
        with self.assertRaises(OSError):
            self.registration.enable()
        self.registration._task.assert_not_called()

    def test_status_cache_avoids_periodic_powershell_processes(self):
        for _ in range(100):
            self.registration.is_enabled()
        self.assertEqual(self.calls, ["Status"])
        self.registration.status()
        self.assertEqual(self.calls, ["Status", "Status"])

    def test_context_preserves_only_codex_home_not_tokens(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.folder / "Codex Home"),
                                     "OPENAI_API_KEY": "secret-example", "GH_TOKEN": "secret-example"}):
            self.registration.enable()
        context = json.loads((self.folder / ".local" / "startup-context.json").read_text())
        self.assertEqual(context, {"codex_home": str(self.folder / "Codex Home")})
        self.assertNotIn("secret-example", json.dumps(context))

    def test_restore_context_does_not_override_explicit_environment(self):
        self.registration.enable()
        context = self.folder / ".local" / "startup-context.json"
        context.write_text(json.dumps({"codex_home": "stored-home"}))
        with patch.dict(os.environ, {"CODEX_HOME": "explicit-home"}):
            self.registration.restore_environment()
            self.assertEqual(os.environ["CODEX_HOME"], "explicit-home")

    def test_command_quotes_paths_and_uses_managed_entry(self):
        command = self.registration.command
        self.assertEqual(command, subprocess.list2cmdline([str(self.folder / "pythonw.exe")]) +
                         ' "' + str(self.script) + '" --background')
        self.assertNotIn("token", command)

    def test_task_identity_is_stable_and_scoped_to_installation(self):
        same = StartupRegistration(self.script)
        other = StartupRegistration(self.folder / "other.py")
        self.assertEqual(same.task_name, self.registration.task_name)
        self.assertNotEqual(other.task_name, self.registration.task_name)


class TaskTransportTests(unittest.TestCase):
    def test_untrusted_powershell_error_is_not_exposed(self):
        registration = StartupRegistration(Path("run.py"))
        failure = subprocess.CompletedProcess([], 1, '{"error":"failure","hresult":"80070005"}', "password=secret-example")
        with patch("quota_widget.startup.subprocess.run", return_value=failure):
            with self.assertRaises(OSError) as raised:
                registration.status()
        self.assertIn("80070005", str(raised.exception))
        self.assertNotIn("secret-example", str(raised.exception))

    def test_task_invocation_uses_literal_argument_list_and_hidden_console(self):
        registration = StartupRegistration(Path("run.py"))
        result = subprocess.CompletedProcess([], 0, '{"installed":false,"enabled":false}', "")
        with patch("quota_widget.startup.subprocess.run", return_value=result) as run:
            registration.status()
        argv = run.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertIn("-NonInteractive", argv)
        self.assertIn(str(registration.script_path), argv)
        self.assertFalse(run.call_args.kwargs.get("shell", False))


@unittest.skipUnless(sys.platform == "win32", "Windows named mutex/events")
class InstanceTests(unittest.TestCase):
    def test_single_instance_is_scoped_to_configuration_and_can_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            first = SingleInstance(Path(directory) / "settings.json")
            second = SingleInstance(Path(directory) / "settings.json")
            separate = SingleInstance(Path(directory) / "demo.json")
            try:
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
                self.assertTrue(separate.acquire())
                self.assertTrue(second.running())
                first.release()
                self.assertTrue(second.acquire())
            finally:
                first.release(); second.release(); separate.release()

    def test_duplicate_launch_restores_via_cross_process_event(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            first = SingleInstance(config)
            try:
                self.assertTrue(first.acquire())
                script = ("from pathlib import Path; from quota_widget.startup import SingleInstance; "
                          "import sys; i=SingleInstance(Path(sys.argv[1])); "
                          "assert not i.acquire(); assert i.signal('restore')")
                result = subprocess.run([sys.executable, "-c", script, str(config)], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(first.pending(), ["restore"])
                self.assertEqual(first.pending(), [])
            finally:
                first.release()
            self.assertFalse(first.running())

    def test_unrecognized_instance_command_is_rejected(self):
        with self.assertRaises(ValueError):
            SingleInstance(Path("unused")).signal("unknown")
