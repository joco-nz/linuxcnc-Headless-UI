# AGENTS.md — linuxcnc-Headless-UI

## Rules

- NEVER update, modify. or rewrite the PRD document.

- The PRD is read-only. Treat all requirements in PRD.md as fixed constraints.

## Current State

**Phase 1: Core Sidecar is complete.** The sidecar component (proto, LinuxCncSidecar, gRPC server, CLI, FleetServiceRPC) is implemented with 186/186 unit tests passing.

**Phase 2: Gateway & Auth is complete.** The gateway package (auth, policies, registry, server, CLI) and mTLS interceptor are implemented with 257/257 unit tests passing.

**Phase 3: FleetClient is complete.** The client library (OIDC auth interceptor, FleetClient with all RPC wrappers, retry logic, streaming subscriptions) is implemented with 66/66 unit tests passing.

**Phase 4: Integration Tests are complete.** Full flow tests (FleetClient → Gateway → Sidecar) with 17/17 tests passing.

**Phase 5: Syslog Logging is complete.** Shared logging module with syslog support and CLI integration with 16/16 unit tests passing.

**Phase 6: FleetUI Dashboard is complete.** aiohttp web dashboard with SSE streaming, HTML/CSS UI, and XSS protections with 70/70 unit tests passing.

**Phase 7: FleetApp E2E Integration Tests is complete.** End-to-end tests for FleetApp reactive/proactive renewal, auto-fetch startup, and full session lifecycle with real gateway + sidecar stack — 6/6 tests passing.

**Phase 8: Prometheus Metrics & Health is complete.** Prometheus metrics and readiness health endpoints for gateway and sidecar with 27/27 unit tests passing.

**Total: 843/843 tests passing** (612 original + 35 token issuance + 18 CLI HTTP args + 14 UI enhancements + 35 token refresh + 18 Phase 3 UI + 13 integration renewal + 6 FleetApp E2E + 27 Prometheus metrics)

### UI Enhancements — Token Issuance & Auto-Renewal

Full plan documented in `ui_enhancements.md`. All phases complete.

| Component              | Files                                 | Tests                     | Status |
| ---------------------- | ------------------------------------- | ------------------------- | ------ |
| Token servicer         | `gateway/server.py` — `TokenIssuanceServicer` | 35 (test_token_issuance.py) | ✅      |
| HTTP route             | `gateway/server.py` — `_handle_auth_token`, `_handle_auth_token_wrapper` | —                          | ✅      |
| CLI args               | `gateway/cli.py` — 7 new arguments    | 18 (test_gateway_cli.py appended) | ✅      |
| Interceptor refresh    | `fleet_client/auth.py` — `refresh_token()` on both classes | 15 (test_fleet_client_auth.py appended) | ✅      |
| FleetClient refresh    | `fleet_client/client.py` — `refresh_token()` method | 17 (test_fleet_client.py appended) | ✅      |
| FleetApp proactive     | `fleet_ui/server.py` — `_start_proactive_refresh()` | 3 (test_fleet_ui.py) | ✅      |
| FleetApp reactive      | `fleet_ui/server.py` — `_grpc_call_with_retry()` | 2 (test_fleet_ui.py) | ✅      |
| Auth status endpoint   | `fleet_ui/server.py` — `/api/auth/status` | 4 (test_fleet_ui.py) | ✅      |
| UI auto-fetch flow     | `fleet_ui/server.py` — `handle_index` | 1 (test_fleet_ui.py) | ✅      |
| CLI http-port          | `gateway/cli.py` — `--http-port` arg  | 1 (test_gateway_cli.py) | ✅      |

### Phase 7: Integration Renewal Tests (NEW)

End-to-end integration tests for full token lifecycle (issue → use → expire → renew → continue working).

| Component        | Files                                 | Tests                     | Status |
| ---------------- | ------------------------------------- | ------------------------- | ------ |
| Token issuance   | `tests/test_integration_renewal.py` — 6 test classes | 13 (test_integration_renewal.py) | ✅      |

### Key implementation notes

- 13 integration tests across 6 test classes: `TestTokenIssueAndUse`, `TestTokenExpiryAndRenewal`, `TestFleetClientRenewalFlow`, `TestProactiveRenewalFlow`, `TestReactiveRenewalFlow`, `TestTokenSecurityModel`
- Helper `_start_gateway_with_http()` starts real gRPC + HTTP servers in background threads with dynamic port allocation
- Token TTL set to 3 seconds for tests (vs production 900s) to accelerate expiry/renewal cycles
- Fixed: changed `MachineId(name="...")` to `DiscoverRequest(facility="")` per existing test patterns; adjusted assertions for HTTP token response format; verified viewer tokens without facility claim return empty machine list (correct policy engine behavior)

### Phase 7: FleetApp E2E Integration Tests (NEW)

End-to-end integration tests for FleetApp with real gateway + sidecar stack. Covers reactive renewal, proactive refresh, auto-fetch startup, and full session lifecycle.

| Component        | Files                                 | Tests                     | Status |
| ---------------- | ------------------------------------- | ------------------------- | ------ |
| E2E integration  | `tests/test_integration_e2e.py`       | 6 (test_integration_e2e.py — NEW) | ✅      |

### Key implementation notes

- 6 tests across 4 test classes: `TestFleetAppReactiveRenewal`, `TestFleetAppProactiveRenewal`, `TestFleetAppAutoFetch`, `TestE2EActiveSession`
- Full stack fixture: sidecar + gateway with HTTP token issuance (`allow_admin_token=True`, `allowed_roles=["viewer", "operator", "admin"]`)
- Reactive renewal: FleetApp's `_grpc_call_with_retry()` catches UNAUTHENTICATED, calls `_fetch_token()`, calls `client.refresh_token()`, retries operation
- Proactive renewal: FleetApp's `_start_proactive_refresh()` background task calls `_fetch_token()` every 30s when near-expiry
- Auto-fetch: FleetApp with empty token calls `_fetch_token()` on startup to get JWT from gateway HTTP endpoint
- E2E lifecycle: issues admin token → discovers machines → waits for expiry → reactive renewal fetches new token → continues working
- `discover_machines()` swallows exceptions and returns `[]` on error (correct UI behavior)
- Fix applied: indentation error in `test_proactive_refresh_fetches_from_gateway_http` test method

### Phase 8: Prometheus Metrics & Health (NEW)

Prometheus metrics and readiness health endpoints for gateway and sidecar with 27/27 unit tests passing.

| Component        | Files                                 | Tests                     | Status |
| ---------------- | ------------------------------------- | ------------------------- | ------ |
| Sidecar metrics  | `linuxcnc_fleet/metrics.py` (85 lines)| 12 (test_sidecar_metrics.py — NEW) | ✅      |
| Gateway metrics  | `gateway/metrics.py` (98 lines)       | 16 (test_gateway_metrics.py — NEW) | ✅      |

### Key implementation notes

- Readiness-only health endpoint — returns `{"status": "ok", ...}` without liveness probe
- Gateway reuses existing `--http-port` aiohttp AppRunner — adds `/health` and `/metrics` routes
- Sidecar gets new `--metrics-port` flag — separate HTTP server via aiohttp on opt-in port
- Metrics format: Prometheus text exposition (`text/plain; version=0.0.4`)
- Sidecar metrics: counters for polls, HAL reads/writes, commands, errors + gauge for snapshot state
- Gateway metrics: counters for RPCs, broadcasts, tokens issued + gauges for registered machines
- Registry access: gateway metrics read from `MachineRegistry`; sidecar reads from `LinuxCncSidecar` snapshot

## Phase 3 Deliverables

| Component        | Files                                 | Tests                     | Status |
| ---------------- | ------------------------------------- | ------------------------- | ------ |
| Auth interceptor   | `fleet_client/auth.py` (92 lines)     | 20 (test_fleet_client_auth.py — NEW)    | ✅      |
| FleetClient        | `fleet_client/client.py` (1173 lines) | 46 (test_fleet_client.py)               | ✅      |

### Key implementation notes

- Async-only FleetClient with automatic OIDC token injection via gRPC interceptor
- Machine channel caching with TTL expiry (default 300s) and thread-safe cleanup
- Retry only for read-only RPCs (exponential backoff, 3 retries max) — catches UNAVAILABLE/DEADLINE_EXCEEDED/RESOURCE_EXHAUSTED
- FleetClient constructor accepts `_gateway_stub`, `_fleet_stub_factory`, and `_gateway_channel` params for test injection
- Streaming subscriptions implemented as async generators: `subscribe_status()`, `subscribe_hal_pins()`, `subscribe_errors()`
- FleetClient covers all FleetService RPCs including home_axis(), load_program(), send_mdi_command()

## Phase 4 Deliverables

| Component         | Files                                   | Tests | Status |
| ----------------- | --------------------------------------- | ----- | ------ |
| Integration tests | `tests/test_integration.py` (589 lines) | 17    | ✅      |

### Test classes

- `TestDiscoverRouteGetStatus`: discover, route, get_status_via_gateway, viewer_can_discover (4 tests)
- `TestBroadcastCommand`: broadcast_mdi_to_all, broadcast_mode_change (2 tests)
- `TestStreamingStatus`: subscribe_all_status (1 test)
- `TestSidecarDirectCommands`: set_mode, home_axis, send_mdi_command, load_program, subscribe_status_stream, get_errors (6 tests)
- `TestGatewayAuthIntegration`: unauthenticated_request_rejected, viewer_cannot_broadcast (2 tests)
- `TestRegistryHeartbeat`: heartbeat_updates_last_seen, expired_machine_removed (2 tests)

### Key implementation notes

- Integration tests use real gRPC servers (not stubs) to exercise serialization, channel setup, auth interceptor chaining, broadcast fan-out
- Fixture chain: `sidecar_server` → `gateway_server` → `multi_gateway_server` with proper teardown via stop functions
- Fixed bugs discovered during integration testing:
  - Missing `FleetServiceStub` import in `gateway/server.py` (caused streaming to fail silently)
  - Added `context.is_active()` check in `SubscribeAllStatus` to prevent hanging on client disconnect
  - Mock `estop_state` default changed from `ESTOP_ACK` to `0` in conftest (was blocking mode-change RPCs)
- All tests pass: 526 total (186 Phase 1 + 257 Phase 2 + 66 FleetClient + 17 Integration)

### Fixed Known Issues

All issues documented in `headless_ui.md` have been resolved:

| #   | File                     | Issue                                                                                | Status  |
| --- | ------------------------ | ------------------------------------------------------------------------------------ | ------- |
| 1   | `gateway/server.py`      | Type hint `DiscoveryRequest` didn't exist — should be `DiscoverRequest`              | ✅ Fixed |
| 2   | `linuxcnc_fleet/cli.py`  | `AuthManager(secret=...)` was wrong — now uses `AuthManager(secret_key=...)`         | ✅ Fixed |
| 3   | `fleet_client/client.py` | `_get_or_create_machine_channel()` created insecure channels when `tls_enabled=True` | ✅ Fixed |

## Architecture Source

Read `headless_ui.md` before making any changes. It defines:

- The full gRPC protocol (`fleet.proto` — see the Protocol Definition section)
- The Python stack: grpcio, protobuf, linuxcnc/_hal modules (already on target machines)
- Four components: **sidecar** (`linuxcnc_fleet/`), **gateway** (`gateway/`), **client** (`fleet_client/`), **dashboard** (`fleet_ui/`)
- 6 implementation phases (Weeks 1–12). Respect this order unless told otherwise.
- The file layout target (line 728) — use it as the directory structure template.

## Phase 1 Deliverables

| Component     | Files                                                                   | Tests                                                                    | Status |
| ------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------ |
| Proto + stubs | `proto/fleet.proto`, `linuxcnc_fleet/fleet_pb2.py`, `fleet_pb2_grpc.py` | —                                                                        | ✅      |
| Sidecar              | `linuxcnc_fleet/headless.py` (759 lines)                  | 26 (test_state_mapping.py) + 7 (test_snapshot.py) + 63 (test_sidecar.py)               | ✅      |
| FleetServiceRPC      | `linuxcnc_fleet/server.py` (486 lines, updated)           | 64 (test_fleet_service_rpc.py — NEW)                                                   | ✅      |
| gRPC server   | `linuxcnc_fleet/server.py` (484 lines)                                  | —                                                                        | ✅      |
| CLI           | `linuxcnc_fleet/cli.py` (167 lines)                                     | 26 (test_cli.py)                                                         | ✅      |
| Test infra    | `tests/conftest.py` (mock linuxcnc/_hal via pytest_configure)           |                                                                          | ✅      |

### Key implementation notes

- `LinuxCncSidecar.shutdown()` stops the polling loop (not `stop()` — that's the execution stop RPC handler)
- State mapping functions use protobuf enum values directly (no `.value` — they are ints)
- Proto uses `MODE_MDA`, not `MODE_MDI` (linuxcnc constant is `MODE_MDI`)
- Mock linuxcnc/_hal modules injected via `pytest_configure` in conftest.py (before test collection)

## Phase 2 Deliverables

| Component          | Files                                | Tests                    | Status |
| ------------------ | ------------------------------------ | ------------------------ | ------ |
| Auth module        | `gateway/auth.py` (250 lines)        | 38 (test_auth.py)        | ✅      |
| Policy engine      | `gateway/policies.py` (315 lines)    | 62 (test_policies.py)    | ✅      |
| Registry           | `gateway/registry.py` (206 lines)    | 41 (test_registry.py)    | ✅      |
| Gateway server     | `gateway/server.py` (525 lines)      | 64 (test_gateway.py)     | ✅      |
| Gateway CLI        | `gateway/cli.py` (174 lines)         | 31 (test_gateway_cli.py) | ✅      |
| mTLS interceptor   | `linuxcnc_fleet/auth.py` (167 lines) | 21 (test_interceptor.py) | ✅      |
| Server auth wiring | `linuxcnc_fleet/server.py` (updated) | —                        | ✅      |
| CLI auth wiring    | `linuxcnc_fleet/cli.py` (updated)    | —                        | ✅      |

### Key implementation notes

- Gateway AuthManager uses `secret_key` parameter (not `secret`) and requires `issuer` + `audience`
- mTLS interceptor uses callable-based user_extractor (decoupled from specific AuthManager)
- FleetServiceRPC checks auth context for control/write/admin operations via role hierarchy
- Gateway CLI validates args before starting server, exits with code 1 on validation errors
- All tests pass: 443 total (186 Phase 1 + 257 Phase 2)

## When Implementing Phase 2+

- The sidecar wraps existing LinuxCNC Python modules (`linuxcnc.stat`, `linuxcnc.command`, `_hal`). These are **not** installed here — they exist only on target machines.
- State mapping from `linuxcnc.stat.*` values to protobuf enums is defined in the plan (lines 432–455). Trust that mapping.
- The polling loop runs at 50Hz with atomic snapshot swaps (no locks needed).

## Package Distribution

The project builds as a single `linuxcnc-fleet` pip package with optional dependency groups:

```bash
# Build distribution
python -m build

# Install from wheel
pip install dist/linuxcnc_fleet-0.1.0-py3-none-any.whl

# Install with extras
pip install "linuxcnc-fleet[sidecar]"   # sidecar + core deps
pip install "linuxcnc-fleet[gateway]"   # gateway + core + PyJWT, cryptography
pip install "linuxcnc-fleet[client]"    # client + core deps
pip install "linuxcnc-fleet[dev]"       # dev tools (pytest, mypy)
```

### Entry points

- `headless-server` → `linuxcnc_fleet.cli:main`
- `fleet-gateway` → `gateway.cli:main`
- `fleet-ui` → `fleet_ui.server:main`

### Proto code generation

Run from project root:

```bash
python -m grpc_tools.protoc -Iproto \
    --python_out=linuxcnc_fleet \
    --grpc_python_out=linuxcnc_fleet \
    fleet.proto
# Then fix imports: sed -i 's/^import fleet_pb2 as/import linuxcnc_fleet.fleet_pb2 as/' linuxcnc_fleet/fleet_pb2_grpc.py
```

Key points:
- `-Iproto` (not `-I.`) — the include path must point to the proto directory, and input is just `fleet.proto`
- Output goes into `linuxcnc_fleet/` directly (not `linuxcnc_fleet/proto/`)
- The generated `fleet_pb2_grpc.py` uses top-level imports (`import fleet_pb2`) which break when installed as a package. A post-generation sed fix is required to change it to `import linuxcnc_fleet.fleet_pb2`.

## Setup Notes

- Project uses `pyproject.toml` with dependencies: grpcio>=1.60.0, protobuf>=4.25.0 (core), grpcio-tools>=1.60.0 (optional)
- Entry points: `headless-server`, `fleet-gateway`, `fleet-ui`
- Run tests: `python -m pytest tests/ -v`
- Build distributions: `python -m build` (produces sdist + wheel in `dist/`)
- Target machines need: LinuxCNC installed, Python 3.10+, the `linuxcnc` and `_hal` C extensions (bundled with LinuxCNC).
