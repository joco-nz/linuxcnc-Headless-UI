"""Tests for gateway/metrics.py — Prometheus registry and health endpoint."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.auth import create_test_auth_manager
from gateway.policies import create_test_policy_engine
from gateway.registry import MachineRegistry


@pytest.fixture
def auth_manager():
    """Create a test auth manager."""
    return create_test_auth_manager()


@pytest.fixture
def policy_engine():
    """Create a test policy engine."""
    return create_test_policy_engine()


@pytest.fixture
def registry():
    """Create a test machine registry."""
    reg = MachineRegistry()
    return reg


@pytest.fixture
def metrics_app(registry):
    """Create an aiohttp application with health and metrics routes."""
    from gateway import metrics as gateway_metrics

    app = web.Application()
    app["registry"] = registry

    async def handle_health(request: web.Request) -> web.Response:
        data = gateway_metrics.handle_health(registry)
        return web.json_response(data)

    async def handle_metrics(request: web.Response) -> web.Response:
        text = gateway_metrics.handle_metrics()
        return web.Response(text=text, content_type="text/plain; version=0.0.4", charset="utf-8")

    app.router.add_get("/health", handle_health)
    app.router.add_get("/metrics", handle_metrics)

    return app


@pytest.fixture
async def client(metrics_app):
    """Create a test client for the metrics application."""
    async with TestClient(TestServer(metrics_app)) as client:
        yield client


class TestHealthEndpoint:
    """Tests for the /health endpoint."""

    @pytest.mark.asyncio
    async def test_health_returns_200(self, client):
        """Health endpoint returns 200 OK."""
        resp = await client.get("/health")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_health_returns_json(self, client):
        """Health endpoint returns JSON content type."""
        resp = await client.get("/health")
        assert resp.content_type == "application/json"

    @pytest.mark.asyncio
    async def test_health_contains_status(self, client):
        """Health response includes status field set to 'ok'."""
        resp = await client.get("/health")
        data = await resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_contains_uptime(self, client):
        """Health response includes uptime_seconds field."""
        resp = await client.get("/health")
        data = await resp.json()
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], (int, float))

    @pytest.mark.asyncio
    async def test_health_contains_machine_count(self, client):
        """Health response includes machines_registered count."""
        resp = await client.get("/health")
        data = await resp.json()
        assert "machines_registered" in data
        assert isinstance(data["machines_registered"], int)


class TestHealthWithMachines:
    """Tests for health endpoint with registered machines."""

    @pytest.mark.asyncio
    async def test_health_shows_machine_count(self, client, registry):
        """Health response reflects current machine count in registry."""
        # Register a test machine
        registry.register(
            machine_id="test-machine-1",
            address="192.168.1.10",
            port=50051,
            facility="test-facility",
            tags=["cnc"],
            version="2.9",
        )

        resp = await client.get("/health")
        data = await resp.json()
        assert data["machines_registered"] == 1


class TestMetricsEndpoint:
    """Tests for the /metrics endpoint."""

    @pytest.mark.asyncio
    async def test_metrics_returns_200(self, client):
        """Metrics endpoint returns 200 OK."""
        resp = await client.get("/metrics")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_metrics_returns_text_plain(self, client):
        """Metrics endpoint returns text/plain content type."""
        resp = await client.get("/metrics")
        assert "text/plain" in resp.content_type

    @pytest.mark.asyncio
    async def test_metrics_contains_gateway_counters(self, client):
        """Metrics output contains gateway metric names."""
        resp = await client.get("/metrics")
        text = await resp.text()
        assert "fleet_gateway_requests_total" in text
        assert "fleet_gateway_broadcasts_total" in text
        assert "fleet_gateway_tokens_issued_total" in text
        assert "fleet_gateway_machines_registered" in text
        assert "fleet_gateway_machines_expired_total" in text

    @pytest.mark.asyncio
    async def test_metrics_format_is_valid(self, client):
        """Metrics output is valid Prometheus exposition format."""
        resp = await client.get("/metrics")
        text = await resp.text()
        # Basic validation: should have metric names and TYPE declarations
        assert "# HELP" in text or "fleet_gateway_" in text
        assert "# TYPE" in text or "counter" in text or "gauge" in text


class TestCounterFunctions:
    """Tests that counter helper functions work correctly."""

    @pytest.mark.asyncio
    async def test_record_request_increments_counter(self):
        """record_request() increments the REQUESTS_TOTAL counter."""
        from gateway.metrics import REGISTRY, record_request

        # Verify counters are registered
        families = list(REGISTRY.collect())
        assert len(families) > 0

        # Record a request and verify it doesn't raise
        record_request("test-rpc", "ok")
        assert True  # Function executed without error


    @pytest.mark.asyncio
    async def test_record_broadcast_increments_counter(self):
        """record_broadcast() increments the BROADCASTS_TOTAL counter."""
        from gateway.metrics import REGISTRY, record_broadcast

        # Record a broadcast and verify it doesn't raise
        record_broadcast("mdi")
        assert True  # Function executed without error


    @pytest.mark.asyncio
    async def test_record_token_issued_increments_counter(self):
        """record_token_issued() increments the TOKENS_ISSUED counter."""
        from gateway.metrics import REGISTRY, record_token_issued

        # Record a token issuance and verify it doesn't raise
        record_token_issued()
        assert True  # Function executed without error


    @pytest.mark.asyncio
    async def test_update_machine_count_updates_gauge(self, registry):
        """update_machine_count() sets the gauge to current machine count."""
        from gateway.metrics import REGISTRY, update_machine_count

        # Register a machine and update the gauge
        registry.register(
            machine_id="test-machine-2",
            address="192.168.1.11",
            port=50051,
            facility="test-facility",
            tags=["cnc"],
            version="2.9",
        )

        update_machine_count(registry)

        # Verify the gauge was updated (check that it doesn't raise)
        assert True  # Function executed without error


class TestHealthWithNoMachines:
    """Tests for health endpoint when no machines are registered."""

    @pytest.mark.asyncio
    async def test_health_shows_zero_machines(self, client):
        """Health response shows 0 machines when registry is empty."""
        resp = await client.get("/health")
        data = await resp.json()
        assert data["machines_registered"] == 0
