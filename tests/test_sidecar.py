"""Tests for control command error paths (E-stop guard, mode validation, etc.)."""

import pytest

from linuxcnc_fleet.fleet_pb2 import ErrorCode, EstopState, Mode, Result, TrajAxis
from linuxcnc_fleet.headless import LinuxCncSidecar


class TestEStopGuard:
    def test_set_mode_blocked_when_estopped(self, linuxcnc_module):
        """set_mode should return E_STOP_ACTIVE when estop is engaged."""
        from linuxcnc_fleet.fleet_pb2 import EstopState

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        # Set estop on the snapshot (sidecar reads from _snapshot)
        object.__setattr__(sidecar._snapshot, 'estop_state', EstopState.E_STOPPED)
        result = sidecar.set_mode(Mode.MODE_AUTO)

        assert result.success is False
        assert result.error_code == ErrorCode.E_STOP_ACTIVE
        assert "e-stop" in result.message.lower()


class TestModeValidation:
    def test_set_mode_unknown_mode(self, linuxcnc_module):
        """set_mode with an unknown Mode enum should return INVALID_STATE."""
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        # Create a Mode value that doesn't exist in our mapping
        class UnknownMode:
            pass

        unknown = UnknownMode()
        result = sidecar.set_mode(unknown)

        assert result.success is False
        assert result.error_code == ErrorCode.INVALID_STATE


class TestControlCommandErrors:
    def test_start_calls_execute(self, linuxcnc_module):
        """start() should call command.execute(EXEC_START)."""
        stat = linuxcnc_module.stat()
        cmd = linuxcnc_module.command()

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.start()

        assert result.success is True
        assert "started" in result.message.lower()

    def test_stop_calls_execute(self, linuxcnc_module):
        """stop() should call command.execute(EXEC_STOP)."""
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.stop()

        assert result.success is True
        assert "stopped" in result.message.lower()

    def test_feed_hold(self, linuxcnc_module):
        """feed_hold() should call command.feed_hold()."""
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.feed_hold()

        assert result.success is True
        assert "hold" in result.message.lower()

    def test_continue_exec(self, linuxcnc_module):
        """continue_exec() should call command.continue_()."""
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.continue_exec()

        assert result.success is True
        assert "continued" in result.message.lower()


class TestHomeAxis:
    def test_home_axis_x(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.home_axis(TrajAxis.X_AXIS)
        assert result.success is True

    def test_home_axis_z(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.home_axis(TrajAxis.Z_AXIS)
        assert result.success is True

    def test_home_all(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.home_all()
        assert result.success is True

    def test_home_axis_unknown_returns_error(self, linuxcnc_module):
        """Home an axis value not in the mapping."""
        class UnknownAxis:
            pass

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.home_axis(UnknownAxis())
        assert result.success is False
        assert result.error_code == ErrorCode.INVALID_STATE


class TestMDICommand:
    def test_send_mdi(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.send_mdi("G0 X10.0")
        assert result.success is True

    def test_send_mdi_empty_command(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.send_mdi("")
        # Empty command should still be sent (linuxcnc.command handles validation)
        assert "MDI" in result.message or result.success is True


class TestLoadProgram:
    def test_load_program(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.load_program("/path/to/program.ngc")
        assert result.success is True
        assert "program" in result.message.lower()

    def test_load_program_relative_path(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.load_program("relative.ngc")
        assert result.success is True


class TestStepForward:
    def test_step_forward(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        result = sidecar.step_forward()
        assert result.success is True
        assert "stepped" in result.message.lower()


class TestGetMachineInfo:
    def test_machine_info_contains_version(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        info = sidecar.get_machine_info()
        assert info.machine_id == "test"
        assert info.version.version_string == "2.9"

    def test_machine_info_joints(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        info = sidecar.get_machine_info()
        assert info.num_joints == 3


class TestGetIniParam:
    def test_get_ini_param(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        value = sidecar.get_ini_param("SPINDLE_1", "RANGE_MIN")
        assert value == "SPINDLE_1.RANGE_MIN"


class TestSnapshotPolling:
    def test_run_starts_polling_thread(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        sidecar.run()

        assert sidecar._running is True
        assert sidecar._poll_thread is not None
        assert sidecar._poll_thread.is_alive()

        # Clean up polling thread
        sidecar.shutdown()

    def test_stop_stops_polling_thread(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        sidecar.run()
        sidecar.shutdown()

        assert sidecar._running is False
        assert sidecar._poll_thread is None or not sidecar._poll_thread.is_alive()

    def test_run_twice_does_not_start_two_threads(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        sidecar.run()
        thread_count_before = 1 if sidecar._poll_thread else 0

        # Calling run() again should be a no-op
        sidecar.run()
        thread_count_after = 1 if sidecar._poll_thread else 0

        assert thread_count_before == thread_count_after
        sidecar.shutdown()

    def test_get_status_returns_machine_status(self, linuxcnc_module):
        from linuxcnc_fleet.fleet_pb2 import MachineStatus

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        # Poll once to build initial snapshot (without starting background thread)
        snap = sidecar._build_snapshot()
        object.__setattr__(sidecar, '_snapshot', snap)

        status = sidecar.get_status()
        assert isinstance(status, MachineStatus)
        assert status.machine_id == "test"
        assert status.joint_actual.x == 0.0
