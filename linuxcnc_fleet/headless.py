"""LinuxCNC sidecar — wraps linuxcnc module bindings with a 50Hz polling loop."""

from __future__ import annotations

import dataclasses
import logging
import os
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


class _NoDefault:
    """Sentinel for optional constructor parameters."""


from linuxcnc_fleet.fleet_pb2 import (
    BroadcastRequest,
    ErrorEvent,
    ErrorCode,
    EstopState,
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
    MachineControlState,
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
    if execution == 0:
        return ExecutionState.EXEC_IDLE
    if execution == 1:
        return ExecutionState.RUN
    if execution == 2:
        return ExecutionState.FAST_RUN
    if execution == 3:
        return ExecutionState.STEP
    if execution == 4:
        return ExecutionState.RETRACT
    if execution == 5:
        return ExecutionState.MDA
    return ExecutionState.EXEC_IDLE


def _map_interp_state(interp: int) -> InterpState:
    if interp == linuxcnc.INTERP_IDLE:
        return InterpState.INTERP_IDLE
    if interp == linuxcnc.INTERP_READING:
        return InterpState.READ
    if interp == linuxcnc.INTERP_WAITING:
        return InterpState.PREDICT
    if interp == linuxcnc.INTERP_PAUSED:
        return InterpState.EXECUTE
    return InterpState.INTERP_IDLE


def _map_estop_state(estop: int) -> EstopState:
    if estop == 0:
        return EstopState.NOT_E_STOPPED
    return EstopState.E_STOPPED


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
    """Wraps linuxcnc module bindings with a configurable polling loop.

    The default poll rate is 50Hz (0.02s), which provides up to 20ms latency
    for status updates — acceptable for dashboard visibility but not for
    closed-loop control.

    Poll interval can be configured via:
    - Constructor parameter ``poll_interval``
    - Environment variable ``LINUXCNC_FLEET_POLL_INTERVAL`` (seconds)
    - Falls back to 0.02 (50Hz) if neither is set
    """

    DEFAULT_POLL_INTERVAL = 0.02  # 50 Hz

    def __init__(self, ini_path: Optional[str] = None, machine_id: Optional[str] = None,
                 poll_interval: Optional[float] = None, _hal_override=_NoDefault):
        import os as _os

        if linuxcnc is None:
            raise RuntimeError("linuxcnc module not available — requires LinuxCNC installation")

        self._ini_path = ini_path or (linuxcnc.find_file('ini') if hasattr(linuxcnc, 'find_file') else None)
        self._machine_id = machine_id or "default"
        self._hal = _hal_override if _hal_override is not _NoDefault else _hal

        # Poll interval: constructor > env var > default
        poll_interval_sec = poll_interval
        if poll_interval_sec is None:
            env_val = _os.environ.get("LINUXCNC_FLEET_POLL_INTERVAL")
            if env_val is not None:
                try:
                    poll_interval_sec = float(env_val)
                except ValueError:
                    log.warning("Invalid LINUXCNC_FLEET_POLL_INTERVAL '%s', using default 0.02", env_val)
                    poll_interval_sec = self.DEFAULT_POLL_INTERVAL
            else:
                poll_interval_sec = self.DEFAULT_POLL_INTERVAL

        if poll_interval_sec <= 0:
            raise ValueError(f"poll_interval must be positive, got {poll_interval_sec}")

        self.POLL_INTERVAL = poll_interval_sec

        # Initialize linuxcnc bindings
        self._stat = linuxcnc.stat()
        self._command = linuxcnc.command()
        self._error_channel = linuxcnc.error_channel()
        if self._ini_path is None:
            raise RuntimeError(
                "INI file path is required. Pass --ini to CLI or set LINUXCNC_INIPATH env var"
            )
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

    def shutdown(self) -> None:
        """Stop the polling loop and clean up resources."""
        self._running = False
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Background loop: poll at configured rate, build new snapshot each iteration."""
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
                err = self._error_channel.poll()
                if err is None:
                    break
                msg = f"{err['text']} (line {err['line']})"
                self._error_queue.append(msg)
                # Keep last 100 errors
                if len(self._error_queue) > 100:
                    self._error_queue.pop(0)

        # joint_actual_position and joint_position are flat tuples indexed by axis
        joint_actual = self._stat.joint_actual_position
        joint_commanded = self._stat.joint_position

        # position/actual_position are 9-tuples: (x, y, z, a, b, c, u, v, w)
        pos = self._stat.position
        actual = getattr(self._stat, 'actual_position', pos)

        # Spindle is a tuple of dicts; use first spindle
        spindles = self._stat.spindle
        spindle0 = spindles[0] if spindles else {}

        return _Snapshot(
            machine_id=self._machine_id,
            state=self._stat.state,
            execution=self._stat.exec_state,
            interp_state=self._stat.interp_state,
            estop_state=self._stat.estop,
            mode=self._stat.task_mode,
            joint_actual_x=joint_actual[0] if len(joint_actual) > 0 else 0.0,
            joint_actual_y=joint_actual[1] if len(joint_actual) > 1 else 0.0,
            joint_actual_z=joint_actual[2] if len(joint_actual) > 2 else 0.0,
            joint_actual_a=joint_actual[3] if len(joint_actual) > 3 else 0.0,
            joint_actual_b=joint_actual[4] if len(joint_actual) > 4 else 0.0,
            joint_actual_c=joint_actual[5] if len(joint_actual) > 5 else 0.0,
            joint_actual_u=joint_actual[6] if len(joint_actual) > 6 else 0.0,
            joint_actual_v=joint_actual[7] if len(joint_actual) > 7 else 0.0,
            joint_actual_w=joint_actual[8] if len(joint_actual) > 8 else 0.0,
            joint_actual_p=joint_actual[9] if len(joint_actual) > 9 else 0.0,
            joint_actual_q=joint_actual[10] if len(joint_actual) > 10 else 0.0,
            joint_commanded_x=joint_commanded[0] if len(joint_commanded) > 0 else 0.0,
            joint_commanded_y=joint_commanded[1] if len(joint_commanded) > 1 else 0.0,
            joint_commanded_z=joint_commanded[2] if len(joint_commanded) > 2 else 0.0,
            joint_commanded_a=joint_commanded[3] if len(joint_commanded) > 3 else 0.0,
            joint_commanded_b=joint_commanded[4] if len(joint_commanded) > 4 else 0.0,
            joint_commanded_c=joint_commanded[5] if len(joint_commanded) > 5 else 0.0,
            joint_commanded_u=joint_commanded[6] if len(joint_commanded) > 6 else 0.0,
            joint_commanded_v=joint_commanded[7] if len(joint_commanded) > 7 else 0.0,
            joint_commanded_w=joint_commanded[8] if len(joint_commanded) > 8 else 0.0,
            joint_commanded_p=joint_commanded[9] if len(joint_commanded) > 9 else 0.0,
            joint_commanded_q=joint_commanded[10] if len(joint_commanded) > 10 else 0.0,
            world_x=pos[0],
            world_y=pos[1],
            world_z=pos[2],
            world_a=pos[3],
            world_b=pos[4],
            world_c=pos[5],
            interp_line=getattr(self._stat, 'motion_line', 0),
            program_file=self._stat.file or "",
            feedrate=self._stat.feedrate,
            feedrate_override=getattr(self._stat, 'feed_override', 1.0) or 1.0,
            spindle_speed=spindle0.get('speed', 0.0),
            spindle_speed_override=spindle0.get('override', 1.0),
            coolant_mist=bool(self._stat.mist),
            coolant_flood=bool(self._stat.flood),
            coolant_mazak=False,
            errors=list(self._error_queue),
            motion_type=self._stat.motion_type,
            num_joints=getattr(self._stat, 'joints', 0),
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
            num_joints=getattr(self._stat, 'joints', 0),
            num_hal_components=0,  # populated by HAL enumeration
        )

    def get_ini_param(self, section: str, option: str) -> str:
        """Read an INI parameter."""
        return self._ini(section, option) or ""

    def read_hal_pin(self, name: str) -> HalPinValue:
        """Read a HAL pin value via the _hal module."""
        if self._hal is None:
            raise RuntimeError("_hal module not available")

        try:
            pin_type = self._hal.get_type(name)
            value = self._hal.get_value(name)
        except Exception as e:
            raise ValueError(f"HAL pin '{name}' not found: {e}") from e

        hal_type = _map_hal_pin_type(pin_type)

        if hal_type == HalPinType.PIN_TYPE_BIT:
            return HalPinValue(
                pin_name=name, type=hal_type, value_bit=bool(value),
            )
        elif hal_type == HalPinType.PIN_TYPE_U32:
            return HalPinValue(
                pin_name=name, type=hal_type, value_u32=int(value),
            )
        elif hal_type == HalPinType.PIN_TYPE_S32:
            return HalPinValue(
                pin_name=name, type=hal_type, value_s32=int(value),
            )
        else:
            return HalPinValue(
                pin_name=name, type=hal_type, value_f=float(value),
            )

    def list_hal_components(self) -> HalComponentList:
        """Enumerate HAL components and their pins."""
        if self._hal is None:
            raise RuntimeError("_hal module not available")

        components: list[HalComponentInfo] = []
        try:
            for comp_name in self._hal.comp_list():
                pins = []
                for pin_name in self._hal.list_pins(comp_name):
                    try:
                        pin_type = self._hal.get_type(pin_name)
                        value = self._hal.get_value(pin_name)
                        is_output = self._hal.is_output(pin_name) if hasattr(self._hal, 'is_output') else False

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

                update_period = self._hal.get_update_period(comp_name) if hasattr(self._hal, 'get_update_period') else 0.0
                params: dict[str, float] = {}
                for param_name in self._hal.list_params(comp_name) if hasattr(self._hal, 'list_params') else []:
                    try:
                        params[param_name] = float(self._hal.get_param(param_name))
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
        if self._snapshot.estop_state == EstopState.E_STOPPED:
            return Result(
                success=False, message="Cannot change mode — E-stop is active",
                error_code=ErrorCode.E_STOP_ACTIVE,
            )

        try:
            mode_map = {
                Mode.MODE_MANUAL: linuxcnc.MODE_MANUAL,
                Mode.MODE_AUTO: linuxcnc.MODE_AUTO,
                Mode.MODE_MDA: linuxcnc.MODE_MDI,
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
                self.set_mode(Mode.MODE_MDA)

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

    def list_programs(self, directory: str = "", max_depth: int = 0) -> list[dict]:
        """List available G-code programs on the machine.

        Reads program_extension from INI [TRAJ] and scans subdirectories
        to find matching files.

        Args:
            directory: Base directory to scan (default: INI subdirectory).
            max_depth: Recursion depth limit, 0=infinite.

        Returns:
            List of dicts with path, name, size_bytes, modified_time.
        """
        try:
            extensions_str = self._ini("TRAJ", "program_extension") or "ngc"
            extensions = set(extensions_str.split())

            # Trusted root is the INI-configured program subdirectory
            ini_subdir = self._ini("RS274", "subdirectory") or ""
            if not ini_subdir:
                ini_subdir = self._ini("EMC_TASK_CALL_SUB_DIRECTORY", "") or "."
            trusted_root = os.path.realpath(os.path.abspath(ini_subdir))

            # Resolve user-provided directory; clamp to trusted root if it escapes
            if directory:
                candidate = os.path.realpath(os.path.abspath(directory))
                if not (candidate == trusted_root or candidate.startswith(trusted_root + os.sep)):
                    log.warning(
                        "list_programs: directory '%s' resolved outside trusted root '%s', clamping",
                        directory, trusted_root,
                    )
                    directory = ini_subdir if ini_subdir else "."
            else:
                directory = ini_subdir if ini_subdir else "."

            base_dir = os.path.realpath(os.path.abspath(directory))

            programs = []
            root_depth = directory.rstrip(os.sep).count(os.sep)

            for root, dirs, files in os.walk(directory):
                resolved_root = os.path.realpath(os.path.abspath(root))
                if not resolved_root.startswith(base_dir + os.sep) and resolved_root != base_dir:
                    dirs.clear()
                    continue
                if max_depth > 0:
                    current_depth = root.count(os.sep) - root_depth
                    if current_depth >= max_depth:
                        dirs.clear()
                        continue

                for f in files:
                    ext = os.path.splitext(f)[1].lstrip('.').lower()
                    if ext in extensions:
                        full_path = os.path.join(root, f)
                        try:
                            stat = os.stat(full_path)
                            programs.append({
                                "path": full_path,
                                "name": os.path.splitext(f)[0],
                                "size_bytes": stat.st_size,
                                "modified_time": stat.st_mtime,
                            })
                        except OSError:
                            pass

            return sorted(programs, key=lambda p: p["modified_time"], reverse=True)

        except Exception as e:
            log.error("ListPrograms failed: %s", e)
            return []

    def step_forward(self) -> Result:
        """Step forward one block in MDA mode."""
        try:
            self._command.execute(linuxcnc.EXEC_STEP)
            return Result(success=True, message="Stepped forward")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def set_machine_state(self, state: MachineControlState) -> Result:
        """Set machine control state (e-stop reset, power on/off).

        Args:
            state: MachineControlState enum value.
                STATE_ESTOP_RESET (1) — Clear E-stop
                STATE_ON (3) — Power on the machine
                STATE_OFF (2) — Power off the machine
                STATE_ESTOP (0) — Enter E-stop (internal to LinuxCNC)

        Returns:
            Result with success status and error message.
        """
        state_map = {
            MachineControlState.STATE_ESTOP: linuxcnc.STATE_ESTOP,
            MachineControlState.STATE_ESTOP_RESET: linuxcnc.STATE_ESTOP_RESET,
            MachineControlState.STATE_OFF: linuxcnc.STATE_OFF,
            MachineControlState.STATE_ON: linuxcnc.STATE_ON,
        }
        lcnc_state = state_map.get(state)
        if lcnc_state is None:
            return Result(
                success=False, message=f"Unknown machine control state: {state}",
                error_code=ErrorCode.INVALID_STATE,
            )
        try:
            self._command.state(lcnc_state)
            return Result(success=True, message=f"Machine state set to {state}")
        except Exception as e:
            return Result(success=False, message=str(e), error_code=ErrorCode.INTERNAL_ERROR)

    def write_hal_pin(self, pin_name: str, value_f: float = 0.0,
                      value_u32: int = 0, value_s32: int = 0,
                      value_bit: bool = False) -> Result:
        """Write a HAL pin value (output pins only)."""
        if self._hal is None:
            return Result(
                success=False, message="_hal module not available",
                error_code=ErrorCode.INTERNAL_ERROR,
            )

        try:
            is_output = self._hal.is_output(pin_name) if hasattr(self._hal, 'is_output') else False
            if not is_output:
                return Result(
                    success=False, message=f"Pin '{pin_name}' is not an output",
                    error_code=ErrorCode.HAL_WRITE_PROTECTED,
                )

            # Determine which value to write based on pin type
            try:
                pin_type = self._hal.get_type(pin_name)
            except Exception:
                return Result(
                    success=False, message=f"Pin '{pin_name}' not found",
                    error_code=ErrorCode.HAL_PIN_NOT_FOUND,
                )

            if pin_type == self._hal.HAL_BIT:
                self._hal.set_value(pin_name, 1.0 if value_bit else 0.0)
            elif pin_type == self._hal.HAL_U32:
                self._hal.set_value(pin_name, float(value_u32))
            elif pin_type == self._hal.HAL_S32:
                self._hal.set_value(pin_name, float(value_s32))
            else:
                self._hal.set_value(pin_name, value_f)

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
