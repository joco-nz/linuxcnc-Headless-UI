"""LinuxCNC Fleet Sidecar — gRPC server for LinuxCNC machine integration."""

from linuxcnc_fleet.headless import LinuxCncSidecar
from linuxcnc_fleet.server import FleetServiceRPC, GatewayServiceRPC, create_server, run_server
from linuxcnc_fleet.cli import parse_args, main
from linuxcnc_fleet.auth import AuthContext, AuthInterceptor, AuthDecorator, create_auth_interceptor
from linuxcnc_fleet.logging_config import (
    CONSOLE_FORMAT,
    DATEFMT,
    SYSLOG_FORMAT,
    DEFAULT_SYSLOG_ADDRESS,
    setup_logging,
)
from linuxcnc_fleet.metrics import (
    REGISTRY,
    POLL_COUNT,
    HAL_READS,
    HAL_WRITES,
    COMMANDS,
    ERRORS,
    handle_health,
    handle_metrics,
)

__all__ = [
    # Core sidecar
    "LinuxCncSidecar",
    # gRPC servicers and server factory
    "FleetServiceRPC",
    "GatewayServiceRPC",
    "create_server",
    "run_server",
    # CLI
    "parse_args",
    "main",
    # Auth
    "AuthContext",
    "AuthInterceptor",
    "AuthDecorator",
    "create_auth_interceptor",
    # Logging
    "CONSOLE_FORMAT",
    "DATEFMT",
    "SYSLOG_FORMAT",
    "DEFAULT_SYSLOG_ADDRESS",
    "setup_logging",
    # Metrics
    "REGISTRY",
    "POLL_COUNT",
    "HAL_READS",
    "HAL_WRITES",
    "COMMANDS",
    "ERRORS",
    "handle_health",
    "handle_metrics",
]
