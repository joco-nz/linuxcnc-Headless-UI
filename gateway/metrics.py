"""Prometheus metrics and health endpoint for the gateway."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

if TYPE_CHECKING:
    from gateway.registry import MachineRegistry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus registry (module-level singleton)
# ---------------------------------------------------------------------------

REGISTRY = CollectorRegistry()

REQUESTS_TOTAL = Counter(
    "fleet_gateway_requests_total",
    "Total gRPC requests by RPC name and status",
    labelnames=["rpc", "status"],
    registry=REGISTRY,
)

BROADCASTS_TOTAL = Counter(
    "fleet_gateway_broadcasts_total",
    "Broadcast operations by command type",
    labelnames=["command_type"],
    registry=REGISTRY,
)

TOKENS_ISSUED = Counter(
    "fleet_gateway_tokens_issued_total",
    "Total tokens issued via HTTP endpoint",
    registry=REGISTRY,
)

MACHINES_REGISTERED = Gauge(
    "fleet_gateway_machines_registered",
    "Current number of machines in the registry",
    registry=REGISTRY,
)

MACHINES_EXPIRED = Counter(
    "fleet_gateway_machines_expired_total",
    "Total machines expired from the registry",
    registry=REGISTRY,
)


def _get_health_data(
    registry: MachineRegistry,
    tls_enabled: bool = False,
    grpc_port: int | None = None,
) -> dict[str, Any]:
    """Extract health data from the gateway's current state."""
    try:
        machine_count = len(registry.list_all()) if registry else 0
    except Exception:
        machine_count = 0

    data: dict[str, Any] = {
        "status": "ok",
        "uptime_seconds": time.time() - _start_time,
        "machines_registered": machine_count,
    }
    if tls_enabled:
        data["tls_enabled"] = True
    if grpc_port is not None:
        data["grpc_port"] = grpc_port

    return data


_start_time = time.time()


def handle_health(
    registry: MachineRegistry,
    tls_enabled: bool = False,
    grpc_port: int | None = None,
) -> dict[str, Any]:
    """Health check handler — returns readiness data."""
    return _get_health_data(registry, tls_enabled=tls_enabled, grpc_port=grpc_port)


def handle_metrics() -> str:
    """Metrics handler — returns Prometheus text exposition format."""
    return generate_latest(REGISTRY).decode("utf-8")


def record_request(rpc_name: str, status: str = "ok") -> None:
    """Record a gRPC request in the metrics counter."""
    REQUESTS_TOTAL.labels(rpc=rpc_name, status=status).inc()


def record_broadcast(command_type: str) -> None:
    """Record a broadcast operation in the metrics counter."""
    BROADCASTS_TOTAL.labels(command_type=command_type).inc()


def record_token_issued() -> None:
    """Record a token issuance event."""
    TOKENS_ISSUED.inc()


def update_machine_count(registry: MachineRegistry) -> None:
    """Update the machines registered gauge from the registry."""
    try:
        MACHINES_REGISTERED.set(len(registry.list_all()))
    except Exception:
        pass
