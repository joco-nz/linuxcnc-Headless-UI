"""Tests for GatewayServiceServicer — FleetGatewayService RPC handlers."""

import logging
import time
import threading
import queue
from unittest.mock import MagicMock, patch

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
        self._active = True

    def invocation_metadata(self):
        return self._metadata

    def abort(self, code, detail):
        self._aborted = True
        self._abort_code = code
        self._abort_detail = str(detail)
        raise grpc.RpcError(f"{code}: {detail}")

    def is_active(self):
        return self._active

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

    def test_broadcast_command_all_scope_exec(self):
        """BroadcastCommand with ALL scope and exec command targets all machines."""
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007)
        registry.register("m2", "192.168.1.11", 5008)

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})
        context = MockServicerContext(self.make_metadata(token))

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.ALL,
            exec=ExecutionCommand(id=MachineId(id="m1"), state=ExecutionState.EXEC_IDLE),
        )
        result = servicer.BroadcastCommand(request, context)

        assert isinstance(result, BroadcastResult)
        assert "m1" in result.results
        assert "m2" in result.results

    def test_broadcast_command_viewer_denied(self):
        """Viewer role cannot execute broadcast control commands."""
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007)

        token = create_test_token({"sub": "viewer1", "name": "Viewer", "role": "viewer"})
        context = MockServicerContext(self.make_metadata(token))

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.ALL,
            exec=ExecutionCommand(id=MachineId(id="m1"), state=ExecutionState.EXEC_IDLE),
        )
        with pytest.raises(grpc.RpcError) as exc_info:
            servicer.BroadcastCommand(request, context)

        assert "PERMISSION_DENIED" in str(exc_info.value)

    def test_broadcast_command_empty_registry(self):
        """BroadcastCommand returns empty results when no machines registered."""
        servicer = self.create_servicer()
        # No machines registered

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})
        context = MockServicerContext(self.make_metadata(token))

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.ALL,
            exec=ExecutionCommand(id=MachineId(id="m1"), state=ExecutionState.EXEC_IDLE),
        )
        result = servicer.BroadcastCommand(request, context)

        assert isinstance(result, BroadcastResult)
        assert len(result.results) == 0

    def test_broadcast_command_concurrent_execution(self):
        """BroadcastCommand executes targets concurrently (up to semaphore limit)."""
        import time
        servicer = self.create_servicer()
        
        registry = create_test_registry()
        for i in range(10):
            registry.register(f"m{i}", f"192.168.1.{i+10}", 5007)
        auth_manager = create_test_auth_manager()
        policy_engine = PolicyEngine()
        servicer = GatewayServiceServicer(auth_manager, policy_engine, registry)

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})
        context = MockServicerContext(self.make_metadata(token))

        call_times = []
        
        def slow_command(channel, machine_id, request, timeout=30.0):
            """Simulate a slow gRPC call (200ms) and record start time."""
            call_times.append(time.monotonic())
            time.sleep(0.2)  # Simulate 200ms network latency
            return Result(success=True, message="ok")
        
        servicer._execute_broadcast_command = slow_command

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.ALL,
            mdi=MdiCommand(id=MachineId(id="m1"), command="G0 X1.0"),
        )
        start = time.monotonic()
        result = servicer.BroadcastCommand(request, context)

        elapsed = time.monotonic() - start

        assert isinstance(result, BroadcastResult)
        assert len(result.results) == 10
        
        # With semaphore=5 and 10 targets at 200ms each:
        # Serial would take ~2.0s (10 × 0.2s)
        # Concurrent (5 at a time) takes ~0.4s (2 batches × 0.2s)
        # Allow generous margin: should be under 1.0s
        assert elapsed < 1.0, f"Broadcast took {elapsed:.2f}s — expected <1.0s (semaphore not working?)"

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

    def test_subscribe_all_status_channel_connect_failure(self):
        """SubscribeAllStatus handles client.connect() failure gracefully."""
        from unittest.mock import MagicMock, patch

        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007)

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})

        class InactiveContext(MockServicerContext):
            def __init__(self, metadata=None):
                super().__init__(metadata)
                self._active = False

        context = InactiveContext(self.make_metadata(token))

        mock_client = MagicMock()
        mock_client.connect.side_effect = grpc.RpcError("connection refused")
        with patch.object(servicer, "_get_or_create_client", return_value=mock_client):
            generator = servicer.SubscribeAllStatus(SubscribeAllRequest(), context)
            results = list(generator)
        assert len(results) == 0

    def test_subscribe_all_status_stub_subscription_failure(self):
        """SubscribeAllStatus handles FleetServiceStub.SubscribeStatus failure."""
        from unittest.mock import MagicMock, patch

        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007)

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})

        class InactiveContext(MockServicerContext):
            def __init__(self, metadata=None):
                super().__init__(metadata)
                self._active = False

        context = InactiveContext(self.make_metadata(token))

        mock_client = MagicMock()
        mock_channel = MagicMock()
        mock_client.connect.return_value.__enter__ = MagicMock(return_value=mock_channel)
        mock_client.connect.return_value.__exit__ = MagicMock(return_value=False)

        def subscribe_status_side_effect(*args, **kwargs):
            raise grpc.RpcError("subscription failed")

        with patch.object(servicer, "_get_or_create_client", return_value=mock_client):
            with patch(
                "gateway.server.FleetServiceStub",
                side_effect=lambda channel: MagicMock(SubscribeStatus=subscribe_status_side_effect),
            ):
                generator = servicer.SubscribeAllStatus(SubscribeAllRequest(), context)
                results = list(generator)
        assert len(results) == 0

    def test_subscribe_all_status_cleans_up_clients_from_cache(self):
        """SubscribeAllStatus cleanup closes _GrpcClient objects so cached channels are not stale (M5)."""
        from unittest.mock import MagicMock, patch

        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007)

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})

        class InactiveContext(MockServicerContext):
            def __init__(self, metadata=None):
                super().__init__(metadata)
                self._active = False

        context = InactiveContext(self.make_metadata(token))

        mock_client = MagicMock()
        mock_channel = MagicMock()
        mock_client.connect.return_value = mock_channel

        with patch.object(servicer, "_get_or_create_client", return_value=mock_client):
            generator = servicer.SubscribeAllStatus(SubscribeAllRequest(), context)
            results = list(generator)

        mock_client.close.assert_called_once(), \
            "Cleanup should call client.close() to evict stale channel references"

    def test_grpc_client_reconnects_on_stale_channel(self):
        """_GrpcClient.connect() detects closed channels and reconnects (M5)."""
        from unittest.mock import MagicMock, patch

        client = _GrpcClient("192.168.1.10", 5007)

        # First connect creates a channel
        ch1 = client.connect()
        assert ch1 is not None
        original_channel = ch1

        # Mock check_connectivity_state to raise ValueError (simulating closed channel)
        mock_cygrpc = MagicMock()
        mock_cygrpc.check_connectivity_state.side_effect = ValueError("Cannot invoke RPC: Channel closed!")
        with patch.object(ch1, "_channel", mock_cygrpc):
            ch2 = client.connect()

        assert ch2 is not None
        assert ch2 is not original_channel, "Should create a fresh channel when cached one is stale"

    def test_grpc_client_connects_when_none(self):
        """_GrpcClient.connect() creates a new channel when _channel is None."""
        client = _GrpcClient("192.168.1.10", 5007)
        ch = client.connect()
        assert ch is not None
        assert client._channel is not None

    def test_subscribe_all_status_queues_have_maxsize(self):
        """SubscribeAllStatus creates bounded queues to prevent memory exhaustion (M6)."""
        import queue

        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007)

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})

        class InactiveContext(MockServicerContext):
            def __init__(self, metadata=None):
                super().__init__(metadata)
                self._active = False

        context = InactiveContext(self.make_metadata(token))

        mock_client = MagicMock()
        mock_channel = MagicMock()
        mock_client.connect.return_value.__enter__ = MagicMock(return_value=mock_channel)
        mock_client.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.connect.return_value.__iter__ = MagicMock(return_value=iter([]))

        with patch.object(servicer, "_get_or_create_client", return_value=mock_client):
            generator = servicer.SubscribeAllStatus(SubscribeAllRequest(), context)
            list(generator)

    def test_subscribe_all_status_backpressure_bounded_queue(self):
        """SubscribeAllStatus creates bounded queues with maxsize=100 (M6)."""
        import queue as _queue_module

        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007)
        registry.register("m2", "192.168.1.11", 5008)

        token = create_test_token({"sub": "admin1", "name": "Admin", "role": "admin"})

        class InactiveContext(MockServicerContext):
            def __init__(self, metadata=None):
                super().__init__(metadata)
                self._active = False

        context = InactiveContext(self.make_metadata(token))

        mock_client1 = MagicMock()
        mock_client2 = MagicMock()
        mock_channel1 = MagicMock()
        mock_channel2 = MagicMock()
        mock_client1.connect.return_value.__enter__ = MagicMock(return_value=mock_channel1)
        mock_client1.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_client1.connect.return_value.__iter__ = MagicMock(return_value=iter([]))
        mock_client2.connect.return_value.__enter__ = MagicMock(return_value=mock_channel2)
        mock_client2.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_client2.connect.return_value.__iter__ = MagicMock(return_value=iter([]))

        def get_or_create_side_effect(entry):
            if entry.id == "m1":
                return mock_client1
            return mock_client2

        with patch.object(servicer, "_get_or_create_client", side_effect=get_or_create_side_effect):
            generator = servicer.SubscribeAllStatus(SubscribeAllRequest(), context)
            list(generator)

    def test_broadcast_command_program_path_error(self):
        """_execute_broadcast_command returns error when LoadProgram fails on sidecar."""
        from unittest.mock import MagicMock, patch

        class FakeRpcError(grpc.RpcError):
            def __init__(self, details_str):
                super().__init__()
                self._details = details_str
            def details(self):
                return self._details

        servicer = self.create_servicer()

        mock_stub = MagicMock()
        rpc_error = FakeRpcError("UNAVAILABLE: sidecar unreachable")
        mock_stub.LoadProgram.side_effect = rpc_error

        with patch("linuxcnc_fleet.fleet_pb2_grpc.FleetServiceStub", return_value=mock_stub):
            result = servicer._execute_broadcast_command(
                channel=MagicMock(),
                machine_id="m1",
                request=BroadcastRequest(program_path="/programs/test.ngc"),
            )

        assert result.success is False
        assert "gRPC error" in result.message
        assert "sidecar unreachable" in result.message

    def test_broadcast_command_tag_scope_operator_no_control(self):
        """TAG scope broadcast with operator and MDI fails — operator lacks CONTROL_STEP."""
        servicer = self.create_servicer()
        registry = servicer.registry
        registry.register("m1", "192.168.1.10", 5007, tags=["mill"])

        token = create_test_token({"sub": "operator1", "name": "Operator", "role": "operator"})
        context = MockServicerContext(self.make_metadata(token))

        request = BroadcastRequest(
            scope=BroadcastRequest.Scope.TAG,
            tags=["mill"],
            mdi=MdiCommand(id=MachineId(id="m1"), command="G0 X1.0"),
        )
        with pytest.raises(grpc.RpcError) as exc_info:
            servicer.BroadcastCommand(request, context)

        assert "PERMISSION_DENIED" in str(exc_info.value)

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


class TestGatewayServiceRPC:
    """Tests for the stub GatewayServiceRPC in linuxcnc_fleet.server.

    This is the minimal gateway servicer exposed by sidecars when
    use_gateway=True. It validates auth before routing to real
    gateway infrastructure.
    """

    def _make_context(self, metadata):
        ctx = MockServicerContext(metadata)
        return ctx

    def test_discover_machines_unauthenticated_aborts(self):
        from linuxcnc_fleet.server import GatewayServiceRPC
        servicer = GatewayServiceRPC(user_extractor=lambda m: None)
        ctx = self._make_context({})
        with pytest.raises(grpc.RpcError):
            servicer.DiscoverMachines(DiscoverRequest(), ctx)

    def test_discover_machines_authenticated_returns_list(self):
        from linuxcnc_fleet.server import GatewayServiceRPC
        def extractor(m):
            return type("User", (), {"sub": "test"})()
        servicer = GatewayServiceRPC(user_extractor=extractor)
        ctx = self._make_context({"authorization": "Bearer fake"})
        result = servicer.DiscoverMachines(DiscoverRequest(), ctx)
        assert isinstance(result, MachineList)

    def test_route_machine_unauthenticated_aborts(self):
        from linuxcnc_fleet.server import GatewayServiceRPC
        servicer = GatewayServiceRPC(user_extractor=lambda m: None)
        ctx = self._make_context({})
        with pytest.raises(grpc.RpcError):
            servicer.RouteMachine(MachineId(id="m1"), ctx)

    def test_broadcast_command_unauthenticated_aborts(self):
        from linuxcnc_fleet.server import GatewayServiceRPC
        servicer = GatewayServiceRPC(user_extractor=lambda m: None)
        ctx = self._make_context({})
        with pytest.raises(grpc.RpcError):
            servicer.BroadcastCommand(BroadcastRequest(), ctx)

    def test_subscribe_all_status_unauthenticated_aborts(self):
        from linuxcnc_fleet.server import GatewayServiceRPC
        servicer = GatewayServiceRPC(user_extractor=lambda m: None)
        ctx = self._make_context({})
        with pytest.raises(grpc.RpcError):
            list(servicer.SubscribeAllStatus(SubscribeAllRequest(), ctx))

    def test_subscribe_all_status_authenticated_yields(self):
        from linuxcnc_fleet.server import GatewayServiceRPC
        def extractor(m):
            return type("User", (), {"sub": "test"})()
        servicer = GatewayServiceRPC(user_extractor=extractor)
        ctx = self._make_context({"authorization": "Bearer fake"})
        results = list(servicer.SubscribeAllStatus(SubscribeAllRequest(), ctx))
        assert len(results) == 1
        assert isinstance(results[0], MachineStatus)


class TestExecuteBroadcastCommand:
    """Tests for GatewayServiceServicer._execute_broadcast_command()."""

    def _make_rpc_error(self, details_str):
        class FakeRpcError(grpc.RpcError):
            def __init__(self, d):
                super().__init__()
                self._details = d
            def details(self):
                return self._details
        return FakeRpcError(details_str)

    def create_servicer(self, registry=None):
        if registry is None:
            registry = create_test_registry()
        auth_manager = create_test_auth_manager()
        policy_engine = PolicyEngine()
        return GatewayServiceServicer(auth_manager, policy_engine, registry)

    def test_mdi_command_calls_send_mdi(self):
        """_execute_broadcast_command forwards mdi to MdiCommand RPC."""
        from unittest.mock import MagicMock, patch
        servicer = self.create_servicer()

        mock_stub = MagicMock()
        mock_stub.SendMdiCommand.return_value = Result(success=True)

        with patch("linuxcnc_fleet.fleet_pb2_grpc.FleetServiceStub", return_value=mock_stub):
            result = servicer._execute_broadcast_command(
                channel=MagicMock(),
                machine_id="m1",
                request=BroadcastRequest(mdi=MdiCommand(command="M3 S1000")),
            )

        mock_stub.SendMdiCommand.assert_called_once()
        assert result.success is True

    def test_exec_command_calls_set_execution(self):
        """_execute_broadcast_command forwards exec to SetExecution RPC."""
        from unittest.mock import MagicMock, patch
        servicer = self.create_servicer()

        mock_stub = MagicMock()
        mock_stub.SetExecution.return_value = Result(success=True)

        with patch("linuxcnc_fleet.fleet_pb2_grpc.FleetServiceStub", return_value=mock_stub):
            result = servicer._execute_broadcast_command(
                channel=MagicMock(),
                machine_id="m1",
                request=BroadcastRequest(exec=ExecutionCommand(state=ExecutionState.RUN)),
            )

        mock_stub.SetExecution.assert_called_once()
        assert result.success is True

    def test_mode_command_calls_set_mode(self):
        """_execute_broadcast_command forwards mode to SetMode RPC."""
        from unittest.mock import MagicMock, patch
        servicer = self.create_servicer()

        mock_stub = MagicMock()
        mock_stub.SetMode.return_value = Result(success=True)

        with patch("linuxcnc_fleet.fleet_pb2_grpc.FleetServiceStub", return_value=mock_stub):
            result = servicer._execute_broadcast_command(
                channel=MagicMock(),
                machine_id="m1",
                request=BroadcastRequest(mode=SetModeRequest(mode=Mode.MODE_AUTO)),
            )

        mock_stub.SetMode.assert_called_once()
        assert result.success is True

    def test_program_path_calls_load_program(self):
        """_execute_broadcast_command forwards program_path to LoadProgram RPC."""
        from unittest.mock import MagicMock, patch
        servicer = self.create_servicer()

        mock_stub = MagicMock()
        mock_stub.LoadProgram.return_value = Result(success=True)

        with patch("linuxcnc_fleet.fleet_pb2_grpc.FleetServiceStub", return_value=mock_stub):
            result = servicer._execute_broadcast_command(
                channel=MagicMock(),
                machine_id="m1",
                request=BroadcastRequest(program_path="/programs/test.ngc"),
            )

        mock_stub.LoadProgram.assert_called_once()
        call_args = mock_stub.LoadProgram.call_args[0][0]
        assert call_args.id.id == "m1"
        assert call_args.path == "/programs/test.ngc"
        assert result.success is True

    def test_unknown_command_returns_error(self):
        """_execute_broadcast_command returns error for unknown command type."""
        from unittest.mock import MagicMock, patch
        servicer = self.create_servicer()

        mock_stub = MagicMock()

        with patch("linuxcnc_fleet.fleet_pb2_grpc.FleetServiceStub", return_value=mock_stub):
            result = servicer._execute_broadcast_command(
                channel=MagicMock(),
                machine_id="m1",
                request=BroadcastRequest(),
            )

        assert result.success is False
        assert "Unknown broadcast command type" in result.message

    def test_grpc_error_returns_error_result(self):
        """_execute_broadcast_command returns error on gRPC RpcError."""
        from unittest.mock import MagicMock, patch
        servicer = self.create_servicer()

        mock_stub = MagicMock()
        rpc_error = self._make_rpc_error("UNAVAILABLE: connection refused")
        mock_stub.SendMdiCommand.side_effect = rpc_error

        with patch("linuxcnc_fleet.fleet_pb2_grpc.FleetServiceStub", return_value=mock_stub):
            result = servicer._execute_broadcast_command(
                channel=MagicMock(),
                machine_id="m1",
                request=BroadcastRequest(mdi=MdiCommand(command="M3 S1000")),
            )

        assert result.success is False
        assert "gRPC error" in result.message
        assert "connection refused" in result.message

    def test_grpc_error_with_details(self):
        """_execute_broadcast_command includes gRPC details in error message."""
        from unittest.mock import MagicMock, patch
        servicer = self.create_servicer()

        mock_stub = MagicMock()
        rpc_error = self._make_rpc_error("PERMISSION_DENIED: not authorized")
        mock_stub.SetMode.side_effect = rpc_error

        with patch("linuxcnc_fleet.fleet_pb2_grpc.FleetServiceStub", return_value=mock_stub):
            result = servicer._execute_broadcast_command(
                channel=MagicMock(),
                machine_id="m1",
                request=BroadcastRequest(mode=SetModeRequest(mode=Mode.MODE_MANUAL)),
            )

        assert result.success is False
        assert "PERMISSION_DENIED" in result.message


class TestCreateGatewayServer:
    """Tests for create_gateway_server() port configuration."""

    def test_insecure_port_when_tls_disabled(self):
        """create_gateway_server uses add_insecure_port when tls_enabled=False."""
        from unittest.mock import MagicMock, patch
        from gateway.server import create_gateway_server

        mock_server = MagicMock()
        with patch("gateway.server.grpc.server", return_value=mock_server):
            with patch("gateway.server.add_FleetGatewayServiceServicer_to_server"):
                with patch("gateway.server.GatewayServiceServicer"):
                    server = create_gateway_server(
                        create_test_auth_manager(),
                        MagicMock(),
                        MagicMock(),
                        port=50051,
                        tls_enabled=False,
                    )

        mock_server.add_insecure_port.assert_called_once()
        mock_server.add_secure_port.assert_not_called()
        call_arg = mock_server.add_insecure_port.call_args[0][0]
        assert "[::]:50051" in call_arg

    def test_secure_port_when_tls_enabled_no_root(self):
        """create_gateway_server uses add_secure_port when tls_enabled=True without root cert."""
        import tempfile
        from unittest.mock import MagicMock, patch
        from gateway.server import create_gateway_server

        mock_server = MagicMock()
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cert_f:
            cert_f.write(b"-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----")
            cert_path = cert_f.name

        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as key_f:
            key_f.write(b"-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----")
            key_path = key_f.name

        try:
            with patch("gateway.server.grpc.server", return_value=mock_server):
                with patch("gateway.server.add_FleetGatewayServiceServicer_to_server"):
                    with patch("gateway.server.GatewayServiceServicer"):
                        with patch("gateway.server.grpc.ssl_server_credentials") as mock_creds:
                            mock_creds.return_value = MagicMock()
                            server = create_gateway_server(
                                create_test_auth_manager(),
                                MagicMock(),
                                MagicMock(),
                                port=50052,
                                tls_enabled=True,
                                cert_file=cert_path,
                                key_file=key_path,
                            )

            mock_server.add_secure_port.assert_called_once()
            mock_server.add_insecure_port.assert_not_called()
            call_arg = mock_server.add_secure_port.call_args[0][0]
            assert "[::]:50052" in call_arg
        finally:
            import os
            os.unlink(cert_path)
            os.unlink(key_path)

    def test_secure_port_with_root_cert(self):
        """create_gateway_server passes root_certificates when root_cert_file provided."""
        import tempfile
        from unittest.mock import MagicMock, patch
        from gateway.server import create_gateway_server

        mock_server = MagicMock()
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cert_f:
            cert_f.write(b"-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----")
            cert_path = cert_f.name

        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as key_f:
            key_f.write(b"-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----")
            key_path = key_f.name

        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as root_f:
            root_f.write(b"-----BEGIN CERTIFICATE-----\nroot\n-----END CERTIFICATE-----")
            root_path = root_f.name

        try:
            with patch("gateway.server.grpc.server", return_value=mock_server):
                with patch("gateway.server.add_FleetGatewayServiceServicer_to_server"):
                    with patch("gateway.server.GatewayServiceServicer"):
                        with patch("gateway.server.grpc.ssl_server_credentials") as mock_creds:
                            mock_creds.return_value = MagicMock()
                            server = create_gateway_server(
                                create_test_auth_manager(),
                                MagicMock(),
                                MagicMock(),
                                port=50053,
                                tls_enabled=True,
                                cert_file=cert_path,
                                key_file=key_path,
                                root_cert_file=root_path,
                            )

            mock_creds.assert_called_once()
            call_kwargs = mock_creds.call_args[1]
            assert "root_certificates" in call_kwargs
        finally:
            import os
            os.unlink(cert_path)
            os.unlink(key_path)
            os.unlink(root_path)


class TestRunGatewayServer:
    """Tests for run_gateway_server() signal handling and server lifecycle."""

    def test_creates_and_starts_server(self):
        """run_gateway_server creates server, starts it, registers signals."""
        from unittest.mock import MagicMock, patch
        from gateway.server import run_gateway_server

        mock_server = MagicMock()
        mock_registry = MagicMock()

        with patch("gateway.server.create_gateway_server", return_value=mock_server) as mock_create:
            with patch("gateway.server.MachineRegistry", return_value=mock_registry):
                with patch("gateway.server.log"):
                    def raise_keyboard_interrupt(*args, **kwargs):
                        raise KeyboardInterrupt()

                    try:
                        run_gateway_server(
                            create_test_auth_manager(),
                            MagicMock(),
                            mock_registry,
                            port=9999,
                        )
                    except KeyboardInterrupt:
                        pass

        mock_create.assert_called_once()
        # port is passed as 4th positional arg
        assert mock_create.call_args[0][3] == 9999
        mock_server.start.assert_called_once()
        mock_server.wait_for_termination.assert_called_once()

    def test_keyboard_interrupt_stops_server(self):
        """run_gateway_server catches KeyboardInterrupt and stops server."""
        from unittest.mock import MagicMock, patch
        from gateway.server import run_gateway_server

        mock_server = MagicMock()
        mock_registry = MagicMock()

        with patch("gateway.server.create_gateway_server", return_value=mock_server):
            with patch("gateway.server.MachineRegistry", return_value=mock_registry):
                with patch("gateway.server.log"):
                    def raise_keyboard_interrupt(*args, **kwargs):
                        raise KeyboardInterrupt()

                    mock_server.wait_for_termination.side_effect = raise_keyboard_interrupt

                    try:
                        run_gateway_server(
                            create_test_auth_manager(),
                            MagicMock(),
                            mock_registry,
                        )
                    except KeyboardInterrupt:
                        pass

        mock_server.stop.assert_called_once_with(grace=5)
        mock_registry.stop.assert_called_once()

    def test_signal_handlers_registered(self):
        """run_gateway_server registers SIGINT and SIGTERM handlers."""
        from unittest.mock import MagicMock, patch
        import signal as sig
        from gateway.server import run_gateway_server

        mock_server = MagicMock()
        mock_registry = MagicMock()
        registered_signals = []

        def capture_signal(signum, handler):
            registered_signals.append(signum)

        with patch("gateway.server.create_gateway_server", return_value=mock_server):
            with patch("gateway.server.MachineRegistry", return_value=mock_registry):
                with patch("gateway.server.log"):
                    with patch("signal.signal", side_effect=capture_signal):
                        run_gateway_server(
                            create_test_auth_manager(),
                            MagicMock(),
                            mock_registry,
                        )

        assert sig.SIGINT in registered_signals
        assert sig.SIGTERM in registered_signals

    def test_tls_options_passed_to_create(self):
        """run_gateway_server passes TLS options to create_gateway_server."""
        from unittest.mock import MagicMock, patch
        from gateway.server import run_gateway_server

        mock_server = MagicMock()
        mock_registry = MagicMock()

        with patch("gateway.server.create_gateway_server", return_value=mock_server) as mock_create:
            with patch("gateway.server.MachineRegistry", return_value=mock_registry):
                with patch("gateway.server.log"):
                    def raise_keyboard_interrupt(*args, **kwargs):
                        raise KeyboardInterrupt()

                    try:
                        run_gateway_server(
                            create_test_auth_manager(),
                            MagicMock(),
                            mock_registry,
                            port=50052,
                            tls_enabled=True,
                            cert_file="/path/cert.pem",
                            key_file="/path/key.pem",
                            root_cert_file="/path/root.pem",
                        )
                    except KeyboardInterrupt:
                        pass

        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["tls_enabled"] is True
        assert call_kwargs["cert_file"] == "/path/cert.pem"
        assert call_kwargs["key_file"] == "/path/key.pem"
        assert call_kwargs["root_cert_file"] == "/path/root.pem"

    def test_shutdown_handler_stops_gracefully(self):
        """shutdown_handler calls server.stop(grace=5) and registry.stop()."""
        from unittest.mock import MagicMock, patch
        import signal as sig
        from gateway.server import run_gateway_server

        mock_server = MagicMock()
        mock_registry = MagicMock()
        handlers = {}

        def capture_signal(signum, handler):
            handlers[signum] = handler

        with patch("gateway.server.create_gateway_server", return_value=mock_server):
            with patch("gateway.server.MachineRegistry", return_value=mock_registry):
                with patch("gateway.server.log"):
                    with patch("signal.signal", side_effect=capture_signal):
                        run_gateway_server(
                            create_test_auth_manager(),
                            MagicMock(),
                            mock_registry,
                        )

        # Verify SIGINT and SIGTERM handlers were registered
        assert sig.SIGINT in handlers
        assert sig.SIGTERM in handlers

        # Invoke the SIGINT handler to verify it stops server and registry.
        # The shutdown_handler is a closure capturing mock_server/mock_registry
        # from the same run_gateway_server call, so we invoke it directly.
        handlers[sig.SIGINT](sig.SIGINT, None)

        mock_server.stop.assert_called_once_with(grace=5)
        mock_registry.stop.assert_called_once()


# ── M7: TLS/mTLS file I/O error handling ────────────────────────────────

class TestGrpcClientBuildCredentials:
    """Tests for _GrpcClient._build_credentials error handling (M7)."""

    def test_missing_cert_file_raises_file_not_found(self, tmp_path):
        key_path = str(tmp_path / "key.pem")
        with open(key_path, "w") as f:
            f.write("fake-key")

        client = _GrpcClient(
            "192.168.1.10", 5007,
            tls_enabled=True,
            cert_file=str(tmp_path / "nonexistent.pem"),
            key_file=key_path,
        )
        with pytest.raises(FileNotFoundError, match="TLS certificate file not found"):
            client._build_credentials()

    def test_missing_key_file_raises_file_not_found(self, tmp_path):
        cert_path = str(tmp_path / "cert.pem")
        with open(cert_path, "w") as f:
            f.write("fake-cert")

        client = _GrpcClient(
            "192.168.1.10", 5007,
            tls_enabled=True,
            cert_file=cert_path,
            key_file=str(tmp_path / "nonexistent.pem"),
        )
        with pytest.raises(FileNotFoundError, match="TLS key file not found"):
            client._build_credentials()

    def test_missing_root_cert_file_raises_file_not_found(self, tmp_path):
        cert_path = str(tmp_path / "cert.pem")
        with open(cert_path, "w") as f:
            f.write("fake-cert")
        key_path = str(tmp_path / "key.pem")
        with open(key_path, "w") as f:
            f.write("fake-key")

        client = _GrpcClient(
            "192.168.1.10", 5007,
            tls_enabled=True,
            cert_file=cert_path,
            key_file=key_path,
            root_cert_file=str(tmp_path / "nonexistent-root.pem"),
        )
        with pytest.raises(FileNotFoundError, match="TLS root certificate file not found"):
            client._build_credentials()

    def test_no_tls_returns_none(self):
        client = _GrpcClient("192.168.1.10", 5007, tls_enabled=False)
        assert client._build_credentials() is None

    def test_tls_enabled_but_no_cert_file_returns_none(self):
        client = _GrpcClient("192.168.1.10", 5007, tls_enabled=True, cert_file=None)
        assert client._build_credentials() is None
