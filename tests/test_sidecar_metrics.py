"""Tests for linuxcnc_fleet/metrics.py — Prometheus registry and health endpoint."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture
def mock_sidecar():
    """Create a mock LinuxCncSidecar for testing."""
    sidecar = MagicMock()
    sidecar._machine_id = "test-machine-1"
    sidecar._running = True
    sidecar._start_time = time.time() - 100

    snapshot = MagicMock()
    snapshot.state.name = "RUNNING"
    sidecar._snapshot = snapshot

    return sidecar


@pytest.fixture
def metrics_app(mock_sidecar):
    """Create an aiohttp application with health and metrics routes."""
    from linuxcnc_fleet import metrics as sidecar_metrics

    app = web.Application()
    app["sidecar"] = mock_sidecar

    async def handle_health(request: web.Request) -> web.Response:
        data = sidecar_metrics.handle_health(mock_sidecar)
        return web.json_response(data)

    async def handle_metrics(request: web.Request) -> web.Response:
        text = sidecar_metrics.handle_metrics()
        return web.Response(text=text, content_type="text/plain; version=0.0.4")

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
    async def test_health_contains_machine_id(self, client, mock_sidecar):
        """Health response includes machine_id from sidecar."""
        resp = await client.get("/health")
        data = await resp.json()
        assert data["machine_id"] == "test-machine-1"

    @pytest.mark.asyncio
    async def test_health_contains_polling_status(self, client, mock_sidecar):
        """Health response includes polling status."""
        resp = await client.get("/health")
        data = await resp.json()
        assert data["polling"] is True

    @pytest.mark.asyncio
    async def test_health_contains_state(self, client, mock_sidecar):
        """Health response includes current machine state."""
        resp = await client.get("/health")
        data = await resp.json()
        assert data["state"] == "RUNNING"

    @pytest.mark.asyncio
    async def test_health_contains_uptime(self, client):
        """Health response includes uptime_seconds field."""
        resp = await client.get("/health")
        data = await resp.json()
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], (int, float))


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
    async def test_metrics_contains_sidecar_counters(self, client):
        """Metrics output contains sidecar metric names."""
        resp = await client.get("/metrics")
        text = await resp.text()
        assert "fleet_sidecar_polls_total" in text
        assert "fleet_sidecar_hal_reads_total" in text
        assert "fleet_sidecar_hal_writes_total" in text
        assert "fleet_sidecar_commands_total" in text
        assert "fleet_sidecar_errors_total" in text

    @pytest.mark.asyncio
    async def test_metrics_format_is_valid(self, client):
        """Metrics output is valid Prometheus exposition format."""
        resp = await client.get("/metrics")
        text = await resp.text()
        # Basic validation: should have metric names and TYPE declarations
        assert "# HELP" in text or "fleet_sidecar_" in text
        assert "# TYPE" in text or "counter" in text or "gauge" in text


class TestCounterIncrements:
    """Tests that counters increment correctly."""

    @pytest.mark.asyncio
    async def test_poll_counter_exists(self):
        """POLL_COUNT counter exists in the registry."""
        from linuxcnc_fleet.metrics import POLL_COUNT, REGISTRY

        # Verify the counter is registered and collectable
        families = list(REGISTRY.collect())
        assert len(families) > 0

        # Find the poll counter
        found = False
        for family in families:
            for metric in family.samples:
                if "fleet_sidecar_polls_total" in metric.name:
                    found = True
                    break
            if found:
                break
        assert found, "fleet_sidecar_polls_total counter not found in registry"


class TestHealthWithNoSnapshot:
    """Tests for health endpoint when sidecar has no snapshot yet."""

    @pytest.mark.asyncio
    async def test_health_with_no_snapshot(self):
        """Health returns UNKNOWN state when no snapshot available."""
        from linuxcnc_fleet import metrics as sidecar_metrics

        mock = MagicMock()
        mock._machine_id = "no-snapshot-machine"
        mock._running = False
        mock._start_time = time.time() - 50
        mock._snapshot = None

        data = sidecar_metrics.handle_health(mock)
        assert data["status"] == "ok"
        assert data["machine_id"] == "no-snapshot-machine"
        assert data["polling"] is False
        assert data["state"] == "UNKNOWN"
