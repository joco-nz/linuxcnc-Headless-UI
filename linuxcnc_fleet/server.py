"""gRPC server — maps FleetService RPCs to LinuxCncSidecar methods."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent import futures
from typing import Any, AsyncGenerator, Generator, Optional

import grpc

from linuxcnc_fleet.fleet_pb2 import (
    BroadcastRequest,
    BroadcastResult,
    Empty,
    ErrorEvent,
    ErrorCode,
    ErrorList,
    ExecutionCommand,
    ExecutionState,
    GatewayRoute,
    GetErrorsRequest,
    HalComponentList,
    HalPinSubscribe,
    HalPinType,
    HalPinUpdate,
    DiscoverRequest,
    HalPinRead,
    HalPinValue,
    HalPinWrite,
    HomeAxisRequest,
    IniParamRequest,
    IniParamValue,
    ListHalRequest,
    ListProgramsRequest,
    MachineControlState,
    MachineId,
    MachineInfo,
    MachineList,
    MachineStateCommand,
    MachineStatus,
    MdiCommand,
    Mode,
    PositionRequest,
    PositionResponse,
    ProgramEntry,
    ProgramList,
    ProgramPath,
    Result,
    SetModeRequest,
    SubscribeAllRequest,
    TrajAxis,
)
from linuxcnc_fleet.fleet_pb2_grpc import (
    FleetGatewayServiceServicer,
    FleetServiceServicer,
    add_FleetGatewayServiceServicer_to_server,
    add_FleetServiceServicer_to_server,
)

from linuxcnc_fleet.headless import LinuxCncSidecar

log = logging.getLogger(__name__)


class FleetServiceRPC(FleetServiceServicer):
    """Maps FleetService RPCs to LinuxCncSidecar methods."""

    def __init__(self, sidecar: LinuxCncSidecar, auth_interceptor=None) -> None:
        self.sidecar = sidecar
        self.auth_interceptor = auth_interceptor
        self.role_hierarchy = {
            "viewer": 0,
            "operator": 1,
            "programmer": 2,
            "maintainer": 3,
            "admin": 4,
        }

    def _get_auth_context(self, request: Any, context: grpc.ServicerContext) -> Any:
        """Extract auth context from gRPC request or context."""
        if hasattr(request, 'auth_context') and not callable(getattr(request, 'auth_context', None)):
            return request.auth_context
        return None

    def _check_read_access(self, request: Any, context: grpc.ServicerContext, method_name: str) -> bool:
        """Check if caller has read access. Returns True if allowed."""
        auth_ctx = self._get_auth_context(request, context)
        if auth_ctx is None:
            return True  # No auth configured — allow all
        
        user_level = self.role_hierarchy.get(auth_ctx.role)
        if user_level is None:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Unknown role '{auth_ctx.role}'")
            return False
        if user_level < 0:  # viewer or higher required
            context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Role '{auth_ctx.role}' insufficient for read operations")
            return False
        return True

    def _check_control_access(self, request: Any, context: grpc.ServicerContext, method_name: str) -> bool:
        """Check if caller has control access. Returns True if allowed."""
        auth_ctx = self._get_auth_context(request, context)
        if auth_ctx is None:
            return True  # No auth configured — allow all
        
        user_level = self.role_hierarchy.get(auth_ctx.role)
        if user_level is None:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Unknown role '{auth_ctx.role}'")
            return False
        if user_level < 1:  # operator or higher required
            context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Role '{auth_ctx.role}' insufficient for control operations")
            return False
        return True

    def _check_write_access(self, request: Any, context: grpc.ServicerContext, method_name: str) -> bool:
        """Check if caller has write access. Returns True if allowed."""
        auth_ctx = self._get_auth_context(request, context)
        if auth_ctx is None:
            return True  # No auth configured — allow all
        
        user_level = self.role_hierarchy.get(auth_ctx.role)
        if user_level is None:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Unknown role '{auth_ctx.role}'")
            return False
        if user_level < 2:  # programmer or higher required
            context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Role '{auth_ctx.role}' insufficient for write operations")
            return False
        return True

    def _check_admin_access(self, request: Any, context: grpc.ServicerContext, method_name: str) -> bool:
        """Check if caller has admin access. Returns True if allowed."""
        auth_ctx = self._get_auth_context(request, context)
        if auth_ctx is None:
            return True  # No auth configured — allow all
        
        user_level = self.role_hierarchy.get(auth_ctx.role)
        if user_level is None:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Unknown role '{auth_ctx.role}'")
            return False
        if user_level < 4:  # admin required
            context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Role '{auth_ctx.role}' insufficient for admin operations")
            return False
        return True

    # -- Status queries --

    def GetStatus(self, request: MachineId, context: grpc.ServicerContext) -> MachineStatus:
        return self.sidecar.get_status()

    def SubscribeStatus(
        self, request: MachineId, context: grpc.ServicerContext
    ) -> Generator[MachineStatus, None, None]:
        import time as _time
        while True:
            if not context.is_active():
                return
            yield self.sidecar.get_status()
            _time.sleep(0.02)

    # -- Control commands --

    def SetMode(self, request: SetModeRequest, context: grpc.ServicerContext) -> Result:
        if not self._check_control_access(request, context, "SetMode"):
            return Result(success=False, message="Access denied")
        return self.sidecar.set_mode(request.mode)

    def SetExecution(self, request: ExecutionCommand, context: grpc.ServicerContext) -> Result:
        if not self._check_control_access(request, context, "SetExecution"):
            return Result(success=False, message="Access denied")
        return self.sidecar.set_execution(request.state)

    def SetMachineState(self, request: MachineStateCommand, context: grpc.ServicerContext) -> Result:
        if not self._check_admin_access(request, context, "SetMachineState"):
            return Result(success=False, message="Access denied")
        return self.sidecar.set_machine_state(request.state)

    def Start(self, request: Empty, context: grpc.ServicerContext) -> Result:
        if not self._check_control_access(request, context, "Start"):
            return Result(success=False, message="Access denied")
        return self.sidecar.start()

    def Stop(self, request: Empty, context: grpc.ServicerContext) -> Result:
        if not self._check_control_access(request, context, "Stop"):
            return Result(success=False, message="Access denied")
        return self.sidecar.stop()

    def FeedHold(self, request: Empty, context: grpc.ServicerContext) -> Result:
        if not self._check_control_access(request, context, "FeedHold"):
            return Result(success=False, message="Access denied")
        return self.sidecar.feed_hold()

    def Continue(self, request: Empty, context: grpc.ServicerContext) -> Result:
        if not self._check_control_access(request, context, "Continue"):
            return Result(success=False, message="Access denied")
        return self.sidecar.continue_exec()

    def HomeAll(self, request: Empty, context: grpc.ServicerContext) -> Result:
        if not self._check_control_access(request, context, "HomeAll"):
            return Result(success=False, message="Access denied")
        return self.sidecar.home_all()

    def HomeAxis(self, request: HomeAxisRequest, context: grpc.ServicerContext) -> Result:
        if not self._check_control_access(request, context, "HomeAxis"):
            return Result(success=False, message="Access denied")
        return self.sidecar.home_axis(request.axis)

    # -- G-code / MDI --

    def SendMdiCommand(self, request: MdiCommand, context: grpc.ServicerContext) -> Result:
        if not self._check_write_access(request, context, "SendMdiCommand"):
            return Result(success=False, message="Access denied")
        return self.sidecar.send_mdi(request.command)

    def LoadProgram(self, request: ProgramPath, context: grpc.ServicerContext) -> Result:
        if not self._check_write_access(request, context, "LoadProgram"):
            return Result(success=False, message="Access denied")
        return self.sidecar.load_program(request.path)

    def ListPrograms(
        self, request: ListProgramsRequest, context: grpc.ServicerContext
    ) -> ProgramList:
        if not self._check_read_access(request, context, "ListPrograms"):
            return ProgramList()
        try:
            programs = self.sidecar.list_programs(request.directory, request.max_depth)
            entries = [
                ProgramEntry(
                    path=p["path"], name=p["name"],
                    size_bytes=p["size_bytes"], modified_time=p["modified_time"],
                )
                for p in programs
            ]
            return ProgramList(
                programs=entries, directory=request.directory or "",
                total_count=len(entries),
            )
        except Exception as e:
            log.error("ListPrograms RPC failed: %s", e)
            return ProgramList()

    def StepForward(self, request: Empty, context: grpc.ServicerContext) -> Result:
        if not self._check_control_access(request, context, "StepForward"):
            return Result(success=False, message="Access denied")
        return self.sidecar.step_forward()

    # -- Position --

    def GetPosition(
        self, request: PositionRequest, context: grpc.ServicerContext
    ) -> PositionResponse:
        status = self.sidecar.get_status()
        if request.type == PositionRequest.JOINT:
            pos = status.joint_commanded
        elif request.type == PositionRequest.DEVICE:
            pos = status.joint_actual
        else:
            pos = status.world_actual
        return PositionResponse(id=request.id, position=pos)

    # -- HAL operations --

    def ListHalComponents(
        self, request: ListHalRequest, context: grpc.ServicerContext
    ) -> HalComponentList:
        return self.sidecar.list_hal_components()

    def ReadHalPin(self, request: HalPinRead, context: grpc.ServicerContext) -> HalPinValue:
        try:
            return self.sidecar.read_hal_pin(request.pin_name)
        except ValueError as e:
            context.abort(grpc.StatusCode.NOT_FOUND, str(e))
            return HalPinValue()

    def WriteHalPin(self, request: HalPinWrite, context: grpc.ServicerContext) -> Result:
        if not self._check_write_access(request, context, "WriteHalPin"):
            return Result(success=False, message="Access denied")
        return self.sidecar.write_hal_pin(
            pin_name=request.pin_name,
            value_f=request.value_f,
            value_u32=request.value_u32,
            value_s32=request.value_s32,
            value_bit=request.value_bit,
        )

    def SubscribeHalPins(
        self, request: HalPinSubscribe, context: grpc.ServicerContext
    ) -> Generator[HalPinUpdate, None, None]:
        updates = self.sidecar.subscribe_hal_pins(request.pin_names, request.poll_interval_seconds)
        for update in updates:
            if not self.sidecar._running or not context.is_active():
                return
            yield update

    # -- Errors --

    def GetErrors(self, request: GetErrorsRequest, context: grpc.ServicerContext) -> ErrorList:
        errors = self.sidecar.get_errors(request.limit)
        return ErrorList(errors=errors)

    def SubscribeErrors(
        self, request: MachineId, context: grpc.ServicerContext
    ) -> Generator[ErrorEvent, None, None]:
        events = self.sidecar.subscribe_errors()
        for event in events:
            if not self.sidecar._running or not context.is_active():
                return
            yield event

    # -- Configuration --

    def GetMachineInfo(self, request: MachineId, context: grpc.ServicerContext) -> MachineInfo:
        return self.sidecar.get_machine_info()

    def GetIniParam(
        self, request: IniParamRequest, context: grpc.ServicerContext
    ) -> IniParamValue:
        value = self.sidecar.get_ini_param(request.section, request.option)
        return IniParamValue(id=request.id, value=value)


# ---------------------------------------------------------------------------
# Gateway servicer (stub — full implementation in gateway/ package)
# ---------------------------------------------------------------------------

class GatewayServiceRPC(FleetGatewayServiceServicer):
    """Minimal gateway servicer. Full implementation lives in gateway/server.py."""

    def __init__(self, auth_manager=None, user_extractor=None) -> None:
        self._auth_manager = auth_manager
        self._user_extractor = user_extractor

    def _validate_auth(self, context: grpc.ServicerContext) -> Optional[Any]:
        """Validate OIDC token from gRPC metadata. Returns user object or aborts context."""
        if self._auth_manager is not None:
            try:
                metadata = dict(context.invocation_metadata())
                return self._auth_manager.extract_user(metadata)
            except Exception as e:
                context.abort(grpc.StatusCode.UNAUTHENTICATED, str(e))
                return None
        elif self._user_extractor is not None:
            try:
                metadata = dict(context.invocation_metadata())
                return self._user_extractor(metadata)
            except Exception as e:
                context.abort(grpc.StatusCode.UNAUTHENTICATED, str(e))
                return None
        return None

    def DiscoverMachines(
        self, request: DiscoverRequest, context: grpc.ServicerContext
    ) -> MachineList:
        user = self._validate_auth(context)
        if user is None:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Gateway not configured")
        return MachineList()

    def RouteMachine(self, request: MachineId, context: grpc.ServicerContext) -> GatewayRoute:
        user = self._validate_auth(context)
        if user is None:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Gateway not configured")
        return GatewayRoute()

    def BroadcastCommand(
        self, request: BroadcastRequest, context: grpc.ServicerContext
    ) -> BroadcastResult:
        user = self._validate_auth(context)
        if user is None:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Gateway not configured")
        return BroadcastResult()

    def SubscribeAllStatus(
        self, request: SubscribeAllRequest, context: grpc.ServicerContext
    ) -> Generator[MachineStatus, None, None]:
        user = self._validate_auth(context)
        if user is None:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Gateway not configured")
        yield MachineStatus()


# ---------------------------------------------------------------------------
# Server creation and management
# ---------------------------------------------------------------------------

def create_server(
    sidecar: LinuxCncSidecar,
    port: int = 50051,
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    root_cert_file: Optional[str] = None,
    use_gateway: bool = False,
    user_extractor=None,
    max_workers: int = 8,
) -> grpc.Server:
    """Create and configure a gRPC server for the sidecar.

    Args:
        sidecar: The LinuxCncSidecar instance to serve.
        port: Port to listen on.
        cert_file: Server TLS certificate path (PEM).
        key_file: Server TLS private key path (PEM).
        root_cert_file: Root CA certificate for mTLS client verification.
        use_gateway: If True, also expose FleetGatewayService RPCs.
        user_extractor: Callable that extracts user from metadata dict.
        max_workers: Number of worker threads for gRPC request handling.

    Returns:
        Configured grpc.Server instance (not yet started).
    """
    interceptors = []
    if user_extractor:
        from linuxcnc_fleet.auth import create_auth_interceptor
        interceptors.append(create_auth_interceptor(user_extractor))

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        interceptors=interceptors,
    )

    fleet_servicer = FleetServiceRPC(sidecar)
    add_FleetServiceServicer_to_server(fleet_servicer, server)

    if use_gateway:
        gateway_servicer = GatewayServiceRPC(user_extractor=user_extractor)
        add_FleetGatewayServiceServicer_to_server(gateway_servicer, server)

    if cert_file and key_file:
        creds = _build_credentials(cert_file, key_file, root_cert_file)
        server.add_secure_port(f"[::]:{port}", creds)
    else:
        server.add_insecure_port(f"[::]:{port}")

    return server


def _build_credentials(
    cert_file: str,
    key_file: str,
    root_cert_file: Optional[str] = None,
) -> grpc.ServerCredentials:
    """Build TLS or mTLS credentials from certificate files.

    Raises FileNotFoundError if any file is missing, or PermissionError
    if the process lacks read access.
    """
    try:
        with open(cert_file, "rb") as f:
            cert = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"TLS certificate file not found: {cert_file}") from None
    except PermissionError:
        raise PermissionError(f"Permission denied reading TLS certificate: {cert_file}") from None

    try:
        with open(key_file, "rb") as f:
            private_key = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"TLS key file not found: {key_file}") from None
    except PermissionError:
        raise PermissionError(f"Permission denied reading TLS key: {key_file}") from None

    if root_cert_file:
        try:
            with open(root_cert_file, "rb") as f:
                root_certs = f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"TLS root certificate file not found: {root_cert_file}") from None
        except PermissionError:
            raise PermissionError(f"Permission denied reading TLS root certificate: {root_cert_file}") from None
        creds = grpc.ssl_server_credentials(
            [(private_key, cert)],
            root_certificates=root_certs,
            require_client_auth=True,
        )
    else:
        creds = grpc.ssl_server_credentials(
            [(private_key, cert)],
        )

    return creds


def run_server(
    sidecar: LinuxCncSidecar,
    port: int = 50051,
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    root_cert_file: Optional[str] = None,
    use_gateway: bool = False,
    user_extractor=None,
    max_workers: int = 8,
    metrics_port: Optional[int] = None,
) -> None:
    """Create, start, and block on a gRPC server.

    Sidecar polling must be started before calling this function.

    Args:
        sidecar: The LinuxCncSidecar instance to serve.
        port: Port to listen on for gRPC.
        cert_file: Server TLS certificate path (PEM).
        key_file: Server TLS private key path (PEM).
        root_cert_file: Root CA certificate for mTLS client verification.
        use_gateway: If True, also expose FleetGatewayService RPCs.
        user_extractor: Callable that extracts user from metadata dict.
        max_workers: Number of worker threads for gRPC request handling.
        metrics_port: Optional port for Prometheus metrics/health HTTP server.
    """
    import asyncio
    import threading

    from aiohttp import web

    sidecar.run()

    server = create_server(
        sidecar=sidecar,
        port=port,
        cert_file=cert_file,
        key_file=key_file,
        root_cert_file=root_cert_file,
        use_gateway=use_gateway,
        user_extractor=user_extractor,
        max_workers=max_workers,
    )

    server.start()
    log.info("gRPC server started on port %d", port)

    http_runner = None
    if metrics_port:
        from linuxcnc_fleet import metrics as sidecar_metrics

        async def _start_http():
            nonlocal http_runner
            app = web.Application()
            app["sidecar"] = sidecar

            async def handle_health(request: web.Request) -> web.Response:
                data = sidecar_metrics.handle_health(sidecar)
                return web.json_response(data)

            async def handle_metrics(request: web.Request) -> web.Response:
                text = sidecar_metrics.handle_metrics()
                return web.Response(text=text, content_type="text/plain; version=0.0.4", charset="utf-8")

            app.router.add_get("/health", handle_health)
            app.router.add_get("/metrics", handle_metrics)

            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", metrics_port)
            await site.start()
            http_runner = runner
            log.info("Metrics server started on port %d", metrics_port)

        def _run_http_server():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_start_http())
                loop.run_forever()
            except KeyboardInterrupt:
                pass
            finally:
                if http_runner:
                    loop.run_until_complete(http_runner.cleanup())
                loop.close()

        metrics_thread = threading.Thread(target=_run_http_server, daemon=True)
        metrics_thread.start()

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        log.info("Shutting down gRPC server")
        server.stop(grace=5)
        sidecar.shutdown()
        if http_runner:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(http_runner.cleanup())
                loop.close()
            except Exception:
                pass
