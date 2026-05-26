# Option C: gRPC-Based Headless Fleet Management UI — Plan

## Overview

A centralized Python UI connects to LinuxCNC instances over a secure network using gRPC. Each remote instance runs a lightweight Python "headless sidecar" that wraps existing `linuxcnc` and `_hal` Python modules, exposing them as gRPC RPCs. A central gateway handles SSO authentication, authorization policies, and machine routing.

**No modifications to LinuxCNC C++ core or real-time components.** The sidecar is a pure-Python process running alongside LinuxCNC.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Central UI (Python)                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐ │
│  │ Dashboard │  │ Program. │  │ HAL Conf.│  │ Fleet Mgmt │ │
│  └─────┬────┘  └────┬─────┘  └────┬─────┘  └──────┬─────┘ │
│        │             │              │               │       │
│  ┌─────▼──────────────▼──────────────▼───────────────▼────┐ │
│  │           Fleet Client Library (gRPC stubs)            │ │
│  └──────────────────────────┬─────────────────────────────┘ │
└─────────────────────────────┼───────────────────────────────┘
                              │ gRPC over TLS (mTLS)
┌─────────────────────────────┼───────────────────────────────┐
│                    Fleet Gateway                          │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  OIDC/SSO Token Validation │ RBAC Policy Engine       │  │
│  │  Discovery Service         │ Rate Limiting            │  │
│  │  Machine Routing / Load Balancing                    │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────┬───────────────────────────────┘
                              │ routed gRPC per machine
          ┌───────────────────┼───────────────────┐
          │                   │                   │
   ┌──────▼──────┐    ┌──────▼──────┐    ┌───────▼───────┐
   │ Instance A  │    │ Instance B  │    │ Instance N    │
   │  machine1   │    │  machine2   │    │  machineN     │
   ├─────────────┤    ├─────────────┤    ├───────────────┤
   │ headless.py │    │ headless.py │    │ headless.py   │
   │ gRPC server │    │ gRPC server │    │ gRPC server   │
   └──────┬──────┘    └──────┬──────┘    └───────┬───────┘
          │                  │                   │
     ┌────▼────┐      ┌─────▼─────┐      ┌──────▼──────┐
     │linuxcnc │      │ linuxcnc  │      │  linuxcnc   │
     │ _hal    │      │  _hal     │      │   _hal      │
     │ EMC core│      │ EMC core  │      │  EMC core   │
     └─────────┘      └───────────┘      └─────────────┘
```

---

## Protocol Definition (`fleet.proto`)

### Service: `FleetService` (per-instance)

Exposes all operations a single LinuxCNC machine supports. The gateway routes calls to the correct instance based on machine ID.

#### Enums

```protobuf
enum MachineState {
  UNKNOWN = 0;
  OFF = 1;
  INITIALIZING = 2;
  RUNNING = 3;
  PAUSED = 4;
  HOLD = 5;
  E_STOP = 6;
  JOG = 7;
  MANUAL = 8;
  AUTO_DONE = 9;
}

enum ExecutionState {
  IDLE = 0;
  RUN = 1;
  FAST_RUN = 2;
  STEP = 3;
  RETRACT = 4;
  MDA = 5;
}

enum InterpState {
  IDLE = 0;
  READ = 1;
  PREDICT = 2;
  EXECUTE = 3;
  ERROR = 4;
}

enum EstopState {
  UNKNOWABLE = 0;
  NOT_E_STOPPED = 1;
  E_STOPPED = 2;
}

enum Mode {
  MODE_UNKNOWN = 0;
  MODE_MANUAL = 1;
  MODE_AUTO = 2;
  MODE_MDA = 3;
}

enum TrajAxis {
  X_AXIS = 0;
  Y_AXIS = 1;
  Z_AXIS = 2;
  A_AXIS = 3;
  B_AXIS = 4;
  C_AXIS = 5;
  U_AXIS = 6;
  V_AXIS = 7;
  W_AXIS = 8;
  P_AXIS = 9;
  Q_AXIS = 10;
}

enum HalPinType {
  PIN_TYPE_BIT = 0;
  PIN_TYPE_U32 = 1;
  PIN_TYPE_S32 = 2;
  PIN_TYPE_FLOAT = 3;
  PIN_TYPE_DIR = 4;
}

enum ErrorCode {
  SUCCESS = 0;
  MACHINE_OFF = 1;
  E_STOP_ACTIVE = 2;
  INCORRECT_MODE = 3;
  BUSY = 4;
  INVALID_STATE = 5;
  HAL_PIN_NOT_FOUND = 6;
  HAL_WRITE_PROTECTED = 7;
  TIMEOUT = 8;
  INTERNAL_ERROR = 9;
}
```

#### Messages — Status

```protobuf
message Position {
  double x = 1;
  double y = 2;
  double z = 3;
  double a = 4;
  double b = 5;
  double c = 6;
  double u = 7;
  double v = 8;
  double w = 9;
  double p = 10;
  double q = 11;
}

message MachineStatus {
  string machine_id = 1;
  MachineState state = 2;
  ExecutionState execution = 3;
  InterpState interp_state = 4;
  EstopState estop_state = 5;
  Mode mode = 6;
  Position joint_actual = 7;       // actual joint positions
  Position joint_commanded = 8;    // commanded joint positions
  Position world_actual = 9;       // world (cartesian) position
  int32 interp_line = 10;          // current interpreter line
  string program_file = 11;        // currently running program
  string remaining_time = 12;      // estimated time remaining
  double feedrate = 13;            // current feedrate
  double feedrate_override = 14;   // current override %
  double spindle_speed = 15;       // current spindle RPM
  double spindle_speed_override = 16; // spindle override %
  bool coolant_mist = 17;
  bool coolant_flood = 18;
  bool coolant_mazak = 19;
  repeated string active_errors = 20;
  double cycle_time = 21;
  int32 motion_type = 22;
}

message HalPinInfo {
  string name = 1;
  HalPinType type = 2;
  bool is_output = 3;
  float value_f = 4;
  uint32 value_u32 = 5;
  int32 value_s32 = 6;
  bool value_bit = 7;
}

message HalComponentInfo {
  string name = 1;
  double update_period_ns = 2;
  repeated HalPinInfo pins = 3;
  map<string, float> params = 4;
}

message LinuxCncVersion {
  string version_string = 1;
  string build_type = 2;
  string git_hash = 3;
}

message MachineInfo {
  string machine_id = 1;
  string machine_name = 2;
  string host_address = 3;
  LinuxCncVersion version = 4;
  int32 num_joints = 5;
  int32 num_hal_components = 6;
}
```

#### RPCs — FleetService

```protobuf
// Status polling (unary or stream)
rpc GetStatus(MachineId) returns (MachineStatus);

// Status streaming for real-time dashboard
rpc SubscribeStatus(MachineId) returns (stream MachineStatus);

// Machine control commands
rpc SetMode(SetModeRequest) returns (Result);
rpc SetExecution(ExecutionCommand) returns (Result);
rpc Start() returns (Result);
rpc Stop() returns (Result);
rpc FeedHold() returns (Result);
rpc Continue() returns (Result);
rpc HomeAll() returns (Result);
rpc HomeAxis(TrajAxis) returns (Result);

// G-code / MDI
rpc SendMdiCommand(MdiCommand) returns (Result);
rpc LoadProgram(ProgramPath) returns (Result);
rpc StepForward() returns (Result);

// Position
rpc GetPosition(PositionRequest) returns (PositionResponse);

// HAL operations
rpc ListHalComponents(ListHalRequest) returns (HalComponentList);
rpc ReadHalPin(HalPinRead) returns (HalPinValue);
rpc WriteHalPin(HalPinWrite) returns (Result);
rpc SubscribeHalPins(HalPinSubscribe) returns (stream HalPinUpdate);

// Error / log channel
rpc GetErrors(GetErrorsRequest) returns (ErrorList);
rpc SubscribeErrors(MachineId) returns (stream ErrorEvent);

// Configuration
rpc GetMachineInfo(MachineId) returns (MachineInfo);
rpc GetIniParam(IniParamRequest) returns (IniParamValue);
```

#### RPC: `FleetGatewayService` (central)

```protobuf
rpc DiscoverMachines(DiscoverRequest) returns (MachineList);
rpc RouteMachine(MachineId) returns (GatewayRoute);
rpc BroadcastCommand(BroadcastRequest) returns (BroadcastResult);
rpc SubscribeAllStatus(SubscribeAllRequest) returns (stream MachineStatus);
```

#### Additional Messages

`HomeAxisRequest` was added after initial design:

```protobuf
message HomeAxisRequest {
  MachineId id = 1;
  TrajAxis axis = 2;
}
```

#### Request/Response Messages

```protobuf
message MachineId { string id = 1; }

message SetModeRequest { MachineId id = 1; Mode mode = 2; }
message ExecutionCommand { MachineId id = 1; ExecutionState state = 2; }
message MdiCommand { MachineId id = 1; string command = 2; }
message ProgramPath { MachineId id = 1; string path = 2; }

message PositionRequest {
  MachineId id = 1;
  enum Type { WORLD = 0; JOINT = 1; DEVICE = 2; }
  Type type = 2;
}
message PositionResponse { MachineId id = 1; Position position = 2; }

message ListHalRequest { MachineId id = 1; }
message HalComponentList { repeated HalComponentInfo components = 1; }

message HalPinRead {
  MachineId id = 1;
  string pin_name = 2;
}
message HalPinValue {
  string pin_name = 1;
  HalPinType type = 2;
  float value_f = 3;
  uint32 value_u32 = 4;
  int32 value_s32 = 5;
  bool value_bit = 6;
}

message HalPinWrite {
  MachineId id = 1;
  string pin_name = 2;
  float value_f = 3;
  uint32 value_u32 = 4;
  int32 value_s32 = 5;
  bool value_bit = 6;
}

message HalPinSubscribe {
  MachineId id = 1;
  repeated string pin_names = 2;
  double poll_interval_seconds = 3;
}
message HalPinUpdate {
  string pin_name = 1;
  float value_f = 2;
  uint32 value_u32 = 3;
  int32 value_s32 = 4;
  bool value_bit = 5;
}

message GetErrorsRequest { MachineId id = 1; int32 limit = 2; }
message ErrorEvent { string message = 1; double timestamp = 2; }
message ErrorList { repeated ErrorEvent errors = 1; }

message IniParamRequest { MachineId id = 1; string section = 2; string option = 3; }
message IniParamValue { MachineId id = 1; string value = 2; }

message Result { bool success = 1; string message = 2; ErrorCode error_code = 3; }

// Gateway messages
message DiscoverRequest { string facility = 1; }
message MachineList { repeated MachineInfo machines = 1; }
message GatewayRoute { string instance_address = 1; int32 instance_port = 2; }
message BroadcastRequest {
  enum Scope { ALL = 0; FACILITY = 1; TAG = 2; }
  Scope scope = 1;
  string facility = 2;
  repeated string tags = 3;
  oneof command {
    MdiCommand mdi = 4;
    ExecutionCommand exec = 5;
    SetModeRequest mode = 6;
  }
}
message BroadcastResult { map<string, Result> results = 1; }

message SubscribeAllRequest {
  string facility = 1;
  double poll_interval_seconds = 2;
}
```

---

## Headless Sidecar (`linuxcnc_fleet/headless.py`)

Wraps the existing `linuxcnc` Python module with a polling loop. This is the core of the per-instance agent.

### Class: `LinuxCncSidecar`

```python
class LinuxCncSidecar:
    """Wraps linuxcnc module bindings with a 50Hz polling loop."""

    def __init__(self, ini_path: str = None, machine_id: str = None):
        # Initialize linuxcnc.stat(), command(), error_channel(), ini()
        # Start background polling thread at 50Hz (0.02s interval)
        # Store latest snapshot in thread-safe atomic structure

    def poll(self):
        """Single poll iteration — called by background thread."""
        # stat.poll() -> extract: state, execution, interp_state, estop, mode
        # stat.position -> world + joint positions
        # stat.joint_actual_pos, stat.joint_commanded_pos
        # stat.linear_axis, stat.angular_axis for axis identification
        # command.get_feedrate(), get_spindle_speed()
        # error_channel.poll() for new errors
        # Update shared snapshot atomically

    def get_status(self) -> MachineStatus:
        """Return latest snapshot as protobuf message."""
        # Map linuxcnc.stat fields to MachineStatus proto
        # Handle state mapping (linuxcnc states -> MachineState enum)

    def set_mode(self, mode: Mode) -> Result:
        """command.mode(linuxcnc.MODE_*) with validation."""
        # Check E-stop state, machine state before allowing mode change

    def set_execution(self, state: ExecutionState) -> Result:
        """command.program_open(), step(), sdo_mode(), etc."""

    def send_mdi(self, command: str) -> Result:
        """command.mdi(command) with error checking."""

    def feed_hold(self) -> Result:
        """command.feed_hold()"""

    def continue_exec(self) -> Result:
        """command.continue()"""

    def stop(self) -> Result:
        """command.stop()"""

    def home_axis(self, axis: TrajAxis) -> Result:
        """command.home(axis) with axis validation."""

    def read_hal_pin(self, name: str) -> HalPinValue:
        """Use _hal module to read pin value."""
        # hal = _hal()
        # hal.get_value(name) or similar

    def write_hal_pin(self, name: str, value) -> Result:
        """Use _hal module to write pin value (output pins only)."""
        # Validate pin is output type before writing

    def list_hal_components(self) -> HalComponentList:
        """Enumerate HAL components and their pins."""
        # Use _hal.list_components() or iterate hal.comp_list

    def get_ini_param(self, section: str, option: str) -> str:
        """ini.get(section, option)"""

    def get_machine_info(self) -> MachineInfo:
        """Assemble MachineInfo from ini + stat."""

    def run(self):
        """Start polling loop (non-blocking)."""
        # Daemon thread: while True: self.poll(); time.sleep(0.02)
```

### State Mapping (`linuxcnc` -> protobuf enum)

| linuxcnc.stat.state value | MachineState enum |
|---|---|
| `linuxcnc.RCS_IDLE` | OFF (distinguished by execution field) |
| `linuxcnc.RCS_RUNNING` | RUNNING |
| `linuxcnc.RCS_DONE` | AUTO_DONE |

| linuxcnc.stat.execution value | ExecutionState enum (proto name) |
|---|---|
| `linuxcnc.EXEC_STATE_IDLE` | EXEC_IDLE (proto: `EXEC_IDLE = 0`, not `IDLE`) |
| `linuxcnc.EXEC_STATE_RUN` | RUN |
| `linuxcnc.EXEC_STATE_FAST_RUN` | FAST_RUN |
| `linuxcnc.EXEC_STATE_STEP` | STEP |
| `linuxcnc.EXEC_STATE_RETRACT` | RETRACT |
| `linuxcnc.EXEC_STATE_MDA` | MDA |

| linuxcnc.stat.interp_state value | InterpState enum (proto name) |
|---|---|
| `linuxcnc.INTERP_IDLE` | INTERP_IDLE (proto: `INTERP_IDLE = 0`, not `IDLE`) |
| `linuxcnc.INTERP_READ` | READ |
| `linuxcnc.INTERP_EXEC` | EXECUTE |

| linuxcnc.stat.mode value | Mode enum |
|---|---|
| `linuxcnc.MODE_MANUAL` (1) | MODE_MANUAL |
| `linuxcnc.MODE_AUTO` (2) | MODE_AUTO |
| `linuxcnc.MODE_MDI` (3) | MODE_MDA (proto uses MDA, not MDI) |

Note: Mode mapping converts `linuxcnc.MODE_MDI` → `MODE_MDA` because the proto enum is named `MODE_MDA`.

### Thread Safety

- Snapshot uses `dataclasses.replace()` for immutability on update.
- Background thread writes to new snapshot object; reader atomically swaps reference.
- No locks needed for reads — single-writer, multi-reader atomic reference swap.

---

## gRPC Server (`linuxcnc_fleet/server.py`)

Maps RPC calls to `LinuxCncSidecar` methods. One server instance per LinuxCNC machine.

```python
class FleetServiceServicer(fleet_pb2_grpc.FleetServiceServicer):
    def __init__(self, sidecar: LinuxCncSidecar):
        self.sidecar = sidecar

    async def GetStatus(self, request, context):
        # Validate auth metadata (token from gateway or direct mTLS)
        status = self.sidecar.get_status()
        return status

    def SubscribeStatus(self, request, context):
        # NOTE: gRPC server-streaming requires regular generators (not async generators)
        import time as _time
        while True:
            yield self.sidecar.get_status()
            _time.sleep(0.02)

    async def SetMode(self, request, context):
        return self.sidecar.set_mode(request.mode)

    async def SendMdiCommand(self, request, context):
        return self.sidecar.send_mdi(request.command)

    # ... other RPCs mapped similarly

    async def ReadHalPin(self, request, context):
        return self.sidecar.read_hal_pin(request.pin_name)

    async def WriteHalPin(self, request, context):
        return self.sidecar.write_hal_pin(request.pin_name, ...)

    async def SubscribeErrors(self, request, context):
        # Stream from error_channel in sidecar
        ...
```

### Server Startup

```python
def create_server(sidecar: LinuxCncSidecar, port: int = 50051,
                  cert_file: str = None, key_file: str = None,
                  root_cert_file: str = None, user_extractor=None):
    # grpcio 1.80.0 removed futures_executor= kwarg; use positional arg + interceptors=
    interceptors = []
    if user_extractor:
        from linuxcnc_fleet.auth import create_auth_interceptor
        interceptors.append(create_auth_interceptor(user_extractor))

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=8),
        interceptors=interceptors,
    )
    fleet_pb2_grpc.add_FleetServiceServicer_to_server(
        FleetServiceRPC(sidecar), server)

    if cert_file and key_file:
        creds = _build_credentials(cert_file, key_file, root_cert_file)
        server.add_secure_port(f'[::]:{port}', creds)
    else:
        server.add_insecure_port(f'[::]:{port}')

    return server


def _build_credentials(cert_file, key_file, root_cert_file=None):
    # mTLS if root_cert_file provided, otherwise plain TLS
    with open(cert_file, "rb") as f:
        cert = f.read()
    with open(key_file, "rb") as f:
        private_key = f.read()
    if root_cert_file:
        with open(root_cert_file, "rb") as f:
            root_certs = f.read()
        return grpc.ssl_server_credentials(
            [(private_key, cert)],
            root_certificates=root_certs,
            require_client_auth=True,
        )
    else:
        return grpc.ssl_server_credentials([(private_key, cert)])
```

### systemd Service Template

```ini
[Unit]
Description=LinuxCNC Fleet Sidecar
After=linuxcnc.service
Wants=linuxcnc.service

[Service]
Type=simple
User=linuxcnc
Group=linuxcnc
ExecStart=/opt/linuxcnc-fleet/bin/headless-server \
    --ini /path/to/machine.ini \
    --machine-id machine1 \
    --port 50051 \
    --cert /etc/linuxcnc-fleet/certs/server.pem \
    --key /etc/linuxcnc-fleet/certs/server-key.pem

# Sidecar should start after LinuxCNC core is ready
ExecStartPre=/bin/sh -c 'sleep 5'

[Install]
WantedBy=multi-user.target
```

---

## SSO Gateway (`gateway/server.py`)

Central service that handles authentication, authorization, and routing.

### Responsibilities

1. **OIDC Token Validation** — Verify JWT signatures, expiration, issuer.
2. **Authorization Policy Engine** — RBAC roles + attribute-based rules (facility, machine tags).
3. **Machine Discovery** — Sidecars register themselves with gateway on startup.
4. **Routing** — Translate machine ID to instance address:port.
5. **Broadcast** — Fan-out commands to multiple instances.

### Registration Protocol

Machines are registered with the gateway via the `MachineRegistry` API. In production, a sidecar agent would call `registry.register()` on startup and send heartbeats periodically (default TTL 30s). The registry runs a background cleanup thread that removes expired entries.

```python
# Example registration (called by sidecar or external orchestrator):
registry.register(
    machine_id="lathe-01",
    address="192.168.1.10",
    port=50051,
    facility="shop-floor-1",
    tags=["cnc", "lathe"],
)

# Heartbeat (sidecar calls periodically, e.g., every 10s):
registry.heartbeat("lathe-01")

# TTL-based expiry: entries older than heartbeat_ttl are removed by background cleanup
```

### Authorization Model

| Role | Read Status | HAL Read | HAL Write | Control (Start/Stop/Home) | Mode Change | Step/MDI | Load Program | Execution Control | Scope |
|---|---|---|---|---|---|---|---|---|---|
| `viewer` | Yes | Yes | No | No | No | No | No | No | Facility |
| `operator` | Yes | Yes | Yes | Yes | Yes | No | No | No | Facility |
| `programmer` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | No | Facility |
| `maintainer` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Facility |
| `admin` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Global (no scope) |

Attributes for scoping:
- `facility`: e.g., "shop-floor-1", "lab" — restricts access to machines in the same facility
- `role`: one of the above
- `machine_tags`: e.g., ["cnc-mill", "legacy"] — used in policy evaluation

Role hierarchy (each role inherits all permissions from roles below it):
`viewer` → `operator` → `programmer` → `maintainer` → `admin`

Admin users bypass all facility/tag scoping restrictions.

### Gateway Service Implementation

The gateway uses a `GatewayServiceServicer` that integrates with the auth manager, policy engine, and machine registry. Each RPC validates OIDC tokens, checks RBAC policies, and routes to the correct instance.

```python
class GatewayServiceServicer(FleetGatewayServiceServicer):
    def __init__(self, auth_manager: AuthManager, policy_engine: PolicyEngine, registry: MachineRegistry):
        self.auth = auth_manager
        self.policies = policy_engine
        self.registry = registry
        self._client_cache: dict[str, _GrpcClient] = {}  # per-machine gRPC channel cache

    def DiscoverMachines(self, request, context):
        # NOTE: Type hint should be DiscoverRequest (proto message), not DiscoveryRequest
        user = self.auth.extract_user(context.invocation_metadata())

        # Resolve facility from request or user claims separately
        user_facility = getattr(user, 'facility', None)
        request_facility = request.facility if request.facility else None

        all_machines = self.registry.list_all()
        filtered = self.policies.filter_machines_by_scope(
            user.role, user_facility, [...])

        # Narrow by request facility if provided
        if request_facility:
            filtered = [m for m in filtered if m.get('facility') == request_facility]

    def RouteMachine(self, request, context):
        user = self.auth.extract_user(context.invocation_metadata())
        machine_id = request.id.id
        result = self.policies.can_read_status(user.role)
        if not result.allowed:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, result.reason)
        entry = self.registry.lookup(machine_id)
        if entry is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f'Machine {machine_id} not found')
        return GatewayRoute(instance_address=entry.address, instance_port=entry.port)

    def BroadcastCommand(self, request, context):
        user = self.auth.extract_user(context.invocation_metadata())
        # Resolve target machines based on scope (ALL/FACILITY/TAG)
        targets = self.registry.resolve_scope(request.scope, facility=request.facility, tags=request.tags)
        if not targets:
            return BroadcastResult(results={})

        # Check broadcast authorization per command type
        cmd_type = 'mdi' if request.HasField('mdi') else 'execution' if request.HasField('exec') else 'mode'
        result = self.policies.check_broadcast_authorization(user.role, cmd_type)
        if not result.allowed:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, result.reason)

        # Fan-out to each instance via FleetService RPCs (synchronous per-machine calls)
        results = {}
        for target in targets:
            client = self._get_or_create_client(target)
            with client.connect() as channel:
                stub = FleetServiceStub(channel)
                if request.HasField('mdi'):
                    resp = stub.SendMdiCommand(MdiCommand(id=MachineId(id=target.id), command=request.mdi.command))
                    results[target.id] = Result(success=resp.success, message=resp.message, error_code=resp.error_code)
                # ... similar for exec/mode commands
        return BroadcastResult(results=results)

    def SubscribeAllStatus(self, request, context):
        user = self.auth.extract_user(context.invocation_metadata())
        if not self.policies.can_subscribe(user.role).allowed:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, 'Not authorized to subscribe')

        # Resolve target machines and stream status from each in background threads
        targets = self.registry.resolve_scope(request.scope, facility=request.facility)
        # For each machine, create a background thread that subscribes to SubscribeStatus
        # Interleave results into the server-streaming response
```

Key implementation details:
- `_GrpcClient.connect()` returns a lazily-created `grpc.insecure_channel`; channels are cached and reused (no context manager)
- Broadcast fan-out is synchronous per-machine — each target gets its own gRPC stub call via `_execute_broadcast_command()`
- `SubscribeAllStatus` uses background daemon threads to subscribe to each machine's `SubscribeStatus`, collecting results via `queue.Queue` into a shared dict
- `context.is_active()` check in the main streaming loop prevents hanging on client disconnect
- Per-machine access checks (`_check_control_access`) are performed before fan-out, with denied machines getting `Result(success=False)` entries

### Auth Flow (Central UI -> Gateway -> Instance)

```
1. User logs into central UI via OIDC provider (Keycloak/Auth0/etc.)
2. Central UI obtains ID token + access token from OIDC provider
3. UI attaches token to gRPC metadata: "authorization: Bearer <token>"
4. Gateway intercepts, validates token, checks policy
5. If authorized, gateway either:
   a) Proxies the call directly (single-machine operations), or
   b) Creates its own mTLS-secured gRPC call to the target instance
6. Instance receives the call from gateway (trusted internal network)
```

For mTLS between gateway and instances:
- Gateway holds client certificate issued by same CA as instance server certs.
- Instances validate gateway's certificate — no additional auth needed on this leg.
- This way, the gateway is the only process that needs to talk to instances over the network.

---

## Central Client Library (`fleet_client/client.py`)

Python library for the central UI to interact with the fleet. Lowercased method names (Python convention).

```python
class FleetClient:
    """High-level async client for the LinuxCNC fleet management API."""

    def __init__(
        self,
        gateway_address: str,
        token: str,
        tls_enabled: bool = False,
        machine_channel_ttl: float = 300.0,
        _gateway_stub=None,       # test injection
        _fleet_stub_factory=None, # test injection
        _gateway_channel=None,    # test injection
    ):
        self._gateway_address = gateway_address
        self._token = token
        self._tls_enabled = tls_enabled
        self._machine_channel_ttl = machine_channel_ttl

        # Gateway channel with OIDC auth interceptor
        auth_interceptor = create_auth_interceptor(self._token)
        if self._tls_enabled:
            creds = grpc.ssl_channel_credentials()
            self._gateway_channel = grpc.intercept_channel(
                grpc.secure_channel(gateway_address, creds),
                auth_interceptor,
            )
        else:
            self._gateway_channel = grpc.intercept_channel(
                grpc.insecure_channel(gateway_address),
                auth_interceptor,
            )
        self._gateway_stub = grpc.aio.FleetGatewayServiceStub(self._gateway_channel)
        self._machine_channels: dict[str, _CachedChannel] = {}
        self._cache_lock = threading.Lock()

    async def get_machines(self, facility: str = None) -> list[MachineEntry]:
        """Discover available machines. Returns MachineEntry dataclass (not raw protobuf)."""
        ...

    async def get_status(self, machine_id: str) -> MachineStatus:
        """Get current status with retry for read-only RPCs."""
        route = await self.route_machine(machine_id)
        channel = await self._get_or_create_machine_channel(*route)
        stub = self._fleet_stub_factory(channel)
        return await stub.GetStatus(MachineId(id=machine_id))

    async def subscribe_status(self, machine_id: str):
        """Async generator — stream status updates for a single machine."""
        stub, _, _ = await self._get_fleet_stub(machine_id)
        async for status in stub.SubscribeStatus(MachineId(id=machine_id)):
            yield status

    async def send_mdi(self, machine_id: str, command: str) -> Result:
        """Send MDI command to a machine."""
        ...

    async def broadcast_command(self, scope, command_type, command_value, facility=None, tags=None):
        """Generic broadcast wrapper — builds BroadcastRequest from params.
        
        Args:
            scope: "ALL", "FACILITY", or "TAG"
            command_type: "mdi", "execution", or "mode"
            command_value: Command-specific value (string for MDI, int for execution/mode)
        """
        ...

    async def broadcast_mdi(self, scope, command, facility=None, tags=None):
        """Convenience wrapper around broadcast_command(command_type='mdi', ...)"""
        ...

    async def route_machine(self, machine_id: str) -> tuple[str, int]:
        """Resolve machine ID to (address, port) via gateway."""
        ...

    async def _get_or_create_machine_channel(self, address: str, port: int) -> grpc.Channel:
        """Get or create cached gRPC channel to a machine instance.
        
        NOTE: Currently creates insecure channels even when tls_enabled=True
        (bug — should use grpc.secure_channel when tls_enabled).
        """
        ...
```

Key implementation details:
- `_CachedChannel` wraps `grpc.Channel` with TTL tracking (`created_at`, `last_used`, `ref_count`)
- Background cleanup runs on next gateway RPC call (lazy), not a dedicated timer thread
- Retry only for read-only RPCs in `_READ_ONLY_RPC` set: GetStatus, SubscribeStatus, ListHalComponents, ReadHalPin, GetErrors, SubscribeErrors, GetMachineInfo, GetPosition, GetIniParam
- `_retry_read()` accepts coroutine factory callable (not pre-created coroutine) — avoids stale coroutine bug
- Test injection via `_gateway_stub`, `_fleet_stub_factory`, `_gateway_channel` constructor params

---

## File Layout

```
linuxcnc-fleet/
├── proto/
│   └── fleet.proto              # gRPC service definition (all RPCs + messages)
├── linuxcnc_fleet/
│   ├── __init__.py
│   ├── headless.py              # LinuxCncSidecar class — wraps linuxcnc module (699 lines)
│   ├── server.py                # gRPC server per instance + FleetServiceRPC (405 lines)
│   ├── cli.py                   # CLI entry point: headless-server --ini ...
│   └── auth.py                  # mTLS/OIDC interceptor for FleetService (~120 lines)
├── gateway/
│   ├── __init__.py
│   ├── server.py                # FleetGatewayService implementation (408 lines)
│   ├── auth.py                  # OIDC token validation + user extraction (228 lines)
│   ├── policies.py              # RBAC policy engine (303 lines)
│   └── registry.py              # Machine registration + discovery store (206 lines)
│   └── cli.py                   # Gateway CLI entry point: fleet-gateway (139 lines)
├── fleet_client/
│   ├── __init__.py
│   ├── client.py                # FleetClient high-level library (1057 lines)
│   └── auth.py                  # OIDC bearer auth interceptor (~60 lines)
├── tests/
│   ├── conftest.py              # Shared mock fixtures (linuxcnc, _hal, gRPC servers)
│   ├── test_state_mapping.py    # Phase 1: state mapping correctness (26 tests)
│   ├── test_snapshot.py         # Phase 1: snapshot immutability (7 tests)
│   ├── test_sidecar.py          # Phase 1: control command error paths (22 tests)
│   ├── test_cli.py              # Phase 1: CLI argument parsing (18 tests)
│   ├── test_auth.py             # Phase 2: OIDC token parsing + expiration (31 tests)
│   ├── test_policies.py         # Phase 2: RBAC policy evaluation (62 tests)
│   ├── test_registry.py         # Phase 2: machine registry CRUD + TTL expiry (41 tests)
│   ├── test_gateway.py          # Phase 2: broadcast fan-out (35 tests)
│   ├── test_gateway_cli.py      # Phase 2: gateway CLI parsing (20 tests)
│   ├── test_interceptor.py      # Phase 2: OIDC interceptor behavior (19 tests)
│   ├── test_fleet_client.py     # Phase 3: FleetClient routing, streaming, retry (46 tests)
│   └── test_integration.py      # Phase 4: full flow integration (17 tests)
├── scripts/
│   └── linuxcnc-fleet.service   # systemd service template (deferred)
├── certs/                       # TLS certificates (git-ignored)
│   ├── ca.pem
│   ├── server.pem
│   └── server-key.pem
├── pyproject.toml               # Package definition + dependencies
└── Makefile                     # Build: proto generation, install
```

Note: `linuxcnc_fleet/auth.py` contains the mTLS interceptor for FleetService RPCs (separate from `gateway/auth.py` which handles OIDC for the gateway). The interceptor uses a callable-based `user_extractor` to decouple from specific AuthManager implementations.

---

## Dependencies

### Sidecar (`linuxcnc_fleet/`)
- `grpcio`, `grpcio-tools` (gRPC Python)
- `protobuf` (message definitions)
- `linuxcnc` (existing LinuxCNC Python module — already available on instance)
- `_hal` (existing HAL Python extension — already available on instance)

### Gateway (`gateway/`)
- `grpcio`, `grpcio-tools`
- `protobuf`
- `PyJWT` (OIDC token validation — HS256 + RS256 via JWKS)
- `cryptography` (JWK to PEM conversion for RS256 key validation)
- In-memory dict with threading lock (machine registry store)

### Client (`fleet_client/`)
- `grpcio`, `protobuf`
- Generated stubs from `fleet.proto`

### Build Tools
- `protoc` + `grpcio-tools` for code generation
- `mypy` for type checking
- `pytest` for testing

---

## Deployment Model

### Per-Instance (Remote Machine)

```bash
# 1. Install linuxcnc-fleet package
pip install linuxcnc-fleet

# 2. Configure TLS certs (CA-signed)
cp ca.pem /etc/linuxcnc-fleet/
cp server.pem /etc/linuxcnc-fleet/
cp server-key.pem /etc/linuxcnc-fleet/

# 3. Register systemd service
sudo systemctl enable linuxcnc-fleet@machine1
sudo systemctl start linuxcnc-fleet@machine1

# Service starts after LinuxCNC, registers with gateway, begins polling
```

### Central (Gateway + UI)

```bash
# 1. Run gateway (uses HS256 secret for dev, JWKS URL for production)
fleet-gateway --port 50050 \
    --jwt-secret "your-32-byte-minimum-secret-key-here!!" \
    --issuer https://keycloak.example.com/realms/linuxcnc \
    --audience fleet-api

# For RS256 with JWKS:
fleet-gateway --port 50050 \
    --jwks-url https://keycloak.example.com/realms/linuxcnc/protocol/openid-connect/certs \
    --issuer https://keycloak.example.com/realms/linuxcnc \
    --audience fleet-api

# With mTLS (require client certs from instances):
fleet-gateway --port 50050 \
    --jwt-secret "your-32-byte-minimum-secret-key-here!!" \
    --issuer https://keycloak.example.com/realms/linuxcnc \
    --audience fleet-api \
    --cert /etc/linuxcnc-fleet/gateway.pem \
    --key /etc/linuxcnc-fleet/gateway-key.pem \
    --root-cert /etc/linuxcnc-fleet/ca.pem

# 2. Run central UI (separate process)
python -m fleet_ui --gateway localhost:50050
```

---

## Security Model

### TLS/mTLS
- All gRPC traffic encrypted with TLS 1.3.
- Instances use server certificates signed by internal CA.
- Gateway uses client certificate signed by same CA to authenticate to instances.
- Central UI connects to gateway with TLS + OIDC bearer token in metadata.

### Authentication Layers
1. **UI -> Gateway**: OIDC access token (Bearer) in gRPC metadata.
2. **Gateway -> Instance**: mTLS client certificate (gateway is trusted CA member).
3. **No auth needed on instance localhost** if running sidecar locally (but TLS still applied for defense-in-depth).

### Authorization Layers
1. Gateway validates OIDC token claims and extracts user identity + attributes.
2. Policy engine checks RBAC role against requested operation.
3. Attribute-based filtering restricts machines by facility/tags.
4. HAL write operations additionally check pin direction (output only).

---

## Implementation Phases

### Phase 1: Core Sidecar (Week 1-2) ✅ COMPLETE
- [x] Define and generate `fleet.proto` with FleetService RPCs
- [x] Implement `LinuxCncSidecar` class in `headless.py`
  - [x] Polling loop at 50Hz with atomic snapshot updates
  - [x] Status extraction from linuxcnc.stat
  - [x] Mode/executive control wrappers
  - [x] HAL pin read/write via _hal module
  - [x] INI param access
- [x] Implement gRPC server in `server.py`
- [x] CLI entry point with TLS/mTLS validation (`linuxcnc_fleet/cli.py`)
- [ ] Write systemd service template (deferred)
- [ ] Test against a single LinuxCNC instance (uspace mode) — requires target machine
- [x] Unit tests: state mapping correctness (26 tests, `test_state_mapping.py`)
- [x] Unit tests: snapshot immutability and atomic swap behavior (7 tests, `test_snapshot.py`)
- [x] Unit tests: CLI argument parsing and TLS validation (18 tests, `test_cli.py`)
- [x] Unit tests: control command error paths (22 tests, `test_sidecar.py`)
- **Total: 73/73 tests passing**

### Phase 2: Gateway & Auth (Week 3-4) ✅ COMPLETE
- [x] Implement FleetGatewayService RPCs (`gateway/server.py`)
  - [x] DiscoverMachines — facility filtering + RBAC
  - [x] RouteMachine — address:port lookup with auth
  - [x] BroadcastCommand — fan-out with per-machine auth + result aggregation
  - [x] SubscribeAllStatus — interleaved streaming from multiple machines
- [x] OIDC token validation (support Keycloak/Auth0 format) (`gateway/auth.py`)
  - [x] HS256 symmetric signing
  - [x] RS256/RS384/RS512 asymmetric via JWKS cache
  - [x] Expiration, issuer, audience validation
- [x] RBAC policy engine with attribute-based scoping (`gateway/policies.py`)
  - [x] Role hierarchy: viewer < operator < programmer < maintainer < admin
  - [x] 13 permissions mapped to role sets
  - [x] Facility/tag scope filtering
  - [x] Broadcast authorization per command type
- [x] Machine registration and heartbeat mechanism (`gateway/registry.py`)
  - [x] TTL-based expiry with background cleanup thread
  - [x] CRUD operations (register, heartbeat, unregister, lookup, list_all)
  - [x] Scope resolution (ALL/FACILITY/TAG)
- [x] Broadcast command fan-out — per-machine auth checks + result aggregation
- [ ] TLS/mTLS certificate management (deferred)
- [x] Gateway CLI entry point (`gateway/cli.py`)
  - [x] `fleet-gateway` with --port, --cert, --key, --root-cert, --jwt-secret, --jwks-url, --issuer, --audience
  - [x] Argument validation (TLS pairs, JWT mutual exclusivity)
- [x] mTLS interceptor for FleetService (`linuxcnc_fleet/auth.py`)
  - [x] AuthContext extraction from OIDC tokens via gRPC metadata
  - [x] Role-hierarchy checks on control/write RPCs (operator+ / programmer+)
  - [x] Callable-based user_extractor (decoupled from specific AuthManager)
- [x] Server auth wiring (`linuxcnc_fleet/server.py`) — FleetServiceRPC integrates interceptor
- [x] CLI auth wiring (`linuxcnc_fleet/cli.py`) — --jwt-secret/--jwks-url args, creates user_extractor
  - **Known bug**: `AuthManager(secret=...)` should be `AuthManager(secret_key=...)` (line 117 of cli.py)
- [x] Unit tests: OIDC token parsing and expiration checks (31 tests, `test_auth.py`)
- [x] Unit tests: RBAC policy evaluation — role + facility + tags filtering (62 tests, `test_policies.py`)
- [x] Unit tests: machine registry CRUD and TTL expiry (41 tests, `test_registry.py`)
- [x] Unit tests: broadcast fan-out with per-result tracking (35 tests, `test_gateway.py`)
- [x] Unit tests: gateway CLI parsing and TLS validation (20 tests, `test_gateway_cli.py`)
- [x] Unit tests: OIDC interceptor behavior (19 tests, `test_interceptor.py`)
- **Cumulative: 281/281 tests passing** (73 Phase 1 + 208 Phase 2)

### Phase 3: Client Library & UI Integration (Week 5-6)
- [x] Implement `FleetClient` high-level library (`fleet_client/client.py`, ~1000 lines)
- [x] OIDC auth interceptor (`fleet_client/auth.py`, ~60 lines)
- [x] Generated gRPC stubs for all services (regenerated with HomeAxis, SendMdiCommand, LoadProgram RPCs)
- [x] Channel caching with TTL expiry (default 300s) and thread-safe cleanup
- [x] Streaming status subscription support (async generators)
- [x] Error handling and retry logic (exponential backoff, 3 retries max for read-only RPCs)
- [x] FleetClient wrappers: home_axis(), load_program() (proto + stubs regenerated)
- [x] FleetClient wrapper: send_mdi() updated to use SendMdiCommand RPC (was using SetExecution)
- [x] Unit tests: FleetClient routing and channel caching (6 tests, `test_fleet_client.py`)
- [x] Unit tests: streaming subscription lifecycle (start/stop) (4 tests, `test_fleet_client.py`)
- [x] Unit tests: error handling and retry backoff behavior (3 tests, `test_fleet_client.py`)
- [x] Unit tests: gateway RPC wrappers (4 tests, `test_fleet_client.py`)
- [x] Unit tests: fleet service wrappers (17 tests, `test_fleet_client.py` — includes home_axis, load_program)
- [x] Unit tests: TLS channel creation (2 tests, `test_fleet_client.py`)
- [x] Unit tests: async context manager (2 tests, `test_fleet_client.py`)
- **Cumulative: 327/327 tests passing** (281 + 46 FleetClient)

### Phase 4: Hardening & Packaging (Week 7-8) ✅ COMPLETE (Integration Tests + Packaging)
- [x] Integration tests: full flow — FleetClient → Gateway → Sidecar → linuxcnc.stat (17 tests, `test_integration.py`)
  - [x] `TestDiscoverRouteGetStatus`: discover, route, get_status_via_gateway, viewer_can_discover (4 tests)
  - [x] `TestBroadcastCommand`: broadcast_mdi_to_all, broadcast_mode_change (2 tests)
  - [x] `TestStreamingStatus`: subscribe_all_status (1 test)
  - [x] `TestSidecarDirectCommands`: set_mode, home_axis, send_mdi_command, load_program, subscribe_status_stream, get_errors (6 tests)
  - [x] `TestGatewayAuthIntegration`: unauthenticated_request_rejected, viewer_cannot_broadcast (2 tests)
  - [x] `TestRegistryHeartbeat`: heartbeat_updates_last_seen, expired_machine_removed (2 tests)
- **Cumulative: 344/344 tests passing** (327 + 17 Integration)
- All integration tests use real gRPC servers (not stubs) to exercise serialization, channel setup, auth interceptor chaining, broadcast fan-out
- [x] Package distribution (pip wheel) — `linuxcnc-fleet` package with `[sidecar]`, `[gateway]`, `[client]`, `[dev]` extras
- [ ] Load testing: concurrent connections, broadcast performance
- [ ] Certificate auto-renewal support
- [ ] Metrics/health endpoints (Prometheus / HTTP)

---

## Known Issues

| # | File | Line | Issue | Severity | Status |
|---|------|------|-------|----------|--------|
| 1 | `gateway/server.py` | 133 | Type hint `DiscoveryRequest` doesn't exist — should be `DiscoverRequest` | Low | ✅ Fixed |
| 2 | `linuxcnc_fleet/cli.py` | 117 | `AuthManager(secret=...)` should be `AuthManager(secret_key=...)` | High | ✅ Fixed |
| 3 | `fleet_client/client.py` | 187-191 | `_get_or_create_machine_channel()` creates `insecure_channel` even when `tls_enabled=True` | Medium | ✅ Fixed |

---

## Tradeoffs & Risks

### Why gRPC over HTTP/JSON?
| Factor | gRPC | HTTP/JSON |
|---|---|---|
| Typing | Strong protobuf schema | Loose, manual validation |
| Performance | Binary, efficient serialization | Text-based, larger payloads |
| Streaming | Native bidirectional streaming | WebSocket/SSE workaround |
| Multi-language | Auto-generated clients in 10+ languages | Manual client per language |
| Tooling | protoc, codegen, OpenAPI-like docs | Swagger/OpenAPI (mature) |

gRPC is chosen for strong typing and streaming support, which are critical for real-time status updates.

### Risk: LinuxCNC Python Module Stability
The `linuxcnc` Python module is a C++ extension. Changes to it in future LinuxCNC releases could break the sidecar's field access patterns. Mitigation: version-pin the expected linuxcnc module and test against multiple versions.

### Risk: Polling Latency
50Hz polling means up to 20ms delay for status updates. This is acceptable for dashboard visibility but not for closed-loop control (which this system explicitly does not do).

### Risk: HAL Pin Enumeration
The `_hal` module's API for listing components/pins may vary across LinuxCNC versions. Mitigation: wrap in try/except with graceful degradation, and cache pin metadata.

---

## Open Questions

1. **Should the gateway proxy all calls or use mTLS directly to instances?**
   - Proxy approach: simpler instance config, but gateway is a single point of failure/bottleneck.
   - Direct mTLS: instances see actual client identity for audit, but gateway must distribute certs.
   - **Recommended**: Gateway uses mTLS to instances (as described above). This gives best of both — centralized auth without becoming the data path bottleneck.

2. **Should we support direct instance access (bypassing gateway) for local operators?**
   - Option: sidecar listens on both localhost (insecure, no auth required) and a secure network interface (mTLS + token required).
   - Local operator on machine console uses localhost; remote UI uses gateway.

3. **What about legacy telnet interfaces (5006/5007)?**
   - Not part of Option C. The gRPC sidecar is the new standardized surface.
   - Legacy interfaces remain available for existing tooling but are not exposed through the fleet API.

4. **Should HAL writes require explicit acknowledgment from the target component?**
   - Currently planned: fire-and-forget write with result success/failure based on pin existence and direction.
   - For safety-critical pins, could add a "confirm" RPC that requires the component to acknowledge the write within a timeout window.

5. **How should we handle machine registration without a central config file?**
   - Sidecar broadcasts registration to gateway on startup (auto-discovery).
   - Gateway stores registration with TTL; sidecar heartbeats every 10s.
   - Manual override: static config file listing known machines (for environments without auto-discovery).
