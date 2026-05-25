"""gRPC server — maps FleetService RPCs to LinuxCncSidecar methods."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent import futures
from typing import AsyncGenerator, Optional

import grpc

from linuxcnc_fleet.fleet_pb2 import (
    BroadcastRequest,
    BroadcastResult,
    Empty,
    ErrorEvent,
    ErrorCode,
    ExecutionCommand,
    ExecutionState,
    GatewayRoute,
    GetErrorsRequest,
    HalComponentList,
    HalPinSubscribe,
    HalPinType,
    HalPinUpdate,
    HalPinValue,
    IniParamRequest,
    IniParamValue,
    ListHalRequest,
    MachineId,
    MachineInfo,
    MachineList,
    MachineStatus,
    MdiCommand,
    Mode,
    PositionRequest,
    PositionResponse,
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

    def __init__(self, sidecar: LinuxCncSidecar):
        self.sidecar = sidecar

    # -- Status queries --

    def GetStatus(self, request: MachineId, context: grpc.ServicerContext) -> MachineStatus:
        return self.sidecar.get_status()

    def SubscribeStatus(
        self, request: MachineId, context: grpc.ServicerContext
    ) -> AsyncGenerator[MachineStatus, None]:
        while True:
            yield self.sidecar.get_status()
            await asyncio.sleep(0.02)

    # -- Control commands --

    def SetMode(self, request: SetModeRequest, context: grpc.ServicerContext) -> Result:
        return self.sidecar.set_mode(request.mode)

    def SetExecution(self, request: ExecutionCommand, context: grpc.ServicerContext) -> Result:
        return self.sidecar.set_execution(request.state)

    def Start(self, request: Empty, context: grpc.ServicerContext) -> Result:
        return self.sidecar.start()

    def Stop(self, request: Empty, context: grpc.ServicerContext) -> Result:
        return self.sidecar.stop()

    def FeedHold(self, request: Empty, context: grpc.ServicerContext) -> Result:
        return self.sidecar.feed_hold()

    def Continue(self, request: Empty, context: grpc.ServicerContext) -> Result:
        return self.sidecar.continue_exec()

    def HomeAll(self, request: Empty, context: grpc.ServicerContext) -> Result:
        return self.sidecar.home_all()

    def HomeAxis(self, request: TrajAxis, context: grpc.ServicerContext) -> Result:
        return self.sidecar.home_axis(request)

    # -- G-code / MDI --

    def SendMdiCommand(self, request: MdiCommand, context: grpc.ServicerContext) -> Result:
        return self.sidecar.send_mdi(request.command)

    def LoadProgram(self, request: ProgramPath, context: grpc.ServicerContext) -> Result:
        return self.sidecar.load_program(request.path)

    def StepForward(self, request: Empty, context: grpc.ServicerContext) -> Result:
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
        return self.sidecar.write_hal_pin(
            pin_name=request.pin_name,
            value_f=request.value_f,
            value_u32=request.value_u32,
            value_s32=request.value_s32,
            value_bit=request.value_bit,
        )

    def SubscribeHalPins(
        self, request: HalPinSubscribe, context: grpc.ServicerContext
    ) -> AsyncGenerator[HalPinUpdate, None]:
        updates = self.sidecar.subscribe_hal_pins(request.pin_names, request.poll_interval_seconds)
        for update in updates:
            if not self.sidecar._running:
                return
            yield update

    # -- Errors --

    def GetErrors(self, request: GetErrorsRequest, context: grpc.ServicerContext) -> ErrorList:
        errors = self.sidecar.get_errors(request.limit)
        return ErrorList(errors=errors)

    def SubscribeErrors(
        self, request: MachineId, context: grpc.ServicerContext
    ) -> AsyncGenerator[ErrorEvent, None]:
        events = self.sidecar.subscribe_errors()
        for event in events:
            if not self.sidecar._running:
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

    def DiscoverMachines(
        self, request: DiscoverRequest, context: grpc.ServicerContext
    ) -> MachineList:
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "Gateway not configured")
        return MachineList()

    def RouteMachine(self, request: MachineId, context: grpc.ServicerContext) -> GatewayRoute:
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "Gateway not configured")
        return GatewayRoute()

    def BroadcastCommand(
        self, request: BroadcastRequest, context: grpc.ServicerContext
    ) -> BroadcastResult:
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "Gateway not configured")
        return BroadcastResult()

    def SubscribeAllStatus(
        self, request: SubscribeAllRequest, context: grpc.ServicerContext
    ) -> AsyncGenerator[MachineStatus, None]:
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
) -> grpc.Server:
    """Create and configure a gRPC server for the sidecar.

    Args:
        sidecar: The LinuxCncSidecar instance to serve.
        port: Port to listen on.
        cert_file: Server TLS certificate path (PEM).
        key_file: Server TLS private key path (PEM).
        root_cert_file: Root CA certificate for mTLS client verification.
        use_gateway: If True, also expose FleetGatewayService RPCs.

    Returns:
        Configured grpc.Server instance (not yet started).
    """
    server = grpc.server(futures_executor=futures.ThreadPoolExecutor(max_workers=8))

    add_FleetServiceServicer_to_server(FleetServiceRPC(sidecar), server)

    if use_gateway:
        add_FleetGatewayServiceServicer_to_server(GatewayServiceRPC(), server)

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
    """Build TLS or mTLS credentials from certificate files."""
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
) -> None:
    """Create, start, and block on a gRPC server.

    Sidecar polling must be started before calling this function.
    """
    sidecar.run()

    server = create_server(
        sidecar=sidecar,
        port=port,
        cert_file=cert_file,
        key_file=key_file,
        root_cert_file=root_cert_file,
        use_gateway=use_gateway,
    )

    server.start()
    log.info("gRPC server started on port %d", port)

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        log.info("Shutting down gRPC server")
        server.stop(grace=5)
        sidecar.stop()
