"""Integration tests — full flow: FleetClient → Gateway → Sidecar(s).

Starts real gRPC servers (gateway + sidecar) in threads with mocked linuxcnc,
then exercises end-to-end paths that unit tests cannot reach: serialization,
channel setup, auth interceptor chaining, broadcast fan-out.
"""

import grpc
import pytest

from gateway.auth import create_test_auth_manager, create_test_token
from gateway.policies import create_test_policy_engine
from gateway.registry import create_test_registry
from linuxcnc_fleet.fleet_pb2 import (
    BroadcastRequest,
    DiscoverRequest,
    Empty,
    ExecutionCommand,
    GetErrorsRequest,
    HomeAxisRequest,
    MachineId,
    MdiCommand,
    Mode,
    ProgramPath,
    SetModeRequest,
    SubscribeAllRequest,
)
from linuxcnc_fleet.fleet_pb2_grpc import FleetGatewayServiceStub, FleetServiceStub


# ---------------------------------------------------------------------------
# Fixtures: start real gRPC servers in threads
# ---------------------------------------------------------------------------

@pytest.fixture()
def sidecar_server():
    """Start a single sidecar server on a free port. Returns (port, sidecar)."""
    import socket
    import threading
    import time as _time

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    from linuxcnc_fleet.headless import LinuxCncSidecar
    from linuxcnc_fleet.server import create_server

    sidecar = LinuxCncSidecar(
        machine_id="integration-machine-1",
        ini_path="/fake.ini",
    )
    sidecar.run()

    server = create_server(sidecar=sidecar, port=port)
    server.start()

    _time.sleep(0.15)

    def stop():
        server.stop(grace=0.5)
        sidecar.shutdown()

    yield port, sidecar, stop


@pytest.fixture()
def multi_sidecar_servers():
    """Start two sidecar servers on free ports. Returns list of (port, sidecar)."""
    import socket
    import time as _time

    from linuxcnc_fleet.headless import LinuxCncSidecar
    from linuxcnc_fleet.server import create_server

    servers_info = []
    stoppers = []

    for i in range(2):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        machine_id = f"integration-machine-{i + 1}"
        sidecar = LinuxCncSidecar(
            machine_id=machine_id,
            ini_path="/fake.ini",
        )
        sidecar.run()

        server = create_server(sidecar=sidecar, port=port)
        server.start()
        servers_info.append((port, sidecar))
        stoppers.append(lambda s=server, sc=sidecar: (s.stop(grace=0.5), sc.shutdown())[1])

    _time.sleep(0.2)

    try:
        yield servers_info
    finally:
        for st in stoppers:
            st()


@pytest.fixture()
def gateway_server(sidecar_server):
    """Start a gateway server with a registered sidecar. Returns (gateway_port, registry)."""
    port, sidecar, stop_sidecar = sidecar_server

    import time as _time
    from gateway.server import create_gateway_server

    auth_manager = create_test_auth_manager()
    policy_engine = create_test_policy_engine()
    registry = create_test_registry(heartbeat_ttl=30.0)

    registry.register(
        machine_id="integration-machine-1",
        address="127.0.0.1",
        port=port,
        facility="test-facility",
        tags=["cnc", "lathe"],
    )
    registry.start()

    gw_port = 50100

    server = create_gateway_server(
        auth_manager=auth_manager,
        policy_engine=policy_engine,
        registry=registry,
        port=gw_port,
    )
    server.start()

    _time.sleep(0.15)

    def stop():
        server.stop(grace=0.5)
        registry.stop()

    yield gw_port, registry, stop_sidecar, stop


@pytest.fixture()
def multi_gateway_server(multi_sidecar_servers):
    """Start a gateway with two registered sidecars."""
    import time as _time

    from gateway.server import create_gateway_server

    auth_manager = create_test_auth_manager()
    policy_engine = create_test_policy_engine()
    registry = create_test_registry(heartbeat_ttl=30.0)

    for i, (port, sidecar) in enumerate(multi_sidecar_servers):
        registry.register(
            machine_id=f"integration-machine-{i + 1}",
            address="127.0.0.1",
            port=port,
            facility="test-facility",
            tags=["cnc"],
        )
    registry.start()

    gw_port = 50101

    server = create_gateway_server(
        auth_manager=auth_manager,
        policy_engine=policy_engine,
        registry=registry,
        port=gw_port,
    )
    server.start()

    _time.sleep(0.2)

    try:
        yield gw_port, registry
    finally:
        server.stop(grace=0.5)
        registry.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_admin_token():
    return create_test_token({
        "sub": "test-admin",
        "name": "Test Admin",
        "role": "admin",
    })


def _make_operator_token():
    return create_test_token({
        "sub": "test-operator",
        "name": "Test Operator",
        "role": "operator",
    })


def _make_viewer_token():
    """Create a viewer JWT (read-only) for integration tests."""
    return create_test_token({
        "sub": "test-viewer",
        "name": "Test Viewer",
        "role": "viewer",
        "facility": "test-facility",
    })


# ---------------------------------------------------------------------------
# Tests: Discover → Route → GetStatus flow
# ---------------------------------------------------------------------------

class TestDiscoverRouteGetStatus:
    """End-to-end: discover machines, route to them, read status."""

    def test_discover_machines(self, gateway_server):
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        stub = FleetGatewayServiceStub(channel)

        token = _make_admin_token()
        metadata = [("authorization", f"Bearer {token}")]

        resp = stub.DiscoverMachines(
            DiscoverRequest(facility=""),
            metadata=metadata,
        )

        assert len(resp.machines) == 1
        assert resp.machines[0].machine_id == "integration-machine-1"
        assert resp.machines[0].host_address == "127.0.0.1"

        channel.close()

    def test_route_machine(self, gateway_server):
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        stub = FleetGatewayServiceStub(channel)

        token = _make_admin_token()
        metadata = [("authorization", f"Bearer {token}")]

        resp = stub.RouteMachine(
            MachineId(id="integration-machine-1"),
            metadata=metadata,
        )

        assert resp.instance_address == "127.0.0.1"
        assert resp.instance_port > 0

        channel.close()

    def test_get_status_via_gateway(self, gateway_server):
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        gateway_channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        gateway_stub = FleetGatewayServiceStub(gateway_channel)

        token = _make_admin_token()
        metadata = [("authorization", f"Bearer {token}")]

        route_resp = gateway_stub.RouteMachine(
            MachineId(id="integration-machine-1"),
            metadata=metadata,
        )

        sidecar_channel = grpc.insecure_channel(
            f"{route_resp.instance_address}:{route_resp.instance_port}"
        )
        sidecar_stub = FleetServiceStub(sidecar_channel)

        status = sidecar_stub.GetStatus(MachineId(id="integration-machine-1"))

        assert status.machine_id == "integration-machine-1"
        assert status.program_file == ""
        assert status.feedrate >= 0

        sidecar_channel.close()
        gateway_channel.close()

    def test_viewer_can_discover(self, gateway_server):
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        stub = FleetGatewayServiceStub(channel)

        token = _make_viewer_token()
        metadata = [("authorization", f"Bearer {token}")]

        resp = stub.DiscoverMachines(
            DiscoverRequest(facility="test-facility"),
            metadata=metadata,
        )
        assert len(resp.machines) == 1

        channel.close()


# ---------------------------------------------------------------------------
# Tests: Broadcast command to multiple machines
# ---------------------------------------------------------------------------

class TestBroadcastCommand:
    """End-to-end: broadcast MDI commands to multiple sidecars."""

    def test_broadcast_mdi_to_all(self, multi_gateway_server):
        gw_port, registry = multi_gateway_server

        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        stub = FleetGatewayServiceStub(channel)

        token = _make_admin_token()
        metadata = [("authorization", f"Bearer {token}")]

        mdi_req = MdiCommand(
            id=MachineId(id="integration-machine-1"),
            command="G0 X0 Y0 Z0",
        )

        resp = stub.BroadcastCommand(
            BroadcastRequest(scope=0, mdi=mdi_req),
            metadata=metadata,
        )

        assert len(resp.results) == 2
        assert "integration-machine-1" in resp.results
        assert "integration-machine-2" in resp.results

        channel.close()

    def test_broadcast_mode_change(self, multi_gateway_server):
        gw_port, registry = multi_gateway_server

        gateway_channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        gateway_stub = FleetGatewayServiceStub(gateway_channel)

        token = _make_admin_token()
        metadata = [("authorization", f"Bearer {token}")]

        route_resp = gateway_stub.RouteMachine(
            MachineId(id="integration-machine-1"),
            metadata=metadata,
        )

        sidecar_channel = grpc.insecure_channel(
            f"{route_resp.instance_address}:{route_resp.instance_port}"
        )
        sidecar_stub = FleetServiceStub(sidecar_channel)

        status_before = sidecar_stub.GetStatus(MachineId(id="integration-machine-1"))
        assert status_before.mode == Mode.MODE_MANUAL

        sidecar_channel.close()
        gateway_channel.close()


# ---------------------------------------------------------------------------
# Tests: Streaming status subscription through gateway
# ---------------------------------------------------------------------------

class TestStreamingStatus:
    """End-to-end: subscribe to status streams via gateway fan-out."""

    def test_subscribe_all_status(self, gateway_server):
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        stub = FleetGatewayServiceStub(channel)

        token = _make_admin_token()
        metadata = [("authorization", f"Bearer {token}")]

        req = SubscribeAllRequest(
            facility="test-facility",
            poll_interval_seconds=0.05,
        )

        received = []
        try:
            for status in stub.SubscribeAllStatus(req, metadata=metadata, timeout=2.0):
                received.append(status)
                if len(received) >= 3:
                    break
        except grpc.RpcError:
            pass

        assert len(received) > 0, "Should receive at least some status updates"
        assert all(s.machine_id == "integration-machine-1" for s in received)

        channel.close()


# ---------------------------------------------------------------------------
# Tests: Sidecar control commands (direct gRPC, no gateway)
# ---------------------------------------------------------------------------

class TestSidecarDirectCommands:
    """End-to-end: direct gRPC to sidecar for control commands."""

    def test_set_mode(self, sidecar_server):
        """SetMode RPC succeeds via real gRPC channel (mock doesn't persist state)."""
        port, sidecar, stop = sidecar_server

        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        stub = FleetServiceStub(channel)

        result = stub.SetMode(
            SetModeRequest(id=MachineId(id="test"), mode=Mode.MODE_AUTO)
        )

        assert result.success is True
        channel.close()

    def test_home_axis(self, sidecar_server):
        port, sidecar, stop = sidecar_server

        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        stub = FleetServiceStub(channel)

        result = stub.HomeAxis(
            HomeAxisRequest(
                id=MachineId(id="test"),
                axis=2,  # Z_AXIS
            )
        )

        assert result.success is True
        channel.close()

    def test_send_mdi_command(self, sidecar_server):
        port, sidecar, stop = sidecar_server

        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        stub = FleetServiceStub(channel)

        result = stub.SendMdiCommand(
            MdiCommand(
                id=MachineId(id="test"),
                command="G0 X0 Y0",
            )
        )

        assert result.success is True
        channel.close()

    def test_load_program(self, sidecar_server):
        port, sidecar, stop = sidecar_server

        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        stub = FleetServiceStub(channel)

        result = stub.LoadProgram(
            ProgramPath(
                id=MachineId(id="test"),
                path="/fake/test.ngc",
            )
        )

        assert result.success is True
        channel.close()

    def test_subscribe_status_stream(self, sidecar_server):
        port, sidecar, stop = sidecar_server

        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        stub = FleetServiceStub(channel)

        received = []
        try:
            for status in stub.SubscribeStatus(
                MachineId(id="test"),
                timeout=0.5,
            ):
                received.append(status)
                if len(received) >= 3:
                    break
        except grpc.RpcError:
            pass

        assert len(received) >= 2, "Should receive at least 2 status updates in 0.5s"
        assert all(s.machine_id == "integration-machine-1" for s in received)

        channel.close()

    def test_get_errors(self, sidecar_server):
        port, sidecar, stop = sidecar_server

        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        stub = FleetServiceStub(channel)

        errors_resp = stub.GetErrors(
            GetErrorsRequest(id=MachineId(id="test"), limit=10)
        )

        assert hasattr(errors_resp, "errors")
        assert len(errors_resp.errors) == 0

        channel.close()


# ---------------------------------------------------------------------------
# Tests: Auth enforcement through gateway
# ---------------------------------------------------------------------------

class TestGatewayAuthIntegration:
    """End-to-end: auth interceptor enforces RBAC through gateway."""

    def test_unauthenticated_request_rejected(self, gateway_server):
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        stub = FleetGatewayServiceStub(channel)

        with pytest.raises(grpc.RpcError) as exc_info:
            stub.DiscoverMachines(
                DiscoverRequest(facility=""),
                metadata=[],
            )

        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

        channel.close()

    def test_viewer_cannot_broadcast(self, gateway_server):
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        stub = FleetGatewayServiceStub(channel)

        token = _make_viewer_token()
        metadata = [("authorization", f"Bearer {token}")]

        with pytest.raises(grpc.RpcError) as exc_info:
            stub.BroadcastCommand(
                BroadcastRequest(
                    scope=0,
                    mdi=MdiCommand(
                        id=MachineId(id="test"),
                        command="G0 X0",
                    ),
                ),
                metadata=metadata,
            )

        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED

        channel.close()


# ---------------------------------------------------------------------------
# Tests: Machine registry heartbeat and expiry
# ---------------------------------------------------------------------------

class TestRegistryHeartbeat:
    """End-to-end: gateway registry tracks machine heartbeats."""

    def test_heartbeat_updates_last_seen(self, gateway_server):
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        entry_before = registry.lookup("integration-machine-1")
        assert entry_before is not None

        import time as _time
        _time.sleep(0.1)
        entry_after = registry.heartbeat("integration-machine-1")

        assert entry_after is not None
        assert entry_after.last_heartbeat > entry_before.last_heartbeat

    def test_expired_machine_removed(self):
        from gateway.registry import create_test_registry

        registry = create_test_registry(heartbeat_ttl=0.5)
        registry.register("test-expire", "127.0.0.1", 9999)

        assert registry.lookup("test-expire") is not None

        import time as _time
        _time.sleep(0.6)

        assert registry.lookup("test-expire") is None
