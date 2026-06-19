# linuxcnc-fleet

gRPC-based headless fleet management for LinuxCNC machines. Monitor and control multiple CNC instances from a single centralized interface — no modifications to the LinuxCNC C++ core or real-time components required.

## Architecture

```
                    Central UI (Python)
          ┌──────────┐  ┌──────────┐  ┌──────────┐
          │ Dashboard │  │ Program. │  │ HAL Conf.│
          └─────┬────┘  └────┬─────┘  └────┬─────┘
                │             │              │
          ┌─────▼───────────────────────────▼────┐
          │       Fleet Client Library (gRPC)    │
          └──────────────────┬───────────────────┘
                             │ gRPC over TLS (mTLS)
                    ┌────────▼────────┐
                    │  Fleet Gateway  │
                    │  OIDC / RBAC    │
                    │  Discovery      │
                    └────────┬────────┘
              ┌───────┬───────┼───────┬───────┐
              │       │               │       │
        ┌─────▼──┐ ┌──▼─────┐   ┌────▼────┐ ┌──▼────┐
        │Machine A│ │Machine B│   │ Machine N│ │ ...   │
        │Sidecar  │ │Sidecar  │   │ Sidecar  │       │
        └────┬────┘ └────┬───┘   └────┬────┘ └───┬───┘
             │            │             │          │
          ┌──▼─────────┐ ┌▼──────────┐ ┌▼────────┐
          │  linuxcnc   │ │ linuxcnc  │ │ linuxcnc│
          │  hal        │ │  hal      │ │  hal    │
          └─────────────┘ └───────────┘ └─────────┘
```

Three components, each installable independently:

| Component | Package | Purpose |
|-----------|---------|---------|
| **Sidecar** | `linuxcnc-fleet[sidecar]` | Runs on each CNC machine. Wraps `linuxcnc` and `hal` Python modules behind a gRPC server. |
| **Gateway** | `linuxcnc-fleet[gateway]` | Central auth & routing service. OIDC token validation, RBAC policies, machine discovery, broadcast fan-out. |
| **FleetClient** | `linuxcnc-fleet[client]` | Python client library with automatic OIDC auth injection, retry logic, channel caching, and streaming subscriptions. |

## Requirements

- Python 3.10+
- LinuxCNC installed with Python bindings (`linuxcnc`, `hal`) — sidecar machines only

## Installation

```bash
# Sidecar (runs on CNC machines)
pip install "linuxcnc-fleet[sidecar]"

# Gateway (central auth/routing server)
pip install "linuxcnc-fleet[gateway]"

# Client library (for building UIs or scripts)
pip install "linuxcnc-fleet[client]"

# Development dependencies
pip install "linuxcnc-fleet[dev]"
```

## Usage

### Sidecar

Run the sidecar on each LinuxCNC machine. It polls `linuxcnc.stat` at 50Hz and exposes a gRPC server:

```bash
headless-server \
    --ini /path/to/machine.ini \
    --machine-id lathe-01 \
    --port 50051

# With TLS (mTLS)
headless-server \
    --ini /path/to/machine.ini \
    --machine-id lathe-01 \
    --cert server.pem \
    --key private.pem \
    --root-cert ca.pem

# Logging to syslog (in addition to stderr)
headless-server \
    --ini /path/to/machine.ini \
    --machine-id lathe-01 \
    --syslog \
    --syslog-facility local0
```

| Flag | Description | Default |
|------|-------------|---------|
| `--ini` | Path to LinuxCNC INI file | auto-detect |
| `--machine-id` | Unique machine identifier | `default` |
| `--port` | gRPC listen port | `50051` |
| `--cert` | TLS server certificate (PEM) | — |
| `--key` | TLS server private key (PEM) | — |
| `--root-cert` | Root CA for mTLS client auth | — |
| `-v / -vv` | Increase log verbosity | WARNING |
| `--syslog` | Enable logging to syslog | disabled |
| `--syslog-address` | Syslog socket path | `/dev/log` |
| `--syslog-facility` | Syslog facility (user, daemon, local0-local7) | `user` |

### Gateway

```bash
fleet-gateway \
    --jwt-secret your-hs256-secret-key \
    --issuer https://auth.example.com \
    --audience linuxcnc-fleet \
    --port 50051
```

The gateway maintains a registry of registered machines, validates OIDC tokens, enforces RBAC policies (viewer/operator/programmer/maintainer/admin roles with facility scoping), and routes client requests to the correct sidecar.

| Flag | Description | Default |
|------|-------------|---------|
| `--port` | gRPC server port | `50051` |
| `--cert` | Server TLS certificate (PEM) | — |
| `--key` | Server TLS private key (PEM) | — |
| `--root-cert` | Root CA for mTLS client verification | — |
| `--jwt-secret` | HS256 JWT signing secret | — |
| `--jwks-url` | JWKS endpoint for RS256/RS384/RS512 | — |
| `--issuer` | Expected JWT issuer claim | — |
| `--audience` | Expected JWT audience claim | — |
| `-v` | Enable verbose logging | disabled |
| `--syslog` | Enable logging to syslog | disabled |
| `--syslog-address` | Syslog socket path | `/dev/log` |
| `--syslog-facility` | Syslog facility (user, daemon, local0-local7) | `user` |

### FleetClient (Python)

```python
from fleet_client import FleetClient

# Connect via gateway with automatic OIDC token injection
client = FleetClient(
    gateway_address="gateway:50051",
    token="eyJhbGciOiJIUzI1NiIs...",  # OIDC JWT from auth server
)

# Read-only RPCs are retried automatically (UNAVAILABLE, DEADLINE_EXCEEDED)
status = await client.get_status("lathe-01")
print(f"Mode: {status.mode}, State: {status.state}")

# Control commands
result = await client.send_mdi_command("lathe-01", "G0 X0 Y0 Z0")
await client.set_mode("lathe-01", Mode.MODE_AUTO)
await client.home_axis("lathe-01", axis="X")
await client.load_program("lathe-01", "/path/to/program.ngc")

# Streaming subscriptions (async generators)
async for status in client.subscribe_status("lathe-01"):
    print(status)
async for pin in client.subscribe_hal_pins("lathe-01"):
    print(pin.name, pin.value)
async for error in client.subscribe_errors("lathe-01"):
    print(error.code, error.message)

# Broadcast to all machines in a facility
await client.broadcast_mdi(scope="FACILITY", facility="shop-floor", command="G91 G28 Z")
await client.broadcast_mode_change(scope="FACILITY", facility="shop-floor", mode=Mode.MODE_AUTO)
await client.broadcast_execution(scope="FACILITY", facility="shop-floor", command="START")
```

## Logging

All components log to stderr by default. Use `--syslog` to also send logs to the system syslog (useful for production deployments with journald or rsyslog):

```bash
# Sidecar — logs to both stderr and syslog
headless-server --ini /path/to/machine.ini --machine-id lathe-01 --syslog

# Gateway — logs to both stderr and a custom facility
fleet-gateway --jwt-secret mysecret --syslog --syslog-facility local0

# Use systemd-journald socket instead of /dev/log
headless-server --ini /path/to/machine.ini --syslog --syslog-address /run/systemd/journal/syslog
```

Syslog messages use a simplified format (`LEVEL COMPONENT: message`) and are sent with the configured facility. Console output retains full timestamps and formatting for interactive use.

## Protocol

The gRPC protocol is defined in `proto/fleet.proto`. Key RPCs:

### FleetService (per-instance)

| RPC | Description |
|-----|-------------|
| `GetStatus` | Machine state snapshot (mode, position, estop, errors) |
| `SubscribeStatus` | Server-streaming status updates (~50Hz) |
| `SetMode` | Change machine mode (manual/auto/mda) |
| `HomeAxis` | Home a specific axis |
| `SendMdiCommand` | Execute MDI command string |
| `LoadProgram` | Load and prepare a G-code program |
| `GetHalPins` | List HAL component pins and values |
| `WriteHalPin` | Write a value to a HAL pin |
| `GetErrors` | Active error list |

### FleetGatewayService (central)

| RPC | Description |
|-----|-------------|
| `DiscoverMachines` | List all registered machines in scope |
| `RouteMachine` | Get address/port for a specific machine |
| `BroadcastCommand` | Fan-out a command to multiple machines |
| `SubscribeAllStatus` | Stream status from all machines in scope |

## Authentication & Authorization

- **OIDC/JWT** — HS256 (symmetric key) or RS256/RS384/RS512 (JWKS asymmetric). Tokens validated on every RPC.
- **Role hierarchy**: `admin` > `maintainer` > `programmer` > `operator` > `viewer`. Each role maps to allowed operations:

| Operation | Viewer | Operator | Programmer | Maintainer | Admin |
|-----------|--------|----------|------------|------------|-------|
| Discover machines | Yes | Yes | Yes | Yes | Yes |
| Read status | Yes | Yes | Yes | Yes | Yes |
| Read HAL pins | Yes | Yes | Yes | Yes | Yes |
| Write HAL pins | No | Yes | Yes | Yes | Yes |
| Set mode / MDI / Home | No | Yes | Yes | Yes | Yes |
| Control step | No | No | Yes | Yes | Yes |
| Load program | No | No | Yes | Yes | Yes |
| Control execution | No | No | No | Yes | Yes |
| Broadcast commands | No | No | No | No | Yes |

- **Facility scoping** — users can be restricted to a facility; cross-facility operations require admin role.

## Project Structure

```
proto/
    fleet.proto              # gRPC protocol definition
linuxcnc_fleet/
    headless.py              # LinuxCncSidecar — polling loop, state mapping, atomic snapshots
    server.py                # gRPC server creation (insecure + TLS/mTLS)
    cli.py                   # CLI entry point: headless-server
    auth.py                  # Server-side mTLS interceptor for OIDC token extraction
    logging_config.py        # Shared logging setup (console + optional syslog)
gateway/
    auth.py                  # OIDC token validation (HS256 + RS256 via JWKS)
    policies.py              # RBAC policy engine with role hierarchy and facility scoping
    registry.py              # Machine registry with TTL heartbeat expiry
    server.py                # FleetGatewayService RPC handlers
    cli.py                   # CLI entry point: fleet-gateway
fleet_client/
    auth.py                  # gRPC interceptor that injects OIDC tokens into every call
    client.py                # FleetClient — async wrappers, retry logic, channel caching, streaming
fleet_ui/
    server.py                # Web dashboard (Python/AIOHTTP)
tests/
    conftest.py              # Mock linuxcnc/_hal modules injected via pytest_configure
    test_state_mapping.py    # State enum mapping tests
    test_snapshot.py         # Atomic snapshot swap tests
    test_sidecar.py          # Sidecar polling and RPC handler tests
    test_cli.py              # CLI argument parsing tests
    test_auth.py             # OIDC token validation tests
    test_policies.py         # RBAC policy engine tests (62 tests)
    test_registry.py         # Machine registry heartbeat/expiry tests
    test_gateway.py          # Gateway RPC handler tests
    test_gateway_cli.py      # Gateway CLI tests
    test_interceptor.py      # Server-side mTLS/OIDC interceptor tests
    test_fleet_client.py     # FleetClient async wrappers, retry, streaming tests
    test_integration.py      # Full flow: FleetClient → Gateway → Sidecar (17 tests)
    test_logging_config.py   # Syslog logging configuration tests (16 tests)
```

## Testing

All 379 tests pass:

```bash
python -m pytest tests/ -v
```

Test breakdown by phase:

| Phase | Component | Tests | Status |
|-------|-----------|-------|--------|
| 1 | Core Sidecar | 81 | Passing |
| 2 | Gateway & Auth | 219 | Passing |
| 3 | FleetClient | 46 | Passing |
| 4 | Integration | 17 | Passing |
| 5 | Syslog Logging | 16 | Passing |

## License

MIT
