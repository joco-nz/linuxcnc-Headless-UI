"""LinuxCNC sidecar — wraps linuxcnc module bindings with a 50Hz polling loop."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Any, Optional

try:
    import linuxcnc
except ImportError:
    linuxcnc = None  # type: ignore[assignment]

try:
    import _hal
except ImportError:
    _hal = None  # type: ignore[assignment]

from linuxcnc_fleet.fleet_pb2 import (
    BroadcastRequest,
    ErrorEvent,
    ErrorCode,
    ExecutionCommand,
    ExecutionState,
    HalComponentInfo,
    HalComponentList,
    HalPinInfo,
    HalPinType,
    HalPinUpdate,
    HalPinValue,
    IniParamRequest,
    IniParamValue,
    InterpState,
    LinuxCncVersion,
    MachineInfo,
    MachineState,
    MachineStatus,
    Mode,
    Position,
    Result,
    TrajAxis,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State mapping helpers
# ---------------------------------------------------------------------------

# linuxcnc.stat.state -> MachineState (combined with execution field)
def _map_machine_state(stat: Any) -> MachineState:
    if stat.state == linuxcnc.RCS_DONE:
        return MachineState.AUTO_DONE
    if stat.state == linuxcnc.RCS_RUNNING:
        return MachineState.RUNNING
    if stat.state == linuxcnc.RCS_IDLE:
        # Distinguished by execution field below; default to OFF/INITIALIZING
        return MachineState.OFF
    return MachineState.UNKNOWN


def _map_execution_state(execution: int) -> ExecutionState:
    mapping = {
        linuxcnc.EXEC_STATE_IDLE: ExecutionState.EXEC_IDLE,
        linuxcnc.EXEC_STATE_RUN: ExecutionState.RUN,
        linuxcnc.EXEC_STATE_FAST_RUN: ExecutionState.FAST_RUN,
        linuxcnc.EXEC_STATE_STEP: ExecutionState.STEP,
        linuxcnc.EXEC_STATE_RETRACT: ExecutionState.RETRACT,
        linuxcnc.EXEC_STATE_MDA: ExecutionState.MDA,
    }
    return mapping.get(execution, ExecutionState.EXEC_IDLE)


def _map_interp_state(interp: int) -> InterpState:
    mapping = {
        linuxcnc.INTERP_IDLE: InterpState.INTERP_IDLE,
        linuxcnc.INTERP_READ: InterpState.READ,
        linuxcnc.INTERP_EXEC: InterpState.EXECUTE,
    }
    return mapping.get(interp, InterpState.INTERP_IDLE)


def _map_estop_state(estop: int) -> EstopState:
    if estop == linuxcnc.ESTOP_ACK:
        return EstopState.E_STOPPED
    return EstopState.NOT_E_STOPPED


def _map_mode(mode: int) -> Mode:
    mapping = {
        linuxcnc.MODE_MANUAL: Mode.MODE_MANUAL,
        linuxcnc.MODE_AUTO: Mode.MODE_AUTO,
        linuxcnc.MODE_MDI: Mode.MODE_MDA,
    }
    return mapping.get(mode, Mode.MODE_UNKNOWN)


def _map_hal_pin_type(pin_type: int) -> HalPinType:
    mapping = {
        _hal.HAL_BIT: HalPinType.PIN_TYPE_BIT,
        _hal.HAL_U32: HalPinType.PIN_TYPE_U32,
        _hal.HAL_S32: HalPinType.PIN_TYPE_S32,
        _hal.HAL_FLOAT: HalPinType.PIN_TYPE_FLOAT,
    }
    return mapping.get(pin_type, HalPinType.PIN_TYPE_FLOAT)


def _make_position(x: float = 0.0, y: float = 0.0, z: float = 0.0,
                   a: float = 0.0, b: float = 0.0, c: float = 0.0,
                   u: float = 0.0, v: float = 0.0, w: float = 0.0,
                   p: float = 0.0, q: float = 0.0) -> Position:
    return Position(x=x, y=y, z=z, a=a, b=b, c=c, u=u, v=v, w=w, p=p, q=q)


# ---------------------------------------------------------------------------
# Snapshot dataclass — immutable on update via dataclasses.replace()
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class _Snapshot:
    """Thread-safe atomic snapshot. Updated by writer thread, read atomically."""
    machine_id: str = ""
    state: int = 0
    execution: int = 0
    interp_state: int = 0
    estop_state: int = 0
    mode: int = 0
    joint_actual_x: float = 0.0
    joint_actual_y: float = 0.0
    joint_actual_z: float = 0.0
    joint_actual_a: float = 0.0
    joint_actual_b: float = 0.0
    joint_actual_c: float = 0.0
    joint_actual_u: float = 0.0
    joint_actual_v: float = 0.0
    joint_actual_w: float = 0.0
    joint_actual_p: float = 0.0
    joint_actual_q: float = 0.0
    joint_commanded_x: float = 0.0
    joint_commanded_y: float = 0.0
    joint_commanded_z: float = 0.0
    joint_commanded_a: float = 0.0
    joint_commanded_b: float = 0.0
    joint_commanded_c: float = 0.0
    joint_commanded_u: float = 0.0
    joint_commanded_v: float = 0.0
    joint_commanded_w: float = 0.0
    joint_commanded_p: float = 0.0
    joint_commanded_q: float = 0.0
    world_x: float = 0.0
    world_y: float = 0.0
    world_z: float = 0.0
    world_a: float = 0.0
    world_b: float = 0.0
    world_c: float = 0.0
    interp_line: int = 0
    program_file: str = ""
    remaining_time: str = ""
    feedrate: float = 0.0
    feedrate_override: float = 1.0
    spindle_speed: float = 0.0
    spindle_speed_override: float = 1.0
    coolant_mist: bool = False
    coolant_flood: bool = False
    coolant_mazak: bool = False
    errors: list[str] = dataclasses.field(default_factory=list)
    cycle_time: float = 0.0
    motion_type: int = 0
    num_joints: int = 0
    version_string: str = ""
    build_type: str = ""
    git_hash: str = ""


# ---------------------------------------------------------------------------
# LinuxCncSidecar
# ---------------------------------------------------------------------------

class LinuxCncSidecar:
    """Wraps linuxcnc module bindings with a 50Hz polling loop."""

    POLL_INTERVAL = 0.02  # 50 Hz

    def __init__(self, ini_path: Optional[str] = None, machine_id: Optional[str] = None):
        if linuxcnc is None:
            raise RuntimeError("linuxcnc module not available — requires LinuxCNC installation")

        self._ini_path = ini_path or linuxcnc.ini.find()
        self._machine_id = machine_id or "default"

        # Initialize linuxcnc bindings
        self._stat = linuxcnc.stat()
        self._command = linuxcnc.command()
        self._error_channel = linuxcnc.error_channel()
        self._ini = linuxcnc.ini(self._ini_path)

        # Error queue for streaming
        self._error_queue: list[str] = []
        self._error_lock = threading.Lock()

        # Atomic snapshot — writer thread replaces, reader swaps reference
        self._snapshot: _Snapshot = _Snapshot(machine_id=self._machine_id)
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None

    def run(self) -> None:
        """Start polling loop in a background daemon thread (non-blocking)."""
        if self._running:
            return
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="sidecar-poller")
        self._poll_thread.start()

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Background loop: poll at 50Hz, build new snapshot each iteration."""
        while self._running:
            try:
                new_snapshot = self._build_snapshot()
                # Atomic swap — reader sees either old or new, never half-built
                object.__setattr__(self, '_snapshot', new_snapshot)
            except Exception:
                log.exception("Poll iteration failed")
            time.sleep(self.POLL_INTERVAL)

    def _build_snapshot(self) -> _Snapshot:
        """Single poll iteration — read all linuxcnc state."""
        self._stat.poll()

        # Collect errors from error channel
        with self._error_lock:
            while True:
                try:
                    err = self._error_channel.get()
                    msg = f"{err['text']} (line {err['line']})"
                    self._error_queue.append(msg)
                    # Keep last 100 errors
                    if len(self._error_queue) > 100:
                        self._error_queue.pop(0)
                except IndexError:
                    break

        # Extract positions
        joint_actual = self._stat.joint_actual_pos
        joint_commanded = self._stat.joint_commanded_pos

        return _Snapshot(
            machine_id=self._machine_id,
            state=self._stat.state,
            execution=self._stat.execution,
            interp_state=self._stat.interp_state,
            estop_state=self._stat.estop_state,
            mode=self._stat.mode,
            joint_actual_x=joint_actual[0][0],
            joint_actual_y=joint_actual[1][0],
            joint_actual_z=joint_actual[2][0],
            joint_actual_a=joint_actual[3][0] if len(joint_actual) > 3 else 0.0,
            joint_actual_b=joint_actual[4][0] if len(joint_actual) > 4 else 0.0,
            joint_actual_c=joint_actual[5][0] if len(joint_actual) > 5 else 0.0,
            joint_actual_u=joint_actual[6][0] if len(joint_actual) > 6 else 0.0,
            joint_actual_v=joint_actual[7][0] if len(joint_actual) > 7 else 0.0,
            joint_actual_w=joint_actual[8][0] if len(joint_actual) > 8 else 0.0,
            joint_actual_p=joint_actual[9][0] if len(joint_actual) > 9 else 0.0,
            joint_actual_q=joint_actual[10][0] if len(joint_actual) > 10 else 0.0,
            joint_commanded_x=joint_commanded[0][0],
            joint_commanded_y=joint_commanded[1][0],
            joint_commanded_z=joint_commanded[2][0],
            joint_commanded_a=joint_commanded[3][0] if len(joint_commanded) > 3 else 0.0,
            joint_commanded_b=joint_commanded[4][0] if len(joint_commanded) > 4 else 0.0,
            joint_commanded_c=joint_commanded[5][0] if len(joint_commanded) > 5 else 0.0,
            joint_commanded_u=joint_commanded[6][0] if len(joint_commanded) > 6 else 0.0,
            joint_commanded_v=joint_commanded[7][0] if len(joint_commanded) > 7 else 0.0,
            joint_commanded_w=joint_commanded[8][0] if len(joint_commanded) > 8 else 0.0,
            joint_commanded_p=joint_commanded[9][0] if len(joint_commanded) > 9 else 0.0,
            joint_commanded_q=joint_commanded[10][0] if len(joint_commanded) > 10 else 0.0,
            world_x=self._stat.x,
            world_y=self._stat.y,
            world_z=self._stat.z,
            world_a=self._stat.a,
            world_b=self._stat.b,
            world_c=self._stat.c,
            interp_line=self._stat.interp_line,
            program_file=self._stat.program_file or "",
            feedrate=self._stat.linear_axis.get('feedrate', 0.0),
            feedrate_override=self._stat.linear_axis.get('feed_percent', 1.0),
            spindle_speed=self._stat.spindle_at_speed,
            spindle_speed_override=self._stat.spindle.get('speed_percent', 1.0),
            coolant_mist=bool(self._stat.coolant_mist),
            coolant_flood=bool(self._stat.coolant_flood),
            coolant_mazak=bool(self._stat.coolant_mazak),
            errors=list(self._error_queue),
            motion_type=self._stat.motion_type,
            num_joints=self._stat.joint_config[0] if len(self._stat.joint_config) > 0 else 0,
        )

    # ------------------------------------------------------------------
    # Public API — read operations
    # ------------------------------------------------------------------

    def get_status(self) -> MachineStatus:
        """Return latest snapshot as protobuf MachineStatus."""
        s = self._snapshot

        return MachineStatus(
            machine_id=s.machine_id,
            state=_map_machine_state(s),
            execution=_map_execution_state(s.execution),
            interp_state=_map_interp_state(s.interp_state),
            estop_state=_map_estop_state(s.estop_state),
            mode=_map_mode(s.mode),
            joint_actual=_make_position(
                s.joint_actual_x, s.joint_actual_y, s.joint_actual_z,
                s.joint_actual_a, s.joint_actual_b, s.joint_actual_c,
                s.joint_actual_u, s.joint_actual_v, s.joint_actual_w,
                s.joint_actual_p, s.joint_actual_q,
            ),
            joint_commanded=_make_position(
                s.joint_commanded_x, s.joint_commanded_y, s.joint_commanded_z,
                s.joint_commanded_a, s.joint_commanded_b, s.joint_commanded_c,
                s.joint_commanded_u, s.joint_commanded_v, s.joint_commanded_w,
                s.joint_commanded_p, s.joint_commanded_q,
            ),
            world_actual=_make_position(
                s.world_x, s.world_y, s.world_z,
                s.world_a, s.world_b, s.world_c,
            ),
            interp_line=s.interp_line,
            program_file=s.program_file,
            remaining_time=s.remaining_time,
            feedrate=s.feedrate,
            feedrate_override=s.feedrate_override,
            spindle_speed=s.spindle_speed,
            spindle_speed_override=s.spindle_speed_override,
            coolant_mist=s.coolant_mist,
            coolant_flood=s.coolant_flood,
            coolant_mazak=s.coolant_mazak,
            active_errors=s.errors,
            cycle_time=s.cycle_time,
            motion_type=s.motion_type,
        )

    def get_machine_info(self) -> MachineInfo:
        """Assemble MachineInfo from ini + stat."""
        version_str = self._ini.FILE_VERSION() if hasattr(self._ini, 'FILE_VERSION') else ""
        build_type = self._ini.BUILD_TYPE() if hasattr(self._ini, 'BUILD_TYPE') else ""
        git_hash = self._ini.GIT_HASH() if hasattr(self._ini, 'GIT_HASH') else ""

        return MachineInfo(
            machine_id=self._machine_id,
            machine_name=self._machine_id,
            host_address="",
            version=LinuxCncVersion(
                version_string=version_str,
                build_type=build_type,
                git_hash=git_hash,
            ),
            num_joints=self._snapshot.num_joints,
            num_hal_components=0,  # populated by HAL enumeration
        )

    def get_ini_param(self, section: str, option: str) -> str:
        """Read an INI parameter."""
        return self._ini(section, option) or ""

    def read_hal_pin(self, name: str) -> HalPinValue:
        """Read a HAL pin value via the _hal module."""
        if _hal is None:
            raise RuntimeError("_hal module not available")

        try:
            pin_type = _hal.get_type(name)
            value = _hal.get_value(name)
        except Exception as e:
            raise ValueError(f"HAL pin '{name}' not found: {e}") from e

        hal_type = _map_hal_pin_type(pin_type)
        is_output = _hal.is_output(name) if hasattr(_hal, 'is_output') else False

        if hal_type == HalPinType.PIN_TYPE_BIT:
            return HalPinValue(
                pin_name=name, type=hal_type, value_bit=bool(value), is_output=is_output,
            )
        elif hal_type == HalPinType.PIN_TYPE_U32:
            return HalPinValue(
                pin_name=name, type=hal_type, value_u32=int(value), is_output=is_output,
            )
        elif hal_type == HalPinType.PIN_TYPE_S32:
            return HalPinValue(
                pin_name=name, type=hal_type, value_s32=int(value), is_output=is_output,
            )
        else:
            return HalPinValue(
                pin_name=name, type=hal_type, value_f=float(value), is_output=is_output,
            )

    def list_hal_components(self) -> HalComponentList:
        """Enumerate HAL components and their pins."""
        if _hal is None:
            raise RuntimeError("_hal module not available")

        components: list[HalComponentInfo] = []
        try:
            for comp_name in _hal.comp_list():
                pins = []
                for pin_name in _hal.list_pins(comp_name):
                    try:
                        pin_type = _hal.get_type(pin_name)
                        value = _hal.get_value(pin_name)
                        is_output = _hal.is_output(pin_name) if hasattr(_hal, 'is_output') else False

                        hal_type = _map_hal_pin_type(pin_type)
                        pi = HalPinInfo(
                            name=pin_name,
                            type=hal_type,
                            is_output=is_output,
                        )
                        if hal_type == HalPinType.PIN_TYPE_BIT:
                            pi.value_bit = bool(value)
                        elif hal_type == HalPinType.PIN_TYPE_U32:
                            pi.value_u32 = int(value)
                        elif hal_type == HalPinType.PIN_TYPE_S32:
                            pi.value_s32 = int(value)
                        else:
                            pi.value_f = float(value)
                        pins.append(pi)
                    except Exception:
                        pass

                update_period = _hal.get_update_period(comp_name) if hasattr(_hal, 'get_update_period') else 0.0
                params: dict[str, float] = {}
                for param_name in _hal.list_params(comp_name) if hasattr(_hal, 'list_params') else []:
                    try:
                        params[param_name] = float(_hal.get_param(param_name))
                    except Exception:
                        pass

                components.append(HalComponentInfo(
                    name=comp_name,
                    update_period_ns=update_period,
                    pins=pins,
                    params=params,
                ))
        except Exception:
            log.exception("Failed to enumerate HAL components")

        return HalComponentList(components=components)

    def get_errors(self, limit: int = 100) -> list[ErrorEvent]:
        """Return recent errors with timestamps."""
        with self._error_lock:
            recent = self._error_queue[-limit:] if len(self._error_queue) > limit else self._error_queue[:]
        import time as _time
        now = _time.time()
        return [ErrorEvent(message=e, timestamp=now) for e in recent]

    # ------------------------------------------------------------------
    # Public API — write / control operations
    # ------------------------------------------------------------------

    def set_mode(self, mode: Mode) -> Result:
        """Set machine mode with validation."""
        if self._snapshot.estop_state == EstopState.E_STOPPED.value:
            return Result(
                success=False, message="Cannot change mode — E-stop is active",
                error_code=ErrorCode.E_STOP_ACTIVE,
            )

        try:
            mode_map = {
                Mode.MODE_MANUAL: linuxcnc.MODE_MANUAL,
                Mode.MODE_AUTO: linuxcnc.MODE_AUTO,
                Mode.MODE_MDI: linuxcnc.MODE_MDI,
            }
            lcnc_mode = mode_map.get(mode)
            if lcnc_mode is None:
                return Result(
                    success=False, message=f"Unknown mode: {mode}",
                    error_code=ErrorCode.INVALID_STATE,
                )
            self._command.mode(lcnc_mode)
            return Result(success=True, message=f"Mode set to {mode}")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def set_execution(self, state: ExecutionState) -> Result:
        """Set execution state."""
        if self._snapshot.state != linuxcnc.RCS_IDLE and self._snapshot.state != linuxcnc.RCS_RUNNING:
            return Result(
                success=False, message="Cannot execute — machine is not in an active state",
                error_code=ErrorCode.MACHINE_OFF,
            )

        try:
            exec_map = {
                ExecutionState.EXEC_IDLE: linuxcnc.EXEC_STOP,
                ExecutionState.RUN: linuxcnc.EXEC_START,
                ExecutionState.FAST_RUN: None,  # handled via program open with fast
                ExecutionState.STEP: linuxcnc.EXEC_STEP,
                ExecutionState.RETRACT: None,
                ExecutionState.MDA: None,
            }

            cmd = exec_map.get(state)
            if cmd is not None:
                self._command.execute(cmd)
            elif state == ExecutionState.FAST_RUN:
                # Start in fast run mode
                self._command.fast_mode(True)
                self._command.execute(linuxcnc.EXEC_START)
            elif state == ExecutionState.RETRACT:
                self._command.retract(True)
            elif state == ExecutionState.MDA:
                # Switch to MDA mode if not already
                self.set_mode(Mode.MODE_MDI)

            return Result(success=True, message=f"Execution set to {state}")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def start(self) -> Result:
        """Start program execution."""
        try:
            self._command.execute(linuxcnc.EXEC_START)
            return Result(success=True, message="Execution started")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def stop(self) -> Result:
        """Stop program execution."""
        try:
            self._command.execute(linuxcnc.EXEC_STOP)
            return Result(success=True, message="Execution stopped")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def feed_hold(self) -> Result:
        """Feed hold — pause motion while maintaining spindle."""
        try:
            self._command.feed_hold()
            return Result(success=True, message="Feed hold engaged")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def continue_exec(self) -> Result:
        """Continue after feed hold."""
        try:
            self._command.continue_()
            return Result(success=True, message="Execution continued")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def home_all(self) -> Result:
        """Home all joints."""
        try:
            self._command.home(-1)  # -1 = all axes
            return Result(success=True, message="Homing all axes")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def home_axis(self, axis: TrajAxis) -> Result:
        """Home a specific axis."""
        axis_map = {
            TrajAxis.X_AXIS: 0,
            TrajAxis.Y_AXIS: 1,
            TrajAxis.Z_AXIS: 2,
            TrajAxis.A_AXIS: 3,
            TrajAxis.B_AXIS: 4,
            TrajAxis.C_AXIS: 5,
            TrajAxis.U_AXIS: 6,
            TrajAxis.V_AXIS: 7,
            TrajAxis.W_AXIS: 8,
            TrajAxis.P_AXIS: 9,
            TrajAxis.Q_AXIS: 10,
        }
        joint = axis_map.get(axis)
        if joint is None:
            return Result(
                success=False, message=f"Unknown axis: {axis}",
                error_code=ErrorCode.INVALID_STATE,
            )
        try:
            self._command.home(joint)
            return Result(success=True, message=f"Homing axis {axis}")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def send_mdi(self, command: str) -> Result:
        """Send an MDI command."""
        try:
            self._command.mdi(command)
            return Result(success=True, message="MDI command sent")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def load_program(self, path: str) -> Result:
        """Load a G-code program."""
        try:
            self._command.program_open(path)
            return Result(success=True, message=f"Program loaded: {path}")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def step_forward(self) -> Result:
        """Step forward one block in MDA mode."""
        try:
            self._command.execute(linuxcnc.EXEC_STEP)
            return Result(success=True, message="Stepped forward")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def write_hal_pin(self, pin_name: str, value_f: float = 0.0,
                      value_u32: int = 0, value_s32: int = 0,
                      value_bit: bool = False) -> Result:
        """Write a HAL pin value (output pins only)."""
        if _hal is None:
            return Result(
                success=False, message="_hal module not available",
                error_code=ErrorCode.INTERNAL_ERROR,
            )

        try:
            is_output = _hal.is_output(pin_name) if hasattr(_hal, 'is_output') else False
            if not is_output:
                return Result(
                    success=False, message=f"Pin '{pin_name}' is not an output",
                    error_code=ErrorCode.HAL_WRITE_PROTECTED,
                )

            # Determine which value to write based on pin type
            try:
                pin_type = _hal.get_type(pin_name)
            except Exception:
                return Result(
                    success=False, message=f"Pin '{pin_name}' not found",
                    error_code=ErrorCode.HAL_PIN_NOT_FOUND,
                )

            if pin_type == _hal.HAL_BIT:
                _hal.set_value(pin_name, 1.0 if value_bit else 0.0)
            elif pin_type == _hal.HAL_U32:
                _hal.set_value(pin_name, float(value_u32))
            elif pin_type == _hal.HAL_S32:
                _hal.set_value(pin_name, float(value_s32))
            else:
                _hal.set_value(pin_name, value_f)

            return Result(success=True, message=f"Wrote to pin '{pin_name}'")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def subscribe_hal_pins(self, pin_names: list[str], poll_interval: float = 0.1):
        """Generator that streams HAL pin updates at the given interval."""
        while self._running:
            for name in pin_names:
                try:
                    pin = self.read_hal_pin(name)
                    update = HalPinUpdate(pin_name=name)
                    if pin.type == HalPinType.PIN_TYPE_BIT:
                        update.value_bit = pin.value_bit
                    elif pin.type == HalPinType.PIN_TYPE_U32:
                        update.value_u32 = pin.value_u32
                    elif pin.type == HalPinType.PIN_TYPE_S32:
                        update.value_s32 = pin.value_s32
                    else:
                        update.value_f = pin.value_f
                    yield update
                except Exception as e:
                    log.warning("Error reading HAL pin '%s': %s", name, e)
            time.sleep(poll_interval)

    def subscribe_errors(self):
        """Generator that yields error events as they arrive."""
        last_count = 0
        while self._running:
            with self._error_lock:
                current = len(self._error_queue)
                if current > last_count:
                    for i in range(last_count, current):
                        evt = ErrorEvent(
                            message=self._error_queue[i],
                            timestamp=time.time(),
                        )
                        yield evt
                    last_count = current
            time.sleep(0.1)
