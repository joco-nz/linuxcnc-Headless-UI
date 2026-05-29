"""Shared fixtures and mocks for linuxcnc-fleet tests."""

import sys
from unittest.mock import MagicMock

import pytest


def _make_mock_linuxcnc():
    """Create a mock linuxcnc module with all needed constants and classes."""
    mod = MagicMock()

    # State constants
    mod.RCS_DONE = 4
    mod.RCS_RUNNING = 3
    mod.RCS_IDLE = 1

    # Execution state constants
    mod.EXEC_STATE_IDLE = 0
    mod.EXEC_STATE_RUN = 1
    mod.EXEC_STATE_FAST_RUN = 2
    mod.EXEC_STATE_STEP = 3
    mod.EXEC_STATE_RETRACT = 4
    mod.EXEC_STATE_MDA = 5

    mod.EXEC_STOP = 0
    mod.EXEC_START = 1

    # Interp state constants
    mod.INTERP_IDLE = 0
    mod.INTERP_READ = 1
    mod.INTERP_EXEC = 3

    # Estop constants
    mod.ESTOP_ACK = 2

    # Mode constants
    mod.MODE_MANUAL = 1
    mod.MODE_AUTO = 2
    mod.MODE_MDI = 3

    class MockStat:
        def __init__(self):
            self.state = mod.RCS_IDLE
            self.execution = mod.EXEC_STATE_IDLE
            self.interp_state = mod.INTERP_IDLE
            self.estop_state = 0  # NOT_E_STOPPED (ESTOP_ACK=2 would block mode changes)
            self.mode = mod.MODE_MANUAL
            self.x = 0.0
            self.y = 0.0
            self.z = 0.0
            self.a = 0.0
            self.b = 0.0
            self.c = 0.0
            self.interp_line = 0
            self.program_file = ""
            self.spindle_at_speed = False
            self.coolant_mist = False
            self.coolant_flood = False
            self.coolant_mazak = False
            self.motion_type = 0
            self.linear_axis = {"feedrate": 100.0, "feed_percent": 1.0}
            self.spindle = {"speed_percent": 1.0}
            # joint_actual_pos and joint_commanded_pos: list of [joint_num, pos] pairs
            self.joint_actual_pos = [[0, 0.0], [1, 0.0], [2, 0.0]]
            self.joint_commanded_pos = [[0, 0.0], [1, 0.0], [2, 0.0]]
            self.joint_config = [3]

        def poll(self):
            pass

    class MockCommand:
        def __init__(self):
            pass

        def mode(self, m):
            pass

        def execute(self, e):
            pass

        def feed_hold(self):
            pass

        def continue_(self):
            pass

        def home(self, axis):
            pass

        def mdi(self, cmd):
            pass

        def program_open(self, path):
            pass

        def fast_mode(self, on):
            pass

        def retract(self, on):
            pass

    class MockErrorChannel:
        def __init__(self):
            self._queue = []

        def poll(self):
            pass

        def get(self):
            if not self._queue:
                raise IndexError("empty")
            return self._queue.pop(0)

    class MockIni:
        _find_path = "/fake.ini"

        def __init__(self, path=""):
            self._file_version = "2.9"
            self._build_type = "release"
            self._git_hash = ""

        @classmethod
        def find(cls):
            return cls._find_path

        def __call__(self, section, option):
            return f"{section}.{option}"

        def FILE_VERSION(self):
            return self._file_version

        def BUILD_TYPE(self):
            return self._build_type

        def GIT_HASH(self):
            return self._git_hash

    mod.stat = MockStat
    mod.command = MockCommand
    mod.error_channel = MockErrorChannel
    mod.ini = MockIni

    return mod


def _make_mock_hal():
    """Create a mock _hal module."""
    mod = MagicMock()
    mod.HAL_BIT = 0
    mod.HAL_U32 = 1
    mod.HAL_S32 = 2
    mod.HAL_FLOAT = 3

    return mod


def pytest_configure(config):
    """Inject linuxcnc/_hal mocks before any test modules are imported.

    This runs during pytest's configuration phase, before collection/imports.
    headless.py imports linuxcnc at module level — we must have our mock in
    sys.modules before that import happens.
    """
    # Clear any cached linuxcnc_fleet modules first
    for key in list(sys.modules.keys()):
        if key.startswith("linuxcnc_fleet"):
            del sys.modules[key]

    mod = _make_mock_linuxcnc()
    hal = _make_mock_hal()
    sys.modules["linuxcnc"] = mod
    sys.modules["_hal"] = hal


def pytest_runtest_setup(item):
    """Clean up logging handlers before each test to prevent cross-test pollution.

    Some tests (e.g., syslog config tests) add MagicMock handlers to root.handlers
    that can leak into subsequent tests and cause TypeError when Python's logging
    tries to compare record.levelno >= handler.level (int vs MagicMock).
    """
    import logging

    for handler in logging.root.handlers[:]:
        try:
            handler.close()
        except (OSError, ValueError):
            pass
        logging.root.removeHandler(handler)


def pytest_unconfigure(config):
    """Suppress pyvirtualdisplay/pytest-xvfb logging errors during teardown.

    The virtual display plugin tries to log after pytest has already closed
    stdout/stderr, causing "ValueError: I/O operation on closed file" tracebacks.
    Removing logging handlers before the cleanup phase prevents this.
    """
    import logging

    for handler in logging.root.handlers[:]:
        try:
            handler.close()
        except (OSError, ValueError):
            pass
        logging.root.removeHandler(handler)


@pytest.fixture
def linuxcnc_module():
    """Provide the mock linuxcnc module (for state mapping tests)."""
    return _make_mock_linuxcnc()


@pytest.fixture
def hal_module():
    """Provide the mock _hal module."""
    return _make_mock_hal()
