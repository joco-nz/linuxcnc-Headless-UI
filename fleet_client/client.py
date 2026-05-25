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
    ExecutionCommand,
    GatewayRoute,
    MachineId,
    MachineInfo,
    MdiCommand,
    SetModeRequest,
    SubscribeAllRequest,
)

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
    ) -> None:
        """Initialize FleetClient.
        
        Args:
            gateway_address: Gateway server address (host:port)
            token: OIDC access token for authentication
            tls_enabled: Whether to use TLS for gateway connection
            machine_channel_ttl: Time-to-live for cached machine channels (seconds)
        """
        self._gateway_address = gateway_address
        self._token = token
        self._tls_enabled = tls_enabled
        self._machine_channel_ttl = machine_channel_ttl
        self._closed = False
        
        # Gateway channel with auth interceptor
        self._gateway_channel = self._create_gateway_channel()
        self._gateway_stub = grpc.aio.FleetGatewayServiceStub(self._gateway_channel)
        
        # Machine channel cache
        self._machine_channels: dict[str, _CachedChannel] = {}
        self._cache_lock = threading.Lock()
        
        # Background cleanup task
        self._cleanup_task: Optional[asyncio.Task] = None

    def _create_gateway_channel(self) -> grpc.Channel:
        """Create the gRPC channel to the gateway server."""
        if self._tls_enabled:
            creds = grpc.ssl_channel_credentials()
            return grpc.secure_channel(
                self._gateway_address,
                creds,
                interceptors=[create_auth_interceptor(self._token)],
            )
        else:
            return grpc.insecure_channel(
                self._gateway_address,
                interceptors=[create_auth_interceptor(self._token)],
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
        except grpc.aio.AioRpcError as e:
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
        except grpc.aio.AioRpcError as e:
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
        except grpc.aio.AioRpcError as e:
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
        except grpc.aio.AioRpcError as e:
            log.error("SubscribeAllStatus failed: %s", e.details())
            raise
