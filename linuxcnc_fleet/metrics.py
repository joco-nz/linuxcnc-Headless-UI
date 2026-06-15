"""Prometheus metrics and health endpoint for the LinuxCNC sidecar."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

if TYPE_CHECKING:
    from linuxcnc_fleet.headless import LinuxCncSidecar

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus registry (module-level singleton)
# ---------------------------------------------------------------------------

REGISTRY = CollectorRegistry()

POLL_COUNT = Counter(
    "fleet_sidecar_polls_total",
    "Total status polls completed by the sidecar",
    registry=REGISTRY,
)

HAL_READS = Counter(
    "fleet_sidecar_hal_reads_total",
    "Total HAL pin read operations",
    registry=REGISTRY,
)

HAL_WRITES = Counter(
    "fleet_sidecar_hal_writes_total",
    "Total HAL pin write operations",
    registry=REGISTRY,
)

COMMANDS = Counter(
    "fleet_sidecar_commands_total",
    "Total control commands executed",
    labelnames=["command"],
    registry=REGISTRY,
)

ERRORS = Counter(
    "fleet_sidecar_errors_total",
    "Total errors from the LinuxCNC error channel",
    registry=REGISTRY,
)


def _get_health_data(sidecar: LinuxCncSidecar) -> dict[str, Any]:
    """Extract health data from the sidecar's current state."""
    snapshot = sidecar._snapshot
    if snapshot is not None:
        state = snapshot.state.name if hasattr(snapshot.state, "name") else str(snapshot.state)
    else:
        state = "UNKNOWN"

    return {
        "status": "ok",
        "machine_id": sidecar._machine_id,
        "polling": sidecar._running,
        "state": state,
        "uptime_seconds": time.time() - sidecar._start_time if hasattr(sidecar, "_start_time") else 0,
    }


def handle_health(sidecar: LinuxCncSidecar) -> dict[str, Any]:
    """Health check handler — returns readiness data."""
    return _get_health_data(sidecar)


def handle_metrics() -> str:
    """Metrics handler — returns Prometheus text exposition format."""
    return generate_latest(REGISTRY).decode("utf-8")
