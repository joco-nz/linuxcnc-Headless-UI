# Product Requirements Document — LinuxCNC Fleet

> **Source of truth:** Reverse-engineered from the `linuxcnc-Headless-UI` codebase (git repo). All requirements are derived from actual implemented code, proto definitions, test coverage, and configuration files.

---

## 1. Core Purpose

### Problem

Manufacturing facilities operate fleets of LinuxCNC machines that lack a centralized, remote monitoring and control interface. Traditional access requires physically visiting each machine or using legacy telnet-based interfaces (ports 5006/5007). Operators, programmers, and maintenance staff need real-time visibility into machine states across multiple workcells without modifying the LinuxCNC C++ core or real-time components.

### Solution

LinuxCNC Fleet provides a gRPC-based architecture that wraps each LinuxCNC instance with a lightweight Python sidecar, exposes machine telemetry and control commands through a centralized gateway with authentication/authorization, and delivers a web dashboard for fleet-wide monitoring.

### Target Audience

| Persona                   | Role         | Primary Need                                                               |
| ------------------------- | ------------ | -------------------------------------------------------------------------- |
| Shop Floor Operator       | `operator`   | Monitor running machines, change modes, home axes, start/stop cycles       |
| CNC Programmer            | `programmer` | Load G-code programs, send MDI commands, step through programs             |
| Maintenance Technician    | `maintainer` | Full machine control, HAL pin access for diagnostics, execution control    |
| Facility Manager / Viewer | `viewer`     | Read-only dashboard visibility across all machines in a facility           |
| System Administrator      | `admin`      | Global access to all machines, broadcast commands, unrestricted operations |

---

## 2. User Personas & Stories

### As a Shop Floor Operator

- **As an** operator, **I want to** see the state, mode, position, and E-stop status of every machine on my shop floor at a glance, **so that** I can quickly assess production readiness.
- **As an** operator, **I want to** switch a machine between manual/auto/MDA mode with one click, **so that** I can prepare it for different tasks.
- **As an** operator, **I want to** start, stop, feed-hold, and continue execution of running programs remotely, **so that** I can manage cycles without being at the machine console.
- **As an** operator, **I want to** home all axes or a specific axis, **so that** I can reset positions after setup.

### As a CNC Programmer

- **As a** programmer, **I want to** load G-code programs onto machines and send MDI commands, **so that** I can test toolpaths without physical access.
- **As a** programmer, **I want to** step through a program one block at a time, **so that** I can debug code safely.
- **As a** programmer, **I want to** see the current interpreter line and program file being executed, **so that** I can track progress during runs.

### As a Maintenance Technician

- **As a** maintainer, **I want to** read and write HAL pins on machines in my facility, **so that** I can diagnose sensor/actuator issues through the HAL interface.
- **As a** maintainer, **I want to** see all active errors from the LinuxCNC error channel, **so that** I can troubleshoot machine faults.
- **As a** maintainer, **I want to** control execution states (fast run, retract, MDA mode), **so that** I can perform maintenance procedures.

### As a Viewer / Facility Manager

- **As a** viewer, **I want to** discover all machines in my facility and see their live status, **so that** I can monitor overall production without risking accidental changes.
- **As a** viewer, **I want to** subscribe to real-time status streams via SSE, **so that** I get automatic updates without polling.

### As a System Administrator

- **As an** admin, **I want to** broadcast MDI commands or mode changes to all machines (or by facility/tag), **so that** I can execute coordinated procedures across the fleet.
- **As an** admin, **I want to** bypass all facility and tag scoping restrictions, **so that** I can manage any machine in the organization.

---

## 3. Functional Requirements

### Epic: Machine Telemetry & Status

#### FR-1: Real-Time Status Polling

- **Input:** LinuxCNC `linuxcnc.stat` bindings (state, execution, interp_state, estop, mode, positions, feedrate, spindle speed, coolant state, errors)
- **Behavior:** Background polling thread runs at 50Hz (20ms interval). Each poll extracts all status fields from linuxcnc module and produces an immutable `_Snapshot` dataclass. Snapshots are atomically swapped via reference assignment (no locks needed — single-writer, multi-reader pattern).
- **Output:** `MachineStatus` protobuf message with machine_id, state enums, joint/world/position coordinates, feedrate, spindle speed, coolant flags, active errors, cycle time.

#### FR-2: Status Streaming

- **Input:** `MachineId` request (single machine) or `SubscribeAllRequest` with facility scope (all machines).
- **Behavior:** Server-streaming RPCs yield `MachineStatus` snapshots at the polling rate. `SubscribeAllStatus` fans out to multiple machines via background daemon threads, interleaving results into a single server-stream response.
- **Output:** Continuous stream of `MachineStatus` messages.

#### FR-3: Error Log Access

- **Input:** `GetErrorsRequest` with optional limit.
- **Behavior:** Reads from LinuxCNC error channel, returns up to N most recent errors.
- **Output:** `ErrorList` containing `ErrorEvent` entries (message + timestamp).

### Epic: Machine Control

#### FR-4: Mode Selection

- **Input:** `SetModeRequest` with machine_id and target `Mode` (MANUAL, AUTO, MDA).
- **Behavior:** Calls `linuxcnc.command.mode()`. Validates E-stop state and machine readiness before allowing mode change.
- **Output:** `Result` with success flag, message, and optional `ErrorCode`.

#### FR-5: Execution Control

- **Input:** `ExecutionCommand` (start, stop, feed_hold, continue, home_all, home_axis, step_forward) or individual RPCs (`Start`, `Stop`, `FeedHold`, `Continue`, `HomeAll`, `HomeAxis`, `StepForward`).
- **Behavior:** Maps to corresponding `linuxcnc.command.*` methods. Each validates machine state and E-stop before executing.
- **Output:** `Result` protobuf.

#### FR-6: G-Code Program Management

- **Input:** `ProgramPath` with machine_id and file path, or `MdiCommand` with machine_id and command string.
- **Behavior:** Calls `linuxcnc.command.program_open()` for loading programs, `linuxcnc.command.mdi()` for MDI commands.
- **Output:** `Result` protobuf.

### Epic: HAL Interface

#### FR-7: HAL Component Discovery

- **Input:** `ListHalRequest` with machine_id.
- **Behavior:** Uses `_hal` Python module to enumerate all HAL components, their pins, and parameters. Returns component metadata (name, update period, pins list, params map).
- **Output:** `HalComponentList` containing `HalComponentInfo` entries.

#### FR-8: HAL Pin Read/Write

- **Input:** `HalPinRead` (pin_name) or `HalPinWrite` (pin_name + typed value: bit/u32/s32/float).
- **Behavior:** Reads/writes via `_hal` module. Writes validate that the target pin is an output type before proceeding.
- **Output:** `HalPinValue` for reads; `Result` for writes.

#### FR-9: HAL Pin Subscription

- **Input:** `HalPinSubscribe` with machine_id, list of pin names, and poll interval.
- **Behavior:** Server-streaming RPC that polls specified pins at the given interval and yields updates.
- **Output:** Stream of `HalPinUpdate` messages.

### Epic: Fleet Discovery & Routing

#### FR-10: Machine Registration

- **Input:** Sidecar calls `registry.register()` on startup with machine_id, address, port, facility, tags, version info.
- **Behavior:** Gateway stores entry in-memory with TTL-based heartbeat expiry (default 30s). Background cleanup thread removes expired entries every 60s.
- **Output:** `MachineEntry` record stored in registry.

#### FR-11: Machine Discovery

- **Input:** `DiscoverRequest` with optional facility filter.
- **Behavior:** Gateway queries registry for non-expired machines, filters by RBAC policy scope (facility matching), and returns list. Admin users see all machines.
- **Output:** `MachineList` containing `MachineInfo` entries.

#### FR-12: Machine Routing

- **Input:** `MachineId` request.
- **Behavior:** Gateway looks up machine in registry, returns address:port for direct gRPC connection to the sidecar.
- **Output:** `GatewayRoute` with instance_address and instance_port.

### Epic: Broadcast Operations

#### FR-13: Command Broadcasting

- **Input:** `BroadcastRequest` with scope (ALL/FACILITY/TAG), facility name, tags list, and one of: MDI command, execution command, or mode change.
- **Behavior:** Gateway resolves target machines based on scope, checks broadcast authorization per user role, then fans out commands synchronously to each target via per-machine gRPC stub calls.
- **Output:** `BroadcastResult` mapping machine_id to `Result` for each target.

### Epic: Central Web Dashboard

#### FR-14: HTTP API Endpoints

The `fleet_ui` package provides an aiohttp-based web server with these routes:

| Method | Path                        | Description                                                     |
| ------ | --------------------------- | --------------------------------------------------------------- |
| GET    | `/`                         | Serves the single-page HTML dashboard                           |
| POST   | `/api/connect`              | Initialize FleetClient with JWT token                           |
| GET    | `/api/machines`             | List all machines with latest status                            |
| GET    | `/api/status/{id}`          | Get full status for a specific machine                          |
| GET    | `/api/stream/status/{id}`   | SSE stream for real-time status updates                         |
| GET    | `/api/stream/all`           | SSE stream for all machines simultaneously                      |
| GET    | `/api/info/{id}`            | Get machine metadata (version, joints)                          |
| POST   | `/api/mode/{id}`            | Set machine mode                                                |
| POST   | `/api/mdi/{id}`             | Send MDI command                                                |
| POST   | `/api/program/{id}`         | Load a G-code program                                           |
| POST   | `/api/control/{id}/{cmd}`   | Execute motion control (start/stop/feed_hold/continue/home_all) |
| GET    | `/api/hal/{id}`             | List HAL components and pins                                    |
| GET    | `/api/hal/pin/{id}/{pin}`   | Read a single HAL pin                                           |
| POST   | `/api/hal/write/{id}/{pin}` | Write to a HAL output pin                                       |
| GET    | `/api/errors/{id}`          | Get error log for a machine                                     |

#### FR-15: SSE Streaming Dashboard

- **Input:** Browser connects to `/api/stream/status/{machine_id}` or `/api/stream/all`.
- **Behavior:** Server maintains `_SSEStream` instances with bounded queues (maxsize=100). Background tasks subscribe to FleetClient streaming RPCs and push updates to connected clients. Dropped on queue full.
- **Output:** `text/event-stream` response with JSON-encoded status data.

#### FR-16: Dashboard UI Features

- Machine list sidebar with color-coded status indicators (green=running, yellow=paused, gray=stopped, red pulsing=E-stop)
- Tabbed detail view per machine: Status cards, Controls, HAL Pins table, Error log
- Position display for joint and world coordinates
- Toast notifications for command results

---

## 4. System Architecture & Data Model

### 4.1 Tech Stack

| Layer                  | Technology                                     | Purpose                                               |
| ---------------------- | ---------------------------------------------- | ----------------------------------------------------- |
| RPC Protocol           | gRPC (protobuf 3)                              | Inter-service communication, streaming, strong typing |
| Server Framework       | grpcio + concurrent.futures.ThreadPoolExecutor | gRPC server per sidecar instance                      |
| Web Framework          | aiohttp                                        | Central dashboard HTTP/SSE server                     |
| Auth Library           | PyJWT >= 2.8.0                                 | OIDC token validation (HS256 + RS256/RS384/RS512)     |
| Crypto Library         | cryptography >= 41.0.0                         | JWK to PEM conversion for RSA key validation          |
| Message Serialization  | protobuf >= 4.25.0                             | Protocol buffer message definitions                   |
| HTTP Client (internal) | urllib.request (stdlib)                        | JWKS endpoint fetching                                |

### 4.2 Package Distribution

Single `linuxcnc-fleet` pip package with optional dependency groups:

```
linuxcnc-fleet==0.1.0
├── [sidecar]   → grpcio, grpcio-tools, protobuf
├── [gateway]   → + PyJWT, cryptography, aiohttp
├── [client]    → grpcio, protobuf
├── [ui]        → grpcio, protobuf, aiohttp
└── [dev]       → grpcio-tools, mypy, pytest, pytest-asyncio, aiohttp
```

**Entry points:**

- `headless-server` → `linuxcnc_fleet.cli:main`
- `fleet-gateway` → `gateway.cli:main`
- `fleet-ui` → `fleet_ui.server:main`

### 4.3 Data Models

#### MachineRegistry Entry (in-memory)

```python
@dataclass(frozen=True)
class MachineEntry:
    id: str                    # Unique machine identifier
    address: str               # Network address of sidecar gRPC server
    port: int                  # gRPC port (default 50051)
    facility: str              # Facility/workshop name for scoping
    tags: list[str]            # Arbitrary labels (e.g., ["cnc", "lathe"])
    version: str               # LinuxCNC version string
    git_hash: str              # Sidecar build hash
    last_heartbeat: float      # Unix timestamp of last heartbeat
    registered_at: float       # Unix timestamp of initial registration
```

#### User Identity (from OIDC claims)

```python
@dataclass(frozen=True)
class User:
    sub: str                   # Subject (unique user identifier)
    name: str                  # Display name
    email: Optional[str]       # Email address
    role: str = "viewer"       # viewer | operator | programmer | maintainer | admin
    facility: Optional[str]    # Assigned facility for scoping
    machine_tags: list[str]    # Tags associated with user scope
```

#### MachineStatus (protobuf)

```protobuf
message MachineStatus {
  string machine_id = 1;
  MachineState state = 2;           // OFF, INITIALIZING, RUNNING, PAUSED, HOLD, E_STOP, JOG, MANUAL, AUTO_DONE
  ExecutionState execution = 3;     // EXEC_IDLE, RUN, FAST_RUN, STEP, RETRACT, MDA
  InterpState interp_state = 4;     // INTERP_IDLE, READ, PREDICT, EXECUTE, ERROR
  EstopState estop_state = 5;       // UNKNOWABLE, NOT_E_STOPPED, E_STOPPED
  Mode mode = 6;                    // MODE_UNKNOWN, MODE_MANUAL, MODE_AUTO, MODE_MDA
  Position joint_actual = 7;        // Actual joint positions (x,y,z,a,b,c,u,v,w,p,q)
  Position joint_commanded = 8;     // Commanded joint positions
  Position world_actual = 9;        // World (cartesian) position
  int32 interp_line = 10;           // Current interpreter line number
  string program_file = 11;         // Currently running program path
  string remaining_time = 12;       // Estimated time remaining
  double feedrate = 13;             // Current feedrate
  double feedrate_override = 14;    // Feedrate override percentage
  double spindle_speed = 15;        // Current spindle RPM
  double spindle_speed_override = 16; // Spindle override percentage
  bool coolant_mist = 17;
  bool coolant_flood = 18;
  bool coolant_mazak = 19;
  repeated string active_errors = 20;
  double cycle_time = 21;
  int32 motion_type = 22;
}
```

#### State Mapping (linuxcnc → protobuf)

| linuxcnc Value | Protobuf Enum | Notes                                     |
| -------------- | ------------- | ----------------------------------------- |
| `RCS_IDLE`     | `OFF`         | Distinguished by execution field          |
| `RCS_RUNNING`  | `RUNNING`     | —                                         |
| `RCS_DONE`     | `AUTO_DONE`   | Program completed                         |
| `MODE_MDI`     | `MODE_MDA`    | Proto name differs from linuxcnc constant |

### 4.4 External Dependencies

| Dependency                          | On Whom               | Purpose                                                         |
| ----------------------------------- | --------------------- | --------------------------------------------------------------- |
| `linuxcnc` Python module            | Sidecar machines only | C++ extension providing stat/command/error_channel/ini bindings |
| `_hal` C extension                  | Sidecar machines only | HAL pin read/write/list operations                              |
| OIDC Provider (Keycloak/Auth0/etc.) | Gateway               | Token issuance; JWKS endpoint for RS256 validation              |

---

## 5. Non-Functional Requirements

### 5.1 Security

| Requirement                 | Implementation                                                                                                                      |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| **Transport encryption**    | TLS 1.3 on all gRPC channels (insecure mode available for dev)                                                                      |
| **Mutual TLS (mTLS)**       | Gateway uses client certificate signed by internal CA to authenticate to instances; instances validate gateway cert                 |
| **OIDC Authentication**     | HS256 symmetric or RS256/RS384/RS512 asymmetric via JWKS. Tokens validated on every RPC for signature, expiration, issuer, audience |
| **RBAC Authorization**      | 5-tier role hierarchy with 13 granular permissions (see section 5.2)                                                                |
| **Attribute-based scoping** | Facility and tag filtering restricts machine visibility per user role                                                               |
| **HAL write protection**    | Writes only allowed to output-type pins; validated at sidecar level                                                                 |
| **Token caching**           | JWKS keys cached for 5 minutes to reduce external API calls                                                                         |

### 5.2 Role Hierarchy & Permissions

```
viewer ──► operator ──► programmer ──► maintainer ──► admin
```

| Permission                              | Viewer   | Operator | Programmer | Maintainer | Admin             |
| --------------------------------------- | -------- | -------- | ---------- | ---------- | ----------------- |
| `read_status`                           | Yes      | Yes      | Yes        | Yes        | Yes               |
| `read_hal_pin`                          | Yes      | Yes      | Yes        | Yes        | Yes               |
| `write_hal_pin`                         | No       | Yes      | Yes        | Yes        | Yes               |
| `control_start/stop/hold/continue/home` | No       | Yes      | Yes        | Yes        | Yes               |
| `control_mode`                          | No       | Yes      | Yes        | Yes        | Yes               |
| `control_step`                          | No       | No       | Yes        | Yes        | Yes               |
| `control_execution`                     | No       | No       | No         | Yes        | Yes               |
| `load_program`                          | No       | No       | Yes        | Yes        | Yes               |
| `subscribe_status`                      | Yes      | Yes      | Yes        | Yes        | Yes               |
| **Scope**                               | Facility | Facility | Facility   | Facility   | Global (no scope) |

### 5.3 Performance

| Metric                  | Value                    | Notes                                                              |
| ----------------------- | ------------------------ | ------------------------------------------------------------------ |
| Status polling rate     | 50Hz (20ms)              | Background daemon thread per sidecar                               |
| Snapshot update latency | ~1 message delay (≤20ms) | Atomic reference swap, no locks                                    |
| Channel caching TTL     | 300s (configurable)      | FleetClient machine channels cached with lazy cleanup              |
| SSE queue depth         | 100 messages max         | QueueFull triggers warning log + drop                              |
| JWKS cache TTL          | 300s                     | Reduces external OIDC provider calls                               |
| Registry heartbeat TTL  | 30s (configurable)       | Expired entries cleaned up on lookup + background thread every 60s |

### 5.4 Scalability Design

- **Decoupled architecture:** Gateway routes to instances but does not proxy data plane traffic — FleetClient connects directly to sidecars after routing resolution.
- **In-memory registry:** No database dependency; suitable for single-gateway deployments with hundreds of machines.
- **Thread-per-sidecar:** Each gRPC server uses ThreadPoolExecutor(max_workers=8).
- **Async client library:** FleetClient is fully async, enabling concurrent operations across many machines.

### 5.5 Reliability

| Feature                        | Implementation                                                                                                         |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| **Retry logic**                | Automatic exponential backoff (max 3 retries) for read-only RPCs on UNAVAILABLE/DEADLINE_EXCEEDED/RESOURCE_EXHAUSTED   |
| **Heartbeat expiry**           | Machines with no heartbeat for >TTL are automatically removed from registry                                            |
| **Client disconnect handling** | `context.is_active()` check in streaming loops prevents hanging on client disconnect                                   |
| **Atomic snapshots**           | No locks needed — single-writer creates new `_Snapshot` via `dataclasses.replace()`, reader swaps reference atomically |

---

## 6. Assumptions & Gaps

### 6.1 Incomplete / Deferred Features

| #   | Feature                               | Status                      | Notes                                                                                                  |
| --- | ------------------------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------ |
| 1   | systemd service template              | Deferred (planned)          | Template exists in `headless_ui.md` but not packaged as installable file                               |
| 2   | TLS/mTLS certificate management       | Deferred (planned)          | Certs directory exists (`certs/`) but auto-provisioning is out of scope                                |
| 3   | Load testing                          | Not started                 | No benchmarking for concurrent connections or broadcast performance                                    |
| 4   | Certificate auto-renewal              | Not started                 | No mechanism for cert rotation                                                                         |
| 5   | Prometheus metrics / health endpoints | Not started                 | No `/metrics` or `/health` HTTP endpoints                                                              |
| 6   | Real LinuxCNC integration testing     | Deferred                    | Requires target machine with actual LinuxCNC installed; all tests use mocked `linuxcnc`/`_hal` modules |
| 7   | FleetClient TLS for machine channels  | Bug-fixed but not re-tested | Previously created insecure channels when `tls_enabled=True`; fixed in code                            |

### 6.2 Codebase Observations (TODOs / Known Issues)

| #   | Location                         | Issue                                                                                | Severity | Status                                     |
| --- | -------------------------------- | ------------------------------------------------------------------------------------ | -------- | ------------------------------------------ |
| 1   | `gateway/server.py:133`          | Type hint `DiscoveryRequest` should be `DiscoverRequest`                             | Low      | Fixed in proto, may need cleanup in server |
| 2   | `linuxcnc_fleet/cli.py:117`      | Previously used `AuthManager(secret=...)` instead of `secret_key=...`                | High     | Fixed                                      |
| 3   | `fleet_client/client.py:187-191` | `_get_or_create_machine_channel()` created insecure channels when `tls_enabled=True` | Medium   | Fixed                                      |

### 6.3 Assumptions

1. **OIDC Provider Availability:** The system assumes an OIDC provider (Keycloak/Auth0) is available for token issuance. The gateway requires `issuer` + `audience` claims to be configured at startup. No local user registration exists — all identity comes from external tokens.

2. **Machine Registration Mechanism:** Sidecars are expected to call `registry.register()` on startup and heartbeat periodically. However, the current CLI (`headless-server`) does not automatically register with a gateway — registration is assumed to happen via an external orchestrator or manual API call to the gateway.

3. **Network Topology:** The architecture assumes a trusted internal network between gateway and sidecars. mTLS is recommended but insecure mode is available for development. No NAT traversal or VPN considerations are addressed.

4. **Single Gateway Deployment:** The registry is in-memory with no replication. A single gateway instance is the documented deployment model; horizontal scaling of the gateway layer is not addressed.

5. **LinuxCNC Version Compatibility:** The `linuxcnc` Python module is a C++ extension whose API may change between LinuxCNC releases. The sidecar directly accesses `linuxcnc.stat.*` fields, `linuxcnc.command.*` methods, and `_hal` module functions without version abstraction.

6. **No Program Storage:** The system loads programs by file path on the remote machine — there is no program repository or transfer mechanism. Programs must already exist on the target machine's filesystem.

7. **Direct Instance Access Option:** The architecture supports direct gRPC connections to sidecars (bypassing gateway proxy) after routing resolution via `RouteMachine`. However, the sidecar CLI does not currently expose both localhost (unauthenticated) and network (mTLS) interfaces simultaneously as discussed in open questions.

### 6.4 Open Questions from Design Document

1. **Gateway proxy vs. direct mTLS:** Current implementation uses direct mTLS to instances (gateway routes, client connects directly). This was the recommended approach.
2. **Local operator access:** Sidecar could listen on localhost without auth for local console operators — not yet implemented.
3. **Legacy telnet interfaces (5006/5007):** Not part of this system; remain available for existing tooling.
4. **HAL write acknowledgment:** Currently fire-and-forget. Safety-critical pins might need a confirm RPC with timeout window — not yet implemented.
5. **Machine registration without central config:** Auto-discovery via gateway registration on startup is the planned approach, but auto-registration from `headless-server` CLI is not wired up.

---

## 7. Test Coverage Summary

**Total: 344 tests passing across 13 test files**

| Phase | Component    | Test File               | Tests | Status  |
| ----- | ------------ | ----------------------- | ----- | ------- |
| 1     | Core Sidecar | `test_state_mapping.py` | 26    | Passing |
| 1     | Core Sidecar | `test_snapshot.py`      | 7     | Passing |
| 1     | Core Sidecar | `test_sidecar.py`       | 22    | Passing |
| 1     | CLI          | `test_cli.py`           | 18    | Passing |
| 2     | Auth         | `test_auth.py`          | 31    | Passing |
| 2     | Policies     | `test_policies.py`      | 62    | Passing |
| 2     | Registry     | `test_registry.py`      | 41    | Passing |
| 2     | Gateway      | `test_gateway.py`       | 35    | Passing |
| 2     | Gateway CLI  | `test_gateway_cli.py`   | 20    | Passing |
| 2     | Interceptor  | `test_interceptor.py`   | 19    | Passing |
| 3     | FleetClient  | `test_fleet_client.py`  | 46    | Passing |
| 4     | Integration  | `test_integration.py`   | 17    | Passing |

---

## 8. File Layout

```
linuxcnc-fleet/
├── proto/
│   └── fleet.proto              # gRPC service definition (354 lines, 2 services, 30+ messages)
├── linuxcnc_fleet/
│   ├── __init__.py
│   ├── headless.py              # LinuxCncSidecar class — polling loop, state mapping (~699 lines)
│   ├── server.py                # gRPC server creation + FleetServiceServicer (~435 lines)
│   ├── cli.py                   # CLI entry point: headless-server (148 lines)
│   ├── auth.py                  # Server-side mTLS/OIDC interceptor (~146 lines)
│   ├── fleet_pb2.py             # Generated protobuf messages
│   ├── fleet_pb2_grpc.py        # Generated gRPC stubs
│   └── proto/                   # Proto source copy for package distribution
├── gateway/
│   ├── __init__.py
│   ├── server.py                # FleetGatewayService RPC handlers (~488 lines)
│   ├── auth.py                  # OIDC token validation (HS256 + RS256 via JWKS) (234 lines)
│   ├── policies.py              # RBAC policy engine with role hierarchy (303 lines)
│   ├── registry.py              # Machine registry with TTL heartbeat expiry (206 lines)
│   └── cli.py                   # Gateway CLI entry point: fleet-gateway (~144 lines)
├── fleet_client/
│   ├── __init__.py
│   ├── client.py                # FleetClient high-level library (~1058 lines)
│   └── auth.py                  # OIDC bearer auth gRPC interceptor (~60 lines)
├── fleet_ui/
│   ├── __init__.py
│   └── server.py                # aiohttp web dashboard with SSE streaming (1500+ lines, includes HTML)
├── tests/
│   ├── conftest.py              # Shared mock fixtures (linuxcnc, _hal, gRPC servers)
│   ├── test_state_mapping.py    # State enum mapping correctness (26 tests)
│   ├── test_snapshot.py         # Snapshot immutability & atomic swap (7 tests)
│   ├── test_sidecar.py          # Control command error paths (22 tests)
│   ├── test_cli.py              # CLI argument parsing (18 tests)
│   ├── test_auth.py             # OIDC token parsing + expiration (31 tests)
│   ├── test_policies.py         # RBAC policy evaluation (62 tests)
│   ├── test_registry.py         # Machine registry CRUD + TTL expiry (41 tests)
│   ├── test_gateway.py          # Gateway RPC handlers + broadcast fan-out (35 tests)
│   ├── test_gateway_cli.py      # Gateway CLI parsing (20 tests)
│   ├── test_interceptor.py      # mTLS/OIDC interceptor behavior (19 tests)
│   ├── test_fleet_client.py     # FleetClient routing, streaming, retry (46 tests)
│   └── test_integration.py      # Full flow: FleetClient → Gateway → Sidecar (17 tests)
├── scripts/                     # Deployment scripts (deferred)
├── certs/                       # TLS certificates (git-ignored)
├── pyproject.toml               # Package definition + dependencies
├── README.md                    # Project overview and usage guide
├── headless_ui.md               # Full architecture plan & design document
└── AGENTS.md                    # Development workflow for AI assistants
```

---

*This PRD was reverse-engineered from the `linuxcnc-Headless-UI` repository. All functional requirements, data models, and non-functional characteristics are derived directly from the implemented code, proto definitions, test suites, and configuration files.*
