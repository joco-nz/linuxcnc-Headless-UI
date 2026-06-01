"""Tests for GatewayServiceServicer — FleetGatewayService RPC handlers."""

import time
import threading
import queue

import grpc
import pytest

from gateway.auth import AuthManager, create_test_auth_manager, create_test_token, User
from gateway.policies import PolicyEngine, Permission, Role, PolicyResult, create_test_policy_engine
from gateway.registry import MachineRegistry, create_test_registry
from gateway.server import GatewayServiceServicer, _GrpcClient, create_gateway_server

from linuxcnc_fleet.fleet_pb2 import (
    BroadcastRequest,
    BroadcastResult,
    DiscoverRequest,
    Empty,
    ExecutionCommand,
    ExecutionState,
    GatewayRoute,
    MachineId,
    MachineInfo,
    MachineList,
    MachineStatus,
    MdiCommand,
    Mode,
    Result,
    SetModeRequest,
    SubscribeAllRequest,
)


class MockServicerContext:
    """Mock gRPC ServicerContext for testing."""

    def __init__(self, metadata=None):
        self._metadata = metadata or {}
        self._aborted = False
        self._abort_code = None
        self._abort_detail = None

    def invocation_metadata(self):
        return self._metadata

    def abort(self, code, detail):
        self._aborted = True
        self._abort_code = code
        self._abort_detail = str(detail)
        raise grpc.RpcError(f"{code}: {detail}")

    def send_initial_metadata(self, *args, **kwargs):
        pass


class TestGrpcClient:
    """Tests for the _GrpcClient wrapper."""

    def test_connect_creates_channel(self):
        client = _GrpcClient("192.168.1.10", 5007)
        try:
            channel = client.connect()
            assert channel is not None
        except Exception:
            pytest.fail("_GrpcClient.connect() raised unexpected exception")

    def test_connect_returns_same_channel(self):
        client = _GrpcClient("192.168.1.10", 5007)
        ch1 = client.connect()
        ch2 = client.connect()
        assert ch1 is ch2

    def test_close_clears_channel(self):
        client = _GrpcClient("192.168.1.10", 5007)
        client.connect()
        client.close()
        assert client._channel is None

    def test_connect_concurrent_threads_same_channel(self):
        import threading

        client = _GrpcClient("192.168.1.10", 5007)
        results: list[grpc.Channel] = []
        lock = threading.Lock()

        def connect_and_collect():
            ch = client.connect()
            with lock:
                results.append(ch)

        threads = [threading.Thread(target=connect_and_collect) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert all(ch is results[0] for ch in results)


class TestGatewayServiceServicer:
    """Tests for GatewayServiceServicer RPC implementations."""

    def create_servicer(self, registry=None, auth_manager=None):
        if registry is None:
            registry = create_test_registry()
        if auth_manager is None:
            auth_manager = create_test_auth_manager()
        policy_engine = PolicyEngine()
        return GatewayServiceServicer(auth_manager, policy_engine, registry)

    def make_metadata(self, token):
        return {"authorization": f"Bearer {token}"}

    # --- DiscoverMachines ---

    def test_discover_machines_authenticated_viewer(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")

        token = create_test_token({"sub": "user1", "name": "User", "role": "viewer", "facility": "shop-1"})
        context = MockServicerContext(self.make_metadata(token))

        request = DiscoverRequest(facility="shop-1")
        result = servicer.DiscoverMachines(request, context)

        assert context._aborted is False
        assert isinstance(result, MachineList)
        assert len(result.machines) == 1

    def test_discover_machines_unauthenticated(self):
        servicer = self.create_servicer()
        context = MockServicerContext({})

        request = DiscoverRequest()
        with pytest.raises(grpc.RpcError):
            servicer.DiscoverMachines(request, context)

    def test_discover_machines_viewer_wrong_facility(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-2")

        token = create_test_token({"sub": "user1", "name": "User", "role": "viewer", "facility": "shop-1"})
        context = MockServicerContext(self.make_metadata(token))

        request = DiscoverRequest(facility="shop-2")
        result = servicer.DiscoverMachines(request, context)

        assert len(result.machines) == 0

    def test_discover_machines_admin_sees_all(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")
        registry.register("m2", "192.168.1.11", 5008, facility="shop-2")

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})
        context = MockServicerContext(self.make_metadata(token))

        request = DiscoverRequest()
        result = servicer.DiscoverMachines(request, context)

        assert len(result.machines) == 2

    def test_discover_machines_no_facility_viewer(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")

        token = create_test_token({"sub": "user1", "name": "User", "role": "viewer"})
        context = MockServicerContext(self.make_metadata(token))

        request = DiscoverRequest()
        result = servicer.DiscoverMachines(request, context)

        assert len(result.machines) == 0

    # --- RouteMachine ---

    def test_route_machine_success(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")

        token = create_test_token({"sub": "user1", "name": "User", "role": "viewer", "facility": "shop-1"})
        context = MockServicerContext(self.make_metadata(token))

        request = MachineId(id="m1")
        result = servicer.RouteMachine(request, context)

        assert isinstance(result, GatewayRoute)
        assert result.instance_address == "192.168.1.10"
        assert result.instance_port == 5007

    def test_route_machine_not_found(self):
        servicer = self.create_servicer()
        token = create_test_token({"sub": "user1", "name": "User", "role": "viewer"})
        context = MockServicerContext(self.make_metadata(token))

        request = MachineId(id="nonexistent")
        with pytest.raises(grpc.RpcError) as exc_info:
            servicer.RouteMachine(request, context)

        assert "NOT_FOUND" in str(exc_info.value) or "PERMISSION_DENIED" in str(exc_info.value)

    def test_route_machine_facility_mismatch(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-2")

        token = create_test_token({"sub": "user1", "name": "User", "role": "viewer", "facility": "shop-1"})
        context = MockServicerContext(self.make_metadata(token))

        request = MachineId(id="m1")
        with pytest.raises(grpc.RpcError) as exc_info:
            servicer.RouteMachine(request, context)

        assert "PERMISSION_DENIED" in str(exc_info.value)

    def test_route_machine_admin_sees_all(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})
        context = MockServicerContext(self.make_metadata(token))

        request = MachineId(id="m1")
        result = servicer.RouteMachine(request, context)

        assert result.instance_address == "192.168.1.10"
        assert result.instance_port == 5007

    # --- BroadcastCommand ---

    def test_broadcast_command_unauthenticated(self):
        servicer = self.create_servicer()
        context = MockServicerContext({})

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.ALL,
            mdi=MdiCommand(id=MachineId(id="m1"), command="G0 X1.0"),
        )
        with pytest.raises(grpc.RpcError):
            servicer.BroadcastCommand(request, context)

    def test_broadcast_command_no_targets(self):
        servicer = self.create_servicer()
        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})
        context = MockServicerContext(self.make_metadata(token))

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.ALL,
            mdi=MdiCommand(id=MachineId(id="m1"), command="G0 X1.0"),
        )
        result = servicer.BroadcastCommand(request, context)

        assert isinstance(result, BroadcastResult)
        assert len(result.results) == 0

    def test_broadcast_command_per_machine_access_denied(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")
        registry.register("m2", "192.168.1.11", 5008, facility="shop-2")

        token = create_test_token({"sub": "user1", "name": "User", "role": "operator", "facility": "shop-1"})
        context = MockServicerContext(self.make_metadata(token))

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.ALL,
            exec=ExecutionCommand(id=MachineId(id="m1"), state=ExecutionState.EXEC_IDLE),
        )
        result = servicer.BroadcastCommand(request, context)

        assert isinstance(result, BroadcastResult)
        assert "m1" in result.results
        assert "m2" in result.results
        # m1: access granted but connection fails (no real sidecar running)
        assert result.results["m1"].success is False
        assert "Connection failed" in result.results["m1"].message or "gRPC error" in result.results["m1"].message
        # m2: access denied due to facility mismatch
        assert result.results["m2"].success is False
        assert "Access denied" in result.results["m2"].message

    def test_broadcast_command_facility_scope(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")
        registry.register("m2", "192.168.1.11", 5008, facility="shop-2")

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})
        context = MockServicerContext(self.make_metadata(token))

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.FACILITY,
            facility="shop-1",
            exec=ExecutionCommand(id=MachineId(id="m1"), state=ExecutionState.EXEC_IDLE),
        )
        result = servicer.BroadcastCommand(request, context)

        assert isinstance(result, BroadcastResult)
        assert "m1" in result.results
        assert "m2" not in result.results

    def test_broadcast_command_tag_scope(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1", tags=["mill"])
        registry.register("m2", "192.168.1.11", 5008, facility="shop-1", tags=["lathe"])

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})
        context = MockServicerContext(self.make_metadata(token))

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.TAG,
            tags=["mill"],
            exec=ExecutionCommand(id=MachineId(id="m1"), state=ExecutionState.EXEC_IDLE),
        )
        result = servicer.BroadcastCommand(request, context)

        assert isinstance(result, BroadcastResult)
        assert "m1" in result.results
        assert "m2" not in result.results

    def test_broadcast_command_viewer_no_control(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007)  # Register a machine so broadcast doesn't return early

        token = create_test_token({"sub": "viewer1", "name": "Viewer", "role": "viewer"})
        context = MockServicerContext(self.make_metadata(token))

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.ALL,
            exec=ExecutionCommand(id=MachineId(id="m1"), state=ExecutionState.EXEC_IDLE),
        )
        with pytest.raises(grpc.RpcError) as exc_info:
            servicer.BroadcastCommand(request, context)

        assert "PERMISSION_DENIED" in str(exc_info.value)

    def test_broadcast_command_mdi_type(self):
        servicer = self.create_servicer()
        token = create_test_token({"sub": "programmer1", "name": "Programmer", "role": "programmer"})
        context = MockServicerContext(self.make_metadata(token))

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.ALL,
            mdi=MdiCommand(id=MachineId(id="m1"), command="G0 X1.0"),
        )
        result = servicer.BroadcastCommand(request, context)
        assert isinstance(result, BroadcastResult)

    def test_broadcast_command_mode_type(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007)

        token = create_test_token({"sub": "operator1", "name": "Operator", "role": "operator"})
        context = MockServicerContext(self.make_metadata(token))

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.TAG,
            tags=[],
            mode=SetModeRequest(id=MachineId(id="m1"), mode=Mode.MODE_MDA),
        )
        result = servicer.BroadcastCommand(request, context)
        assert isinstance(result, BroadcastResult)

    # --- SubscribeAllStatus ---

    def test_subscribe_all_status_unauthenticated(self):
        servicer = self.create_servicer()
        context = MockServicerContext({})

        request = SubscribeAllRequest()
        with pytest.raises(grpc.RpcError):
            list(servicer.SubscribeAllStatus(request, context))

    def test_subscribe_all_status_viewer_has_permission(self):
        servicer = self.create_servicer()
        token = create_test_token({"sub": "viewer1", "name": "Viewer", "role": "viewer"})
        context = MockServicerContext(self.make_metadata(token))

        request = SubscribeAllRequest()
        generator = servicer.SubscribeAllStatus(request, context)
        assert generator is not None

    def test_subscribe_all_status_no_targets(self):
        servicer = self.create_servicer()
        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})
        context = MockServicerContext(self.make_metadata(token))

        request = SubscribeAllRequest()
        generator = servicer.SubscribeAllStatus(request, context)
        results = list(generator)
        assert len(results) == 0

    def test_subscribe_all_status_facility_scope(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")
        registry.register("m2", "192.168.1.11", 5008, facility="shop-2")

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})
        context = MockServicerContext(self.make_metadata(token))

        request = SubscribeAllRequest()
        generator = servicer.SubscribeAllStatus(request, context)
        assert generator is not None

    # --- _check_read_access ---

    def test_check_read_access_success(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")

        user = User(sub="user1", name="User", role="viewer", facility="shop-1")
        result = servicer._check_read_access(user, "m1")

        assert result.allowed is True

    def test_check_read_access_machine_not_found(self):
        servicer = self.create_servicer()
        user = User(sub="user1", name="User", role="viewer", facility="shop-1")
        result = servicer._check_read_access(user, "nonexistent")

        assert result.allowed is False
        assert "not found" in result.reason

    def test_check_read_access_facility_mismatch(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-2")

        user = User(sub="user1", name="User", role="viewer", facility="shop-1")
        result = servicer._check_read_access(user, "m1")

        assert result.allowed is False
        assert "facility" in result.reason.lower()

    def test_check_read_access_admin_bypasses_facility(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-2")

        user = User(sub="admin1", name="Admin", role="admin")
        result = servicer._check_read_access(user, "m1")

        assert result.allowed is True

    # --- _check_control_access ---

    def test_check_control_access_success(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")

        user = User(sub="user1", name="User", role="operator", facility="shop-1")
        result = servicer._check_control_access(user, "m1")

        assert result.allowed is True

    def test_check_control_access_viewer_denied(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")

        user = User(sub="user1", name="User", role="viewer", facility="shop-1")
        result = servicer._check_control_access(user, "m1")

        assert result.allowed is False

    def test_check_control_access_facility_mismatch(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-2")

        user = User(sub="user1", name="User", role="operator", facility="shop-1")
        result = servicer._check_control_access(user, "m1")

        assert result.allowed is False

    def test_check_control_access_admin_bypasses_facility(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, facility="shop-2")

        user = User(sub="admin1", name="Admin", role="admin")
        result = servicer._check_control_access(user, "m1")

        assert result.allowed is True

    # --- _get_or_create_client ---

    def test_get_or_create_client_caches(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        entry = registry.register("m1", "192.168.1.10", 5007)

        client1 = servicer._get_or_create_client(entry)
        client2 = servicer._get_or_create_client(entry)

        assert client1 is client2

    def test_get_or_create_client_different_entries(self):
        servicer = self.create_servicer()
        registry = servicer.registry
        entry1 = registry.register("m1", "192.168.1.10", 5007)
        entry2 = registry.register("m2", "192.168.1.11", 5008)

        client1 = servicer._get_or_create_client(entry1)
        client2 = servicer._get_or_create_client(entry2)

        assert client1 is not client2

    # --- create_gateway_server ---

    def test_create_gateway_server(self):
        auth = create_test_auth_manager()
        policy_engine = PolicyEngine()
        registry = create_test_registry()

        server = create_gateway_server(auth, policy_engine, registry, port=50051)

        assert server is not None
        server.stop(grace=0)
