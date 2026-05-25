# AGENTS.md — linuxcnc-Headless-UI

## Current State

This repo is in **planning phase**. The single source of truth for architecture and implementation plan is `headless_ui.md`. There is no code yet.

## Architecture Source

Read `headless_ui.md` before making any changes. It defines:
- The full gRPC protocol (`fleet.proto` — see the Protocol Definition section)
- The Python stack: grpcio, protobuf, linuxcnc/_hal modules (already on target machines)
- Three components: **sidecar** (`linuxcnc_fleet/`), **gateway** (`gateway/`), **client** (`fleet_client/`)
- 4 implementation phases (Weeks 1–8). Respect this order unless told otherwise.
- The file layout target (line 728) — use it as the directory structure template.

## When Implementing

- Start with Phase 1: proto generation → sidecar (`LinuxCncSidecar` in `headless.py`) → gRPC server (`server.py`).
- The sidecar wraps existing LinuxCNC Python modules (`linuxcnc.stat`, `linuxcnc.command`, `_hal`). These are **not** installed here — they exist only on target machines.
- State mapping from `linuxcnc.stat.*` values to protobuf enums is defined in the plan (lines 432–455). Trust that mapping.
- The polling loop runs at 50Hz with atomic snapshot swaps (no locks needed).

## Setup Notes

- No `pyproject.toml`, no lockfile, no CI — nothing exists yet. An agent should create these when moving from planning to implementation.
- Target machines need: LinuxCNC installed, Python 3.10+, the `linuxcnc` and `_hal` C extensions (bundled with LinuxCNC).
