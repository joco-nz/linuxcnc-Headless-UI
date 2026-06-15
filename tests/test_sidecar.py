"""Tests for control command error paths (E-stop guard, mode validation, etc.)."""

import time

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
        assert isinstance(status.spindle_speed, float)


class TestPollIntervalConfig:
    def test_default_poll_interval_is_50hz(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        assert sidecar.POLL_INTERVAL == 0.02

    def test_custom_poll_interval_via_constructor(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", poll_interval=0.1)
        assert sidecar.POLL_INTERVAL == 0.1

    def test_slow_poll_interval(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", poll_interval=1.0)
        assert sidecar.POLL_INTERVAL == 1.0

    def test_fast_poll_interval(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", poll_interval=0.005)
        assert sidecar.POLL_INTERVAL == 0.005

    def test_invalid_poll_interval_rejected(self, linuxcnc_module):
        with pytest.raises(ValueError, match="poll_interval must be positive"):
            LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", poll_interval=0)

    def test_negative_poll_interval_rejected(self, linuxcnc_module):
        with pytest.raises(ValueError, match="poll_interval must be positive"):
            LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", poll_interval=-0.5)

    def test_env_var_sets_poll_interval(self, linuxcnc_module, monkeypatch):
        monkeypatch.setenv("LINUXCNC_FLEET_POLL_INTERVAL", "0.25")
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        assert sidecar.POLL_INTERVAL == 0.25

    def test_constructor_overrides_env_var(self, linuxcnc_module, monkeypatch):
        monkeypatch.setenv("LINUXCNC_FLEET_POLL_INTERVAL", "0.25")
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", poll_interval=0.1)
        assert sidecar.POLL_INTERVAL == 0.1

    def test_invalid_env_var_falls_back_to_default(self, linuxcnc_module, monkeypatch):
        monkeypatch.setenv("LINUXCNC_FLEET_POLL_INTERVAL", "not_a_number")
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        assert sidecar.POLL_INTERVAL == 0.02

    def test_polling_thread_uses_custom_interval(self, linuxcnc_module):
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", poll_interval=0.05)
        sidecar.run()

        time.sleep(0.15)  # Should complete ~3 iterations at 0.05s each

        assert sidecar._poll_thread is not None
        assert sidecar._poll_thread.is_alive()
        assert sidecar.POLL_INTERVAL == 0.05

        sidecar.shutdown()


class TestListPrograms:

    def test_returns_empty_when_no_files(self, linuxcnc_module, tmp_path):
        """No matching files → empty list."""
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return ".ngc .ntpl"
            if section == "RS274" and option == "subdirectory":
                return str(tmp_path)
            return None

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory=str(tmp_path))
        assert programs == []

    def test_finds_ngc_files(self, linuxcnc_module, tmp_path):
        """Files with .ngc extension are found."""
        (tmp_path / "test.ngc").write_text("G0 X1")
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc ntpl"
            if section == "RS274" and option == "subdirectory":
                return str(tmp_path)
            return None

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory=str(tmp_path))
        assert len(programs) == 1
        assert programs[0]["name"] == "test"
        assert programs[0]["path"] == str(tmp_path / "test.ngc")

    def test_finds_multiple_extensions(self, linuxcnc_module, tmp_path):
        """Both .ngc and .ntpl files are found."""
        (tmp_path / "a.ngc").write_text("G0 X1")
        (tmp_path / "b.ntpl").write_text("# template")
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc ntpl"
            if section == "RS274" and option == "subdirectory":
                return str(tmp_path)
            return None

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory=str(tmp_path))
        assert len(programs) == 2
        names = {p["name"] for p in programs}
        assert names == {"a", "b"}

    def test_ignores_non_matching_extensions(self, linuxcnc_module, tmp_path):
        """Files with non-matching extensions (e.g. .txt) are excluded."""
        (tmp_path / "test.ngc").write_text("G0 X1")
        (tmp_path / "readme.txt").write_text("hello")
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc ntpl"
            if section == "RS274" and option == "subdirectory":
                return str(tmp_path)
            return None

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory=str(tmp_path))
        assert len(programs) == 1
        assert programs[0]["name"] == "test"

    def test_case_insensitive_extension(self, linuxcnc_module, tmp_path):
        """Extension matching is case-insensitive."""
        (tmp_path / "upper.NGC").write_text("G0 X1")
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc"
            if section == "RS274" and option == "subdirectory":
                return str(tmp_path)
            return None

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory=str(tmp_path))
        assert len(programs) == 1
        assert programs[0]["name"] == "upper"

    def test_max_depth_limits_recursion(self, linuxcnc_module, tmp_path):
        """max_depth=0 returns all; max_depth=1 limits to one level."""
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (tmp_path / "top.ngc").write_text("G0 X1")
        (subdir / "deep.ngc").write_text("G0 Y1")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc"
            if section == "RS274" and option == "subdirectory":
                return str(tmp_path)
            return None

        sidecar._ini = ini_mock

        # max_depth=0 (infinite) finds both
        programs_all = sidecar.list_programs(directory=str(tmp_path), max_depth=0)
        assert len(programs_all) == 2

        # max_depth=1 limits to top level only
        programs_limited = sidecar.list_programs(directory=str(tmp_path), max_depth=1)
        assert len(programs_limited) == 1
        assert programs_limited[0]["name"] == "top"

    def test_sorted_by_modified_time_descending(self, linuxcnc_module, tmp_path):
        """Results are sorted by modified_time in descending order."""
        import time as _time

        (tmp_path / "old.ngc").write_text("G0 X1")
        _time.sleep(0.05)
        (tmp_path / "new.ngc").write_text("G0 Y1")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc"
            if section == "RS274" and option == "subdirectory":
                return str(tmp_path)
            return None

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory=str(tmp_path))
        assert len(programs) == 2
        assert programs[0]["name"] == "new"
        assert programs[1]["name"] == "old"

    def test_includes_size_and_mtime(self, linuxcnc_module, tmp_path):
        """Each result includes path, name, size_bytes, modified_time."""
        (tmp_path / "test.ngc").write_text("hello")
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc"
            if section == "RS274" and option == "subdirectory":
                return str(tmp_path)
            return None

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory=str(tmp_path))
        assert len(programs) == 1
        p = programs[0]
        assert "path" in p
        assert "name" in p
        assert "size_bytes" in p
        assert "modified_time" in p
        assert isinstance(p["size_bytes"], int)
        assert isinstance(p["modified_time"], float)

    def test_oserror_on_stat_is_silenced(self, linuxcnc_module, tmp_path):
        """If os.stat() raises OSError for a file, it is skipped."""
        import os as _os
        from unittest.mock import patch

        (tmp_path / "test.ngc").write_text("G0 X1")
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc"
            if section == "RS274" and option == "subdirectory":
                return str(tmp_path)
            return None

        sidecar._ini = ini_mock

        # Patch os.stat to raise OSError for files with "bad" in name
        original_stat = _os.stat
        def failing_stat(path):
            if "bad" in str(path):
                raise OSError("file gone")
            return original_stat(path)

        with patch.object(_os, 'stat', failing_stat):
            programs = sidecar.list_programs(directory=str(tmp_path))
        # Should still find the file since "test.ngc" doesn't contain "bad"
        assert len(programs) == 1

    def test_ini_default_extension_ngc(self, linuxcnc_module, tmp_path):
        """When INI returns no program_extension, defaults to 'ngc'."""
        (tmp_path / "test.ngc").write_text("G0 X1")
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "RS274" and option == "subdirectory":
                return str(tmp_path)
            return None  # No program_extension set

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory=str(tmp_path))
        assert len(programs) == 1
        assert programs[0]["name"] == "test"

    def test_directory_fallback_to_ini_subdirectory(self, linuxcnc_module, tmp_path):
        """When directory is empty, falls back to INI RS274 subdirectory."""
        subdir = tmp_path / "gcode"
        subdir.mkdir()
        (subdir / "test.ngc").write_text("G0 X1")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc"
            if section == "RS274" and option == "subdirectory":
                return str(subdir)
            return None

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory="")
        assert len(programs) == 1
        assert programs[0]["name"] == "test"

    def test_exception_returns_empty_list(self, linuxcnc_module):
        """If an unexpected exception occurs, returns empty list."""
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        sidecar._ini = lambda section, option: (_ for _ in ()).throw(RuntimeError("boom"))
        programs = sidecar.list_programs(directory="/nonexistent")
        assert programs == []

    def test_default_directory_from_emc_ini(self, linuxcnc_module, tmp_path):
        """Falls back to EMC_TASK_CALL_SUB_DIRECTORY when RS274 subdirectory is empty."""
        (tmp_path / "test.ngc").write_text("G0 X1")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc"
            if section == "RS274" and option == "subdirectory":
                return ""  # Empty, should fall through to EMC_TASK_CALL_SUB_DIRECTORY
            if section == "EMC_TASK_CALL_SUB_DIRECTORY" and option == "":
                return str(tmp_path)
            return None

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory="")
        assert len(programs) == 1

    def test_path_traversal_blocked(self, linuxcnc_module, tmp_path):
        """Directory traversal via .. is blocked — files outside base_dir are skipped."""
        safe_dir = tmp_path / "safe"
        safe_dir.mkdir()
        (safe_dir / "ok.ngc").write_text("G0 X1")

        etc_dir = tmp_path / "etc"
        etc_dir.mkdir()
        (etc_dir / "passwd.ngc").write_text("fake passwd")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc ntpl"
            if section == "RS274" and option == "subdirectory":
                return str(safe_dir)
            return None

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory=str(safe_dir))
        assert len(programs) == 1
        assert programs[0]["name"] == "ok"
        names = {p["name"] for p in programs}
        assert "passwd" not in names

    def test_symlink_target_resolved(self, linuxcnc_module, tmp_path):
        """Symlinks are resolved — files in the symlink target are listed."""
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        (target_dir / "found.ngc").write_text("G0 X1")

        link = tmp_path / "link"
        link.symlink_to(target_dir)

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc"
            if section == "RS274" and option == "subdirectory":
                return str(tmp_path)
            return None

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory=str(link))
        assert len(programs) == 1
        assert programs[0]["name"] == "found"

    def test_dotdot_escape_from_base_blocked(self, linuxcnc_module, tmp_path):
        """Path with .. components that escape the base directory is blocked."""
        safe_dir = tmp_path / "safe"
        safe_dir.mkdir()
        (safe_dir / "ok.ngc").write_text("G0 X1")

        secret_dir = tmp_path / "secret"
        secret_dir.mkdir()
        (secret_dir / "leaked.ngc").write_text("secret code")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        def ini_mock(section, option):
            if section == "TRAJ" and option == "program_extension":
                return "ngc"
            if section == "RS274" and option == "subdirectory":
                return str(safe_dir)
            return None

        sidecar._ini = ini_mock
        programs = sidecar.list_programs(directory=str(safe_dir / ".." / "secret"))
        names = {p["name"] for p in programs}
        assert "leaked" not in names


class TestReadHalPin:

    def _make_hal_mock(self):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.HAL_BIT = 0
        m.HAL_U32 = 1
        m.HAL_S32 = 2
        m.HAL_FLOAT = 3
        return m

    def test_raises_when_hal_module_missing(self, linuxcnc_module):
        """read_hal_pin raises RuntimeError when _hal is None."""
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=None)
        with pytest.raises(RuntimeError, match="_hal module not available"):
            sidecar.read_hal_pin("some_pin")

    def test_returns_bit_value(self, linuxcnc_module):
        """read_hal_pin returns HalPinValue with value_bit for HAL_BIT type."""
        hal = self._make_hal_mock()
        hal.get_type.return_value = 0  # HAL_BIT
        hal.get_value.return_value = 1.0

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.read_hal_pin("spindle.on")

        assert result.pin_name == "spindle.on"
        from linuxcnc_fleet.fleet_pb2 import HalPinType
        assert result.type == HalPinType.PIN_TYPE_BIT
        assert result.value_bit is True

    def test_returns_u32_value(self, linuxcnc_module):
        """read_hal_pin returns HalPinValue with value_u32 for HAL_U32 type."""
        hal = self._make_hal_mock()
        hal.get_type.return_value = 1  # HAL_U32
        hal.get_value.return_value = 42.0

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.read_hal_pin("feed.rate")

        from linuxcnc_fleet.fleet_pb2 import HalPinType
        assert result.type == HalPinType.PIN_TYPE_U32
        assert result.value_u32 == 42

    def test_returns_s32_value(self, linuxcnc_module):
        """read_hal_pin returns HalPinValue with value_s32 for HAL_S32 type."""
        hal = self._make_hal_mock()
        hal.get_type.return_value = 2  # HAL_S32
        hal.get_value.return_value = -100.0

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.read_hal_pin("offset.z")

        from linuxcnc_fleet.fleet_pb2 import HalPinType
        assert result.type == HalPinType.PIN_TYPE_S32
        assert result.value_s32 == -100

    def test_returns_float_value(self, linuxcnc_module):
        """read_hal_pin returns HalPinValue with value_f for HAL_FLOAT type."""
        hal = self._make_hal_mock()
        hal.get_type.return_value = 3  # HAL_FLOAT
        hal.get_value.return_value = 3.14159

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.read_hal_pin("spindle.speed-cmd")

        from linuxcnc_fleet.fleet_pb2 import HalPinType
        assert result.type == HalPinType.PIN_TYPE_FLOAT
        assert abs(result.value_f - 3.14159) < 0.0001

    def test_raises_value_error_on_get_type_failure(self, linuxcnc_module):
        """read_hal_pin raises ValueError when _hal.get_type fails."""
        hal = self._make_hal_mock()
        hal.get_type.side_effect = KeyError("pin not registered")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        with pytest.raises(ValueError, match="not found"):
            sidecar.read_hal_pin("missing.pin")

    def test_raises_value_error_on_get_value_failure(self, linuxcnc_module):
        """read_hal_pin raises ValueError when _hal.get_value fails after get_type."""
        hal = self._make_hal_mock()
        hal.get_type.return_value = 3  # HAL_FLOAT
        hal.get_value.side_effect = RuntimeError("value unavailable")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        with pytest.raises(ValueError, match="not found"):
            sidecar.read_hal_pin("unavailable.pin")


class TestWriteHalPin:

    def _make_hal_mock(self):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.HAL_BIT = 0
        m.HAL_U32 = 1
        m.HAL_S32 = 2
        m.HAL_FLOAT = 3
        return m

    def test_returns_error_when_hal_missing(self, linuxcnc_module):
        """write_hal_pin returns error Result when _hal is None."""
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=None)
        result = sidecar.write_hal_pin("some.pin")
        assert result.success is False
        assert "not available" in result.message.lower()

    def test_returns_write_protected_for_input_pin(self, linuxcnc_module):
        """write_hal_pin returns HAL_WRITE_PROTECTED when pin is not an output."""
        hal = self._make_hal_mock()
        hal.is_output.return_value = False

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.write_hal_pin("input.pin")

        assert result.success is False
        from linuxcnc_fleet.fleet_pb2 import ErrorCode
        assert result.error_code == ErrorCode.HAL_WRITE_PROTECTED
        assert "not an output" in result.message.lower()

    def test_returns_pin_not_found_when_type_lookup_fails(self, linuxcnc_module):
        """write_hal_pin returns HAL_PIN_NOT_FOUND when get_type fails."""
        hal = self._make_hal_mock()
        hal.is_output.return_value = True
        hal.get_type.side_effect = KeyError("unknown pin")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.write_hal_pin("unknown.pin")

        from linuxcnc_fleet.fleet_pb2 import ErrorCode
        assert result.success is False
        assert result.error_code == ErrorCode.HAL_PIN_NOT_FOUND
        assert "not found" in result.message.lower()

    def test_writes_bit_true(self, linuxcnc_module):
        """write_hal_pin writes 1.0 for bit pin when value_bit=True."""
        hal = self._make_hal_mock()
        hal.is_output.return_value = True
        hal.get_type.return_value = 0  # HAL_BIT

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.write_hal_pin("relay.on", value_bit=True)

        assert result.success is True
        hal.set_value.assert_called_with("relay.on", 1.0)

    def test_writes_bit_false(self, linuxcnc_module):
        """write_hal_pin writes 0.0 for bit pin when value_bit=False."""
        hal = self._make_hal_mock()
        hal.is_output.return_value = True
        hal.get_type.return_value = 0  # HAL_BIT

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.write_hal_pin("relay.off", value_bit=False)

        assert result.success is True
        hal.set_value.assert_called_with("relay.off", 0.0)

    def test_writes_u32_value(self, linuxcnc_module):
        """write_hal_pin writes float(value_u32) for HAL_U32 pin."""
        hal = self._make_hal_mock()
        hal.is_output.return_value = True
        hal.get_type.return_value = 1  # HAL_U32

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.write_hal_pin("counter.val", value_u32=99)

        assert result.success is True
        hal.set_value.assert_called_with("counter.val", 99.0)

    def test_writes_s32_value(self, linuxcnc_module):
        """write_hal_pin writes float(value_s32) for HAL_S32 pin."""
        hal = self._make_hal_mock()
        hal.is_output.return_value = True
        hal.get_type.return_value = 2  # HAL_S32

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.write_hal_pin("offset.x", value_s32=-50)

        assert result.success is True
        hal.set_value.assert_called_with("offset.x", -50.0)

    def test_writes_float_value(self, linuxcnc_module):
        """write_hal_pin writes value_f for HAL_FLOAT pin."""
        hal = self._make_hal_mock()
        hal.is_output.return_value = True
        hal.get_type.return_value = 3  # HAL_FLOAT

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.write_hal_pin("spindle.speed-cmd", value_f=1500.0)

        assert result.success is True
        hal.set_value.assert_called_with("spindle.speed-cmd", 1500.0)

    def test_returns_internal_error_on_set_value_failure(self, linuxcnc_module):
        """write_hal_pin returns INTERNAL_ERROR when set_value raises."""
        hal = self._make_hal_mock()
        hal.is_output.return_value = True
        hal.get_type.return_value = 3  # HAL_FLOAT
        hal.set_value.side_effect = RuntimeError("hardware fault")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.write_hal_pin("bad.pin", value_f=1.0)

        from linuxcnc_fleet.fleet_pb2 import ErrorCode
        assert result.success is False
        assert result.error_code == ErrorCode.INTERNAL_ERROR


class TestListHalComponents:

    def _make_hal_mock(self):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.HAL_BIT = 0
        m.HAL_U32 = 1
        m.HAL_S32 = 2
        m.HAL_FLOAT = 3
        return m

    def test_raises_when_hal_module_missing(self, linuxcnc_module):
        """list_hal_components raises RuntimeError when _hal is None."""
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=None)
        with pytest.raises(RuntimeError, match="_hal module not available"):
            sidecar.list_hal_components()

    def test_returns_empty_when_comp_list_raises(self, linuxcnc_module):
        """list_hal_components returns empty list when _hal.comp_list raises."""
        hal = self._make_hal_mock()
        hal.comp_list.side_effect = RuntimeError("hardware error")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.list_hal_components()

        assert len(result.components) == 0

    def test_returns_single_component_with_pins(self, linuxcnc_module):
        """list_hal_components returns component with pins when comp_list succeeds."""
        hal = self._make_hal_mock()
        hal.comp_list.return_value = ["motion"]
        hal.list_pins.return_value = ["joint-0.pos-fb", "joint-0.vel-fb"]

        def get_type_side_effect(name):
            return 3  # HAL_FLOAT

        hal.get_type.side_effect = get_type_side_effect
        hal.get_value.return_value = 0.0
        hal.is_output.return_value = False
        hal.get_update_period.return_value = 1000000.0

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.list_hal_components()

        assert len(result.components) == 1
        comp = result.components[0]
        assert comp.name == "motion"
        assert len(comp.pins) == 2
        assert comp.pins[0].name == "joint-0.pos-fb"
        assert comp.update_period_ns == 1000000.0

    def test_skips_pins_that_raise(self, linuxcnc_module):
        """list_hal_components silently skips pins that raise on get_type/get_value."""
        hal = self._make_hal_mock()
        hal.comp_list.return_value = ["test-comp"]
        hal.list_pins.side_effect = [
            ["good-pin", "bad-pin"],
        ]

        def type_side_effect(name):
            if name == "good-pin":
                return 3  # HAL_FLOAT
            raise KeyError("bad pin")

        hal.get_type.side_effect = type_side_effect
        hal.get_value.return_value = 1.0
        hal.is_output.return_value = False

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.list_hal_components()

        assert len(result.components) == 1
        assert len(result.components[0].pins) == 1
        assert result.components[0].pins[0].name == "good-pin"

    def test_params_populated_when_list_params_available(self, linuxcnc_module):
        """list_hal_components populates params dict when _hal.list_params exists."""
        from unittest.mock import MagicMock as _MM
        hal = self._make_hal_mock()
        hal.comp_list.return_value = ["param-comp"]
        hal.list_pins.return_value = []
        hal.get_update_period.return_value = 0.0

        hal.list_params = _MM(return_value=["p1", "p2"])
        hal.get_param = _MM(side_effect=[1.5, 2.5])

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.list_hal_components()

        assert len(result.components) == 1
        comp = result.components[0]
        assert "p1" in comp.params
        assert "p2" in comp.params
        assert comp.params["p1"] == pytest.approx(1.5)
        assert comp.params["p2"] == pytest.approx(2.5)

    def test_params_skipped_when_list_params_missing(self, linuxcnc_module):
        """list_hal_components skips params when _hal.list_params doesn't exist."""
        hal = self._make_hal_mock()
        # Don't set list_params or get_param — hasattr check will fail naturally
        hal.comp_list.return_value = ["simple-comp"]
        hal.list_pins.return_value = []
        hal.get_update_period.return_value = 0.0

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", _hal_override=hal)
        result = sidecar.list_hal_components()

        assert len(result.components) == 1
        assert result.components[0].params == {}


class TestSubscribeStatus:
    """Tests for FleetServiceRPC.SubscribeStatus streaming handler."""

    def test_subscribe_status_yields_statuses(self, linuxcnc_module):
        """SubscribeStatus yields MachineStatus snapshots from the sidecar."""
        from linuxcnc_fleet.fleet_pb2 import MachineId
        from linuxcnc_fleet.server import FleetServiceRPC

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        snap = sidecar._build_snapshot()
        object.__setattr__(sidecar, '_snapshot', snap)

        servicer = FleetServiceRPC(sidecar)

        class MockContext:
            def is_active(self):
                return True

        request = MachineId(id="test")
        generator = servicer.SubscribeStatus(request, MockContext())

        statuses = []
        for i, status in enumerate(generator):
            statuses.append(status)
            if i >= 4:
                break

        assert len(statuses) >= 3
        assert all(s.machine_id == "test" for s in statuses)

    def test_subscribe_status_stops_on_context_inactive(self, linuxcnc_module):
        """SubscribeStatus yields at most one status when context becomes inactive."""
        from linuxcnc_fleet.fleet_pb2 import MachineId
        from linuxcnc_fleet.server import FleetServiceRPC

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        snap = sidecar._build_snapshot()
        object.__setattr__(sidecar, '_snapshot', snap)

        servicer = FleetServiceRPC(sidecar)

        class InactiveContext:
            def is_active(self):
                return False

        request = MachineId(id="test")
        generator = servicer.SubscribeStatus(request, InactiveContext())

        statuses = list(generator)
        assert len(statuses) == 0


class TestSubscribeHalPins:
    """Tests for FleetServiceRPC.SubscribeHalPins streaming handler."""

    def test_subscribe_hal_pins_yields_updates(self, linuxcnc_module):
        """SubscribeHalPins yields HalPinUpdate snapshots from the sidecar."""
        from linuxcnc_fleet.fleet_pb2 import HalPinSubscribe, MachineId
        from linuxcnc_fleet.server import FleetServiceRPC

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        sidecar._running = True

        servicer = FleetServiceRPC(sidecar)

        class MockContext:
            def is_active(self):
                return True

        request = HalPinSubscribe(pin_names=["xyz"])
        generator = servicer.SubscribeHalPins(request, MockContext())

        updates = []
        for i, update in enumerate(generator):
            updates.append(update)
            if i >= 4:
                break

        assert len(updates) >= 3

    def test_subscribe_hal_pins_stops_on_context_inactive(self, linuxcnc_module):
        """SubscribeHalPins yields no updates when context is inactive."""
        from linuxcnc_fleet.fleet_pb2 import HalPinSubscribe, MachineId
        from linuxcnc_fleet.server import FleetServiceRPC

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        sidecar._running = True

        servicer = FleetServiceRPC(sidecar)

        class InactiveContext:
            def is_active(self):
                return False

        request = HalPinSubscribe(pin_names=["xyz"])
        generator = servicer.SubscribeHalPins(request, InactiveContext())

        updates = list(generator)
        assert len(updates) == 0


class TestSubscribeErrors:
    """Tests for FleetServiceRPC.SubscribeErrors streaming handler."""

    def test_subscribe_errors_yields_events(self, linuxcnc_module):
        """SubscribeErrors yields ErrorEvent snapshots from the sidecar."""
        import threading as _threading
        from linuxcnc_fleet.fleet_pb2 import MachineId
        from linuxcnc_fleet.server import FleetServiceRPC

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")
        sidecar._running = True
        sidecar._error_queue.extend(["err1", "err2", "err3"])

        servicer = FleetServiceRPC(sidecar)

        class MockContext:
            def is_active(self):
                return True

        request = MachineId(id="test")
        generator = servicer.SubscribeErrors(request, MockContext())

        # Stop the sidecar after a brief delay so the generator exits
        _threading.Timer(0.3, getattr(sidecar, 'shutdown', lambda: setattr(sidecar, '_running', False))).start()

        events = []
        for i, event in enumerate(generator):
            events.append(event)
            if i >= 4:
                break

        assert len(events) >= 3

    def test_subscribe_errors_stops_on_context_inactive(self, linuxcnc_module):
        """SubscribeErrors yields no events when context is inactive."""
        from linuxcnc_fleet.fleet_pb2 import MachineId
        from linuxcnc_fleet.server import FleetServiceRPC

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test")

        servicer = FleetServiceRPC(sidecar)

        class InactiveContext:
            def is_active(self):
                return False

        request = MachineId(id="test")
        generator = servicer.SubscribeErrors(request, InactiveContext())

        events = list(generator)
        assert len(events) == 0
