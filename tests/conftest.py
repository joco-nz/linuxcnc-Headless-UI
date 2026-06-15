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

    # Interp state constants (LinuxCNC 2.9 actual values)
    mod.INTERP_IDLE = 1
    mod.INTERP_READING = 2
    mod.INTERP_PAUSED = 3
    mod.INTERP_WAITING = 4

    # Estop constants
    mod.ESTOP_ACK = 2

    # Machine control state constants
    mod.STATE_ESTOP = 0
    mod.STATE_ESTOP_RESET = 1
    mod.STATE_OFF = 2
    mod.STATE_ON = 3

    # Mode constants
    mod.MODE_MANUAL = 1
    mod.MODE_AUTO = 2
    mod.MODE_MDI = 3

    class MockStat:
        def __init__(self):
            self.state = mod.RCS_IDLE
            self.exec_state = mod.EXEC_STATE_IDLE
            self.interp_state = mod.INTERP_IDLE  # LinuxCNC 2.9 value = 1
            self.estop = 0  # NOT_E_STOPPED (ESTOP_ACK=2 would block mode changes)
            self.task_mode = mod.MODE_MANUAL
            self.motion_line = 0
            self.file = ""
            self.feedrate = 100.0
            self.joints = 3
            self.spindle_at_speed = False
            self.mist = False
            self.flood = False
            self.motion_type = 0
            # Flat tuples indexed by axis (real LinuxCNC 2.9 API)
            self.joint_actual_position = tuple([0.0] * 16)
            self.joint_position = tuple([0.0] * 16)
            # World position tuples (x, y, z, a, b, c, u, v, w)
            self.position = (0.0,) * 9
            self.actual_position = (0.0,) * 9
            # Spindle is a tuple of dicts in real LinuxCNC 2.9
            self.spindle = ({'speed': 0.0, 'override': 1.0},)

        def poll(self):
            pass

    class MockCommand:
        _instances = []

        def __init__(self):
            self._state = 0  # Current machine state (STATE_ESTOP=0 by default)
            self._calls = []
            MockCommand._instances.append(self)

        def mode(self, m):
            self._calls.append(("mode", m))

        def execute(self, e):
            self._calls.append(("execute", e))

        def feed_hold(self):
            self._calls.append(("feed_hold",))

        def continue_(self):
            self._calls.append(("continue_",))

        def home(self, axis):
            self._calls.append(("home", axis))

        def mdi(self, cmd):
            self._calls.append(("mdi", cmd))

        def program_open(self, path):
            self._calls.append(("program_open", path))

        def fast_mode(self, on):
            self._calls.append(("fast_mode", on))

        def retract(self, on):
            self._calls.append(("retract", on))

        def state(self, s):
            """Set machine control state (STATE_ESTOP, STATE_ESTOP_RESET, STATE_OFF, STATE_ON)."""
            self._state = s
            self._calls.append(("state", s))

    class MockErrorChannel:
        def __init__(self):
            self._errors = []

        def poll(self):
            if self._errors:
                return self._errors.pop(0)
            return None

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
    """Clean up logging handlers and reset shared mocks between tests.

    Some tests (e.g., syslog config tests) add MagicMock handlers to root.handlers
    that can leak into subsequent tests and cause TypeError when Python's logging
    tries to compare record.levelno >= handler.level (int vs MagicMock).

    Also resets the linuxcnc/_hal mock state to prevent cross-test pollution.
    """
    import logging

    for handler in logging.root.handlers[:]:
        try:
            handler.close()
        except (OSError, ValueError):
            pass
        logging.root.removeHandler(handler)

    # Reset shared mocks used by headless.py tests
    for mod_name in ("linuxcnc", "_hal"):
        if mod_name in sys.modules:
            mod = sys.modules[mod_name]
            if hasattr(mod, "reset_mock"):
                mod.reset_mock()
            # Reset MockCommand instances state
            if hasattr(mod, "command") and hasattr(mod.command, "_instances"):
                for instance in mod.command._instances:
                    instance._state = 0
                    instance._calls = []


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
