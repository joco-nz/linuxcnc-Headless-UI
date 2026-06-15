"""Tests for state mapping functions (linuxcnc constants → protobuf enums)."""

import pytest

from linuxcnc_fleet.fleet_pb2 import (
    EstopState,
    ExecutionState,
    HalPinType,
    InterpState,
    MachineState,
    Mode,
)
from linuxcnc_fleet.headless import (
    _map_estop_state,
    _map_execution_state,
    _map_hal_pin_type,
    _map_interp_state,
    _map_machine_state,
    _map_mode,
)


class TestMapMachineState:
    def test_rcs_done_maps_to_auto_done(self, linuxcnc_module):
        """RCS_DONE → AUTO_DONE."""
        stat = linuxcnc_module.stat()
        stat.state = linuxcnc_module.RCS_DONE
        assert _map_machine_state(stat) == MachineState.AUTO_DONE

    def test_rcs_running_maps_to_running(self, linuxcnc_module):
        """RCS_RUNNING → RUNNING."""
        stat = linuxcnc_module.stat()
        stat.state = linuxcnc_module.RCS_RUNNING
        assert _map_machine_state(stat) == MachineState.RUNNING

    def test_rcs_idle_maps_to_off(self, linuxcnc_module):
        """RCS_IDLE → OFF (execution field distinguishes further)."""
        stat = linuxcnc_module.stat()
        stat.state = linuxcnc_module.RCS_IDLE
        assert _map_machine_state(stat) == MachineState.OFF

    def test_unknown_value_maps_to_unknown(self, linuxcnc_module):
        """Unknown state value → UNKNOWN."""
        stat = linuxcnc_module.stat()
        stat.state = 99
        assert _map_machine_state(stat) == MachineState.UNKNOWN


class TestMapExecutionState:
    def test_idle(self, linuxcnc_module):
        assert _map_execution_state(linuxcnc_module.EXEC_STATE_IDLE) == ExecutionState.EXEC_IDLE

    def test_run(self, linuxcnc_module):
        assert _map_execution_state(linuxcnc_module.EXEC_STATE_RUN) == ExecutionState.RUN

    def test_fast_run(self, linuxcnc_module):
        assert _map_execution_state(linuxcnc_module.EXEC_STATE_FAST_RUN) == ExecutionState.FAST_RUN

    def test_step(self, linuxcnc_module):
        assert _map_execution_state(linuxcnc_module.EXEC_STATE_STEP) == ExecutionState.STEP

    def test_retract(self, linuxcnc_module):
        assert _map_execution_state(linuxcnc_module.EXEC_STATE_RETRACT) == ExecutionState.RETRACT

    def test_mda(self, linuxcnc_module):
        assert _map_execution_state(linuxcnc_module.EXEC_STATE_MDA) == ExecutionState.MDA

    def test_unknown_maps_to_idle(self):
        assert _map_execution_state(99) == ExecutionState.EXEC_IDLE


class TestMapInterpState:
    def test_idle(self, linuxcnc_module):
        assert _map_interp_state(linuxcnc_module.INTERP_IDLE) == InterpState.INTERP_IDLE

    def test_reading(self, linuxcnc_module):
        assert _map_interp_state(linuxcnc_module.INTERP_READING) == InterpState.READ

    def test_paused(self, linuxcnc_module):
        assert _map_interp_state(linuxcnc_module.INTERP_PAUSED) == InterpState.EXECUTE

    def test_waiting(self, linuxcnc_module):
        assert _map_interp_state(linuxcnc_module.INTERP_WAITING) == InterpState.PREDICT

    def test_unknown_maps_to_idle(self):
        assert _map_interp_state(99) == InterpState.INTERP_IDLE


class TestMapEstopState:
    def test_estopped(self, linuxcnc_module):
        assert _map_estop_state(linuxcnc_module.ESTOP_ACK) == EstopState.E_STOPPED

    def test_not_estopped(self):
        assert _map_estop_state(0) == EstopState.NOT_E_STOPPED


class TestMapMode:
    def test_manual(self, linuxcnc_module):
        assert _map_mode(linuxcnc_module.MODE_MANUAL) == Mode.MODE_MANUAL

    def test_auto(self, linuxcnc_module):
        assert _map_mode(linuxcnc_module.MODE_AUTO) == Mode.MODE_AUTO

    def test_mdi(self, linuxcnc_module):
        assert _map_mode(linuxcnc_module.MODE_MDI) == Mode.MODE_MDA

    def test_unknown_maps_to_unknown(self):
        assert _map_mode(99) == Mode.MODE_UNKNOWN


class TestMapHalPinType:
    def test_bit(self, hal_module):
        assert _map_hal_pin_type(hal_module.HAL_BIT) == HalPinType.PIN_TYPE_BIT

    def test_u32(self, hal_module):
        assert _map_hal_pin_type(hal_module.HAL_U32) == HalPinType.PIN_TYPE_U32

    def test_s32(self, hal_module):
        assert _map_hal_pin_type(hal_module.HAL_S32) == HalPinType.PIN_TYPE_S32

    def test_float(self, hal_module):
        assert _map_hal_pin_type(hal_module.HAL_FLOAT) == HalPinType.PIN_TYPE_FLOAT

    def test_unknown_maps_to_float(self):
        assert _map_hal_pin_type(99) == HalPinType.PIN_TYPE_FLOAT
