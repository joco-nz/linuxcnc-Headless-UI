"""FleetGatewayService implementation — RPC handlers for the central gateway."""

from __future__ import annotations

import logging
import threading
from concurrent import futures
from typing import AsyncGenerator, Generator, Optional

import grpc

from gateway.auth import AuthManager, User, TokenValidationError
from gateway.policies import PolicyEngine, Permission, Role, PolicyResult
from gateway.registry import MachineEntry, MachineRegistry

from linuxcnc_fleet.fleet_pb2 import (
    BroadcastRequest,
    BroadcastResult,
    DiscoverRequest,
    Empty,
    ExecutionCommand,
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
from linuxcnc_fleet.fleet_pb2_grpc import (
    FleetGatewayServiceServicer,
    FleetServiceStub,
    add_FleetGatewayServiceServicer_to_server,
)

log = logging.getLogger(__name__)


class _GrpcClient:
    """Thin wrapper around a gRPC channel to a single machine."""

    def __init__(self, address: str, port: int) -> None:
        self._address = address
        self._port = port
        self._channel: Optional[grpc.Channel] = None

    def connect(self) -> grpc.Channel:
        if self._channel is None:
            target = f"{self._address}:{self._port}"
            self._channel = grpc.insecure_channel(target)
        return self._channel

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None


class GatewayServiceServicer(FleetGatewayServiceServicer):
    """Implements FleetGatewayService RPCs with auth, RBAC, and routing."""

    def __init__(
        self,
        auth_manager: AuthManager,
        policy_engine: PolicyEngine,
        registry: MachineRegistry,
    ) -> None:
        self.auth = auth_manager
        self.policies = policy_engine
        self.registry = registry
        self._client_cache: dict[str, _GrpcClient] = {}
        self._client_lock = threading.Lock()

    def _get_user(self, context: grpc.ServicerContext) -> User:
        """Extract and validate user from gRPC metadata."""
        metadata = dict(context.invocation_metadata())
        try:
            return self.auth.extract_user(metadata)
        except TokenValidationError as e:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, str(e))

    def _check_read_access(
        self, user: User, machine_id: str
    ) -> PolicyResult:
        """Check if user can read status for a specific machine."""
        entry = self.registry.lookup(machine_id)
        if entry is None:
            return PolicyResult(allowed=False, reason=f"Machine {machine_id} not found")

        result = self.policies.can_read_status(user.role)
        if not result.allowed:
            return result

        # Facility scoping check
        if user.facility and entry.facility != user.facility and user.role != Role.admin.value:
            return PolicyResult(
                allowed=False,
                reason=f"Machine {machine_id} is in facility '{entry.facility}', user scope is '{user.facility}'",
            )

        return PolicyResult(allowed=True)

    def _check_control_access(self, user: User, machine_id: str) -> PolicyResult:
        """Check if user can control a specific machine."""
        entry = self.registry.lookup(machine_id)
        if entry is None:
            return PolicyResult(allowed=False, reason=f"Machine {machine_id} not found")

        result = self.policies.can_control_machine(user.role)
        if not result.allowed:
            return result

        # Facility scoping check
        if user.facility and entry.facility != user.facility and user.role != Role.admin.value:
            return PolicyResult(
                allowed=False,
                reason=f"Machine {machine_id} is in facility '{entry.facility}', user scope is '{user.facility}'",
            )

        return PolicyResult(allowed=True)

    def _get_or_create_client(self, entry: MachineEntry) -> _GrpcClient:
        """Get or create a gRPC client for a machine entry."""
        with self._client_lock:
            if entry.id not in self._client_cache:
                self._client_cache[entry.id] = _GrpcClient(entry.address, entry.port)
            return self._client_cache[entry.id]

    def DiscoverMachines(
        self, request: DiscoveryRequest, context: grpc.ServicerContext
    ) -> MachineList:
        """Discover machines visible to the authenticated user."""
        user = self._get_user(context)

        # Check read access permission
        result = self.policies.can_read_status(user.role)
        if not result.allowed:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, result.reason)

        # Resolve facility from request or user claims
        # User's facility claim is the hard boundary; request facility can only narrow scope
        user_facility = getattr(user, 'facility', None)
        request_facility = request.facility if request.facility else None

        # Get all machines and filter by scope (user_facility takes precedence for scoping)
        all_machines = self.registry.list_all()
        filtered = self.policies.filter_machines_by_scope(
            user.role, user_facility, [
                {'id': m.id, 'facility': m.facility, 'tags': m.tags}
                for m in all_machines
            ]
        )

        # Additional filter by request facility if provided (narrowing only)
        if request_facility:
            filtered = [m for m in filtered if m.get('facility') == request_facility]

        # Convert to MachineInfo protobuf messages
        machine_infos = []
        for m_data in filtered:
            mid = m_data['id']
            entry = self.registry.lookup(mid)
            if entry is not None:
                info = MachineInfo(
                    machine_id=entry.id,
                    machine_name=entry.id,
                    host_address=entry.address,
                    version=None,  # Would need to fetch from sidecar
                    num_joints=0,
                    num_hal_components=0,
                )
                machine_infos.append(info)

        return MachineList(machines=machine_infos)

    def RouteMachine(
        self, request: MachineId, context: grpc.ServicerContext
    ) -> GatewayRoute:
        """Route a machine ID to its address:port."""
        user = self._get_user(context)
        machine_id = request.id if hasattr(request, 'id') else str(request)

        # Check control access (routing requires at least read + route permission)
        result = self._check_read_access(user, machine_id)
        if not result.allowed:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, result.reason)

        entry = self.registry.lookup(machine_id)
        if entry is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Machine {machine_id} not found")

        return GatewayRoute(
            instance_address=entry.address,
            instance_port=entry.port,
        )

    def BroadcastCommand(
        self, request: BroadcastRequest, context: grpc.ServicerContext
    ) -> BroadcastResult:
        """Broadcast a command to multiple machines based on scope."""
        user = self._get_user(context)

        # Resolve target machines based on scope
        scope_value = request.scope  # proto3 enum stored as int
        scope_names = {0: "ALL", 1: "FACILITY", 2: "TAG"}
        scope_str = scope_names.get(scope_value, "ALL")
        facility_val = request.facility if request.facility else None
        targets = self.registry.resolve_scope(
            scope_str,
            facility=facility_val,
            tags=list(request.tags) if request.tags else None,
        )

        if not targets:
            return BroadcastResult(results={})

        # Check broadcast authorization
        cmd_type = "unknown"
        if request.HasField('mdi'):
            cmd_type = "mdi"
        elif request.HasField('exec'):
            cmd_type = "execution"
        elif request.HasField('mode'):
            cmd_type = "mode"

        auth_result = self.policies.check_broadcast_authorization(user.role, cmd_type)
        if not auth_result.allowed:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, auth_result.reason)

        # Fan-out to each target machine
        results: dict[str, Result] = {}
        for target in targets:
            target_id = target.id

            # Check per-machine access
            access_result = self._check_control_access(user, target_id)
            if not access_result.allowed:
                results[target_id] = Result(
                    success=False,
                    message=f"Access denied: {access_result.reason}",
                )
                continue

            # Execute command via gRPC to the target machine
            try:
                client = self._get_or_create_client(target)
                channel = client.connect()

                # Build and send the appropriate RPC call
                result = self._execute_broadcast_command(channel, target_id, request)
                results[target_id] = result

            except Exception as e:
                log.exception("Broadcast to %s failed", target_id)
                results[target_id] = Result(
                    success=False,
                    message=f"Connection failed: {e}",
                )

        return BroadcastResult(results=results)

    def _execute_broadcast_command(
        self,
        channel: grpc.Channel,
        machine_id: str,
        request: BroadcastRequest,
    ) -> Result:
        """Execute a single broadcast command against a target machine."""
        from linuxcnc_fleet.fleet_pb2_grpc import FleetServiceStub

        stub = FleetServiceStub(channel)

        try:
            if request.HasField('mdi'):
                return stub.MdiCommand(request.mdi)
            elif request.HasField('exec'):
                return stub.SetExecution(request.exec)
            elif request.HasField('mode'):
                return stub.SetMode(request.mode)
            else:
                return Result(
                    success=False,
                    message="Unknown broadcast command type",
                )

        except grpc.RpcError as e:
            return Result(
                success=False,
                message=f"gRPC error: {e.details()}",
            )

    def SubscribeAllStatus(
        self, request: SubscribeAllRequest, context: grpc.ServicerContext
    ) -> Generator[MachineStatus, None, None]:
        """Stream status updates from all machines in scope."""
        user = self._get_user(context)

        # Check subscribe permission
        result = self.policies.can_subscribe(user.role)
        if not result.allowed:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, result.reason)

        # Resolve target machines
        facility_val = request.facility if request.facility else None
        targets = self.registry.resolve_scope(
            "FACILITY" if facility_val else "ALL",
            facility=facility_val,
        )

        if not targets:
            return

        # Start streaming from each target machine in a background thread pool
        clients: list[tuple[str, _GrpcClient]] = []
        for target in targets:
            entry = self.registry.lookup(target.id)
            if entry is not None:
                client = self._get_or_create_client(entry)
                clients.append((target.id, client))

        # Stream status from each machine using separate threads
        import queue

        streams: dict[str, queue.Queue] = {mid: queue.Queue() for mid, _ in clients}
        stop_event = threading.Event()

        def stream_from_machine(machine_id: str, client: _GrpcClient) -> None:
            try:
                channel = client.connect()
                stub = FleetServiceStub(channel)
                # Subscribe to status updates
                for status in stub.SubscribeStatus(MachineId(id=machine_id)):
                    if stop_event.is_set():
                        break
                    streams[machine_id].put(status)
            except Exception:
                pass

        threads: list[threading.Thread] = []
        for mid, client in clients:
            t = threading.Thread(target=stream_from_machine, args=(mid, client), daemon=True)
            t.start()
            threads.append(t)

        try:
            # Interleave status updates from all machines
            while not stop_event.is_set():
                if not context.is_active():
                    break
                for mid, q in streams.items():
                    try:
                        status = q.get(timeout=0.1)
                        yield status
                    except queue.Empty:
                        continue
        except grpc.RpcError:
            pass
        finally:
            stop_event.set()
            for t in threads:
                t.join(timeout=2.0)


def create_gateway_server(
    auth_manager: AuthManager,
    policy_engine: PolicyEngine,
    registry: MachineRegistry,
    port: int = 50051,
) -> grpc.Server:
    """Create a gRPC server with the gateway service registered."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    servicer = GatewayServiceServicer(auth_manager, policy_engine, registry)
    add_FleetGatewayServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"[::]:{port}")
    return server


def run_gateway_server(
    auth_manager: AuthManager,
    policy_engine: PolicyEngine,
    registry: MachineRegistry,
    port: int = 50051,
) -> None:
    """Create and run the gateway gRPC server (blocking)."""
    import signal

    server = create_gateway_server(auth_manager, policy_engine, registry, port)
    server.start()
    log.info("Gateway server started on port %d", port)

    # Graceful shutdown on SIGINT/SIGTERM
    def shutdown_handler(signum, frame):
        log.info("Shutting down gateway server")
        server.stop(grace=5)
        registry.stop()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        log.info("Gateway server interrupted")
        server.stop(grace=5)
        registry.stop()
