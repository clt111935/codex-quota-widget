from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quota_widget.supervisor import ProcessSupervisor, supervisor_config


class SupervisorTests(unittest.TestCase):
    def make(self, outcomes):
        self.log = Mock()
        self.child_control = Mock()
        self.launch = Mock(return_value=SimpleNamespace(pid=123))
        self.retry = Mock(return_value=True)
        return ProcessSupervisor([], Mock(), self.child_control, self.log,
                                 launcher=self.launch, wait=Mock(side_effect=outcomes), retry_wait=self.retry)

    def test_intentional_exit_does_not_respawn(self):
        supervisor = self.make([0])
        self.assertEqual(supervisor.run(), 0)
        self.launch.assert_called_once()
        self.retry.assert_not_called()
        self.log.state.assert_called_with("stopped", managed=True)

    def test_failures_restart_with_bounded_backoff(self):
        supervisor = self.make([7] * 7 + [0])
        supervisor.run()
        self.assertEqual([call.args[0] for call in self.retry.call_args_list], [1, 5, 15, 30, 60, 60, 60])
        self.assertEqual(self.launch.call_count, 8)

    def test_launch_error_can_recover_without_logging_raw_error(self):
        supervisor = self.make([0])
        self.launch.side_effect = [OSError("do not expose this"), SimpleNamespace(pid=123)]
        supervisor.run()
        self.assertEqual(self.launch.call_count, 2)
        self.log.failure.assert_called_once()

    def test_stop_during_backoff_ends_supervisor(self):
        supervisor = self.make([7])
        self.retry.return_value = False
        self.assertEqual(supervisor.run(), 0)
        self.launch.assert_called_once()

    def test_long_running_worker_resets_backoff(self):
        supervisor = self.make([7, 7, 0])
        with patch("quota_widget.supervisor.time.monotonic", side_effect=[0, 1, 10, 311, 320]):
            supervisor.run()
        self.assertEqual([call.args[0] for call in self.retry.call_args_list], [1, 1])

    def test_restore_during_backoff_reaches_new_child(self):
        supervisor = self.make([0])
        supervisor._restore_after_launch = True
        supervisor.run()
        self.child_control.signal.assert_called_once_with("restore", wait=5)

    def test_supervisor_uses_separate_instance_identity(self):
        self.assertEqual(supervisor_config(Path(".local/settings.json")), Path(".local/settings.supervisor.json"))

