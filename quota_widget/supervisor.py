"""Blocking, low-resource supervision of this project's own GUI child only."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import subprocess
import time


def supervisor_config(config):
    return config.with_name(config.stem + ".supervisor.json")


class ProcessSupervisor:
    def __init__(self, command, control, child_control, log, *, launcher=None, wait=None, retry_wait=None):
        self.command = command
        self.control = control
        self.child_control = child_control
        self.log = log
        self.launcher = launcher or self._launch
        self.wait = wait or self._wait
        self.retry_wait = retry_wait or self._retry_wait
        self._restore_after_launch = False

    def _launch(self):
        return subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)

    def _wait_handles(self, handles, milliseconds):
        native = self.control._kernel32
        native.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
                                                  wintypes.BOOL, wintypes.DWORD]
        native.WaitForMultipleObjects.restype = wintypes.DWORD
        values = (wintypes.HANDLE * len(handles))(*handles)
        result = native.WaitForMultipleObjects(len(handles), values, False, milliseconds)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def _wait(self, process):
        while True:
            result = self._wait_handles([int(process._handle), self.control._events["stop"],
                                         self.control._events["restore"]], 0xFFFFFFFF)
            if result == 0:
                return process.wait()
            if result == 1:
                self.child_control.signal("stop", wait=1)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # The user explicitly requested stop; this is our own child.
                    process.terminate()
                    process.wait(timeout=5)
                return 0
            if result == 2:
                self.child_control.signal("restore", wait=5)

    def _retry_wait(self, delay):
        result = self._wait_handles([self.control._events["stop"], self.control._events["restore"]], int(delay * 1000))
        if result == 1:
            self._restore_after_launch = True
        return result != 0

    def run(self):
        failures = 0
        while True:
            try:
                started = time.monotonic()
                process = self.launcher()
                self.log.state("running", managed=True)
                self.log.event("worker_started", pid=process.pid, managed=True)
                if self._restore_after_launch:
                    self.child_control.signal("restore", wait=5)
                    self._restore_after_launch = False
                code = self.wait(process)
                if code == 0:
                    self.log.state("stopped", managed=True)
                    return 0
                if time.monotonic() - started > 300:
                    failures = 0
                self.log.event("worker_exited_abnormally", exit_code=code)
            except OSError as error:
                self.log.failure("worker_launch_failed", error)
            delay = (1, 5, 15, 30, 60)[min(failures, 4)]
            failures += 1
            self.log.state("restarting", managed=True)
            if not self.retry_wait(delay):
                self.log.state("stopped", managed=True)
                return 0
