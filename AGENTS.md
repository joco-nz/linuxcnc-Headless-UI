# AGENTS.md — linuxcnc-Headless-UI

## Current State

**Phase 1: Core Sidecar is complete.** The sidecar component (proto, LinuxCncSidecar, gRPC server, CLI) is implemented with 73/73 unit tests passing. Next phase is Phase 2: Gateway & Auth.

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

## When Implementing Phase 2+

- The sidecar wraps existing LinuxCNC Python modules (`linuxcnc.stat`, `linuxcnc.command`, `_hal`). These are **not** installed here — they exist only on target machines.
- State mapping from `linuxcnc.stat.*` values to protobuf enums is defined in the plan (lines 432–455). Trust that mapping.
- The polling loop runs at 50Hz with atomic snapshot swaps (no locks needed).

## Setup Notes

- Project uses `pyproject.toml` with dependencies: grpcio>=1.80.0, grpcio-tools>=1.80.0, protobuf>=6.33.0
- Entry point: `headless-server = "linuxcnc_fleet.cli:main"`
- Run tests: `python -m pytest tests/ -v`
- Target machines need: LinuxCNC installed, Python 3.10+, the `linuxcnc` and `_hal` C extensions (bundled with LinuxCNC).
