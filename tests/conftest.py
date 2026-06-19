"""Shared fixtures and mocks for linuxcnc-fleet tests."""

import asyncio
import socket
import sys
import threading
import time
from concurrent import futures
from unittest.mock import MagicMock

import grpc
import pytest


# ---------------------------------------------------------------------------
# Shared integration test helpers (IT10 — deduplicated from renewal + E2E tests)
# ---------------------------------------------------------------------------

def _find_free_port():
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_gateway_with_http(gw_port, http_port, auth_manager, policy_engine, registry,
                            allowed_roles=None, allowed_subjects=None, allowed_ips=None,
                            token_ttl=3, allow_admin_token=False, permissive=False):
    """Start gateway gRPC + HTTP servers in background threads.

    Returns (grpc_server, cleanup_fn) for cleanup.
    """
    from aiohttp import web as aiohttp_web
    from gateway.server import TokenIssuanceServicer, GatewayServiceServicer
    from linuxcnc_fleet.fleet_pb2_grpc import add_FleetGatewayServiceServicer_to_server

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    servicer = GatewayServiceServicer(auth_manager, policy_engine, registry)
    add_FleetGatewayServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"[::]:{gw_port}")

    token_servicer = TokenIssuanceServicer(
        auth_manager=auth_manager,
        policy_engine=policy_engine,
        allowed_roles=allowed_roles or ["viewer", "operator"],
        allowed_subjects=allowed_subjects or ["fleet-ui"],
        allowed_ips=allowed_ips or ["127.0.0.1", "::1"],
        token_ttl=token_ttl,
        allow_admin_token=allow_admin_token,
        permissive=permissive,
    )

    http_app = aiohttp_web.Application()
    http_app["token_servicer"] = token_servicer

    async def _auth_token_handler(request):
        servicer_instance: TokenIssuanceServicer = request.app["token_servicer"]
        client_ip = request.remote or "0.0.0.0"
        role = request.query.get("role", "viewer")
        sub = request.query.get("sub", "fleet-ui")
        try:
            result = servicer_instance.issue_token(role=role, sub=sub, client_ip=client_ip)
            return aiohttp_web.json_response(result)
        except Exception as e:
            error_code = getattr(e, "error_code", 403) if hasattr(e, "error_code") else 403
            return aiohttp_web.json_response(
                {"error": str(e), "error_code": error_code}, status=error_code
            )

    http_app.router.add_post("/api/auth/token", _auth_token_handler)

    state = {"runner": None, "thread": None}

    def _run_http():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _async_start():
            runner = aiohttp_web.AppRunner(http_app)
            await runner.setup()
            site = aiohttp_web.TCPSite(runner, "127.0.0.1", http_port)
            await site.start()
            state["runner"] = runner

        loop.run_until_complete(_async_start())
        state["thread"] = threading.Thread(target=loop.run_forever, daemon=True)
        state["thread"].start()

    http_thread = threading.Thread(target=_run_http, daemon=True)
    http_thread.start()
    time.sleep(0.25)

    server.start()

    def cleanup():
        server.stop(grace=0.5)
        runner = state.get("runner")
        if runner:
            try:
                l = asyncio.new_event_loop()
                l.run_until_complete(runner.cleanup())
                l.close()
            except Exception:
                pass
        thread = state.get("thread")
        if thread:
            thread.join(timeout=2)

    return server, cleanup


def _make_mock_linuxcnc():
    """Create a mock linuxcnc module with all needed constants and classes."""
    mod = MagicMock()

    # State constants (real LinuxCNC values)
    mod.RCS_DONE = 1
    mod.RCS_EXEC = 2
    mod.RCS_IDLE = 0

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
    mod.INTERP_ERROR = 5

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
            MockCommand._stats.append(self)
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
        _stats = []

        def __init__(self):
            self._state = 0  # Current machine state (STATE_ESTOP=0 by default)
            self._calls = []
            MockCommand._instances.append(self)

        def mode(self, m):
            self._calls.append(("mode", m))
            for stat in MockCommand._stats:
                stat.task_mode = m

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
    """Create a mock hal module."""
    mod = MagicMock()
    mod.HAL_BIT = 0
    mod.HAL_U32 = 1
    mod.HAL_S32 = 2
    mod.HAL_FLOAT = 3
    mod.HAL_IN = 16
    mod.HAL_OUT = 32
    mod.HAL_IO = 48

    return mod


def pytest_configure(config):
    """Inject linuxcnc/hal mocks before any test modules are imported.

    This runs during pytest's configuration phase, before collection/imports.
    headless.py imports linuxcnc at module level — we must have our mock in
    sys.modules before that import happens.
    """
    # Clear any cached linuxcnc_fleet modules first
    for key in list(sys.modules.keys()):
        if key.startswith("linuxcnc_fleet"):
            del sys.modules[key]

    mod = _make_mock_linuxcnc()
    hal_mod = _make_mock_hal()
    sys.modules["linuxcnc"] = mod
    sys.modules["hal"] = hal_mod


def pytest_runtest_setup(item):
    """Clean up logging handlers and reset shared mocks between tests.

    Some tests (e.g., syslog config tests) add MagicMock handlers to root.handlers
    that can leak into subsequent tests and cause TypeError when Python's logging
    tries to compare record.levelno >= handler.level (int vs MagicMock).

    Also resets the linuxcnc/hal mock state to prevent cross-test pollution.
    """
    import logging

    for handler in logging.root.handlers[:]:
        try:
            handler.close()
        except (OSError, ValueError):
            pass
        logging.root.removeHandler(handler)

    # Reset shared mocks used by headless.py tests
    for mod_name in ("linuxcnc", "hal"):
        if mod_name not in sys.modules:
            # Safety net: if the mock was deleted (e.g., by integration fixtures)
            # and never restored, re-inject it so unit tests don't import real modules.
            sys.modules[mod_name] = _make_mock_linuxcnc() if mod_name == "linuxcnc" else _make_mock_hal()
        else:
            mod = sys.modules[mod_name]
            if not hasattr(mod, "reset_mock"):
                # Real module was imported instead of mock — replace with mock.
                # Also clear cached linuxcnc_fleet modules so they re-import with the mock.
                sys.modules[mod_name] = _make_mock_linuxcnc() if mod_name == "linuxcnc" else _make_mock_hal()
                for key in list(sys.modules.keys()):
                    if key.startswith("linuxcnc_fleet"):
                        del sys.modules[key]
            else:
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
    """Provide the mock hal module."""
    return _make_mock_hal()
