"""Tests for FleetClient — routing, channel caching, retry, streaming."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import grpc
import pytest

from fleet_client.client import (
    FleetClient,
    _INITIAL_BACKOFF,
    _MAX_RETRIES,
)


class FakeAioRpcError(Exception):
    """Fake gRPC error for testing retry logic."""

    def __init__(self, code_value, details_msg):
        super().__init__(details_msg)
        self._code = code_value
        self._details = details_msg

    def code(self):
        return self._code

    def details(self):
        return self._details


# ── Channel caching tests ──────────────────────────────────────────────

class TestChannelCaching:

    def test_get_or_create_creates_new_channel(self):
        """First call creates a new channel."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_channel = Mock()
            mock_grpc.aio.insecure_channel.return_value = mock_channel
            MockAioGatewayStub.return_value = Mock()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            try:
                ch = asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))
                assert ch is mock_channel
                # Called once for gateway + once for machine channel
                assert mock_grpc.aio.insecure_channel.call_count == 1
            finally:
                asyncio.run(client.close())

    def test_get_or_create_returns_cached_channel(self):
        """Second call returns the cached channel."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_channel = Mock()
            mock_grpc.aio.insecure_channel.return_value = mock_channel
            MockAioGatewayStub.return_value = Mock()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            try:
                ch1 = asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))
                ch2 = asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))
                assert ch1 is ch2
                # Called once for gateway + once for machine channel
                assert mock_grpc.aio.insecure_channel.call_count == 1
            finally:
                asyncio.run(client.close())

    def test_get_or_create_different_address_new_channel(self):
        """Different address creates a new channel."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_gw_channel = Mock()
            mock_channel1 = Mock()
            mock_channel2 = Mock()
            mock_grpc.aio.insecure_channel.side_effect = [mock_gw_channel, mock_channel1, mock_channel2]
            MockAioGatewayStub.return_value = Mock()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            try:
                ch1 = asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))
                ch2 = asyncio.run(client._get_or_create_machine_channel("10.0.0.2", 5007))
                assert ch1 is not ch2
            finally:
                asyncio.run(client.close())

    def test_cache_key_is_host_port(self):
        """Cache key combines address and port."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_gw_channel = Mock()
            mock_channel1 = Mock()
            mock_channel2 = Mock()
            mock_grpc.aio.insecure_channel.side_effect = [mock_gw_channel, mock_channel1, mock_channel2]
            MockAioGatewayStub.return_value = Mock()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            try:
                ch1 = asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))
                ch2 = asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5008))
                assert ch1 is not ch2
            finally:
                asyncio.run(client.close())

    def test_ttl_expiry_closes_and_recreates(self):
        """Expired channel is closed and a new one created."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_channel = Mock()
            mock_grpc.aio.insecure_channel.return_value = mock_channel
            MockAioGatewayStub.return_value = Mock()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
                machine_channel_ttl=0.01,  # 10ms TTL
            )
            try:
                ch1 = asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))
                assert ch1 is mock_channel

                # Wait for TTL expiry
                asyncio.run(asyncio.sleep(0.05))

                mock_channel2 = Mock()
                mock_grpc.aio.insecure_channel.return_value = mock_channel2

                ch2 = asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))
                assert ch2 is mock_channel2
                assert ch1 is not ch2
            finally:
                asyncio.run(client.close())

    def test_cleanup_expired_channels_removes_old(self):
        """_cleanup_expired_channels removes expired entries."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_channel = Mock()
            mock_grpc.aio.insecure_channel.return_value = mock_channel
            MockAioGatewayStub.return_value = Mock()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
                machine_channel_ttl=1.0,
            )
            try:
                ch = asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))

                # Manually expire the cache entry
                with client._cache_lock:
                    key = "10.0.0.1:5007"
                    cached = client._machine_channels[key]
                    cached.created_at = 0.0  # ancient timestamp

                client._cleanup_expired_channels()
                assert len(client._machine_channels) == 0
            finally:
                asyncio.run(client.close())


# ── GatewayService RPC tests ───────────────────────────────────────────

class TestGatewayRpcWrappers:

    def test_get_machines_returns_list(self):
        """get_machines returns parsed MachineEntry list."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_stub = AsyncMock()
            MockAioGatewayStub.return_value = mock_stub

            mock_response = MagicMock()
            mock_response.machines = [MagicMock(machine_id="m1", machine_name="n1",
                                                 host_address="10.0.0.1", version="v1",
                                                 num_joints=4, num_hal_components=2)]
            mock_stub.DiscoverMachines = AsyncMock(return_value=mock_response)

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            try:
                machines = asyncio.run(client.get_machines(facility="factory-a"))
                assert len(machines) == 1
                assert machines[0].machine_id == "m1"
                assert machines[0].machine_name == "n1"
                assert machines[0].host_address == "10.0.0.1"
            finally:
                asyncio.run(client.close())

    def test_route_machine_returns_tuple(self):
        """route_machine returns (address, port) tuple."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_stub = AsyncMock()
            MockAioGatewayStub.return_value = mock_stub

            mock_response = MagicMock(instance_address="10.0.0.5", instance_port=5007)
            mock_stub.RouteMachine = AsyncMock(return_value=mock_response)

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            try:
                addr, port = asyncio.run(client.route_machine("machine-42"))
                assert addr == "10.0.0.5"
                assert port == 5007
            finally:
                asyncio.run(client.close())

    def test_broadcast_command_all_scope(self):
        """broadcast_command with ALL scope sends correct request."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_stub = AsyncMock()
            MockAioGatewayStub.return_value = mock_stub

            mock_result = MagicMock(success=True, message="ok")
            mock_response = MagicMock()
            mock_response.results = {"m1": mock_result}
            mock_stub.BroadcastCommand = AsyncMock(return_value=mock_response)

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            try:
                results = asyncio.run(client.broadcast_command(scope="ALL", command_type="mdi",
                                                               command_value="G0 X1.0"))
                assert results == {"m1": (True, "ok")}
            finally:
                asyncio.run(client.close())

    def test_broadcast_mdi_convenience(self):
        """broadcast_mdi delegates to broadcast_command."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_stub = AsyncMock()
            MockAioGatewayStub.return_value = mock_stub

            mock_result = MagicMock(success=True, message="ok")
            mock_response = MagicMock()
            mock_response.results = {"m1": mock_result}
            mock_stub.BroadcastCommand = AsyncMock(return_value=mock_response)

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            try:
                results = asyncio.run(client.broadcast_mdi(scope="FACILITY", command="G0 X1.0",
                                                            facility="factory-a"))
                assert "m1" in results
            finally:
                asyncio.run(client.close())


# ── Closed client tests ────────────────────────────────────────────────

class TestClosedClient:

    def test_get_machines_raises_when_closed(self):
        """get_machines raises RuntimeError when client is closed."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            MockFleetGatewayServiceStub = Mock()
            mock_grpc.aio.insecure_channel = Mock()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            asyncio.run(client.close())
            with pytest.raises(RuntimeError, match="Client is closed"):
                asyncio.run(client.get_machines())

    def test_route_machine_raises_when_closed(self):
        """route_machine raises RuntimeError when client is closed."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            MockFleetGatewayServiceStub = Mock()
            mock_grpc.aio.insecure_channel = Mock()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            asyncio.run(client.close())
            with pytest.raises(RuntimeError, match="Client is closed"):
                asyncio.run(client.route_machine("m1"))

    def test_get_status_raises_when_closed(self):
        """get_status raises RuntimeError when client is closed."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_grpc.aio.FleetServiceStub = Mock()
            MockFleetGatewayServiceStub = Mock()
            mock_grpc.aio.insecure_channel = Mock()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            asyncio.run(client.close())
            with pytest.raises(RuntimeError, match="Client is closed"):
                asyncio.run(client.get_status("m1"))

    def test_set_mode_raises_when_closed(self):
        """set_mode raises RuntimeError when client is closed."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_grpc.aio.FleetServiceStub = Mock()
            MockFleetGatewayServiceStub = Mock()
            mock_grpc.aio.insecure_channel = Mock()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            asyncio.run(client.close())
            with pytest.raises(RuntimeError, match="Client is closed"):
                asyncio.run(client.set_mode("m1", 2))

    def test_subscribe_status_raises_when_closed(self):
        """subscribe_status raises RuntimeError when client is closed."""
        with patch("fleet_client.client.grpc") as mock_grpc, \
            patch("fleet_client.client._AioFleetGatewayServiceStub") as MockAioGatewayStub:
            mock_grpc.aio.FleetServiceStub = Mock()
            MockFleetGatewayServiceStub = Mock()
            mock_grpc.aio.insecure_channel = Mock()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            asyncio.run(client.close())

            async def _try():
                async for _ in client.subscribe_status("m1"):
                    pass

            with pytest.raises(RuntimeError, match="Client is closed"):
                asyncio.run(_try())


# ── FleetService RPC wrapper tests ─────────────────────────────────────

class TestFleetServiceWrappers:

    def _make_fleet_client_with_stubs(self):
        """Create a client with injected mock stubs (no grpc module patching needed)."""
        fleet_stub_mock = AsyncMock()
        gateway_stub_mock = AsyncMock()
        mock_channel = MagicMock()

        # Factory that returns our fleet stub mock for any channel
        def fleet_factory(ch):
            return fleet_stub_mock

        client = FleetClient(
            gateway_address="127.0.0.1:50051",
            token="fake-token",
            tls_enabled=False,
            _gateway_stub=gateway_stub_mock,
            _fleet_stub_factory=fleet_factory,
            _gateway_channel=mock_channel,
        )
        yield client, fleet_stub_mock, gateway_stub_mock
        asyncio.run(client.close())

    def test_get_status_calls_fleet_service(self):
        """get_status routes to FleetService.GetStatus."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_status = MagicMock(machine_id="m1", state=3)
            fleet_stub.GetStatus = AsyncMock(return_value=mock_status)

            # Mock route_machine response
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            status = asyncio.run(client.get_status("m1"))
            assert status is mock_status
            fleet_stub.GetStatus.assert_called_once()
            call_args = fleet_stub.GetStatus.call_args
            assert call_args[0][0].id == "m1"

    def test_set_mode_calls_fleet_service(self):
        """set_mode routes to FleetService.SetMode."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_result = MagicMock(success=True, message="ok", error_code=0)
            fleet_stub.SetMode = AsyncMock(return_value=mock_result)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.set_mode("m1", 2))
            assert result is mock_result
            call_args = fleet_stub.SetMode.call_args[0][0]
            assert call_args.id.id == "m1"
            assert call_args.mode == 2

    def test_set_execution_calls_fleet_service(self):
        """set_execution routes to FleetService.SetExecution."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_result = MagicMock(success=True, message="ok", error_code=0)
            fleet_stub.SetExecution = AsyncMock(return_value=mock_result)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.set_execution("m1", 1))
            call_args = fleet_stub.SetExecution.call_args[0][0]
            assert call_args.id.id == "m1"
            assert call_args.state == 1

    def test_start_calls_fleet_service(self):
        """start routes to FleetService.Start."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_result = MagicMock(success=True, message="ok", error_code=0)
            fleet_stub.Start = AsyncMock(return_value=mock_result)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.start("m1"))
            assert result is mock_result
            fleet_stub.Start.assert_called_once()

    def test_stop_calls_fleet_service(self):
        """stop routes to FleetService.Stop."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_result = MagicMock(success=True, message="ok", error_code=0)
            fleet_stub.Stop = AsyncMock(return_value=mock_result)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.stop("m1"))
            assert result is mock_result
            fleet_stub.Stop.assert_called_once()

    def test_feed_hold_calls_fleet_service(self):
        """feed_hold routes to FleetService.FeedHold."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_result = MagicMock(success=True, message="ok", error_code=0)
            fleet_stub.FeedHold = AsyncMock(return_value=mock_result)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.feed_hold("m1"))
            assert result is mock_result
            fleet_stub.FeedHold.assert_called_once()

    def test_continue_exec_calls_fleet_service(self):
        """continue_exec routes to FleetService.Continue."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_result = MagicMock(success=True, message="ok", error_code=0)
            fleet_stub.Continue = AsyncMock(return_value=mock_result)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.continue_exec("m1"))
            assert result is mock_result
            fleet_stub.Continue.assert_called_once()

    def test_home_all_calls_fleet_service(self):
        """home_all routes to FleetService.HomeAll."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_result = MagicMock(success=True, message="ok", error_code=0)
            fleet_stub.HomeAll = AsyncMock(return_value=mock_result)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.home_all("m1"))
            assert result is mock_result
            fleet_stub.HomeAll.assert_called_once()

    def test_step_forward_calls_fleet_service(self):
        """step_forward routes to FleetService.StepForward."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_result = MagicMock(success=True, message="ok", error_code=0)
            fleet_stub.StepForward = AsyncMock(return_value=mock_result)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.step_forward("m1"))
            assert result is mock_result
            fleet_stub.StepForward.assert_called_once()

    def test_send_mdi_calls_fleet_service(self):
        """send_mdi routes to FleetService.SendMdiCommand."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_result = MagicMock(success=True, message="ok", error_code=0)
            fleet_stub.SendMdiCommand = AsyncMock(return_value=mock_result)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.send_mdi("m1", "G0 X1.0"))
            assert result is mock_result
            call_args = fleet_stub.SendMdiCommand.call_args[0][0]
            assert call_args.id.id == "m1"
            assert call_args.command == "G0 X1.0"

    def test_home_axis_calls_fleet_service(self):
        """home_axis routes to FleetService.HomeAxis."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_result = MagicMock(success=True, message="ok", error_code=0)
            fleet_stub.HomeAxis = AsyncMock(return_value=mock_result)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.home_axis("m1", 2))
            assert result is mock_result
            call_args = fleet_stub.HomeAxis.call_args[0][0]
            assert call_args.id.id == "m1"
            assert call_args.axis == 2

    def test_load_program_calls_fleet_service(self):
        """load_program routes to FleetService.LoadProgram."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_result = MagicMock(success=True, message="ok", error_code=0)
            fleet_stub.LoadProgram = AsyncMock(return_value=mock_result)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.load_program("m1", "/path/to/program.ngc"))
            assert result is mock_result
            call_args = fleet_stub.LoadProgram.call_args[0][0]
            assert call_args.id.id == "m1"
            assert call_args.path == "/path/to/program.ngc"

    def test_get_position_calls_fleet_service(self):
        """get_position routes to FleetService.GetPosition."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_response = MagicMock()
            fleet_stub.GetPosition = AsyncMock(return_value=mock_response)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.get_position("m1", position_type=0))
            assert result is mock_response
            call_args = fleet_stub.GetPosition.call_args[0][0]
            assert call_args.id.id == "m1"

    def test_list_hal_components_calls_fleet_service(self):
        """list_hal_components routes to FleetService.ListHalComponents."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_response = MagicMock()
            fleet_stub.ListHalComponents = AsyncMock(return_value=mock_response)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.list_hal_components("m1"))
            assert result is mock_response

    def test_read_hal_pin_calls_fleet_service(self):
        """read_hal_pin routes to FleetService.ReadHalPin."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_value = MagicMock(pin_name="spindle.speed", type=3)
            fleet_stub.ReadHalPin = AsyncMock(return_value=mock_value)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.read_hal_pin("m1", "spindle.speed"))
            assert result is mock_value
            call_args = fleet_stub.ReadHalPin.call_args[0][0]
            assert call_args.pin_name == "spindle.speed"

    def test_write_hal_pin_calls_fleet_service(self):
        """write_hal_pin routes to FleetService.WriteHalPin."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_result = MagicMock(success=True, message="ok", error_code=0)
            fleet_stub.WriteHalPin = AsyncMock(return_value=mock_result)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.write_hal_pin("m1", "output.pin", bit_value=True))
            assert result is mock_result
            call_args = fleet_stub.WriteHalPin.call_args[0][0]
            assert call_args.pin_name == "output.pin"
            assert call_args.value_bit is True

    def test_get_errors_calls_fleet_service(self):
        """get_errors routes to FleetService.GetErrors."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_response = MagicMock()
            fleet_stub.GetErrors = AsyncMock(return_value=mock_response)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.get_errors("m1", limit=25))
            assert result is mock_response
            call_args = fleet_stub.GetErrors.call_args[0][0]
            assert call_args.limit == 25

    def test_get_machine_info_calls_fleet_service(self):
        """get_machine_info routes to FleetService.GetMachineInfo."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_response = MagicMock(machine_id="m1", machine_name="lathe-1")
            fleet_stub.GetMachineInfo = AsyncMock(return_value=mock_response)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.get_machine_info("m1"))
            assert result is mock_response

    def test_get_ini_param_calls_fleet_service(self):
        """get_ini_param routes to FleetService.GetIniParam."""
        for client, fleet_stub, gw_stub in self._make_fleet_client_with_stubs():
            mock_response = MagicMock(value="100.0")
            fleet_stub.GetIniParam = AsyncMock(return_value=mock_response)
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            result = asyncio.run(client.get_ini_param("m1", "TRAJ", "COORDINATES"))
            assert result is mock_response
            call_args = fleet_stub.GetIniParam.call_args[0][0]
            assert call_args.section == "TRAJ"
            assert call_args.option == "COORDINATES"


# ── Retry logic tests ──────────────────────────────────────────────────

class TestRetryLogic:

    def test_retry_succeeds_after_failures(self):
        """Read RPC retries and succeeds on later attempt."""
        mock_machine_channel = MagicMock()

        fleet_stub_mock = AsyncMock()
        gateway_stub_mock = AsyncMock()
        mock_gw_channel = MagicMock()

        call_count = [0]

        async def failing_get_status(request):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise FakeAioRpcError(grpc.StatusCode.UNAVAILABLE, "service unavailable")
            return MagicMock(machine_id="m1", state=3)

        fleet_stub_mock.GetStatus = failing_get_status

        def fleet_factory(ch):
            return fleet_stub_mock

        gateway_stub_mock.RouteMachine = AsyncMock(
            return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
        )

        client = FleetClient(
            gateway_address="127.0.0.1:50051",
            token="fake-token",
            tls_enabled=False,
            _gateway_stub=gateway_stub_mock,
            _fleet_stub_factory=fleet_factory,
            _gateway_channel=mock_gw_channel,
        )

        try:
            with patch.object(client, "_get_or_create_machine_channel", new_callable=AsyncMock) as mock_ch:
                mock_ch.return_value = mock_machine_channel
                status = asyncio.run(client.get_status("m1"))
                assert status.machine_id == "m1"
                assert call_count[0] == 3  # 2 failures + 1 success
        finally:
            asyncio.run(client.close())

    def test_retry_max_exceeded_raises(self):
        """Read RPC raises after exhausting all retries."""
        mock_machine_channel = MagicMock()

        fleet_stub_mock = AsyncMock()
        gateway_stub_mock = AsyncMock()
        mock_gw_channel = MagicMock()

        call_count = [0]

        async def always_fail(request):
            call_count[0] += 1
            raise FakeAioRpcError(grpc.StatusCode.UNAVAILABLE, "service unavailable")

        fleet_stub_mock.GetStatus = always_fail
        gateway_stub_mock.RouteMachine = AsyncMock(
            return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
        )

        client = FleetClient(
            gateway_address="127.0.0.1:50051",
            token="fake-token",
            tls_enabled=False,
            _gateway_stub=gateway_stub_mock,
            _fleet_stub_factory=lambda ch: fleet_stub_mock,
            _gateway_channel=mock_gw_channel,
        )

        try:
            with patch.object(client, "_get_or_create_machine_channel", new_callable=AsyncMock) as mock_ch:
                mock_ch.return_value = mock_machine_channel
                with pytest.raises(FakeAioRpcError):
                    asyncio.run(client.get_status("m1"))
                assert call_count[0] == _MAX_RETRIES
        finally:
            asyncio.run(client.close())

    def test_non_retryable_error_not_retried(self):
        """Non-retryable errors (e.g. PERMISSION_DENIED) are raised immediately."""
        mock_machine_channel = MagicMock()

        fleet_stub_mock = AsyncMock()
        gateway_stub_mock = AsyncMock()
        mock_gw_channel = MagicMock()

        call_count = [0]

        async def permission_denied(request):
            call_count[0] += 1
            raise FakeAioRpcError(grpc.StatusCode.PERMISSION_DENIED, "access denied")

        fleet_stub_mock.GetStatus = permission_denied
        gateway_stub_mock.RouteMachine = AsyncMock(
            return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
        )

        client = FleetClient(
            gateway_address="127.0.0.1:50051",
            token="fake-token",
            tls_enabled=False,
            _gateway_stub=gateway_stub_mock,
            _fleet_stub_factory=lambda ch: fleet_stub_mock,
            _gateway_channel=mock_gw_channel,
        )

        try:
            with patch.object(client, "_get_or_create_machine_channel", new_callable=AsyncMock) as mock_ch:
                mock_ch.return_value = mock_machine_channel
                with pytest.raises(FakeAioRpcError) as exc_info:
                    asyncio.run(client.get_status("m1"))
                assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED
                assert call_count[0] == 1
        finally:
            asyncio.run(client.close())


# ── Streaming subscription tests ───────────────────────────────────────

class TestStreamingSubscriptions:

    @pytest.fixture
    def stream_client(self):
        """Create a client with injected mock stubs for streaming."""
        fleet_stub_mock = AsyncMock()
        gateway_stub_mock = AsyncMock()
        mock_channel = MagicMock()

        client = FleetClient(
            gateway_address="127.0.0.1:50051",
            token="fake-token",
            tls_enabled=False,
            _gateway_stub=gateway_stub_mock,
            _fleet_stub_factory=lambda ch: fleet_stub_mock,
            _gateway_channel=mock_channel,
        )
        yield client, fleet_stub_mock, gateway_stub_mock
        asyncio.run(client.close())

    def test_subscribe_status_yields_statuses(self, stream_client):
        """subscribe_status yields MachineStatus from the stream."""
        client, fleet_stub, gw_stub = stream_client
        mock_status1 = MagicMock(machine_id="m1", state=3)
        mock_status2 = MagicMock(machine_id="m1", state=4)

        async def stream_statuses(request):
            yield mock_status1
            yield mock_status2

        fleet_stub.SubscribeStatus = stream_statuses
        gw_stub.RouteMachine = AsyncMock(
            return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
        )

        async def _collect():
            collected = []
            async for status in client.subscribe_status("m1"):
                collected.append(status)
            return collected

        collected = asyncio.run(_collect())
        assert len(collected) == 2
        assert collected[0].machine_id == "m1"
        assert collected[0].state == 3
        assert collected[1].state == 4

    def test_subscribe_hal_pins_yields_updates(self, stream_client):
        """subscribe_hal_pins yields HalPinUpdate from the stream."""
        client, fleet_stub, gw_stub = stream_client
        mock_update = MagicMock(pin_name="spindle.speed")

        async def stream_updates(request):
            yield mock_update

        fleet_stub.SubscribeHalPins = stream_updates
        gw_stub.RouteMachine = AsyncMock(
            return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
        )

        async def _collect():
            collected = []
            async for update in client.subscribe_hal_pins("m1", ["spindle.speed"]):
                collected.append(update)
            return collected

        collected = asyncio.run(_collect())
        assert len(collected) == 1
        assert collected[0].pin_name == "spindle.speed"

    def test_subscribe_errors_yields_events(self, stream_client):
        """subscribe_errors yields ErrorEvent from the stream."""
        client, fleet_stub, gw_stub = stream_client
        mock_error = MagicMock(message="E-stop triggered", timestamp=1234567.0)

        async def stream_errors(request):
            yield mock_error

        fleet_stub.SubscribeErrors = stream_errors
        gw_stub.RouteMachine = AsyncMock(
            return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
        )

        async def _collect():
            collected = []
            async for error in client.subscribe_errors("m1"):
                collected.append(error)
            return collected

        collected = asyncio.run(_collect())
        assert len(collected) == 1
        assert "E-stop" in collected[0].message

    def test_subscribe_status_calls_correct_proto_request(self, stream_client):
        """subscribe_status sends MachineId request with correct id."""
        client, fleet_stub, gw_stub = stream_client
        captured_requests = []

        async def capture_and_stream(request):
            captured_requests.append(request)
            return
            yield  # make it an async generator

        fleet_stub.SubscribeStatus = capture_and_stream
        gw_stub.RouteMachine = AsyncMock(
            return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
        )

        async def _collect():
            collected = []
            async for status in client.subscribe_status("m1"):
                collected.append(status)
            return collected

        asyncio.run(_collect())
        assert len(captured_requests) == 1
        assert captured_requests[0].id == "m1"

    def test_subscribe_hal_pins_sends_correct_request(self, stream_client):
        """subscribe_hal_pins sends HalPinSubscribe with correct pin names."""
        client, fleet_stub, gw_stub = stream_client
        captured_requests = []

        async def capture_and_stream(request):
            captured_requests.append(request)
            return
            yield  # make it an async generator

        fleet_stub.SubscribeHalPins = capture_and_stream
        gw_stub.RouteMachine = AsyncMock(
            return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
        )

        async def _collect():
            collected = []
            async for update in client.subscribe_hal_pins("m1", ["pin1", "pin2"], poll_interval=0.25):
                collected.append(update)
            return collected

        asyncio.run(_collect())
        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req.id.id == "m1"


# ── TLS channel tests ──────────────────────────────────────────────────

class TestTLSChannel:

    def test_tls_gateway_channel(self):
        """FleetClient creates secure channel when tls_enabled=True."""
        with patch("fleet_client.client.grpc") as mock_grpc:
            mock_base_channel = Mock()
            mock_grpc.aio.secure_channel.return_value = mock_base_channel
            mock_grpc.ssl_channel_credentials.return_value = MagicMock()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=True,
            )
            try:
                asyncio.run(client._ensure_gateway_channel())
                assert mock_grpc.aio.secure_channel.called
            finally:
                asyncio.run(client.close())

    def test_insecure_gateway_channel_by_default(self):
        """FleetClient creates insecure channel when tls_enabled=False."""
        with patch("fleet_client.client.grpc") as mock_grpc:
            mock_channel = Mock()
            mock_grpc.aio.insecure_channel.return_value = mock_channel

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            try:
                asyncio.run(client._ensure_gateway_channel())
                assert mock_grpc.aio.insecure_channel.called
                assert not mock_grpc.aio.secure_channel.called
            finally:
                asyncio.run(client.close())


# ── Context manager tests ──────────────────────────────────────────────

class TestContextManager:

    def test_async_context_manager(self):
        """FleetClient works as async context manager."""
        with patch("fleet_client.client.grpc") as mock_grpc:
            mock_channel = Mock()
            mock_grpc.aio.insecure_channel.return_value = mock_channel

            async def _test():
                async with FleetClient(
                    gateway_address="127.0.0.1:50051",
                    token="fake-token",
                    tls_enabled=False,
                ) as client:
                    assert client is not None
                    assert not client._closed
                assert client._closed

            asyncio.run(_test())

    def test_close_closes_all_channels(self):
        """close() closes gateway channel and all machine channels."""
        with patch("fleet_client.client.grpc") as mock_grpc:
            mock_channel = Mock()
            mock_grpc.aio.insecure_channel.return_value = mock_channel

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="fake-token",
                tls_enabled=False,
            )
            try:
                asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))
                asyncio.run(client.close())
            except Exception:
                pass


# ── Phase 2: Token refresh tests ─────────────────────────────────────────

class TestTokenRefresh:
    """Tests for FleetClient.refresh_token() — Phase 2."""

    def test_refresh_updates_token(self):
        """refresh_token() updates self._token to the new value."""
        with patch("fleet_client.client.grpc"):
            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token-abc123",
                tls_enabled=False,
            )
            try:
                assert client._token == "old-token-abc123"
                asyncio.run(client.refresh_token("new-token-def456"))
                assert client._token == "new-token-def456"
            finally:
                asyncio.run(client.close())

    def test_refresh_closes_gateway_channel(self):
        """refresh_token() closes the gateway channel and sets it to None."""
        with patch("fleet_client.client.grpc") as mock_grpc:
            mock_channel = Mock()
            mock_grpc.aio.insecure_channel.return_value = mock_channel

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=False,
            )
            try:
                asyncio.run(client._ensure_gateway_channel())
                assert client._gateway_channel is not None

                asyncio.run(client.refresh_token("new-token"))
                mock_channel.close.assert_called_once()
                assert client._gateway_channel is None
                assert client._gateway_stub is None
            finally:
                asyncio.run(client.close())

    def test_refresh_closes_all_machine_channels(self):
        """refresh_token() closes all cached machine channels."""
        with patch("fleet_client.client.grpc") as mock_grpc:
            mock_channel = Mock()
            mock_grpc.aio.insecure_channel.return_value = mock_channel

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=False,
            )
            try:
                asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))
                asyncio.run(client._get_or_create_machine_channel("10.0.0.2", 5007))
                assert len(client._machine_channels) == 2

                asyncio.run(client.refresh_token("new-token"))
                assert client._gateway_channel is None
            finally:
                asyncio.run(client.close())

    def test_refresh_raises_when_closed(self):
        """refresh_token() raises RuntimeError when client is closed."""
        with patch("fleet_client.client.grpc"):
            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=False,
            )
            try:
                asyncio.run(client.close())
                with pytest.raises(RuntimeError, match="Client is closed"):
                    asyncio.run(client.refresh_token("new-token"))
            finally:
                asyncio.run(client.close())

    def test_refresh_recreates_gateway_channel_on_next_use(self):
        """After refresh, the next gateway call recreates the channel with new token."""
        with patch("fleet_client.client.grpc") as mock_grpc:
            mock_ch1 = Mock()
            mock_ch2 = Mock()
            mock_grpc.aio.insecure_channel.side_effect = [mock_ch1, mock_ch2]

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=False,
            )
            try:
                # First channel created
                asyncio.run(client._ensure_gateway_channel())
                assert mock_grpc.aio.insecure_channel.call_count == 1

                # Refresh — closes old channel
                asyncio.run(client.refresh_token("new-token"))

                # Next use recreates with new interceptor
                asyncio.run(client._ensure_gateway_channel())
                assert mock_grpc.aio.insecure_channel.call_count == 2
            finally:
                asyncio.run(client.close())

    def test_refresh_recreates_machine_channels_on_next_use(self):
        """After refresh, machine channels are recreated on next use with new token."""
        with patch("fleet_client.client.grpc") as mock_grpc:
            mock_ch1 = Mock()
            mock_ch2 = Mock()
            mock_grpc.aio.insecure_channel.side_effect = [mock_ch1, mock_ch2]

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=False,
            )
            try:
                # First machine channel created
                asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))
                assert mock_grpc.aio.insecure_channel.call_count == 1

                # Refresh — closes all channels
                asyncio.run(client.refresh_token("new-token"))

                # Next machine call recreates with new interceptor
                asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))
                assert mock_grpc.aio.insecure_channel.call_count == 2
            finally:
                asyncio.run(client.close())

    def test_refresh_with_tls(self):
        """refresh_token() works correctly with TLS enabled."""
        with patch("fleet_client.client.grpc") as mock_grpc:
            mock_channel = Mock()
            mock_grpc.aio.secure_channel.return_value = mock_channel

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=True,
            )
            try:
                asyncio.run(client._ensure_gateway_channel())
                assert mock_grpc.aio.secure_channel.call_count == 1

                asyncio.run(client.refresh_token("new-token"))
                mock_channel.close.assert_called_once()

                asyncio.run(client._ensure_gateway_channel())
                assert mock_grpc.aio.secure_channel.call_count == 2
            finally:
                asyncio.run(client.close())

    def test_refresh_multiple_times(self):
        """refresh_token() can be called multiple times sequentially."""
        with patch("fleet_client.client.grpc") as mock_grpc:
            mock_ch1 = Mock()
            mock_ch2 = Mock()
            mock_ch3 = Mock()
            mock_grpc.aio.insecure_channel.side_effect = [mock_ch1, mock_ch2, mock_ch3]

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="first-token",
                tls_enabled=False,
            )
            try:
                asyncio.run(client._ensure_gateway_channel())
                asyncio.run(client.refresh_token("second-token"))
                asyncio.run(client._ensure_gateway_channel())
                asyncio.run(client.refresh_token("third-token"))
                asyncio.run(client._ensure_gateway_channel())

                assert mock_grpc.aio.insecure_channel.call_count == 3
            finally:
                asyncio.run(client.close())

    def test_refresh_preserves_gateway_address(self):
        """refresh_token() preserves the gateway address after refresh."""
        with patch("fleet_client.client.grpc"):
            client = FleetClient(
                gateway_address="custom-gateway:9999",
                token="old-token",
                tls_enabled=False,
            )
            try:
                asyncio.run(client._ensure_gateway_channel())
                asyncio.run(client.refresh_token("new-token"))
                assert client._gateway_address == "custom-gateway:9999"
            finally:
                asyncio.run(client.close())

    def test_refresh_preserves_tls_setting(self):
        """refresh_token() preserves tls_enabled setting after refresh."""
        with patch("fleet_client.client.grpc"):
            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=True,
            )
            try:
                asyncio.run(client._ensure_gateway_channel())
                asyncio.run(client.refresh_token("new-token"))
                assert client._tls_enabled is True
            finally:
                asyncio.run(client.close())

    def test_refresh_preserves_machine_channel_ttl(self):
        """refresh_token() preserves machine_channel_ttl after refresh."""
        with patch("fleet_client.client.grpc"):
            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=False,
                machine_channel_ttl=600.0,
            )
            try:
                asyncio.run(client._ensure_gateway_channel())
                asyncio.run(client.refresh_token("new-token"))
                assert client._machine_channel_ttl == 600.0
            finally:
                asyncio.run(client.close())

    def test_refresh_logs_message(self, caplog):
        """refresh_token() logs a message about the token refresh."""
        with patch("fleet_client.client.grpc"):
            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token-abc123",
                tls_enabled=False,
            )
            try:
                asyncio.run(client._ensure_gateway_channel())
                with caplog.at_level(logging.INFO):
                    asyncio.run(client.refresh_token("new-token-def456"))
                assert "Token refreshed" in caplog.text
            finally:
                asyncio.run(client.close())

    def test_refresh_with_injected_gateway_channel(self):
        """refresh_token() handles pre-injected gateway channel gracefully."""
        with patch("fleet_client.client.grpc"):
            injected_channel = Mock()
            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=False,
                _gateway_channel=injected_channel,
            )
            try:
                # Even with injected channel, refresh should update the token
                asyncio.run(client.refresh_token("new-token"))
                assert client._token == "new-token"
                # Gateway interceptor is None when channel is injected externally
                assert client._gateway_interceptor is None
            finally:
                asyncio.run(client.close())


class TestTokenRefreshInterceptorPropagation:
    """Tests verifying that refresh propagates to gateway interceptor."""

    def test_gateway_interceptor_updated_on_channel_create(self):
        """Creating a new channel after refresh updates the gateway interceptor."""
        from fleet_client.auth import AioBearerAuthInterceptor

        with patch("fleet_client.client.grpc") as mock_grpc:
            mock_channel = Mock()
            mock_grpc.aio.insecure_channel.return_value = mock_channel

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="first-token",
                tls_enabled=False,
            )
            try:
                asyncio.run(client._ensure_gateway_channel())
                assert isinstance(client._gateway_interceptor, AioBearerAuthInterceptor)
                assert client._gateway_interceptor._token == "first-token"

                asyncio.run(client.refresh_token("second-token"))

                # After refresh and recreate, interceptor has new token
                asyncio.run(client._ensure_gateway_channel())
                assert client._gateway_interceptor._token == "second-token"
            finally:
                asyncio.run(client.close())

    def test_machine_channels_use_new_token_after_refresh(self):
        """Machine channels created after refresh use the new token."""
        with patch("fleet_client.client.grpc") as mock_grpc:
            mock_channel = Mock()
            mock_grpc.aio.insecure_channel.return_value = mock_channel

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=False,
            )
            try:
                # Create initial machine channel
                asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))

                # Refresh the token
                asyncio.run(client.refresh_token("new-token"))

                # Create a new machine channel — should use new token
                asyncio.run(client._get_or_create_machine_channel("10.0.0.1", 5007))

                # The second call creates a new channel with the new token
                assert mock_grpc.aio.insecure_channel.call_count == 2
            finally:
                asyncio.run(client.close())
