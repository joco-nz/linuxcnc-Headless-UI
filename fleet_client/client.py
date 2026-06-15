"""FleetClient — high-level async client for LinuxCNC fleet management."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional

import grpc

from fleet_client.auth import create_aio_auth_interceptor
from linuxcnc_fleet.fleet_pb2_grpc import FleetGatewayServiceStub as _SyncFleetGatewayServiceStub
from linuxcnc_fleet.fleet_pb2_grpc import FleetServiceStub as _SyncFleetServiceStub
from linuxcnc_fleet.fleet_pb2 import (
    BroadcastRequest,
    BroadcastResult,
    DiscoverRequest,
    Empty,
    ErrorEvent,
    ErrorList,
    ExecutionCommand,
    GetErrorsRequest,
    GatewayRoute,
    HalComponentInfo,
    HalComponentList,
    HalPinRead,
    HalPinSubscribe,
    HalPinType,
    HalPinUpdate,
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
    PositionRequest,
    PositionResponse,
    ProgramEntry,
    ProgramList,
   ProgramPath,
    RegisterRequest,
    RegisterResponse,
    Result,
    SetModeRequest,
    SubscribeAllRequest,
    TrajAxis,
)


def _error_details(e: BaseException) -> str:
    """Safely extract error details from gRPC or non-gRPC exceptions."""
    if hasattr(e, 'details'):
        return e.details()
    return str(e)


def _serialize_request(req: Any) -> bytes:
    """Serialize a protobuf request to bytes."""
    if hasattr(req, 'SerializeToString'):
        return req.SerializeToString()
    return req


class _AioFleetGatewayServiceStub:
    """Aio-compatible wrapper around the sync FleetGatewayServiceStub."""

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._channel = channel
        self._discover_machines = channel.unary_unary(
            '/linuxcnc_fleet.FleetGatewayService/DiscoverMachines',
            request_serializer=_serialize_request,
            response_deserializer=MachineList.FromString,
        )
        self._route_machine = channel.unary_unary(
            '/linuxcnc_fleet.FleetGatewayService/RouteMachine',
            request_serializer=_serialize_request,
            response_deserializer=GatewayRoute.FromString,
        )
        self._broadcast_command = channel.unary_unary(
            '/linuxcnc_fleet.FleetGatewayService/BroadcastCommand',
            request_serializer=_serialize_request,
            response_deserializer=BroadcastResult.FromString,
        )
        self._register_machine = channel.unary_unary(
            '/linuxcnc_fleet.FleetGatewayService/RegisterMachine',
            request_serializer=_serialize_request,
            response_deserializer=RegisterResponse.FromString,
        )

    async def DiscoverMachines(self, request: DiscoverRequest, **kwargs: Any) -> Any:
        return await self._discover_machines(request, **kwargs)

    async def RouteMachine(self, request: MachineId, **kwargs: Any) -> GatewayRoute:
        return await self._route_machine(request, **kwargs)

    async def BroadcastCommand(self, request: BroadcastRequest, **kwargs: Any) -> Any:
        return await self._broadcast_command(request, **kwargs)

    async def RegisterMachine(self, request: RegisterRequest, **kwargs: Any) -> RegisterResponse:
        return await self._register_machine(request, **kwargs)


class _AioFleetServiceStub:
    """Aio-compatible wrapper around the sync FleetServiceStub."""

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._channel = channel
        self._get_status = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/GetStatus',
            request_serializer=_serialize_request,
            response_deserializer=MachineStatus.FromString,
        )
        self._set_mode = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/SetMode',
            request_serializer=_serialize_request,
            response_deserializer=Result.FromString,
        )
        self._set_execution = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/SetExecution',
            request_serializer=_serialize_request,
            response_deserializer=Result.FromString,
        )
        self._start = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/Start',
            request_serializer=_serialize_request,
            response_deserializer=Result.FromString,
        )
        self._stop = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/Stop',
            request_serializer=_serialize_request,
            response_deserializer=Result.FromString,
        )
        self._feed_hold = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/FeedHold',
            request_serializer=_serialize_request,
            response_deserializer=Result.FromString,
        )
        self._continue_exec = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/ContinueExec',
            request_serializer=_serialize_request,
            response_deserializer=Result.FromString,
        )
        self._home_all = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/HomeAll',
            request_serializer=_serialize_request,
            response_deserializer=Result.FromString,
        )
        self._step_forward = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/StepForward',
            request_serializer=_serialize_request,
            response_deserializer=Result.FromString,
        )
        self._send_mdi = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/SendMDI',
            request_serializer=_serialize_request,
            response_deserializer=Result.FromString,
        )
        self._home_axis = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/HomeAxis',
            request_serializer=_serialize_request,
            response_deserializer=Result.FromString,
        )
        self._load_program = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/LoadProgram',
            request_serializer=_serialize_request,
            response_deserializer=Result.FromString,
        )
        self._get_position = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/GetPosition',
            request_serializer=_serialize_request,
            response_deserializer=PositionResponse.FromString,
        )
        self._list_hal_components = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/ListHalComponents',
            request_serializer=_serialize_request,
            response_deserializer=HalComponentList.FromString,
        )
        self._read_hal_pin = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/ReadHalPin',
            request_serializer=_serialize_request,
            response_deserializer=HalPinValue.FromString,
        )
        self._write_hal_pin = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/WriteHalPin',
            request_serializer=_serialize_request,
            response_deserializer=Result.FromString,
        )
        self._get_errors = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/GetErrors',
            request_serializer=_serialize_request,
            response_deserializer=ErrorList.FromString,
        )
        self._get_machine_info = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/GetMachineInfo',
            request_serializer=_serialize_request,
            response_deserializer=MachineInfo.FromString,
        )
        self._get_ini_param = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/GetIniParam',
            request_serializer=_serialize_request,
            response_deserializer=IniParamValue.FromString,
        )
        self._list_programs = channel.unary_unary(
            '/linuxcnc_fleet.FleetService/ListPrograms',
            request_serializer=_serialize_request,
            response_deserializer=ProgramList.FromString,
        )
        self._subscribe_status = channel.unary_stream(
            '/linuxcnc_fleet.FleetService/SubscribeStatus',
            request_serializer=_serialize_request,
            response_deserializer=MachineStatus.FromString,
        )
        self._subscribe_hal_pins = channel.unary_stream(
            '/linuxcnc_fleet.FleetService/SubscribeHalPins',
            request_serializer=_serialize_request,
            response_deserializer=HalPinUpdate.FromString,
        )
        self._subscribe_errors = channel.unary_stream(
            '/linuxcnc_fleet.FleetService/SubscribeErrors',
            request_serializer=_serialize_request,
            response_deserializer=ErrorEvent.FromString,
        )
        self._subscribe_all_status = channel.unary_stream(
            '/linuxcnc_fleet.FleetService/SubscribeAllStatus',
            request_serializer=_serialize_request,
            response_deserializer=MachineStatus.FromString,
        )

    async def GetStatus(self, request: MachineId, **kwargs: Any) -> MachineStatus:
        return await self._get_status(request, **kwargs)

    async def SetMode(self, request: SetModeRequest, **kwargs: Any) -> Result:
        return await self._set_mode(request, **kwargs)

    async def SetExecution(self, request: ExecutionCommand, **kwargs: Any) -> Result:
        return await self._set_execution(request, **kwargs)

    async def Start(self, request: MachineId, **kwargs: Any) -> Result:
        return await self._start(request, **kwargs)

    async def Stop(self, request: MachineId, **kwargs: Any) -> Result:
        return await self._stop(request, **kwargs)

    async def FeedHold(self, request: MachineId, **kwargs: Any) -> Result:
        return await self._feed_hold(request, **kwargs)

    async def ContinueExec(self, request: MachineId, **kwargs: Any) -> Result:
        return await self._continue_exec(request, **kwargs)

    async def HomeAll(self, request: MachineId, **kwargs: Any) -> Result:
        return await self._home_all(request, **kwargs)

    async def StepForward(self, request: MachineId, **kwargs: Any) -> Result:
        return await self._step_forward(request, **kwargs)

    async def SendMDI(self, request: MdiCommand, **kwargs: Any) -> Result:
        return await self._send_mdi(request, **kwargs)

    async def HomeAxis(self, request: HomeAxisRequest, **kwargs: Any) -> Result:
        return await self._home_axis(request, **kwargs)

    async def LoadProgram(self, request: ProgramPath, **kwargs: Any) -> Result:
        return await self._load_program(request, **kwargs)

    async def GetPosition(self, request: PositionRequest, **kwargs: Any) -> PositionResponse:
        return await self._get_position(request, **kwargs)

    async def ListHalComponents(self, request: ListHalRequest, **kwargs: Any) -> HalComponentList:
        return await self._list_hal_components(request, **kwargs)

    async def ReadHalPin(self, request: HalPinRead, **kwargs: Any) -> HalPinValue:
        return await self._read_hal_pin(request, **kwargs)

    async def WriteHalPin(self, request: HalPinWrite, **kwargs: Any) -> Result:
        return await self._write_hal_pin(request, **kwargs)

    async def GetErrors(self, request: GetErrorsRequest, **kwargs: Any) -> ErrorList:
        return await self._get_errors(request, **kwargs)

    async def GetMachineInfo(self, request: MachineId, **kwargs: Any) -> MachineInfo:
        return await self._get_machine_info(request, **kwargs)

    async def GetIniParam(self, request: IniParamRequest, **kwargs: Any) -> IniParamValue:
        return await self._get_ini_param(request, **kwargs)

    async def ListPrograms(self, request: ListProgramsRequest, **kwargs: Any) -> ProgramList:
        return await self._list_programs(request, **kwargs)

    async def SubscribeStatus(self, request: MachineId, **kwargs: Any) -> Any:
        return self._subscribe_status(request, **kwargs)

    async def SubscribeHalPins(self, request: HalPinSubscribe, **kwargs: Any) -> Any:
        return self._subscribe_hal_pins(request, **kwargs)

    async def SubscribeErrors(self, request: MachineId, **kwargs: Any) -> Any:
        return self._subscribe_errors(request, **kwargs)

    async def SubscribeAllStatus(self, request: SubscribeAllRequest, **kwargs: Any) -> Any:
        return self._subscribe_all_status(request, **kwargs)


# Read-only RPCs eligible for retry
_READ_ONLY_RPC = frozenset({
    "GetStatus",
    "SubscribeStatus",
    "ListHalComponents",
    "ReadHalPin",
    "GetErrors",
    "SubscribeErrors",
    "GetMachineInfo",
    "GetPosition",
    "GetIniParam",
    "ListPrograms",
})

# Max retries for read-only RPCs
_MAX_RETRIES = 3
_INITIAL_BACKOFF = 0.1

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MachineEntry:
    """Machine information from gateway discovery."""
    machine_id: str
    machine_name: str
    host_address: str
    version: Optional[str] = None
    num_joints: int = 0
    num_hal_components: int = 0


@dataclass
class _CachedChannel:
    """Cached gRPC channel with TTL tracking."""
    channel: grpc.Channel
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    ref_count: int = 0


class FleetClient:
    """High-level async client for the LinuxCNC fleet management API.
    
    Connects to a gateway server and provides methods to discover machines,
    route to them, send commands, and subscribe to status updates.
    """

    def __init__(
        self,
        gateway_address: str,
        token: str,
        tls_enabled: bool = False,
        machine_channel_ttl: float = 300.0,
        _gateway_stub=None,
        _fleet_stub_factory=None,
        _gateway_channel=None,
    ) -> None:
        """Initialize FleetClient.
        
        Args:
            gateway_address: Gateway server address (host:port)
            token: OIDC access token for authentication
            tls_enabled: Whether to use TLS for gateway connection
            machine_channel_ttl: Time-to-live for cached machine channels (seconds)
            _gateway_stub: Internal use — inject a mock gateway stub for testing
            _fleet_stub_factory: Internal use — inject a callable that returns a fleet stub
            _gateway_channel: Internal use — inject a pre-created channel for testing
        """
        self._gateway_address = gateway_address
        self._token = token
        self._tls_enabled = tls_enabled
        self._machine_channel_ttl = machine_channel_ttl
        self._closed = False
        
        # Gateway channel with auth interceptor (created lazily)
        if _gateway_channel is not None:
            self._gateway_channel = _gateway_channel
        else:
            self._gateway_channel = None
        self._gateway_stub = _gateway_stub
        self._fleet_stub_factory = _fleet_stub_factory or (
            lambda ch: _AioFleetServiceStub(ch)
        )
        
        # Machine channel cache
        self._machine_channels: dict[str, _CachedChannel] = {}
        self._cache_lock = threading.Lock()
        
        # Background cleanup task
        self._cleanup_task: Optional[asyncio.Task] = None

    def _create_gateway_channel(self) -> grpc.Channel:
        """Create the gRPC channel to the gateway server."""
        auth_interceptor = create_aio_auth_interceptor(self._token)
        if self._tls_enabled:
            creds = grpc.ssl_channel_credentials()
            return grpc.aio.secure_channel(
                self._gateway_address,
                creds,
                interceptors=[auth_interceptor],
            )
        else:
            return grpc.aio.insecure_channel(
                self._gateway_address,
                interceptors=[auth_interceptor],
            )

    async def _get_or_create_machine_channel(
        self, address: str, port: int
    ) -> grpc.Channel:
        """Get or create a cached gRPC channel to a machine instance.
        
        Args:
            address: Machine IP address or hostname
            port: Machine gRPC port
            
        Returns:
            gRPC Channel to the specified machine
        """
        key = f"{address}:{port}"
        
        with self._cache_lock:
            if key in self._machine_channels:
                cached = self._machine_channels[key]
                # Check TTL expiry
                if time.time() - cached.created_at > self._machine_channel_ttl:
                    log.debug("Machine channel %s expired, closing", key)
                    cached.channel.close()
                    del self._machine_channels[key]
                else:
                    cached.last_used = time.time()
                    cached.ref_count += 1
                    return cached.channel
            
            # Create new channel
            if self._tls_enabled:
                creds = grpc.ssl_channel_credentials()
                channel = grpc.aio.secure_channel(
                    f"{address}:{port}",
                    creds,
                    interceptors=[create_aio_auth_interceptor(self._token)],
                )
            else:
                channel = grpc.aio.insecure_channel(
                    f"{address}:{port}",
                    interceptors=[create_aio_auth_interceptor(self._token)],
                )
            
            self._machine_channels[key] = _CachedChannel(channel=channel)
            return channel

    def _cleanup_expired_channels(self) -> None:
        """Remove expired machine channels from cache."""
        now = time.time()
        expired_keys = []
        
        with self._cache_lock:
            for key, cached in self._machine_channels.items():
                if now - cached.created_at > self._machine_channel_ttl:
                    expired_keys.append(key)
            
            for key in expired_keys:
                cached = self._machine_channels.pop(key)
                cached.channel.close()
                log.debug("Cleaned up expired channel %s", key)

    async def close(self) -> None:
        """Close all channels and clean up resources."""
        if self._closed:
            return
        
        self._closed = True
        
        # Close gateway channel
        if hasattr(self, '_gateway_channel') and self._gateway_channel:
            try:
                await self._gateway_channel.close()
            except Exception:
                pass
        
        # Close all machine channels
        with self._cache_lock:
            for cached in self._machine_channels.values():
                try:
                    cached.channel.close()
                except Exception:
                    pass
            self._machine_channels.clear()

    async def __aenter__(self) -> "FleetClient":
        """Async context manager entry."""
        await self._ensure_gateway_channel()
        return self

    async def _ensure_gateway_channel(self) -> None:
        """Lazily create gateway channel and stub if not already created."""
        if self._gateway_channel is None:
            self._gateway_channel = self._create_gateway_channel()
            if self._gateway_stub is None:
                self._gateway_stub = _AioFleetGatewayServiceStub(self._gateway_channel)

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    # -----------------------------------------------------------------------
    # GatewayService RPCs
    # -----------------------------------------------------------------------

    async def get_machines(
        self, facility: Optional[str] = None
    ) -> list[MachineEntry]:
        """Discover available machines in the fleet.
        
        Args:
            facility: Optional facility filter to narrow results
            
        Returns:
            List of MachineEntry for visible machines
        """
        if self._closed:
            raise RuntimeError("Client is closed")
        
        await self._ensure_gateway_channel()
        
        try:
            request = DiscoverRequest(facility=facility or "")
            response = await self._gateway_stub.DiscoverMachines(request)
            
            return [
                MachineEntry(
                    machine_id=m.machine_id,
                    machine_name=m.machine_name,
                    host_address=m.host_address,
                    version=m.version or None,
                    num_joints=m.num_joints,
                    num_hal_components=m.num_hal_components,
                )
                for m in response.machines
            ]
        except Exception as e:
            log.error("DiscoverMachines failed: %s", _error_details(e))
            raise

    async def route_machine(self, machine_id: str) -> tuple[str, int]:
        """Route a machine ID to its address:port.
        
        Args:
            machine_id: Unique machine identifier
            
        Returns:
            Tuple of (host_address, port)
            
        Raises:
            grpc.aio.AioRpcError: If machine not found or access denied
     """
        if self._closed:
            raise RuntimeError("Client is closed")
        
        await self._ensure_gateway_channel()
        
        try:
            request = MachineId(id=machine_id)
            response: GatewayRoute = await self._gateway_stub.RouteMachine(request)
            return (response.instance_address, response.instance_port)
        except Exception as e:
            log.error("RouteMachine failed for %s: %s", machine_id, _error_details(e))
            raise

    async def register_machine(
        self,
        machine_id: str,
        address: str,
        port: int,
        facility: str = "",
        tags: Optional[list[str]] = None,
        version: str = "",
    ) -> bool:
        """Register a sidecar machine with the gateway.
        
        Args:
            machine_id: Unique machine identifier
            address: Machine host address or hostname
            port: Machine gRPC port
            facility: Facility name for scoping
            tags: Optional list of machine tags
            version: LinuxCNC version string
            
        Returns:
            True if registration succeeded
            
        Raises:
            grpc.aio.AioRpcError: If registration fails (e.g., not authorized)
       """
        if self._closed:
            raise RuntimeError("Client is closed")
        
        await self._ensure_gateway_channel()
        
        try:
            request = RegisterRequest(
                machine_id=machine_id,
                address=address,
                port=port,
                facility=facility,
                tags=tags or [],
                version=version,
            )
            response = await self._gateway_stub.RegisterMachine(request)
            return response.success
        except Exception as e:
            log.error("RegisterMachine failed for %s: %s", machine_id, _error_details(e))
            raise

    async def broadcast_command(
        self,
        scope: str,
        command_type: str,
        command_value: Any,
        facility: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict[str, tuple[bool, str]]:
        """Broadcast a command to multiple machines based on scope.
        
        Args:
            scope: Scope type - "ALL", "FACILITY", or "TAG"
            command_type: Command type - "mdi", "execution", "mode", or "program"
            command_value: Command-specific value (string for MDI/program, int for execution/mode)
            facility: Facility filter (required for FACILITY scope)
            tags: Tag filter (required for TAG scope)
            
        Returns:
            Dict mapping machine_id to (success: bool, message: str)
      """
        if self._closed:
            raise RuntimeError("Client is closed")
        
        await self._ensure_gateway_channel()
        
        # Map scope string to proto enum value
        scope_map = {"ALL": 0, "FACILITY": 1, "TAG": 2}
        if scope not in scope_map:
            raise ValueError(f"Unknown broadcast scope '{scope}'. Must be one of: {', '.join(scope_map)}")
        scope_value = scope_map[scope]
        
        # Build broadcast request
        request = BroadcastRequest(
            scope=scope_value,
            facility=facility or "",
            tags=tags or [],
        )
        
        # Set command based on type
        if command_type == "mdi":
            request.mdi.command = str(command_value)
        elif command_type == "execution":
            request.exec.state = int(command_value)
        elif command_type == "mode":
            request.mode.mode = int(command_value)
        elif command_type == "program":
            request.program_path = str(command_value)
        else:
            raise ValueError(f"Unknown command type: {command_type}")
        
        try:
            response = await self._gateway_stub.BroadcastCommand(request)
            
            results = {}
            for machine_id, result in response.results.items():
                results[machine_id] = (result.success, result.message)
            
            return results
        except Exception as e:
            log.error("BroadcastCommand failed: %s", _error_details(e))
            raise

    async def broadcast_mdi(
        self,
        scope: str,
        command: str,
        facility: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict[str, tuple[bool, str]]:
        """Send MDI command to all matching machines.
        
        Args:
            scope: Scope type - "ALL", "FACILITY", or "TAG"
            command: MDI command string
            facility: Facility filter (required for FACILITY scope)
            tags: Tag filter (required for TAG scope)
            
        Returns:
            Dict mapping machine_id to (success: bool, message: str)
        """
        return await self.broadcast_command(
            scope=scope,
            command_type="mdi",
            command_value=command,
            facility=facility,
            tags=tags,
        )

    async def broadcast_execution(
        self,
        scope: str,
        state: int,
        facility: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict[str, tuple[bool, str]]:
        """Send execution command to all matching machines.
        
        Args:
            scope: Scope type - "ALL", "FACILITY", or "TAG"
            state: Execution state value (from proto enum)
            facility: Facility filter (required for FACILITY scope)
            tags: Tag filter (required for TAG scope)
            
        Returns:
            Dict mapping machine_id to (success: bool, message: str)
        """
        return await self.broadcast_command(
            scope=scope,
            command_type="execution",
            command_value=state,
            facility=facility,
            tags=tags,
        )

    async def broadcast_mode(
        self,
        scope: str,
        mode: int,
        facility: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict[str, tuple[bool, str]]:
        """Send mode command to all matching machines.
        
        Args:
            scope: Scope type - "ALL", "FACILITY", or "TAG"
            mode: Mode value (from proto enum)
            facility: Facility filter (required for FACILITY scope)
            tags: Tag filter (required for TAG scope)
            
        Returns:
            Dict mapping machine_id to (success: bool, message: str)
        """
        return await self.broadcast_command(
            scope=scope,
            command_type="mode",
            command_value=mode,
            facility=facility,
            tags=tags,
        )

    
    async def broadcast_load_program(
        self,
        scope: str,
        path: str,
        facility: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict[str, tuple[bool, str]]:
        """Broadcast loading a G-code program to multiple machines.

        Args:
            scope: Scope type - "ALL", "FACILITY", or "TAG"
            path: Program file path to load on target machines
            facility: Facility filter (required for FACILITY scope)
            tags: Tag filter (required for TAG scope)

        Returns:
            Dict mapping machine_id to (success: bool, message: str)
        """
        return await self.broadcast_command(
            scope=scope,
            command_type="program",
            command_value=path,
            facility=facility,
            tags=tags,
        )

    async def list_programs(
        self,
        machine_id: str,
        directory: str = "",
        max_depth: int = 0,
    ) -> ProgramList:
        """List available G-code programs on a machine.

        Args:
            machine_id: Target machine ID
            directory: Directory to scan (empty = INI configured paths)
            max_depth: Maximum directory depth (0 = unlimited)

        Returns:
            ProgramList with list of ProgramEntry objects
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            return await self._retry_read(
                "ListPrograms",
                lambda: self._do_list_programs(machine_id, directory, max_depth),
            )
        except Exception as e:
            log.error("ListPrograms failed for %s: %s", machine_id, e)
            raise

    async def _do_list_programs(self, machine_id: str, directory: str = "", max_depth: int = 0):
        stub = await self._get_fleet_stub(machine_id)
        request = ListProgramsRequest(
            id=MachineId(id=machine_id),
            directory=directory,
            max_depth=max_depth,
        )
        return await stub[0].ListPrograms(request)

    async def subscribe_all_status(
        self,
        facility: Optional[str] = None,
        poll_interval: float = 0.5,
    ) -> AsyncGenerator[tuple[str, Any], None]:
        """Stream status updates from all machines in scope.
        
        Args:
            facility: Optional facility filter
            poll_interval: Poll interval in seconds
            
        Yields:
            Tuples of (machine_id, MachineStatus)
   """
        if self._closed:
            raise RuntimeError("Client is closed")
        
        await self._ensure_gateway_channel()
        
        try:
            request = SubscribeAllRequest(
                facility=facility or "",
                poll_interval_seconds=poll_interval,
            )
            
            call = self._gateway_stub.SubscribeAllStatus(request)
            async for status in call:
                yield (status.machine_id, status)
        except Exception as e:
            log.error("SubscribeAllStatus failed: %s", _error_details(e))
            raise

    # -----------------------------------------------------------------------
    # Helper — route machine_id to fleet channel + stub
    # -----------------------------------------------------------------------

    async def _get_fleet_stub(
        self, machine_id: str
    ) -> tuple[_AioFleetServiceStub, str, int]:
        """Route a machine and return (_AioFleetServiceStub, address, port).
        
        Raises grpc.aio.AioRpcError if machine not found or access denied.
        """
        addr, port = await self.route_machine(machine_id)
        channel = await self._get_or_create_machine_channel(addr, port)
        stub = self._fleet_stub_factory(channel)
        return (stub, addr, port)

    # -----------------------------------------------------------------------
    # Retry decorator for read-only RPCs
    # -----------------------------------------------------------------------

    async def _retry_read(
        self, rpc_name: str, coro_factory
    ):
        """Execute a coroutine factory with exponential-backoff retry for read-only RPCs."""
        last_exc = None
        for attempt in range(_MAX_RETRIES):
            try:
                return await coro_factory()
            except Exception as e:
                if not hasattr(e, 'code') or e.code() not in (grpc.StatusCode.UNAVAILABLE,
                                     grpc.StatusCode.DEADLINE_EXCEEDED,
                                     grpc.StatusCode.RESOURCE_EXHAUSTED):
                    raise
                last_exc = e
                if attempt < _MAX_RETRIES - 1:
                    backoff = _INITIAL_BACKOFF * (2 ** attempt)
                    log.debug(
                        "Read RPC %s failed (attempt %d/%d), retrying in %.3fs: %s",
                        rpc_name, attempt + 1, _MAX_RETRIES, backoff, _error_details(e),
                    )
                    await asyncio.sleep(backoff)
        raise last_exc

    # -----------------------------------------------------------------------
    # FleetService — per-machine RPC wrappers
    # -----------------------------------------------------------------------

    async def get_status(self, machine_id: str) -> MachineStatus:
        """Get current status of a machine.
        
        Args:
            machine_id: Target machine identifier
            
        Returns:
            MachineStatus protobuf message
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            result = await self._retry_read("GetStatus", lambda: self._do_get_status(machine_id))
            return result
        except Exception as e:
            log.error("GetStatus failed for %s: %s", machine_id, e)
            raise

    async def _do_get_status(self, machine_id: str):
        stub = await self._get_fleet_stub(machine_id)
        return await stub[0].GetStatus(MachineId(id=machine_id))

    async def set_mode(self, machine_id: str, mode: int) -> Result:
        """Set machine operation mode.
        
        Args:
            machine_id: Target machine identifier
            mode: Mode value from proto enum (MODE_MANUAL=1, MODE_AUTO=2, MODE_MDA=3)
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            request = SetModeRequest(id=MachineId(id=machine_id), mode=mode)
            return await stub[0].SetMode(request)
        except Exception as e:
            log.error("SetMode failed for %s: %s", machine_id, _error_details(e))
            raise

    async def set_execution(self, machine_id: str, state: int) -> Result:
        """Send execution command to a machine.
        
        Args:
            machine_id: Target machine identifier
            state: ExecutionState value (RUN=1, FAST_RUN=2, STEP=3, etc.)
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            request = ExecutionCommand(id=MachineId(id=machine_id), state=state)
            return await stub[0].SetExecution(request)
        except Exception as e:
            log.error("SetExecution failed for %s: %s", machine_id, _error_details(e))
            raise

    async def start(self, machine_id: str) -> Result:
        """Start program execution on a machine.
        
        Args:
            machine_id: Target machine identifier
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            return await stub[0].Start(Empty())
        except Exception as e:
            log.error("Start failed for %s: %s", machine_id, _error_details(e))
            raise

    async def stop(self, machine_id: str) -> Result:
        """Stop program execution on a machine.
        
        Args:
            machine_id: Target machine identifier
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            return await stub[0].Stop(Empty())
        except Exception as e:
            log.error("Stop failed for %s: %s", machine_id, _error_details(e))
            raise

    async def feed_hold(self, machine_id: str) -> Result:
        """Send feed hold to a machine.
        
        Args:
            machine_id: Target machine identifier
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            return await stub[0].FeedHold(Empty())
        except Exception as e:
            log.error("FeedHold failed for %s: %s", machine_id, _error_details(e))
            raise

    async def continue_exec(self, machine_id: str) -> Result:
        """Continue after feed hold on a machine.
        
        Args:
            machine_id: Target machine identifier
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            return await stub[0].Continue(Empty())
        except Exception as e:
            log.error("Continue failed for %s: %s", machine_id, _error_details(e))
            raise

    async def home_all(self, machine_id: str) -> Result:
        """Home all axes on a machine.
        
        Args:
            machine_id: Target machine identifier
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            return await stub[0].HomeAll(Empty())
        except Exception as e:
            log.error("HomeAll failed for %s: %s", machine_id, _error_details(e))
            raise

    async def step_forward(self, machine_id: str) -> Result:
        """Step forward one line in MDA mode.
        
        Args:
            machine_id: Target machine identifier
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            return await stub[0].StepForward(Empty())
        except Exception as e:
            log.error("StepForward failed for %s: %s", machine_id, _error_details(e))
            raise

    async def send_mdi(self, machine_id: str, command: str) -> Result:
        """Send an MDI command to a machine.
        
        Args:
            machine_id: Target machine identifier
            command: MDI command string
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            request = MdiCommand(id=MachineId(id=machine_id), command=command)
            return await stub[0].SendMdiCommand(request)
        except Exception as e:
            log.error("SendMDI failed for %s: %s", machine_id, _error_details(e))
            raise

    async def home_axis(self, machine_id: str, axis: int) -> Result:
        """Home a specific axis on a machine.
        
        Args:
            machine_id: Target machine identifier
            axis: Axis to home (TrajAxis enum value — X_AXIS=0, Y_AXIS=1, Z_AXIS=2, etc.)
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            request = HomeAxisRequest(
                id=MachineId(id=machine_id), axis=axis
            )
            return await stub[0].HomeAxis(request)
        except Exception as e:
            log.error("HomeAxis(%d) failed for %s: %s", axis, machine_id, _error_details(e))
            raise

    async def load_program(self, machine_id: str, path: str) -> Result:
        """Load a G-code program file on a machine.
        
        Args:
            machine_id: Target machine identifier
            path: Path to the G-code program file
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            request = ProgramPath(id=MachineId(id=machine_id), path=path)
            return await stub[0].LoadProgram(request)
        except Exception as e:
            log.error("LoadProgram(%s) failed for %s: %s", path, machine_id, _error_details(e))
            raise

    async def get_position(
        self,
        machine_id: str,
        position_type: int = 0,
    ) -> PositionResponse:
        """Get current position of a machine.
        
        Args:
            machine_id: Target machine identifier
            position_type: Position type — WORLD=0, JOINT=1, DEVICE=2
            
        Returns:
            PositionResponse with position coordinates
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            return await self._retry_read(
                "GetPosition",
                lambda: self._do_get_position(machine_id, position_type),
            )
        except Exception as e:
            log.error("GetPosition failed for %s: %s", machine_id, _error_details(e))
            raise

    async def _do_get_position(self, machine_id: str, position_type: int):
        stub = await self._get_fleet_stub(machine_id)
        request = PositionRequest(id=MachineId(id=machine_id), type=position_type)
        return await stub[0].GetPosition(request)

    async def list_hal_components(self, machine_id: str) -> HalComponentList:
        """List all HAL components and their pins on a machine.
        
        Args:
            machine_id: Target machine identifier
            
        Returns:
            HalComponentList with component info
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            return await self._retry_read(
                "ListHalComponents",
                lambda: self._do_list_hal(machine_id),
            )
        except Exception as e:
            log.error("ListHalComponents failed for %s: %s", machine_id, _error_details(e))
            raise

    async def _do_list_hal(self, machine_id: str):
        stub = await self._get_fleet_stub(machine_id)
        request = ListHalRequest(id=MachineId(id=machine_id))
        return await stub[0].ListHalComponents(request)

    async def read_hal_pin(self, machine_id: str, pin_name: str) -> HalPinValue:
        """Read a HAL pin value from a machine.
        
        Args:
            machine_id: Target machine identifier
            pin_name: HAL pin name (e.g., "spindle.speed-feedback")
            
        Returns:
            HalPinValue with the current pin value
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            return await self._retry_read(
                "ReadHalPin",
                lambda: self._do_read_hal_pin(machine_id, pin_name),
            )
        except Exception as e:
            log.error("ReadHalPin '%s' failed for %s: %s", pin_name, machine_id, _error_details(e))
            raise

    async def _do_read_hal_pin(self, machine_id: str, pin_name: str):
        stub = await self._get_fleet_stub(machine_id)
        request = HalPinRead(id=MachineId(id=machine_id), pin_name=pin_name)
        return await stub[0].ReadHalPin(request)

    async def write_hal_pin(
        self,
        machine_id: str,
        pin_name: str,
        float_value: Optional[float] = None,
        u32_value: Optional[int] = None,
        s32_value: Optional[int] = None,
        bit_value: Optional[bool] = None,
    ) -> Result:
        """Write a HAL pin value on a machine.
        
        Args:
            machine_id: Target machine identifier
            pin_name: HAL pin name (must be an output pin)
            float_value: Float value (for PIN_TYPE_FLOAT)
            u32_value: Unsigned 32-bit int value (for PIN_TYPE_U32)
            s32_value: Signed 32-bit int value (for PIN_TYPE_S32)
            bit_value: Boolean value (for PIN_TYPE_BIT)
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            request = HalPinWrite(
                 id=MachineId(id=machine_id),
                 pin_name=pin_name,
                 value_f=float_value if float_value is not None else 0.0,
                 value_u32=u32_value if u32_value is not None else 0,
                 value_s32=s32_value if s32_value is not None else 0,
                 value_bit=bit_value if bit_value is not None else False,
             )
            return await stub[0].WriteHalPin(request)
        except Exception as e:
            log.error("WriteHalPin '%s' failed for %s: %s", pin_name, machine_id, _error_details(e))
            raise

    async def get_errors(
        self,
        machine_id: str,
        limit: int = 50,
    ) -> ErrorList:
        """Get recent error events from a machine.
        
        Args:
            machine_id: Target machine identifier
            limit: Maximum number of errors to return
            
        Returns:
            ErrorList with error events
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            return await self._retry_read(
                "GetErrors",
                lambda: self._do_get_errors(machine_id, limit),
            )
        except Exception as e:
            log.error("GetErrors failed for %s: %s", machine_id, _error_details(e))
            raise

    async def _do_get_errors(self, machine_id: str, limit: int):
        stub = await self._get_fleet_stub(machine_id)
        request = GetErrorsRequest(id=MachineId(id=machine_id), limit=limit)
        return await stub[0].GetErrors(request)

    async def get_machine_info(self, machine_id: str) -> MachineInfo:
        """Get detailed machine information.
        
        Args:
            machine_id: Target machine identifier
            
        Returns:
            MachineInfo with version, joint count, HAL component count
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            return await self._retry_read(
                "GetMachineInfo",
                lambda: self._do_get_machine_info(machine_id),
            )
        except Exception as e:
            log.error("GetMachineInfo failed for %s: %s", machine_id, _error_details(e))
            raise

    async def _do_get_machine_info(self, machine_id: str):
        stub = await self._get_fleet_stub(machine_id)
        request = MachineId(id=machine_id)
        return await stub[0].GetMachineInfo(request)

    async def get_ini_param(
        self,
        machine_id: str,
        section: str,
        option: str,
    ) -> IniParamValue:
        """Read an INI configuration parameter.
        
        Args:
            machine_id: Target machine identifier
            section: INI section name
            option: INI option/key name
            
        Returns:
            IniParamValue with the parameter value
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            return await self._retry_read(
                "GetIniParam",
                lambda: self._do_get_ini_param(machine_id, section, option),
            )
        except Exception as e:
            log.error("GetIniParam [%s]%s failed for %s: %s",
                      section, option, machine_id, _error_details(e))
            raise

    async def _do_get_ini_param(self, machine_id: str, section: str, option: str):
        stub = await self._get_fleet_stub(machine_id)
        request = IniParamRequest(id=MachineId(id=machine_id), section=section, option=option)
        return await stub[0].GetIniParam(request)

    async def set_machine_state(self, machine_id: str, state: int) -> Result:
        """Set machine control state (e-stop reset, power on/off).
        
        Admin-only operation. Requires admin role.
        
        Args:
            machine_id: Target machine identifier
            state: MachineControlState value
                STATE_ESTOP=0, STATE_ESTOP_RESET=1, STATE_OFF=2, STATE_ON=3
            
        Returns:
            Result with success status and error code if applicable
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub = await self._get_fleet_stub(machine_id)
            request = MachineStateCommand(
                id=MachineId(id=machine_id),
                state=state,
            )
            return await stub[0].SetMachineState(request)
        except Exception as e:
            log.error("SetMachineState failed for %s: %s", machine_id, _error_details(e))
            raise

    # -----------------------------------------------------------------------
    # FleetService — streaming subscriptions
    # -----------------------------------------------------------------------

    async def subscribe_status(
        self,
        machine_id: str,
    ) -> AsyncGenerator[MachineStatus, None]:
        """Stream status updates for a single machine.
        
        Args:
            machine_id: Target machine identifier
            
        Yields:
            MachineStatus messages as they arrive
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub, _, _ = await self._get_fleet_stub(machine_id)
            request = MachineId(id=machine_id)
            call = stub.SubscribeStatus(request)
            async for status in call:
                yield status
        except Exception as e:
            log.error("SubscribeStatus failed for %s: %s", machine_id, _error_details(e))
            raise

    async def subscribe_hal_pins(
        self,
        machine_id: str,
        pin_names: list[str],
        poll_interval: float = 0.5,
    ) -> AsyncGenerator[HalPinUpdate, None]:
        """Stream HAL pin value updates for specified pins.
        
        Args:
            machine_id: Target machine identifier
            pin_names: List of HAL pin names to subscribe to
            poll_interval: Poll interval in seconds
            
        Yields:
            HalPinUpdate messages as they arrive
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub, _, _ = await self._get_fleet_stub(machine_id)
            request = HalPinSubscribe(
                id=MachineId(id=machine_id),
                pin_names=pin_names,
                poll_interval_seconds=poll_interval,
            )
            call = stub.SubscribeHalPins(request)
            async for update in call:
                yield update
        except Exception as e:
            log.error("SubscribeHalPins failed for %s: %s", machine_id, _error_details(e))
            raise

    async def subscribe_errors(
        self,
        machine_id: str,
    ) -> AsyncGenerator[ErrorEvent, None]:
        """Stream error events from a machine.
        
        Args:
            machine_id: Target machine identifier
            
        Yields:
            ErrorEvent messages as they arrive
        """
        if self._closed:
            raise RuntimeError("Client is closed")

        try:
            stub, _, _ = await self._get_fleet_stub(machine_id)
            request = MachineId(id=machine_id)
            call = stub.SubscribeErrors(request)
            async for error in call:
                yield error
        except Exception as e:
            log.error("SubscribeErrors failed for %s: %s", machine_id, _error_details(e))
            raise
