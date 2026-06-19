"""Concurrency tests for FleetClient — Group C (Race conditions R4, R5).

Tests token refresh interacting with in-flight RPCs and streaming subscriptions.
Uses asyncio.Event for deterministic synchronization (no time.sleep).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import grpc
import pytest

from fleet_client.client import FleetClient


class FakeAioRpcError(Exception):
    """Fake gRPC error for testing retry logic."""

    def __init__(self, code_value, details_msg):
        super().__init__(details_msg)
        self._code = code_value
        self._details = details_msg

    def code(self):
        return self._code


class TestRefreshDuringInflightRPC:
    """R4: Token refresh closes channels while coroutines hold references."""

    def test_refresh_token_closes_channels_and_stubs(self):
        """refresh_token() properly closes all channels and clears stubs.

        Verifies that when refresh is called, the gateway channel is closed,
        _gateway_stub is set to None, and machine channels are cleared.
        Subsequent RPCs recreate channels with new token.
        """
        # Patch only insecure_channel, not the entire grpc module
        with patch("fleet_client.client.grpc.aio.insecure_channel") as mock_insecure:
            mock_channel = Mock()
            mock_insecure.return_value = mock_channel

            gw_stub = AsyncMock()
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            # Fleet stub factory — returns a MagicMock whose SubscribeStatus
            # is callable and returns an async iterable directly (not a coroutine).
            class MockCall:
                """Simulates a gRPC streaming call that yields statuses."""
                _iter_count = 0

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if MockCall._iter_count == 0:
                        MockCall._iter_count += 1
                        return MagicMock(machine_id="m1", state=3)
                    raise StopAsyncIteration

            fleet_stub_mock = MagicMock()
            fleet_stub_mock.SubscribeStatus.return_value = MockCall()

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=False,
                _gateway_stub=gw_stub,
                _fleet_stub_factory=lambda ch: fleet_stub_mock,
            )

            async def run_test():
                # Ensure gateway channel is created
                await client._ensure_gateway_channel()
                assert client._gateway_channel is not None

                # Create a machine channel
                await client._get_or_create_machine_channel("10.0.0.2", 5007)
                assert len(client._machine_channels) == 1

                # Refresh token — should close channels and clear stubs
                await client.refresh_token("new-token")
                assert client._gateway_channel is None
                assert client._gateway_stub is None
                assert len(client._machine_channels) == 0

                # Subsequent RPC recreates gateway channel
                await client._ensure_gateway_channel()
                assert client._gateway_channel is not None
                assert mock_insecure.call_count >= 2

            try:
                asyncio.run(run_test())
            finally:
                try:
                    asyncio.run(client.close())
                except Exception:
                    pass


class TestRefreshDuringStreamingSubscription:
    """R5: Token refresh closes channels while async for iteration is active."""

    def test_refresh_token_during_streaming_subscription_terminates_cleanly(self):
        """Mid-stream refresh terminates subscription without hanging.

        The mock call object yields one status, then raises on the next
        iteration (simulating a closed gRPC channel). The async for loop
        catches this and the exception propagates up through the subscription
        generator. refresh_token() completes without deadlock.
        """
        class MockCall:
            """Simulates a gRPC call that fails after first yield."""

            def __init__(self):
                self._iter_count = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._iter_count == 0:
                    self._iter_count += 1
                    return MagicMock(machine_id="m1", state=3)
                # Second iteration — simulates channel invalidated by refresh
                raise FakeAioRpcError(grpc.StatusCode.UNAVAILABLE, "channel closed after refresh")

        fleet_stub_mock = MagicMock()
        fleet_stub_mock.SubscribeStatus.return_value = MockCall()

        with patch("fleet_client.client.grpc.aio.insecure_channel") as mock_insecure:
            mock_channel = Mock()
            mock_insecure.return_value = mock_channel

            gw_stub = AsyncMock()
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=False,
                _gateway_stub=gw_stub,
                _fleet_stub_factory=lambda ch: fleet_stub_mock,
            )

            async def run_test():
                # Start streaming subscription
                sub_gen = client.subscribe_status("m1")

                # Get first status successfully
                status = await asyncio.wait_for(sub_gen.__anext__(), timeout=2.0)
                assert status.machine_id == "m1"

                # Refresh token mid-stream — closes machine channels
                refresh_task = asyncio.create_task(client.refresh_token("new-token"))

                # Get next iteration — should fail with UNAVAILABLE (not hang)
                with pytest.raises(FakeAioRpcError):
                    await asyncio.wait_for(sub_gen.__anext__(), timeout=2.0)

                # Refresh completes without deadlock
                await asyncio.wait_for(refresh_task, timeout=2.0)
                assert client._gateway_channel is None

            try:
                asyncio.run(run_test())
            finally:
                try:
                    asyncio.run(client.close())
                except Exception:
                    pass


class TestConcurrentChannelAccessDuringRefresh:
    """R4+R5 combined: Multiple coroutines accessing channels while another refreshes."""

    def test_concurrent_get_status_with_refresh_no_deadlock(self):
        """5 coroutines calling get_status() then 1 calls refresh_token().

        All 5 RPCs complete with UNAVAILABLE errors (simulating closed channels).
        Refresh completes without deadlock or crash. Verifies that channel closure
        during concurrent access propagates errors cleanly to all callers.
        """
        errors = []
        get_status_call_count = 0

        def make_fleet_stub(ch):
            stub = MagicMock()
            async def get_status(request):
                nonlocal get_status_call_count
                get_status_call_count += 1
                raise FakeAioRpcError(grpc.StatusCode.UNAVAILABLE, "channel closed")
            stub.GetStatus = get_status
            return stub

        with patch("fleet_client.client.grpc.aio.insecure_channel") as mock_insecure:
            mock_channel = Mock()
            mock_insecure.return_value = mock_channel

            gw_stub = AsyncMock()
            gw_stub.RouteMachine = AsyncMock(
                return_value=MagicMock(instance_address="10.0.0.1", instance_port=5007)
            )

            client = FleetClient(
                gateway_address="127.0.0.1:50051",
                token="old-token",
                tls_enabled=False,
                _gateway_stub=gw_stub,
                _fleet_stub_factory=make_fleet_stub,
            )

            async def run_test():
                # Launch 5 coroutines that all call get_status concurrently
                tasks = []
                for i in range(5):
                    async def get_status_coro(idx):
                        try:
                            await client.get_status(f"machine-{idx}")
                        except Exception as e:
                            errors.append((type(e).__name__, idx))

                    task = asyncio.create_task(get_status_coro(i))
                    tasks.append(task)

                # Wait for all 5 RPCs to complete (each retries 3x with backoff ~0.7s total)
                done, pending = await asyncio.wait(tasks, timeout=15.0)
                assert len(pending) == 0, f"No tasks should be stuck: {len(pending)} pending"

                # All 5 should have failed (fleet stub raises UNAVAILABLE)
                assert len(errors) == 5, (
                    f"All 5 RPCs should fail, got {len(errors)} errors: {errors}"
                )

                # Refresh token after all concurrent RPCs completed — should not deadlock
                await client.refresh_token("new-token")
                assert client._gateway_channel is None
                assert client._gateway_stub is None

                # Verify GetStatus was called (each task retries 3 times)
                assert get_status_call_count == 15, (
                    f"Expected 15 calls (5 tasks x 3 retries), got {get_status_call_count}"
                )

            try:
                asyncio.run(run_test())
            finally:
                try:
                    asyncio.run(client.close())
                except Exception:
                    pass
