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
| `linuxcnc.RCS_IDLE` (1) | IDLE / AUTO_DONE depending on execution |
| `linuxcnc.RCS_RUNNING` (3) | RUNNING |
| `linuxcnc.RCS_COMPLETED` (4) | AUTO_DONE |
| Combined with `execution` field for precise state |

| linuxcnc.stat.execution value | ExecutionState enum |
|---|---|
| `linuxcnc.EXEC_STATE_IDLE` (0) | IDLE |
| `linuxcnc.EXEC_STATE_RUN` (1) | RUN |
| `linuxcnc.EXEC_STATE_FAST_RUN` (2) | FAST_RUN |
| `linuxcnc.EXEC_STATE_STEP` (3) | STEP |
| `linuxcnc.EXEC_STATE_RETRACT` (4) | RETRACT |
| `linuxcnc.EXEC_STATE_MDA` (5) | MDA |

| linuxcnc.stat.mode value | Mode enum |
|---|---|
| `linuxcnc.MODE_MANUAL` (1) | MANUAL |
| `linuxcnc.MODE_AUTO` (2) | AUTO |
| `linuxcnc.MODE_MDI` (3) | MDA |

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

    async def SubscribeStatus(self, request, context):
        while True:
            yield self.sidecar.get_status()
            await asyncio.sleep(0.02)

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
                  cert_file: str = None, key_file: str = None):
    server = grpc.server(futures_executor=ThreadPoolExecutor(max_workers=8))
    fleet_pb2_grpc.add_FleetServiceServicer_to_server(
        FleetServiceServicer(sidecar), server)

    if cert_file and key_file:
        # mTLS setup with root_certificates, private_key, certificate_chain
        creds = grpc.ssl_channel_credentials(
            root_certificates=open(cert_file).read(),
            private_key=open(key_file).read(),
            certificate_chain=open(cert_file).read())
        server.add_secure_port(f'[::]:{port}', creds)
    else:
        server.add_insecure_port(f'[::]:{port}')

    return server
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

Each sidecar registers on startup:

```python
# In headless server startup:
async def register_with_gateway(gateway_addr, machine_info):
    # gRPC call to GatewayService.RegisterMachine
    # Sidecar sends its MachineInfo + capabilities
    # Gateway stores in registry with TTL (heartbeat every 10s)
```

### Authorization Model

| Role | Can Read Status | Can Control Machine | Can Write HAL Pins | Can Load Programs | Scope |
|---|---|---|---|---|---|
| `viewer` | All assigned machines | No | No | No | Facility |
| `operator` | All assigned machines | Start/Stop/Hold/Home | Output pins only | No | Facility |
| `programmer` | All assigned machines | MDI/Step/Load | No | Yes | Facility |
| `maintainer` | All assigned machines | All controls | All pins | Yes | Machine-level |
| `admin` | All | All | All | All | Global |

Attributes for scoping:
- `facility`: e.g., "shop-floor-1", "lab"
- `role`: one of the above
- `machine_tags`: e.g., ["cnc-mill", "legacy"]

### Gateway Service Implementation

```python
class GatewayServiceServicer(gateway_pb2_grpc.FleetGatewayServiceServicer):
    def __init__(self, auth_manager: AuthManager, registry: MachineRegistry):
        self.auth = auth_manager
        self.registry = registry

    async def DiscoverMachines(self, request, context):
        user = self.auth.get_user(context)
        facility = request.facility or user.attributes.get('facility')
        machines = self.registry.list_by_facility(facility)
        # Filter by user's authorized machines
        return MachineList(machines=authorized_machines)

    async def RouteMachine(self, request, context):
        user = self.auth.get_user(context)
        machine_id = request.id.id
        if not self.auth.can_access(user, machine_id):
            context.abort(grpc.StatusCode.PERMISSION_DENIED, 'Not authorized')
        route = self.registry.lookup(machine_id)
        return GatewayRoute(address=route.address, port=route.port)

    async def BroadcastCommand(self, request, context):
        user = self.auth.get_user(context)
        # Resolve target machines based on scope/facility/tags
        targets = self.registry.resolve_scope(request.scope, ...)
        if not self.auth.can_control(user, [m.id for m in targets]):
            context.abort(grpc.StatusCode.PERMISSION_DENIED, 'Not authorized')

        # Fan-out to each instance via FleetService RPCs
        results = {}
        for target in targets:
            result = await call_fleet_service(target, request.command)
            results[target.id] = result
        return BroadcastResult(results=results)
```

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

## Central Client Library (`fleet_client.py`)

Python library for the central UI to interact with the fleet.

```python
class FleetClient:
    """High-level client for the fleet management API."""

    def __init__(self, gateway_address: str, token: str):
        self.gateway_channel = grpc.secure_channel(
            gateway_address, creds)  # TLS with OIDC auth
        self.stub = fleet_gateway_pb2_grpc.FleetGatewayServiceStub(self.gateway_channel)
        self._machine_channels = {}  # cached per-machine channels

    async def get_machines(self, facility: str = None) -> List[MachineInfo]:
        """Discover available machines."""
        resp = await self.stub.DiscoverMachines(
            DiscoverRequest(facility=facility))
        return resp.machines

    async def get_status(self, machine_id: str) -> MachineStatus:
        """Get current status of a specific machine."""
        route = await self.stub.RouteMachine(MachineId(id=machine_id))
        channel = self._get_machine_channel(route)
        stub = fleet_pb2_grpc.FleetServiceStub(channel)
        return await stub.GetStatus(MachineId(id=machine_id))

    async def stream_status(self, machine_id: str):
        """Subscribe to real-time status updates."""
        route = await self.stub.RouteMachine(MachineId(id=machine_id))
        channel = self._get_machine_channel(route)
        stub = fleet_pb2_grpc.FleetServiceStub(channel)
        async for status in stub.SubscribeStatus(MachineId(id=machine_id)):
            yield status

    async def send_mdi(self, machine_id: str, command: str):
        """Send MDI command to a machine."""
        route = await self.stub.RouteMachine(MachineId(id=machine_id))
        channel = self._get_machine_channel(route)
        stub = fleet_pb2_grpc.FleetServiceStub(channel)
        return await stub.SendMdiCommand(MdiCommand(
            id=MachineId(id=machine_id), command=command))

    async def read_hal_pin(self, machine_id: str, pin_name: str):
        """Read a HAL pin value."""
        route = await self.stub.RouteMachine(MachineId(id=machine_id))
        channel = self._get_machine_channel(route)
        stub = fleet_pb2_grpc.FleetServiceStub(channel)
        return await stub.ReadHalPin(HalPinRead(
            id=MachineId(id=machine_id), pin_name=pin_name))

    async def write_hal_pin(self, machine_id: str, pin_name: str, value):
        """Write a HAL pin value."""
        route = await self.stub.RouteMachine(MachineId(id=machine_id))
        channel = self._get_machine_channel(route)
        stub = fleet_pb2_grpc.FleetServiceStub(channel)
        return await stub.WriteHalPin(HalPinWrite(
            id=MachineId(id=machine_id), pin_name=pin_name, ...))

    async def broadcast_mdi(self, scope, facility=None, tags=None, command: str):
        """Send MDI to all matching machines."""
        return await self.stub.BroadcastCommand(BroadcastRequest(
            scope=scope, facility=facility, tags=tags,
            mdi=MdiCommand(id=MachineId(id='*'), command=command)))

    def _get_machine_channel(self, route: GatewayRoute) -> grpc.Channel:
        """Get or create cached channel to a machine instance."""
        key = f"{route.address}:{route.instance_port}"
        if key not in self._machine_channels:
            # mTLS channel to instance (gateway cert + key)
            self._machine_channels[key] = grpc.secure_channel(
                f"{route.address}:{route.instance_port}", instance_creds)
        return self._machine_channels[key]
```

---

## File Layout

```
linuxcnc-fleet/
├── proto/
│   └── fleet.proto              # gRPC service definition (all RPCs + messages)
├── linuxcnc_fleet/
│   ├── __init__.py
│   ├── headless.py              # LinuxCncSidecar class — wraps linuxcnc module
│   ├── server.py                # gRPC server per instance
│   └── cli.py                   # CLI entry point: headless-server --ini ...
├── gateway/
│   ├── __init__.py
│   ├── server.py                # FleetGatewayService implementation
│   ├── auth.py                  # OIDC token validation + user extraction
│   ├── policies.py              # RBAC policy engine
│   └── registry.py              # Machine registration + discovery store
├── fleet_client/
│   ├── __init__.py
│   └── client.py                # FleetClient high-level library
├── scripts/
│   └── linuxcnc-fleet.service   # systemd service template
├── certs/                       # TLS certificates (git-ignored)
│   ├── ca.pem
│   ├── server.pem
│   └── server-key.pem
├── pyproject.toml               # Package definition + dependencies
└── Makefile                     # Build: proto generation, install
```

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
- `PyJWT` or `jose` (OIDC token validation)
- `aiohttp` or `fastapi` (optional: HTTP health/metrics endpoint)
- Redis or in-memory dict (machine registry store)

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
# 1. Run gateway
linuxcnc-gateway --listen :50050 \
    --oidc-issuer https://keycloak.example.com/realms/linuxcnc \
    --oidc-client-id fleet-ui \
    --cert /etc/linuxcnc-fleet/gateway.pem \
    --key /etc/linuxcnc-fleet/gateway-key.pem

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

### Phase 1: Core Sidecar (Week 1-2)
- [ ] Define and generate `fleet.proto` with FleetService RPCs
- [ ] Implement `LinuxCncSidecar` class in `headless.py`
  - [ ] Polling loop at 50Hz with atomic snapshot updates
  - [ ] Status extraction from linuxcnc.stat
  - [ ] Mode/executive control wrappers
  - [ ] HAL pin read/write via _hal module
  - [ ] INI param access
- [ ] Implement gRPC server in `server.py`
- [ ] Write systemd service template
- [ ] Test against a single LinuxCNC instance (uspace mode)

### Phase 2: Gateway & Auth (Week 3-4)
- [ ] Implement FleetGatewayService RPCs
- [ ] OIDC token validation (support Keycloak/Auth0 format)
- [ ] RBAC policy engine with attribute-based scoping
- [ ] Machine registration and heartbeat mechanism
- [ ] Broadcast command fan-out
- [ ] TLS/mTLS certificate management

### Phase 3: Client Library & UI Integration (Week 5-6)
- [ ] Implement `FleetClient` high-level library
- [ ] Generated gRPC stubs for all services
- [ ] Channel caching and connection management
- [ ] Streaming status subscription support
- [ ] Error handling and retry logic

### Phase 4: Hardening & Packaging (Week 7-8)
- [ ] Central UI integration tests against gateway + sidecar
- [ ] Load testing: concurrent connections, broadcast performance
- [ ] Package distribution (pip wheel)
- [ ] Certificate auto-renewal support
- [ ] Metrics/health endpoints (Prometheus / HTTP)

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
