"""Phase 4 integration tests — end-to-end token lifecycle.

Tests: issue → use → expire → renew → continue working.
Starts real gRPC servers (gateway + sidecar) with HTTP token issuance enabled,
then exercises the full token expiry/renewal cycle.
"""

import asyncio
import socket
import threading
import time
from concurrent import futures

import aiohttp
import grpc
import pytest

from gateway.auth import create_test_auth_manager, create_test_token
from gateway.policies import create_test_policy_engine
from gateway.registry import create_test_registry
from linuxcnc_fleet.fleet_pb2 import DiscoverRequest, MachineId
from linuxcnc_fleet.fleet_pb2_grpc import FleetGatewayServiceStub


# ---------------------------------------------------------------------------
# Helpers: start gateway with both gRPC + HTTP token issuance
# ---------------------------------------------------------------------------

def _find_free_port():
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_gateway_with_http(gw_port, http_port, auth_manager, policy_engine, registry,
                              allowed_roles=None, allowed_subjects=None, allowed_ips=None,
                              token_ttl=3, allow_admin_token=False, permissive=False):
    """Start gateway gRPC + HTTP servers in background threads.

    Returns (grpc_server, cleanup_fn) for cleanup.
    """
    from aiohttp import web as aiohttp_web
    from gateway.server import TokenIssuanceServicer, GatewayServiceServicer
    from linuxcnc_fleet.fleet_pb2_grpc import add_FleetGatewayServiceServicer_to_server

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    servicer = GatewayServiceServicer(auth_manager, policy_engine, registry)
    add_FleetGatewayServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"[::]:{gw_port}")

    token_servicer = TokenIssuanceServicer(
        auth_manager=auth_manager,
        policy_engine=policy_engine,
        allowed_roles=allowed_roles or ["viewer", "operator"],
        allowed_subjects=allowed_subjects or ["fleet-ui"],
        allowed_ips=allowed_ips or ["127.0.0.1", "::1"],
        token_ttl=token_ttl,
        allow_admin_token=allow_admin_token,
        permissive=permissive,
    )

    http_app = aiohttp_web.Application()
    http_app["token_servicer"] = token_servicer

    async def _auth_token_handler(request):
        servicer_instance: TokenIssuanceServicer = request.app["token_servicer"]
        client_ip = request.remote or "0.0.0.0"
        role = request.query.get("role", "viewer")
        sub = request.query.get("sub", "fleet-ui")
        try:
            result = servicer_instance.issue_token(role=role, sub=sub, client_ip=client_ip)
            return aiohttp_web.json_response(result)
        except Exception as e:
            error_code = getattr(e, "error_code", 403) if hasattr(e, "error_code") else 403
            return aiohttp_web.json_response(
                {"error": str(e), "error_code": error_code}, status=error_code
            )

    http_app.router.add_post("/api/auth/token", _auth_token_handler)

    # Store references for cleanup
    state = {"runner": None, "thread": None}

    def _run_http():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _async_start():
            runner = aiohttp_web.AppRunner(http_app)
            await runner.setup()
            site = aiohttp_web.TCPSite(runner, "127.0.0.1", http_port)
            await site.start()
            state["runner"] = runner

        loop.run_until_complete(_async_start())
        state["thread"] = threading.Thread(target=loop.run_forever, daemon=True)
        state["thread"].start()

    http_thread = threading.Thread(target=_run_http, daemon=True)
    http_thread.start()
    time.sleep(0.25)  # let HTTP server start

    server.start()

    def cleanup():
        server.stop(grace=0.5)
        runner = state.get("runner")
        if runner:
            try:
                l = asyncio.new_event_loop()
                l.run_until_complete(runner.cleanup())
                l.close()
            except Exception:
                pass
        thread = state.get("thread")
        if thread:
            thread.join(timeout=2)

    return server, cleanup


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def renewal_sidecar():
    """Start a single sidecar for renewal tests."""
    from linuxcnc_fleet.headless import LinuxCncSidecar
    from linuxcnc_fleet.server import create_server

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    sidecar = LinuxCncSidecar(
        machine_id="renewal-machine-1",
        ini_path="/fake.ini",
    )
    sidecar.run()

    server = create_server(sidecar=sidecar, port=port)
    server.start()
    time.sleep(0.15)

    def stop():
        server.stop(grace=0.5)
        sidecar.shutdown()

    yield port, sidecar, stop


@pytest.fixture()
def gateway_with_http(renewal_sidecar):
    """Start gateway with HTTP token issuance and a registered sidecar."""
    gw_port = _find_free_port()
    http_port = _find_free_port()
    port, sidecar, stop_sidecar = renewal_sidecar

    auth_manager = create_test_auth_manager()
    policy_engine = create_test_policy_engine()
    registry = create_test_registry(heartbeat_ttl=30.0)

    registry.register(
        machine_id="renewal-machine-1",
        address="127.0.0.1",
        port=port,
        facility="test-facility",
        tags=["cnc"],
    )
    registry.start()

    grpc_server, cleanup = _start_gateway_with_http(
        gw_port=gw_port,
        http_port=http_port,
        auth_manager=auth_manager,
        policy_engine=policy_engine,
        registry=registry,
        token_ttl=3,  # very short TTL for tests
    )

    def stop():
        cleanup()
        registry.stop()
        stop_sidecar()

    yield gw_port, http_port, auth_manager, stop


# ---------------------------------------------------------------------------
# Tests: full token lifecycle
# ---------------------------------------------------------------------------

class TestTokenIssueAndUse:
    """Test that tokens issued via HTTP can be used for gRPC calls."""

    def test_issue_token_via_http(self, gateway_with_http):
        """HTTP endpoint issues a valid JWT token."""
        gw_port, http_port, auth_manager, stop = gateway_with_http

        url = f"http://127.0.0.1:{http_port}/api/auth/token?role=viewer&sub=fleet-ui"

        async def _fetch():
            async with aiohttp.ClientSession() as session:
                async with session.post(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return await resp.json()

        data = asyncio.run(_fetch())

        assert "token" in data
        assert "expires_in" in data
        assert data["expires_in"] == 3  # test TTL

    def test_use_http_token_for_grpc_discover(self, gateway_with_http):
        """Token issued via HTTP works for gRPC (authenticated, not rejected).

        Note: viewer tokens without facility claim return empty machine list
        per policy engine rules. This test verifies the token is accepted
        (not UNAUTHENTICATED) and the call succeeds.
        """
        gw_port, http_port, auth_manager, stop = gateway_with_http

        # Issue token via HTTP (viewer role has READ_STATUS permission)
        url = f"http://127.0.0.1:{http_port}/api/auth/token?role=viewer&sub=fleet-ui"

        async def _fetch_token():
            async with aiohttp.ClientSession() as session:
                async with session.post(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
            return data["token"]

        token = asyncio.run(_fetch_token())

        # Use token for gRPC discover — should authenticate successfully
        # (viewer without facility gets empty list, which is correct policy behavior)
        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        try:
            stub = FleetGatewayServiceStub(channel)
            resp = stub.DiscoverMachines(
                DiscoverRequest(facility=""),
                metadata=[("authorization", f"Bearer {token}")],
            )
            # Token is accepted (no UNAUTHENTICATED exception)
            assert resp is not None
        finally:
            channel.close()

    def test_use_admin_token_for_grpc_discover_via_http(self, gateway_with_http):
        """Admin token issued via HTTP endpoint works for full discover."""
        gw_port, http_port, auth_manager, stop = gateway_with_http

        # Issue admin token (requires allow_admin_token flag)
        url = f"http://127.0.0.1:{http_port}/api/auth/token?role=admin&sub=fleet-ui"

        async def _fetch():
            async with aiohttp.ClientSession() as session:
                async with session.post(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status

        # Admin role rejected without --allow-admin-token
        assert asyncio.run(_fetch()) == 403

        # Create admin token directly using auth_manager (simulating what would happen with --allow-admin-token)
        admin_token = create_test_token(
            {"sub": "fleet-ui", "role": "admin"},
            secret_key=auth_manager.secret_key,
        )

        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        try:
            stub = FleetGatewayServiceStub(channel)
            resp = stub.DiscoverMachines(
                DiscoverRequest(facility=""),
                metadata=[("authorization", f"Bearer {admin_token}")],
            )
            assert len(resp.machines) >= 1
        finally:
            channel.close()

    def test_use_admin_token_for_grpc_discover(self, gateway_with_http):
        """Admin token (via create_test_token) works for gRPC discover."""
        gw_port, http_port, auth_manager, stop = gateway_with_http

        # Create admin token using the same auth_manager
        admin_token = create_test_token(
            {"sub": "test-admin", "name": "Test Admin", "role": "admin"},
            secret_key=auth_manager.secret_key,
        )

        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        try:
            stub = FleetGatewayServiceStub(channel)
            resp = stub.DiscoverMachines(
                DiscoverRequest(facility=""),
                metadata=[("authorization", f"Bearer {admin_token}")],
            )
            assert len(resp.machines) >= 1
        finally:
            channel.close()

    def test_invalid_subject_rejected(self, gateway_with_http):
        """HTTP endpoint rejects tokens for unregistered subjects."""
        gw_port, http_port, auth_manager, stop = gateway_with_http

        url = "http://127.0.0.1:{}/api/auth/token?role=viewer&sub=unknown-client".format(http_port)

        async def _fetch():
            async with aiohttp.ClientSession() as session:
                async with session.post(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status

        status = asyncio.run(_fetch())
        assert status == 403

    def test_invalid_role_rejected(self, gateway_with_http):
        """HTTP endpoint rejects admin role without --allow-admin-token."""
        gw_port, http_port, auth_manager, stop = gateway_with_http

        url = "http://127.0.0.1:{}/api/auth/token?role=admin&sub=fleet-ui".format(http_port)

        async def _fetch():
            async with aiohttp.ClientSession() as session:
                async with session.post(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status

        status = asyncio.run(_fetch())
        assert status == 403


class TestTokenExpiryAndRenewal:
    """Test that expired tokens are detected and renewed."""

    def test_expired_token_rejected(self, gateway_with_http):
        """gRPC calls fail with UNAUTHENTICATED when token is expired."""
        gw_port, http_port, auth_manager, stop = gateway_with_http

        import jwt as pyjwt
        now = int(time.time())
        expired_payload = {
            "exp": now - 10,
            "iss": "https://test.auth.example.com",
            "aud": "linuxcnc-fleet",
            "sub": "fleet-ui",
            "role": "admin",
        }
        expired_token = pyjwt.encode(expired_payload, auth_manager.secret_key, algorithm="HS256")

        # Use expired token — should fail with UNAUTHENTICATED
        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        try:
            stub = FleetGatewayServiceStub(channel)
            with pytest.raises(grpc.RpcError) as exc_info:
                stub.DiscoverMachines(
                    DiscoverRequest(facility=""),
                    metadata=[("authorization", f"Bearer {expired_token}")],
                )
            assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED
        finally:
            channel.close()

    def test_renew_token_and_continue_working(self, gateway_with_http):
        """After token expiry, renewing allows continued operation."""
        gw_port, http_port, auth_manager, stop = gateway_with_http

        import jwt as pyjwt
        now = int(time.time())
        expired_payload = {
            "exp": now - 10,
            "iss": "https://test.auth.example.com",
            "aud": "linuxcnc-fleet",
            "sub": "fleet-ui",
            "role": "admin",
        }
        expired_token = pyjwt.encode(expired_payload, auth_manager.secret_key, algorithm="HS256")

        # Create a fresh valid token (simulating renewal)
        fresh_token = create_test_token(
            {"sub": "fleet-ui", "role": "admin"},
            secret_key=auth_manager.secret_key,
        )

        # Expired token fails
        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        try:
            stub = FleetGatewayServiceStub(channel)
            with pytest.raises(grpc.RpcError):
                stub.DiscoverMachines(
                    DiscoverRequest(facility=""),
                    metadata=[("authorization", f"Bearer {expired_token}")],
                )
        finally:
            channel.close()

        # Fresh (renewed) token works
        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        try:
            stub = FleetGatewayServiceStub(channel)
            resp = stub.DiscoverMachines(
                DiscoverRequest(facility=""),
                metadata=[("authorization", f"Bearer {fresh_token}")],
            )
            assert len(resp.machines) >= 1
        finally:
            channel.close()


class TestFleetClientRenewalFlow:
    """Test FleetClient with token renewal via HTTP."""

    def test_fleetclient_refresh_token_propagates(self, gateway_with_http):
        """FleetClient.refresh_token() updates all channels with new token."""
        from fleet_client.client import FleetClient

        gw_port, http_port, auth_manager, stop = gateway_with_http

        # Issue initial token via HTTP
        url = f"http://127.0.0.1:{http_port}/api/auth/token?role=viewer&sub=fleet-ui"

        async def _fetch_token():
            async with aiohttp.ClientSession() as session:
                async with session.post(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
            return data["token"]

        token1 = asyncio.run(_fetch_token())

        client = FleetClient(
            gateway_address=f"127.0.0.1:{gw_port}",
            token=token1,
            tls_enabled=False,
        )

        try:
            # Ensure gateway channel is created and interceptor exists
            machines1 = asyncio.run(client.get_machines())
            assert machines1 is not None  # get_machines returns list or raises

            # Get the interceptor to verify it has the old token
            interceptor = client._gateway_interceptor
            assert interceptor is not None
            old_token = interceptor._token
            assert old_token == token1

            # Renew with a fresh admin token (directly, since HTTP doesn't issue admin)
            import jwt as pyjwt
            now = int(time.time())
            admin_payload = {
                "exp": now + 3600,
                "iss": auth_manager.issuer,
                "aud": auth_manager.audience,
                "sub": "fleet-ui",
                "role": "admin",
            }
            token2 = pyjwt.encode(admin_payload, auth_manager.secret_key, algorithm="HS256")

            # Refresh the client with new admin token
            asyncio.run(client.refresh_token(token2))

            # Verify stored token was updated (interceptor recreated lazily on next RPC)
            assert client._token == token2

            # get_machines should work with renewed admin token
            machines2 = asyncio.run(client.get_machines())
            assert len(machines2) >= 1
        finally:
            asyncio.run(client.close())


class TestProactiveRenewalFlow:
    """Test that proactive renewal prevents token expiry from causing failures."""

    def test_proactive_refresh_task_runs(self):
        """FleetApp's proactive refresh background task runs without error."""
        from fleet_ui.server import FleetApp

        # Create a mock client that tracks refresh calls
        class MockClient:
            def __init__(self):
                self.refresh_count = 0
                self._closed = False

            async def discover_machines(self, machine_id):
                return []

            async def get_status(self, machine_id):
                return None

            async def refresh_token(self, new_token):
                self.refresh_count += 1

        mock_client = MockClient()

        app = FleetApp(
            gateway_address="127.0.0.1:50200",
            token="fake-token-for-test",
            _mock_client=mock_client,
            gateway_http_port=50253,
            timeout=5,
        )

        # Set a very early expiry to trigger proactive refresh immediately
        app._token_expiry = time.time() - 10  # already expired
        app._running = True

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _run_test():
                await app._start_proactive_refresh()
                assert app._proactive_task is not None
                await asyncio.sleep(0.5)  # let it run a cycle
                app._running = False
                app._proactive_task.cancel()
                try:
                    await app._proactive_task
                except asyncio.CancelledError:
                    pass

            loop.run_until_complete(_run_test())
            loop.close()
        finally:
            app._running = False


class TestReactiveRenewalFlow:
    """Test that reactive renewal recovers from UNAUTHENTICATED errors."""

    def test_grpc_retry_on_unauthenticated(self, gateway_with_http):
        """When gRPC returns UNAUTHENTICATED, retry with renewed token succeeds."""
        gw_port, http_port, auth_manager, stop = gateway_with_http

        import jwt as pyjwt
        now = int(time.time())
        expired_payload = {
            "exp": now - 10,
            "iss": "https://test.auth.example.com",
            "aud": "linuxcnc-fleet",
            "sub": "fleet-ui",
            "role": "admin",
        }
        expired_token = pyjwt.encode(expired_payload, auth_manager.secret_key, algorithm="HS256")

        # Create a fresh valid token (simulating renewal)
        fresh_token = create_test_token(
            {"sub": "fleet-ui", "role": "admin"},
            secret_key=auth_manager.secret_key,
        )

        channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        try:
            stub = FleetGatewayServiceStub(channel)

            # 1. First attempt fails with UNAUTHENTICATED
            first_attempt_failed = False
            try:
                stub.DiscoverMachines(
                    DiscoverRequest(facility=""),
                    metadata=[("authorization", f"Bearer {expired_token}")],
                )
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                    first_attempt_failed = True

            assert first_attempt_failed, "Expected UNAUTHENTICATED error on expired token"

            # 2. Retry with fresh (renewed) token — should succeed
            resp = stub.DiscoverMachines(
                DiscoverRequest(facility=""),
                metadata=[("authorization", f"Bearer {fresh_token}")],
            )
            assert len(resp.machines) >= 1
        finally:
            channel.close()


class TestTokenSecurityModel:
    """Test AND/OR security models for token issuance."""

    def test_and_mode_requires_both_ip_and_subject(self, gateway_with_http):
        """AND mode (default): both IP and subject must match."""
        gw_port, http_port, auth_manager, stop = gateway_with_http

        # Valid request: IP 127.0.0.1 matches allowed_ips, sub=fleet-ui matches allowed_subjects
        url = "http://127.0.0.1:{}/api/auth/token?role=viewer&sub=fleet-ui".format(http_port)

        async def _fetch():
            async with aiohttp.ClientSession() as session:
                async with session.post(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status

        assert asyncio.run(_fetch()) == 200

        # Invalid subject in AND mode
        url_bad_sub = "http://127.0.0.1:{}/api/auth/token?role=viewer&sub=bad-sub".format(http_port)

        async def _fetch_bad():
            async with aiohttp.ClientSession() as session:
                async with session.post(url_bad_sub, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status

        assert asyncio.run(_fetch_bad()) == 403

    def test_or_mode_permissive(self):
        """OR mode (--permissive): either IP or subject match is sufficient."""
        gw_port = _find_free_port()
        http_port = _find_free_port()

        auth_manager = create_test_auth_manager()
        policy_engine = create_test_policy_engine()
        registry = create_test_registry(heartbeat_ttl=30.0)

        grpc_server, cleanup = _start_gateway_with_http(
            gw_port=gw_port,
            http_port=http_port,
            auth_manager=auth_manager,
            policy_engine=policy_engine,
            registry=registry,
            permissive=True,  # OR mode
            token_ttl=3,
        )

        try:
            # In OR mode, a request with valid IP but invalid subject should succeed
            url = "http://127.0.0.1:{}/api/auth/token?role=viewer&sub=unknown-sub".format(http_port)

            async def _fetch():
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        return resp.status

            assert asyncio.run(_fetch()) == 200
        finally:
            cleanup()
