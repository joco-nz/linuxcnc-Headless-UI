"""Shared fixtures for LinuxCNC integration tests.

These tests require a real LinuxCNC installation running in simulation mode.
They use Xvfb for headless display and axis_mm.ini for 3-axis simulation.

Fixture hierarchy (all module-scoped):
    xvfb_display → linuxcnc_instance → sidecar_server → gateway_server
"""

import os
import socket
import subprocess
import sys
import time

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
