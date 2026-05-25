"""FleetClient — high-level async client for LinuxCNC fleet management."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional

import grpc

from fleet_client.auth import BearerAuthInterceptor, create_auth_interceptor
from linuxcnc_fleet.fleet_pb2 import (
    BroadcastRequest,
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
    MachineId,
    MachineInfo,
    MachineStatus,
    MdiCommand,
    PositionRequest,
    PositionResponse,
    ProgramPath,
    Result,
    SetModeRequest,
    SubscribeAllRequest,
    TrajAxis,
)

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
        
        # Gateway channel with auth interceptor
        if _gateway_channel is not None:
            self._gateway_channel = _gateway_channel
        else:
            self._gateway_channel = self._create_gateway_channel()
        if _gateway_stub is not None:
            self._gateway_stub = _gateway_stub
        else:
            self._gateway_stub = grpc.aio.FleetGatewayServiceStub(self._gateway_channel)
        self._fleet_stub_factory = _fleet_stub_factory or (
            lambda ch: grpc.aio.FleetServiceStub(ch)
        )
        
        # Machine channel cache
        self._machine_channels: dict[str, _CachedChannel] = {}
        self._cache_lock = threading.Lock()
        
        # Background cleanup task
        self._cleanup_task: Optional[asyncio.Task] = None

    def _create_gateway_channel(self) -> grpc.Channel:
        """Create the gRPC channel to the gateway server."""
        auth_interceptor = create_auth_interceptor(self._token)
        if self._tls_enabled:
            creds = grpc.ssl_channel_credentials()
            channel = grpc.secure_channel(
                self._gateway_address,
                creds,
            )
        else:
            channel = grpc.insecure_channel(
                self._gateway_address,
            )
        return grpc.intercept_channel(channel, auth_interceptor)

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
                channel = grpc.insecure_channel(
                    f"{address}:{port}",
                    interceptors=[create_auth_interceptor(self._token)],
                )
            else:
                channel = grpc.insecure_channel(f"{address}:{port}")
            
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
        return self

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
            log.error("DiscoverMachines failed: %s", e.details())
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
        
        try:
            request = MachineId(id=machine_id)
            response: GatewayRoute = await self._gateway_stub.RouteMachine(request)
            return (response.instance_address, response.instance_port)
        except Exception as e:
            log.error("RouteMachine failed for %s: %s", machine_id, e.details())
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
            command_type: Command type - "mdi", "execution", or "mode"
            command_value: Command-specific value (string for MDI, int for execution/mode)
            facility: Facility filter (required for FACILITY scope)
            tags: Tag filter (required for TAG scope)
            
        Returns:
            Dict mapping machine_id to (success: bool, message: str)
        """
        if self._closed:
            raise RuntimeError("Client is closed")
        
        # Map scope string to proto enum value
        scope_map = {"ALL": 0, "FACILITY": 1, "TAG": 2}
        scope_value = scope_map.get(scope, 0)
        
        # Build broadcast request
        request = BroadcastRequest(
            scope=scope_value,
            facility=facility or "",
            tags=tags or [],
        )
        
        # Set command based on type
        if command_type == "mdi":
            request.mdi.command = str(command_value)
        elif command_type in ("execution", "mode"):
            request.exec.state = int(command_value) if command_type == "execution" else int(command_value)
        else:
            raise ValueError(f"Unknown command type: {command_type}")
        
        try:
            response = await self._gateway_stub.BroadcastCommand(request)
            
            results = {}
            for machine_id, result in response.results.items():
                results[machine_id] = (result.success, result.message)
            
            return results
        except Exception as e:
            log.error("BroadcastCommand failed: %s", e.details())
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
        
        try:
            request = SubscribeAllRequest(
                facility=facility or "",
                poll_interval_seconds=poll_interval,
            )
            
            call = self._gateway_stub.SubscribeAllStatus(request)
            async for status in call:
                yield (status.machine_id, status)
        except Exception as e:
            log.error("SubscribeAllStatus failed: %s", e.details())
            raise

    # -----------------------------------------------------------------------
    # Helper — route machine_id to fleet channel + stub
    # -----------------------------------------------------------------------

    async def _get_fleet_stub(
        self, machine_id: str
    ) -> tuple[grpc.aio.FleetServiceStub, str, int]:
        """Route a machine and return (FleetServiceStub, address, port).
        
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
                        rpc_name, attempt + 1, _MAX_RETRIES, backoff, e.details(),
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
            log.error("SetMode failed for %s: %s", machine_id, e.details())
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
            log.error("SetExecution failed for %s: %s", machine_id, e.details())
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
            log.error("Start failed for %s: %s", machine_id, e.details())
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
            log.error("Stop failed for %s: %s", machine_id, e.details())
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
            log.error("FeedHold failed for %s: %s", machine_id, e.details())
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
            log.error("Continue failed for %s: %s", machine_id, e.details())
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
            log.error("HomeAll failed for %s: %s", machine_id, e.details())
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
            log.error("StepForward failed for %s: %s", machine_id, e.details())
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
            log.error("SendMDI failed for %s: %s", machine_id, e.details())
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
            log.error("HomeAxis(%d) failed for %s: %s", axis, machine_id, e.details())
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
            log.error("LoadProgram(%s) failed for %s: %s", path, machine_id, e.details())
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
            log.error("GetPosition failed for %s: %s", machine_id, e.details())
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
            log.error("ListHalComponents failed for %s: %s", machine_id, e.details())
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
            log.error("ReadHalPin '%s' failed for %s: %s", pin_name, machine_id, e.details())
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
                value_f=float_value or 0.0,
                value_u32=u32_value or 0,
                value_s32=s32_value or 0,
                value_bit=bit_value or False,
            )
            return await stub[0].WriteHalPin(request)
        except Exception as e:
            log.error("WriteHalPin '%s' failed for %s: %s", pin_name, machine_id, e.details())
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
            log.error("GetErrors failed for %s: %s", machine_id, e.details())
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
            log.error("GetMachineInfo failed for %s: %s", machine_id, e.details())
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
                      section, option, machine_id, e.details())
            raise

    async def _do_get_ini_param(self, machine_id: str, section: str, option: str):
        stub = await self._get_fleet_stub(machine_id)
        request = IniParamRequest(id=MachineId(id=machine_id), section=section, option=option)
        return await stub[0].GetIniParam(request)

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
            log.error("SubscribeStatus failed for %s: %s", machine_id, e.details())
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
            log.error("SubscribeHalPins failed for %s: %s", machine_id, e.details())
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
            log.error("SubscribeErrors failed for %s: %s", machine_id, e.details())
            raise
