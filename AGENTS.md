# AGENTS.md — linuxcnc-Headless-UI

## Current State

**Phase 1: Core Sidecar is complete.** The sidecar component (proto, LinuxCncSidecar, gRPC server, CLI) is implemented with 73/73 unit tests passing.

**Phase 2: Gateway & Auth is complete.** The gateway package (auth, policies, registry, server, CLI) and mTLS interceptor are implemented with 208/208 unit tests passing.

**Total: 281/281 tests passing**

## Architecture Source

Read `headless_ui.md` before making any changes. It defines:
- The full gRPC protocol (`fleet.proto` — see the Protocol Definition section)
- The Python stack: grpcio, protobuf, linuxcnc/_hal modules (already on target machines)
- Three components: **sidecar** (`linuxcnc_fleet/`), **gateway** (`gateway/`), **client** (`fleet_client/`)
- 4 implementation phases (Weeks 1–8). Respect this order unless told otherwise.
- The file layout target (line 728) — use it as the directory structure template.

## Phase 1 Deliverables

| Component | Files | Tests | Status |
|-----------|-------|-------|--------|
| Proto + stubs | `proto/fleet.proto`, `linuxcnc_fleet/fleet_pb2.py`, `fleet_pb2_grpc.py` | — | ✅ |
| Sidecar | `linuxcnc_fleet/headless.py` (~699 lines) | 20 (test_state_mapping.py) + 7 (test_snapshot.py) + 28 (test_sidecar.py) | ✅ |
| gRPC server | `linuxcnc_fleet/server.py` (~314 lines) | — | ✅ |
| CLI | `linuxcnc_fleet/cli.py` (~90 lines) | 18 (test_cli.py) | ✅ |
| Test infra | `tests/conftest.py` (mock linuxcnc/_hal via pytest_configure) | | ✅ |

### Key implementation notes
- `LinuxCncSidecar.shutdown()` stops the polling loop (not `stop()` — that's the execution stop RPC handler)
- State mapping functions use protobuf enum values directly (no `.value` — they are ints)
- Proto uses `MODE_MDA`, not `MODE_MDI` (linuxcnc constant is `MODE_MDI`)
- Mock linuxcnc/_hal modules injected via `pytest_configure` in conftest.py (before test collection)

## Phase 2 Deliverables

| Component | Files | Tests | Status |
|-----------|-------|-------|--------|
| Auth module | `gateway/auth.py` (~228 lines) | 32 (test_auth.py) | ✅ |
| Policy engine | `gateway/policies.py` (~303 lines) | 50 (test_policies.py) | ✅ |
| Registry | `gateway/registry.py` (~206 lines) | 41 (test_registry.py) | ✅ |
| Gateway server | `gateway/server.py` (~401 lines) | 35 (test_gateway.py) | ✅ |
| Gateway CLI | `gateway/cli.py` (~147 lines) | 20 (test_gateway_cli.py) | ✅ |
| mTLS interceptor | `linuxcnc_fleet/auth.py` (~120 lines) | 18 (test_interceptor.py) | ✅ |
| Server auth wiring | `linuxcnc_fleet/server.py` (updated) | — | ✅ |
| CLI auth wiring | `linuxcnc_fleet/cli.py` (updated) | — | ✅ |

### Key implementation notes
- Gateway AuthManager uses `secret_key` parameter (not `secret`) and requires `issuer` + `audience`
- mTLS interceptor uses callable-based user_extractor (decoupled from specific AuthManager)
- FleetServiceRPC checks auth context for control/write/admin operations via role hierarchy
- Gateway CLI validates args before starting server, exits with code 1 on validation errors
- All tests pass: 281 total (73 Phase 1 + 208 Phase 2)

## When Implementing Phase 2+

- The sidecar wraps existing LinuxCNC Python modules (`linuxcnc.stat`, `linuxcnc.command`, `_hal`). These are **not** installed here — they exist only on target machines.
- State mapping from `linuxcnc.stat.*` values to protobuf enums is defined in the plan (lines 432–455). Trust that mapping.
- The polling loop runs at 50Hz with atomic snapshot swaps (no locks needed).

## Setup Notes

- Project uses `pyproject.toml` with dependencies: grpcio>=1.80.0, grpcio-tools>=1.80.0, protobuf>=6.33.0
- Entry point: `headless-server = "linuxcnc_fleet.cli:main"`
- Run tests: `python -m pytest tests/ -v`
- Target machines need: LinuxCNC installed, Python 3.10+, the `linuxcnc` and `_hal` C extensions (bundled with LinuxCNC).
