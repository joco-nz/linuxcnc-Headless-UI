"""FleetGatewayService implementation — RPC handlers for the central gateway."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent import futures
from typing import Any, AsyncGenerator, Generator, Optional

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
    ProgramPath,
    RegisterRequest,
    RegisterResponse,
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

    def __init__(
        self,
        address: str,
        port: int,
        tls_enabled: bool = False,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        root_cert_file: Optional[str] = None,
    ) -> None:
        self._address = address
        self._port = port
        self._tls_enabled = tls_enabled
        self._cert_file = cert_file
        self._key_file = key_file
        self._root_cert_file = root_cert_file
        self._channel: Optional[grpc.Channel] = None
        self._connect_lock = threading.Lock()

    def _build_credentials(self) -> grpc.ChannelCredentials:
        if not self._tls_enabled or not self._cert_file:
            return None
        try:
            with open(self._cert_file, "rb") as f:
                cert = f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"TLS certificate file not found: {self._cert_file}") from None
        except PermissionError:
            raise PermissionError(f"Permission denied reading TLS certificate: {self._cert_file}") from None

        try:
            with open(self._key_file, "rb") as f:
                private_key = f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"TLS key file not found: {self._key_file}") from None
        except PermissionError:
            raise PermissionError(f"Permission denied reading TLS key: {self._key_file}") from None

        if self._root_cert_file:
            try:
                with open(self._root_cert_file, "rb") as f:
                    root_certs = f.read()
            except FileNotFoundError:
                raise FileNotFoundError(f"TLS root certificate file not found: {self._root_cert_file}") from None
            except PermissionError:
                raise PermissionError(f"Permission denied reading TLS root certificate: {self._root_cert_file}") from None
            return grpc.ssl_channel_credentials(root_certs, private_key, cert)
        return grpc.ssl_channel_credentials(None, private_key, cert)

    def connect(self) -> grpc.Channel:
        with self._connect_lock:
            if self._channel is None:
                target = f"{self._address}:{self._port}"
                creds = self._build_credentials()
                if creds:
                    self._channel = grpc.secure_channel(target, creds)
                else:
                    self._channel = grpc.insecure_channel(target)
            elif self._channel is not None:
                try:
                    self._channel._channel.check_connectivity_state(False)
                except ValueError:
                    target = f"{self._address}:{self._port}"
                    creds = self._build_credentials()
                    if creds:
                        self._channel = grpc.secure_channel(target, creds)
                    else:
                        self._channel = grpc.insecure_channel(target)
            return self._channel

    def close(self) -> None:
        with self._connect_lock:
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
        tls_enabled: bool = False,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        root_cert_file: Optional[str] = None,
    ) -> None:
        self.auth = auth_manager
        self.policies = policy_engine
        self.registry = registry
        self._client_cache: dict[str, _GrpcClient] = {}
        self._client_lock = threading.Lock()
        # TODO: Make broadcast concurrency configurable (M3 — hardcoded values).
        # Default 5 is reasonable for most fleets but should be tunable via
        # constructor / env var / CLI arg to match fleet size and network conditions.
        self._broadcast_max_workers = 5
        self._tls_enabled = tls_enabled
        self._cert_file = cert_file
        self._key_file = key_file
        self._root_cert_file = root_cert_file

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
                self._client_cache[entry.id] = _GrpcClient(
                    entry.address, entry.port,
                    tls_enabled=self._tls_enabled,
                    cert_file=self._cert_file,
                    key_file=self._key_file,
                    root_cert_file=self._root_cert_file,
                )
            return self._client_cache[entry.id]

    def DiscoverMachines(
        self, request: DiscoverRequest, context: grpc.ServicerContext
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
        elif request.program_path:
            cmd_type = "program"

        auth_result = self.policies.check_broadcast_authorization(user.role, cmd_type)
        if not auth_result.allowed:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, auth_result.reason)

        # Fan-out to each target machine concurrently (up to 5 at a time)
        results: dict[str, Result] = {}
        
        def _broadcast_one(target):
            """Execute a single broadcast command for one target machine."""
            target_id = target.id

            # Check per-machine access
            access_result = self._check_control_access(user, target_id)
            if not access_result.allowed:
                return target_id, Result(
                    success=False,
                    message=f"Access denied: {access_result.reason}",
                )

            # Execute command via gRPC to the target machine
            try:
                client = self._get_or_create_client(target)
                channel = client.connect()

                # Build and send the appropriate RPC call with timeout
                result = self._execute_broadcast_command(
                    channel, target_id, request, timeout=30.0
                )
                return target_id, result

            except Exception as e:
                log.exception("Broadcast to %s failed", target_id)
                return target_id, Result(
                    success=False,
                    message=f"Connection failed: {e}",
                )

        # Use ThreadPoolExecutor for concurrent fan-out (limited by semaphore)
        with futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_target = {executor.submit(_broadcast_one, target): target for target in targets}
            for future in futures.as_completed(future_to_target):
                target_id, result = future.result()
                results[target_id] = result

        return BroadcastResult(results=results)

    def _execute_broadcast_command(
        self,
        channel: grpc.Channel,
        machine_id: str,
        request: BroadcastRequest,
        timeout: float = 30.0,
    ) -> Result:
        """Execute a single broadcast command against a target machine."""
        from linuxcnc_fleet.fleet_pb2_grpc import FleetServiceStub

        stub = FleetServiceStub(channel)

        try:
            if request.HasField('mdi'):
                return stub.SendMdiCommand(request.mdi, timeout=timeout)
            elif request.HasField('exec'):
                return stub.SetExecution(request.exec, timeout=timeout)
            elif request.HasField('mode'):
                return stub.SetMode(request.mode, timeout=timeout)
            elif request.program_path:
                return stub.LoadProgram(
                    ProgramPath(id=MachineId(id=machine_id), path=request.program_path),
                    timeout=timeout,
                )
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

        _MAX_QUEUE_SIZE = 100

        streams: dict[str, queue.Queue] = {mid: queue.Queue(maxsize=_MAX_QUEUE_SIZE) for mid, _ in clients}
        stop_event = threading.Event()
        channels_to_close: list[grpc.Channel] = []
        channel_lock = threading.Lock()

        def stream_from_machine(machine_id: str, client: _GrpcClient) -> None:
            try:
                channel = client.connect()
                with channel_lock:
                    channels_to_close.append(channel)
                stub = FleetServiceStub(channel)
                # Subscribe to status updates
                for status in stub.SubscribeStatus(MachineId(id=machine_id)):
                    if stop_event.is_set():
                        break
                    try:
                        streams[machine_id].put_nowait(status)
                    except queue.Full:
                        log.warning(
                            "Queue full for %s — dropping oldest status update (maxsize=%d)",
                            machine_id, _MAX_QUEUE_SIZE,
                        )
                        try:
                            streams[machine_id].get_nowait()
                        except queue.Empty:
                            pass
                        streams[machine_id].put_nowait(status)
            except Exception:
                log.exception("Stream from %s failed", machine_id)

        threads: list[threading.Thread] = []
        for mid, client in clients:
            t = threading.Thread(target=stream_from_machine, args=(mid, client), daemon=True)
            t.start()
            threads.append(t)

        def _cleanup() -> None:
            stop_event.set()
            # Drain queues to unblock any put_nowait callers
            for q in streams.values():
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
            # Wait for threads to finish
            for t in threads:
                t.join(timeout=2.0)
            # Close gRPC channels and evict clients from cache
            with channel_lock:
                for ch in channels_to_close:
                    try:
                        ch.close()
                    except Exception:
                        pass
            # Close _GrpcClient objects so connect() detects stale channels (M5)
            for mid, client in clients:
                try:
                    client.close()
                except Exception:
                    pass

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
            _cleanup()

    def RegisterMachine(
        self, request: RegisterRequest, context: grpc.ServicerContext
    ) -> RegisterResponse:
        """Register a sidecar machine with the gateway."""
        user = self._get_user(context)

        # Only admin can register machines
        if user.role != "admin":
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "Only admin can register machines")

        entry = self.registry.register(
            machine_id=request.machine_id,
            address=request.address,
            port=request.port,
            facility=request.facility or "",
            tags=list(request.tags) if request.tags else [],
            version=request.version or "",
        )
        log.info("Machine registered: %s at %s:%d", entry.id, entry.address, entry.port)
        return RegisterResponse(success=True, message=f"Registered {entry.id}")


class TokenIssuanceServicer:
    """Handles HTTP token issuance requests for the gateway."""

    def __init__(
        self,
        auth_manager: AuthManager,
        policy_engine: PolicyEngine,
        allowed_roles: list[str] = None,
        allowed_subjects: list[str] = None,
        allowed_ips: list[str] = None,
        token_ttl: int = 900,
        allow_admin_token: bool = False,
        permissive: bool = False,
    ) -> None:
        self.auth_manager = auth_manager
        self.policy_engine = policy_engine
        self.allowed_roles = allowed_roles or ["viewer", "operator"]
        self.allowed_subjects = set(allowed_subjects or ["fleet-ui"])
        self.allowed_ips = set(allowed_ips or ["127.0.0.1", "::1"])
        self.token_ttl = token_ttl
        self.allow_admin_token = allow_admin_token
        self.permissive = permissive

    def _check_security(self, client_ip: str, sub: str) -> tuple[bool, str]:
        """Check if the request passes security model (AND or OR mode)."""
        ip_ok = client_ip in self.allowed_ips
        subject_ok = sub in self.allowed_subjects

        if self.permissive:
            # OR mode: either IP or subject match is sufficient
            if not ip_ok and not subject_ok:
                return False, "Request rejected: source IP not allowed and subject not pre-registered"
        else:
            # AND mode (default): both must pass
            if not ip_ok:
                return False, "Request rejected: source IP not in allowed list"
            if not subject_ok:
                return False, "Request rejected: subject not pre-registered"

        return True, ""

    def _validate_role(self, role: str) -> tuple[bool, str]:
        """Validate that the requested role is allowed."""
        if role not in self.allowed_roles:
            # Admin requires explicit flag
            if role == "admin" and not self.allow_admin_token:
                return False, "Admin tokens require --allow-admin-token flag"
            return False, f"Role '{role}' not in allowed roles: {', '.join(self.allowed_roles)}"
        return True, ""

    def issue_token(
        self, role: str = "viewer", sub: str = "fleet-ui", client_ip: str = "127.0.0.1"
    ) -> dict[str, Any]:
        """Issue a JWT token after validation."""
        import jwt as pyjwt

        # Validate security model
        security_ok, reason = self._check_security(client_ip, sub)
        if not security_ok:
            raise TokenValidationError(reason, error_code=403)

        # Validate role
        role_ok, reason = self._validate_role(role)
        if not role_ok:
            raise TokenValidationError(reason, error_code=403)

        # Issue token
        now = int(time.time())
        payload = {
            "iss": self.auth_manager.issuer,
            "aud": self.auth_manager.audience,
            "sub": sub,
            "role": role,
            "iat": now,
            "exp": now + self.token_ttl,
        }

        # Use the first symmetric key for signing
        if hasattr(self.auth_manager, 'secret_key') and self.auth_manager.secret_key:
            key = self.auth_manager.secret_key
        elif self.auth_manager._symmetric_keys:
            key = next(iter(self.auth_manager._symmetric_keys.values()))
        else:
            raise TokenValidationError("No signing key available")

        token = pyjwt.encode(payload, key, algorithm="HS256")
        return {"token": token, "expires_in": self.token_ttl}


def create_gateway_server(
    auth_manager: AuthManager,
    policy_engine: PolicyEngine,
    registry: MachineRegistry,
    port: int = 50051,
    tls_enabled: bool = False,
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    root_cert_file: Optional[str] = None,
) -> grpc.Server:
    """Create a gRPC server with the gateway service registered."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    servicer = GatewayServiceServicer(
        auth_manager, policy_engine, registry,
        tls_enabled=tls_enabled,
        cert_file=cert_file,
        key_file=key_file,
        root_cert_file=root_cert_file,
    )
    add_FleetGatewayServiceServicer_to_server(servicer, server)
    if tls_enabled and cert_file and key_file:
        with open(cert_file, "rb") as f:
            cert = f.read()
        with open(key_file, "rb") as f:
            private_key = f.read()
        if root_cert_file:
            with open(root_cert_file, "rb") as f:
                root_certs = f.read()
            creds = grpc.ssl_server_credentials(
                [(private_key, cert)],
                root_certificates=root_certs,
                require_client_auth=True,
            )
        else:
            creds = grpc.ssl_server_credentials([(private_key, cert)])
        server.add_secure_port(f"[::]:{port}", creds)
    else:
        server.add_insecure_port(f"[::]:{port}")
    return server


def run_gateway_server(
    auth_manager: AuthManager,
    policy_engine: PolicyEngine,
    registry: MachineRegistry,
    port: int = 50051,
    tls_enabled: bool = False,
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    root_cert_file: Optional[str] = None,
    http_port: Optional[int] = None,
    allowed_roles: list[str] = None,
    allowed_subjects: list[str] = None,
    allowed_ips: list[str] = None,
    token_ttl: int = 900,
    allow_admin_token: bool = False,
    permissive: bool = False,
) -> None:
    """Create and run the gateway gRPC server (blocking).

    If http_port is provided, also starts an HTTP token issuance server.
    """
    import signal

    server = create_gateway_server(
        auth_manager, policy_engine, registry, port,
        tls_enabled=tls_enabled,
        cert_file=cert_file,
        key_file=key_file,
        root_cert_file=root_cert_file,
    )
    server.start()
    log.info("Gateway server started on port %d", port)

    http_runner: Optional[Any] = None

    if http_port:
        token_servicer = TokenIssuanceServicer(
            auth_manager=auth_manager,
            policy_engine=policy_engine,
            allowed_roles=allowed_roles,
            allowed_subjects=allowed_subjects,
            allowed_ips=allowed_ips,
            token_ttl=token_ttl,
            allow_admin_token=allow_admin_token,
            permissive=permissive,
        )

        async def _start_http():
            nonlocal http_runner
            from gateway import metrics as gateway_metrics

            runner = aiohttp.web.AppRunner(aiohttp.web.Application())
            runner.app["token_servicer"] = token_servicer
            runner.app["registry"] = registry

            @runner.app.on_startup.register
            async def _startup(app):
                site = aiohttp.web.TCPSite(runner, "0.0.0.0", http_port)
                await site.start()
                app["http_site"] = site
                log.info("HTTP token server started on port %d", http_port)

            @runner.app.on_shutdown.register
            async def _shutdown(app):
                site = app.get("http_site")
                if site:
                    await site.stop()
                log.info("HTTP token server stopped")

            async def handle_health(request: aiohttp.web.Request) -> aiohttp.web.Response:
                data = gateway_metrics.handle_health(registry)
                return aiohttp.web.json_response(data)

            async def handle_metrics(request: aiohttp.web.Request) -> aiohttp.web.Response:
                text = gateway_metrics.handle_metrics()
                return aiohttp.web.Response(text=text, content_type="text/plain; version=0.0.4", charset="utf-8")

            runner.app.router.add_get("/health", handle_health)
            runner.app.router.add_get("/metrics", handle_metrics)
            runner.app.router.add_post(
                "/api/auth/token",
                _handle_auth_token_wrapper(token_servicer),
            )
            await runner.setup()
            http_runner = runner

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_start_http())

    def shutdown_handler(signum, frame):
        log.info("Shutting down gateway server")
        server.stop(grace=5)
        registry.stop()
        if http_runner:
            try:
                l = asyncio.new_event_loop()
                l.run_until_complete(http_runner.cleanup())
                l.close()
            except Exception:
                pass

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        log.info("Gateway server interrupted")
        server.stop(grace=5)
        registry.stop()
        if http_runner:
            try:
                l = asyncio.new_event_loop()
                l.run_until_complete(http_runner.cleanup())
                l.close()
            except Exception:
                pass


# ── HTTP Token Issuance Server ────────────────────────────────────────────────


async def _handle_auth_token(request: "web.Request") -> "web.Response":
    """Handle POST /api/auth/token requests."""
    import aiohttp

    servicer: TokenIssuanceServicer = request.app["token_servicer"]
    client_ip = request.remote or "0.0.0.0"

    # Parse query parameters
    role = request.query.get("role", "viewer")
    sub = request.query.get("sub", "fleet-ui")

    try:
        result = servicer.issue_token(role=role, sub=sub, client_ip=client_ip)
        return aiohttp.web.json_response(result)
    except TokenValidationError as e:
        return aiohttp.web.json_response(
            {"error": str(e), "error_code": e.error_code}, status=e.error_code
        )



