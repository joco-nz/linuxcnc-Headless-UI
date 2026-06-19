"""Shared fixtures for LinuxCNC integration tests.

These tests require a real LinuxCNC installation running in simulation mode.
They use Xvfb for headless display and axis_mm.ini for 3-axis simulation.

Fixture hierarchy (all module-scoped):
    xvfb_display → linuxcnc_instance → sidecar_server → gateway_server
"""

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent import futures

import grpc
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_for_linuxcnc_ready(timeout: float = 60.0, interval: float = 1.0) -> bool:
    """Wait until linuxcnc milltask process is running and Python bindings connect."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            import linuxcnc as _lc
            stat = _lc.stat()
            stat.poll()
            return True
        except Exception:
            elapsed = time.time() - start
            if int(elapsed) % 5 == 0 or elapsed < 3:
                sys.stderr.write(f"  waiting for linuxcnc ({elapsed:.0f}s/60s)\n")
            time.sleep(interval)
    return False


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def xvfb_display():
    """Start Xvfb virtual framebuffer on :99, yield DISPLAY value.
    
    If Xvfb is already running on :99 (e.g., started manually), reuse it
    without trying to start a new instance.
    """
    # Check if Xvfb is already running on :99
    display_ready = False
    try:
        result = subprocess.run(
            ["xdpyinfo", "-display", ":99"],
            capture_output=True, timeout=3,
        )
        if result.returncode == 0:
            display_ready = True
    except Exception:
        pass

    proc = None
    if not display_ready:
        proc = subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1024x768x24", "+extension", "GLX"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)
        if proc.poll() is not None:
            proc.terminate()
            raise RuntimeError("Xvfb failed to start on :99")

    yield ":99"

    # Teardown — only kill if we started it
    if proc is not None:
        try:
            subprocess.run(["pkill", "-f", "Xvfb :99"], capture_output=True)
        except Exception:
            pass


@pytest.fixture(scope="module")
def linuxcnc_instance(xvfb_display):
    """Start LinuxCNC simulation with axis_mm.ini, verify bindings work.

    Yields dict with keys: ini_path, pid, display
    """
    # Remove mocked linuxcnc/hal so real modules get imported later
    for key in list(sys.modules.keys()):
        if key in ("linuxcnc", "hal"):
            del sys.modules[key]
    # Also clear cached linuxcnc_fleet modules
    for key in list(sys.modules.keys()):
        if key.startswith("linuxcnc_fleet"):
            del sys.modules[key]

    # Clean up stale lock/debug files from previous runs
    for f in ("/tmp/linuxcnc.lock", os.path.expanduser("~/linuxcnc_debug.txt"),
              os.path.expanduser("~/linuxcnc_print.txt")):
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass

    ini_path = os.path.expanduser("~/linuxcnc/configs/sim.axis/axis_mm.ini")
    env = {**os.environ, "DISPLAY": xvfb_display}

    # Wait for display to be ready
    time.sleep(0.5)

    # Use timeout to keep parent process alive; -r prevents stdout/stderr redirection
    proc = subprocess.Popen(
        ["timeout", "300", "linuxcnc", "-r", ini_path],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Verify linuxcnc started by checking for milltask process
    time.sleep(5)
    result = subprocess.run(["pgrep", "-f", "milltask"], capture_output=True, text=True)
    if not result.returncode == 0:
        proc.terminate()
        raise RuntimeError(f"milltask did not start (linuxcnc PID={proc.pid})")

    os.environ["EMC_INI"] = ini_path
    if not _wait_for_linuxcnc_ready(timeout=60.0, interval=1.0):
        proc.terminate()
        raise RuntimeError(
           f"LinuxCNC simulation did not become ready within 60s (PID={proc.pid})"
       )

    yield {"ini_path": ini_path, "pid": proc.pid, "display": xvfb_display}

    # Terminate the timeout wrapper (should propagate to linuxcnc children)
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    # Also kill any remaining linuxcnc processes
    for p in ("milltask", "linuxcncsvr", "halui"):
        subprocess.run(["pkill", "-9", p], capture_output=True)

    # Re-inject mocked linuxcnc/hal so subsequent test modules (unit tests)
    # don't accidentally import the real modules after this fixture tears down.
    sys.modules["linuxcnc"] = _make_mock_linuxcnc()
    sys.modules["hal"] = _make_mock_hal()


def _make_mock_linuxcnc():
    """Create a mock linuxcnc module (mirrors conftest.py for reuse here)."""
    from unittest.mock import MagicMock

    mod = MagicMock()
    mod.RCS_DONE = 1
    mod.RCS_EXEC = 2
    mod.RCS_IDLE = 0
    mod.EXEC_STATE_IDLE = 0
    mod.EXEC_STATE_RUN = 1
    mod.EXEC_STATE_FAST_RUN = 2
    mod.EXEC_STATE_STEP = 3
    mod.EXEC_STATE_RETRACT = 4
    mod.EXEC_STATE_MDA = 5
    mod.EXEC_STOP = 0
    mod.EXEC_START = 1
    mod.INTERP_IDLE = 1
    mod.INTERP_READING = 2
    mod.INTERP_PAUSED = 3
    mod.INTERP_WAITING = 4
    mod.INTERP_ERROR = 5
    mod.ESTOP_ACK = 2
    mod.STATE_ESTOP = 0
    mod.STATE_ESTOP_RESET = 1
    mod.STATE_OFF = 2
    mod.STATE_ON = 3
    mod.MODE_MANUAL = 1
    mod.MODE_AUTO = 2
    mod.MODE_MDI = 3

    class MockStat:
        def __init__(self):
            self.state = mod.RCS_IDLE
            self.exec_state = mod.EXEC_STATE_IDLE
            self.interp_state = mod.INTERP_IDLE
            self.estop = 0
            self.task_mode = mod.MODE_MANUAL
            self.motion_line = 0
            self.file = ""
            self.feedrate = 100.0
            self.joints = 3
            self.spindle_at_speed = False
            self.mist = False
            self.flood = False
            self.motion_type = 0
            self.joint_actual_position = tuple([0.0] * 16)
            self.joint_position = tuple([0.0] * 16)
            self.position = (0.0,) * 9
            self.actual_position = (0.0,) * 9
            self.spindle = ({'speed': 0.0, 'override': 1.0},)

        def poll(self):
            pass

    class MockCommand:
        _instances = []

        def __init__(self):
            self._state = 0
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
    from unittest.mock import MagicMock

    mod = MagicMock()
    mod.HAL_BIT = 0
    mod.HAL_U32 = 1
    mod.HAL_S32 = 2
    mod.HAL_FLOAT = 3
    mod.HAL_IN = 16
    mod.HAL_OUT = 32
    mod.HAL_IO = 48
    return mod


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


@pytest.fixture(scope="module")
def sidecar_server(linuxcnc_instance):
    """Start LinuxCncSidecar connected to real linuxcnc instance.

    Yields (port, sidecar) tuple. Sidecar will connect to running linuxcnc
    via Python bindings using the provided ini_path.
    """
    import threading

    # Clear cached modules so fresh hal import picks up running linuxcnc state
    sys.modules.pop("linuxcnc_fleet.headless", None)
    sys.modules.pop("hal", None)

    from linuxcnc_fleet.headless import LinuxCncSidecar
    from linuxcnc_fleet.server import create_server

    port = _find_free_port()

    sidecar = LinuxCncSidecar(
        ini_path=linuxcnc_instance["ini_path"],
        machine_id="integration-real-machine",
    )
    sidecar.run()

    server = create_server(sidecar=sidecar, port=port)
    server.start()

    time.sleep(0.3)  # Let polling loop stabilize

    def stop():
        server.stop(grace=0.5)
        sidecar.shutdown()

    yield port, sidecar, stop


@pytest.fixture(scope="module")
def gateway_server(sidecar_server):
    """Start Gateway with the real sidecar registered.

    Yields (gateway_port, registry, sidecar_stop_fn, gateway_stop_fn).
    """
    import threading

    from gateway.auth import create_test_auth_manager
    from gateway.policies import create_test_policy_engine
    from gateway.registry import create_test_registry
    from gateway.server import create_gateway_server

    port, sidecar, stop_sidecar = sidecar_server

    auth_manager = create_test_auth_manager()
    policy_engine = create_test_policy_engine()
    registry = create_test_registry(heartbeat_ttl=30.0)

    registry.register(
        machine_id="integration-real-machine",
        address="127.0.0.1",
        port=port,
        facility="test-facility",
        tags=["cnc", "simulation"],
        version="sim-axis-mm",
    )
    registry.start()

    gw_port = _find_free_port()

    server = create_gateway_server(
        auth_manager=auth_manager,
        policy_engine=policy_engine,
        registry=registry,
        port=gw_port,
    )
    server.start()

    time.sleep(0.3)

    def stop_gw():
        server.stop(grace=0.5)
        registry.stop()

    yield gw_port, registry, stop_sidecar, stop_gw


# ---------------------------------------------------------------------------
# HAL pin discovery fixture
# ---------------------------------------------------------------------------

def _find_pin(pins, type_filter=None, keyword=None):
    """Find first pin matching type and/or keyword pattern."""
    for pin in pins:
        if type_filter is not None and pin.type != type_filter:
            continue
        if keyword is not None and keyword not in pin.name.lower():
            continue
        return pin.name
    return None


@pytest.fixture(scope="module")
def discovered_hal_pins(gateway_server):
    """Discover HAL pins once per module and yield a lookup dict.

    Yields a dict mapping structured keys to pin names, matched by type
    and keyword pattern rather than exact name so tests survive INI changes.

    Skips the entire test module if no matching pins are found.
    """
    gw_port, registry, stop_sidecar, stop_gw = gateway_server

    import grpc
    from gateway.auth import create_test_token
    from linuxcnc_fleet.fleet_pb2 import ListHalRequest, MachineId
    from linuxcnc_fleet.fleet_pb2_grpc import FleetGatewayServiceStub, FleetServiceStub

    gateway_channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
    gateway_stub = FleetGatewayServiceStub(gateway_channel)

    token = create_test_token({
        "sub": "test-admin",
        "name": "Test Admin",
        "role": "admin",
    })
    metadata = [("authorization", f"Bearer {token}")]

    route_resp = gateway_stub.RouteMachine(
        MachineId(id="integration-real-machine"),
        metadata=metadata,
    )

    sidecar_channel = grpc.insecure_channel(
        f"{route_resp.instance_address}:{route_resp.instance_port}"
    )
    sidecar_stub = FleetServiceStub(sidecar_channel)

    hal_list = sidecar_stub.ListHalComponents(
        ListHalRequest(id=MachineId(id="integration-real-machine")),
    )
    all_pins = []
    for comp in hal_list.components:
        all_pins.extend(comp.pins)

    from linuxcnc_fleet.fleet_pb2 import HalPinType

    result = {}
    # Float position feedback pin (e.g. joint.0.motor-pos-fb)
    pin = _find_pin(all_pins, type_filter=HalPinType.PIN_TYPE_FLOAT, keyword="pos-fb")
    if pin:
        result["float_position"] = pin

    # Bit enable-in pin (e.g. iocontrol.0.emc-enable-in)
    pin = _find_pin(all_pins, type_filter=HalPinType.PIN_TYPE_BIT, keyword="emc-enable-in")
    if pin:
        result["bit_enable_in"] = pin

    # Bit enable-out pin (e.g. iocontrol.0.user-enable-out)
    pin = _find_pin(all_pins, type_filter=HalPinType.PIN_TYPE_BIT, keyword="user-enable-out")
    if pin:
        result["bit_enable_out"] = pin

    gateway_channel.close()
    sidecar_channel.close()

    if not result:
        pytest.skip("No HAL pins found — skipping integration tests requiring HAL pins")

    yield result
