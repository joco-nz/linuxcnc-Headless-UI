"""Tests for FleetServiceRPC — auth checks, error paths, and handler delegation."""

import grpc
import pytest

from linuxcnc_fleet.fleet_pb2 import (
    Empty,
    ErrorEvent,
    HalComponentList,
    HalPinRead,
    HalPinSubscribe,
    HalPinUpdate,
    HalPinValue,
    IniParamValue,
    MachineId,
    MachineInfo,
    MachineStatus,
    MdiCommand,
    ProgramEntry,
    ProgramList,
    ProgramPath,
    Result,
    SetModeRequest,
)
from linuxcnc_fleet.server import FleetServiceRPC


class _MockAuthContext:
    """Minimal auth context with a role attribute."""

    def __init__(self, role: str):
        self.role = role


class MockSidecar:
    """Minimal mock LinuxCncSidecar for testing RPC handlers."""

    def __init__(self):
        self._running = True
        self._calls = {}

    def _track(self, method_name, *args, **kwargs):
        key = (method_name, args)
        self._calls[key] = True

    # Read-only handlers

    def get_status(self):
        self._track("get_status")
        return MachineStatus()

    def list_hal_components(self):
        self._track("list_hal_components")
        return HalComponentList(components=[])

    def read_hal_pin(self, pin_name: str):
        raise ValueError(f"Pin not found: {pin_name}")

    def get_errors(self, limit=100):
        self._track("get_errors", limit)
        return []

    def get_machine_info(self):
        self._track("get_machine_info")
        return MachineInfo()

    def get_ini_param(self, section, option):
        self._track("get_ini_param", section, option)
        return ""

    def list_programs(self, directory="", max_depth=0):
        self._track("list_programs", directory, max_depth)
        return []

    # Control handlers

    def set_mode(self, mode):
        self._track("set_mode", mode)
        return Result(success=True, message="ok")

    def set_execution(self, state):
        self._track("set_execution", state)
        return Result(success=True, message="ok")

    def start(self):
        self._track("start")
        return Result(success=True, message="ok")

    def stop(self):
        self._track("stop")
        return Result(success=True, message="ok")

    def feed_hold(self):
        self._track("feed_hold")
        return Result(success=True, message="ok")

    def continue_exec(self):
        self._track("continue_exec")
        return Result(success=True, message="ok")

    def home_all(self):
        self._track("home_all")
        return Result(success=True, message="ok")

    def home_axis(self, axis):
        self._track("home_axis", axis)
        return Result(success=True, message="ok")

    # G-code handlers

    def send_mdi(self, command: str):
        self._track("send_mdi", command)
        return Result(success=True, message="ok")

    def load_program(self, path: str):
        self._track("load_program", path)
        return Result(success=True, message="ok")

    def step_forward(self):
        self._track("step_forward")
        return Result(success=True, message="ok")

    # HAL write handlers

    def write_hal_pin(self, pin_name, value_f=0.0, value_u32=0, value_s32=0, value_bit=False):
        self._track("write_hal_pin", pin_name, value_f, value_u32, value_s32, value_bit)
        return Result(success=True, message="ok")

    # Streaming handlers

    def subscribe_status(self):
        for i in range(5):
            yield MachineStatus(machine_id="test")

    def subscribe_hal_pins(self, pin_names, poll_interval=0.1):
        for i in range(5):
            yield HalPinUpdate(pin_name=pin_names[0] if pin_names else "unknown", value_f=float(i))

    def subscribe_errors(self):
        for i in range(3):
            yield ErrorEvent(message=f"error_{i}", timestamp=float(i))


class MockServicerContext:
    """Mock gRPC ServicerContext for testing."""

    def __init__(self, metadata=None):
        self._metadata = metadata or {}
        self._aborted = False
        self._abort_code = None
        self._abort_detail = None

    def invocation_metadata(self):
        return self._metadata

    def abort(self, code, detail):
        self._aborted = True
        self._abort_code = code
        self._abort_detail = str(detail)

    def is_active(self):
        return not self._aborted


class _AuthRequest:
    """Wrapper that adds auth_context to a request."""

    def __init__(self, proto_request, auth_ctx=None):
        self._proto = proto_request
        self.auth_context = auth_ctx

    def __getattr__(self, name):
        if name == "_proto":
            return object.__getattribute__(self, "_proto")
        return getattr(self._proto, name)


def _make_rpc():
    """Create a FleetServiceRPC with mock sidecar."""
    sidecar = MockSidecar()
    return FleetServiceRPC(sidecar)


# ── No-auth (allow all) ───────────────────────────────────────────────

class TestNoAuthAllowAll:

    def test_read_access_no_auth(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc._check_read_access(None, ctx, "test")
        assert result is True
        assert ctx._aborted is False

    def test_control_access_no_auth(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc._check_control_access(None, ctx, "test")
        assert result is True
        assert ctx._aborted is False

    def test_write_access_no_auth(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc._check_write_access(None, ctx, "test")
        assert result is True
        assert ctx._aborted is False

    def test_admin_access_no_auth(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc._check_admin_access(None, ctx, "test")
        assert result is True
        assert ctx._aborted is False


# ── Read access (viewer minimum) ──────────────────────────────────────

class TestReadAccess:

    def test_viewer_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("viewer")
        result = rpc._check_read_access(_AuthRequest(None, auth_ctx), ctx, "GetStatus")
        assert result is True
        assert ctx._aborted is False

    def test_operator_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("operator")
        result = rpc._check_read_access(_AuthRequest(None, auth_ctx), ctx, "GetStatus")
        assert result is True
        assert ctx._aborted is False

    def test_programmer_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("programmer")
        result = rpc._check_read_access(_AuthRequest(None, auth_ctx), ctx, "GetStatus")
        assert result is True
        assert ctx._aborted is False

    def test_maintainer_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("maintainer")
        result = rpc._check_read_access(_AuthRequest(None, auth_ctx), ctx, "GetStatus")
        assert result is True
        assert ctx._aborted is False

    def test_admin_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("admin")
        result = rpc._check_read_access(_AuthRequest(None, auth_ctx), ctx, "GetStatus")
        assert result is True
        assert ctx._aborted is False

    def test_unknown_role_denied(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("superadmin")
        result = rpc._check_read_access(_AuthRequest(None, auth_ctx), ctx, "GetStatus")
        assert result is False
        assert ctx._aborted is True
        assert ctx._abort_code == grpc.StatusCode.PERMISSION_DENIED
        assert "Unknown role 'superadmin'" in ctx._abort_detail

    def test_negative_level_denied(self):
        """A role with level < 0 should be denied."""
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("unknown_role_xyz")
        result = rpc._check_read_access(_AuthRequest(None, auth_ctx), ctx, "GetStatus")
        assert result is False
        assert ctx._aborted is True
        assert ctx._abort_code == grpc.StatusCode.PERMISSION_DENIED


# ── Control access (operator minimum) ─────────────────────────────────

class TestControlAccess:

    def test_viewer_denied(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("viewer")
        result = rpc._check_control_access(_AuthRequest(None, auth_ctx), ctx, "SetMode")
        assert result is False
        assert ctx._aborted is True
        assert ctx._abort_code == grpc.StatusCode.PERMISSION_DENIED
        assert "insufficient for control operations" in ctx._abort_detail

    def test_operator_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("operator")
        result = rpc._check_control_access(_AuthRequest(None, auth_ctx), ctx, "SetMode")
        assert result is True
        assert ctx._aborted is False

    def test_programmer_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("programmer")
        result = rpc._check_control_access(_AuthRequest(None, auth_ctx), ctx, "SetMode")
        assert result is True
        assert ctx._aborted is False

    def test_maintainer_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("maintainer")
        result = rpc._check_control_access(_AuthRequest(None, auth_ctx), ctx, "SetMode")
        assert result is True
        assert ctx._aborted is False

    def test_admin_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("admin")
        result = rpc._check_control_access(_AuthRequest(None, auth_ctx), ctx, "SetMode")
        assert result is True
        assert ctx._aborted is False

    def test_unknown_role_denied(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("superadmin")
        result = rpc._check_control_access(_AuthRequest(None, auth_ctx), ctx, "SetMode")
        assert result is False
        assert ctx._aborted is True
        assert ctx._abort_code == grpc.StatusCode.PERMISSION_DENIED


# ── Write access (programmer minimum) ─────────────────────────────────

class TestWriteAccess:

    def test_viewer_denied(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("viewer")
        result = rpc._check_write_access(_AuthRequest(None, auth_ctx), ctx, "SendMdiCommand")
        assert result is False
        assert ctx._aborted is True

    def test_operator_denied(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("operator")
        result = rpc._check_write_access(_AuthRequest(None, auth_ctx), ctx, "SendMdiCommand")
        assert result is False
        assert ctx._aborted is True

    def test_programmer_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("programmer")
        result = rpc._check_write_access(_AuthRequest(None, auth_ctx), ctx, "SendMdiCommand")
        assert result is True
        assert ctx._aborted is False

    def test_maintainer_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("maintainer")
        result = rpc._check_write_access(_AuthRequest(None, auth_ctx), ctx, "SendMdiCommand")
        assert result is True
        assert ctx._aborted is False

    def test_admin_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("admin")
        result = rpc._check_write_access(_AuthRequest(None, auth_ctx), ctx, "SendMdiCommand")
        assert result is True
        assert ctx._aborted is False

    def test_unknown_role_denied(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("superadmin")
        result = rpc._check_write_access(_AuthRequest(None, auth_ctx), ctx, "SendMdiCommand")
        assert result is False
        assert ctx._aborted is True


# ── Admin access (admin only) ─────────────────────────────────────────

class TestAdminAccess:

    def test_viewer_denied(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("viewer")
        result = rpc._check_admin_access(_AuthRequest(None, auth_ctx), ctx, "TestAdmin")
        assert result is False
        assert ctx._aborted is True

    def test_operator_denied(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("operator")
        result = rpc._check_admin_access(_AuthRequest(None, auth_ctx), ctx, "TestAdmin")
        assert result is False
        assert ctx._aborted is True

    def test_programmer_denied(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("programmer")
        result = rpc._check_admin_access(_AuthRequest(None, auth_ctx), ctx, "TestAdmin")
        assert result is False
        assert ctx._aborted is True

    def test_maintainer_denied(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("maintainer")
        result = rpc._check_admin_access(_AuthRequest(None, auth_ctx), ctx, "TestAdmin")
        assert result is False
        assert ctx._aborted is True
        assert "insufficient for admin operations" in ctx._abort_detail

    def test_admin_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("admin")
        result = rpc._check_admin_access(_AuthRequest(None, auth_ctx), ctx, "TestAdmin")
        assert result is True
        assert ctx._aborted is False

    def test_unknown_role_denied(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("superadmin")
        result = rpc._check_admin_access(_AuthRequest(None, auth_ctx), ctx, "TestAdmin")
        assert result is False
        assert ctx._aborted is True


# ── RPC handler integration with auth ─────────────────────────────────

class TestRpcHandlersWithAuth:

    def test_set_mode_denied_viewer(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("viewer")
        request = _AuthRequest(SetModeRequest(mode=1), auth_ctx)
        result = rpc.SetMode(request, ctx)
        assert result.success is False
        assert result.message == "Access denied"

    def test_set_mode_allowed_operator(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("operator")
        request = _AuthRequest(SetModeRequest(mode=1), auth_ctx)
        result = rpc.SetMode(request, ctx)
        assert result.success is True

    def test_send_mdi_denied_operator(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("operator")
        request = _AuthRequest(MdiCommand(command="G0 X1"), auth_ctx)
        result = rpc.SendMdiCommand(request, ctx)
        assert result.success is False
        assert result.message == "Access denied"

    def test_send_mdi_allowed_programmer(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("programmer")
        request = _AuthRequest(MdiCommand(command="G0 X1"), auth_ctx)
        result = rpc.SendMdiCommand(request, ctx)
        assert result.success is True

    def test_load_program_denied_viewer(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("viewer")
        request = _AuthRequest(ProgramPath(path="/path/to/file.gcode"), auth_ctx)
        result = rpc.LoadProgram(request, ctx)
        assert result.success is False

    def test_list_programs_viewer_allowed(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("viewer")
        from linuxcnc_fleet.fleet_pb2 import ListProgramsRequest
        request = _AuthRequest(ListProgramsRequest(directory="/", max_depth=1), auth_ctx)
        result = rpc.ListPrograms(request, ctx)
        assert isinstance(result, ProgramList)

    def test_list_programs_no_auth_ctx_allowed(self):
        """Without auth context, read access is allowed (no auth mode)."""
        rpc = _make_rpc()
        ctx = MockServicerContext()
        from linuxcnc_fleet.fleet_pb2 import ListProgramsRequest
        request = ListProgramsRequest(directory="/", max_depth=1)
        result = rpc.ListPrograms(request, ctx)
        assert isinstance(result, ProgramList)


# ── ReadHalPin NOT_FOUND abort path ───────────────────────────────────

class TestReadHalPinError:

    def test_pin_not_found_aborts(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        request = HalPinRead(pin_name="nonexistent_pin")
        rpc.ReadHalPin(request, ctx)
        assert ctx._aborted is True
        assert ctx._abort_code == grpc.StatusCode.NOT_FOUND
        assert "not found" in ctx._abort_detail.lower()

    def test_read_hal_pin_success(self):
        """When the sidecar returns a valid HalPinValue, no abort."""
        rpc = _make_rpc()
        rpc.sidecar.read_hal_pin = lambda name: HalPinValue(
            pin_name=name, type=0, value_f=1.0, value_u32=0, value_s32=0, value_bit=False
        )
        ctx = MockServicerContext()
        request = HalPinRead(pin_name="good_pin")
        result = rpc.ReadHalPin(request, ctx)
        assert isinstance(result, HalPinValue)
        assert result.pin_name == "good_pin"
        assert ctx._aborted is False


# ── Auth context extraction ───────────────────────────────────────────

class TestAuthContextExtraction:

    def test_extract_from_request_attribute(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("admin")
        request = _AuthRequest(None, auth_ctx)
        result = rpc._get_auth_context(request, ctx)
        assert result is auth_ctx

    def test_extract_returns_none_without_attribute(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        request = SetModeRequest(mode=1)
        result = rpc._get_auth_context(request, ctx)
        assert result is None

    def test_callable_auth_context_ignored(self):
        """If auth_context is callable, treat as no auth."""
        rpc = _make_rpc()
        ctx = MockServicerContext()
        request = _AuthRequest(SetModeRequest(mode=1), lambda: "not-a-context")
        result = rpc._get_auth_context(request, ctx)
        assert result is None


# ── Role hierarchy integrity ──────────────────────────────────────────

class TestRoleHierarchy:

    def test_all_expected_roles_present(self):
        rpc = _make_rpc()
        for role in ("viewer", "operator", "programmer", "maintainer", "admin"):
            assert role in rpc.role_hierarchy

    def test_hierarchy_increasing(self):
        rpc = _make_rpc()
        levels = [rpc.role_hierarchy[r] for r in ("viewer", "operator", "programmer", "maintainer", "admin")]
        assert levels == sorted(levels)
        assert levels[0] < levels[1] < levels[2] < levels[3] < levels[4]

    def test_viewer_is_zero(self):
        rpc = _make_rpc()
        assert rpc.role_hierarchy["viewer"] == 0

    def test_admin_is_highest(self):
        rpc = _make_rpc()
        assert rpc.role_hierarchy["admin"] == max(rpc.role_hierarchy.values())


# ── RPC Handler Delegation Tests ────────────────────────────────────────

class TestRpcHandlerDelegation:
    """Verify each RPC handler delegates to the correct sidecar method."""

    def test_get_status_delegates(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc.GetStatus(MachineId(id="test"), ctx)
        assert isinstance(result, MachineStatus)
        assert ("get_status", ()) in rpc.sidecar._calls

    def test_set_execution_delegates(self):
        from linuxcnc_fleet.fleet_pb2 import ExecutionCommand
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc.SetExecution(ExecutionCommand(state=1), ctx)
        assert result.success is True
        assert ("set_execution", (1,)) in rpc.sidecar._calls

    def test_start_delegates(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc.Start(Empty(), ctx)
        assert result.success is True
        assert ("start", ()) in rpc.sidecar._calls

    def test_stop_delegates(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc.Stop(Empty(), ctx)
        assert result.success is True
        assert ("stop", ()) in rpc.sidecar._calls

    def test_feed_hold_delegates(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc.FeedHold(Empty(), ctx)
        assert result.success is True
        assert ("feed_hold", ()) in rpc.sidecar._calls

    def test_continue_exec_delegates(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc.Continue(Empty(), ctx)
        assert result.success is True
        assert ("continue_exec", ()) in rpc.sidecar._calls

    def test_home_all_delegates(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc.HomeAll(Empty(), ctx)
        assert result.success is True
        assert ("home_all", ()) in rpc.sidecar._calls

    def test_home_axis_delegates(self):
        from linuxcnc_fleet.fleet_pb2 import HomeAxisRequest, TrajAxis
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc.HomeAxis(HomeAxisRequest(axis=TrajAxis.X_AXIS), ctx)
        assert result.success is True
        assert ("home_axis", (TrajAxis.X_AXIS,)) in rpc.sidecar._calls

    def test_step_forward_delegates(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc.StepForward(Empty(), ctx)
        assert result.success is True
        assert ("step_forward", ()) in rpc.sidecar._calls

    def test_list_hal_components_delegates(self):
        from linuxcnc_fleet.fleet_pb2 import ListHalRequest
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc.ListHalComponents(ListHalRequest(), ctx)
        assert ("list_hal_components", ()) in rpc.sidecar._calls

    def test_get_errors_delegates(self):
        from linuxcnc_fleet.fleet_pb2 import GetErrorsRequest
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc.GetErrors(GetErrorsRequest(limit=50), ctx)
        assert ("get_errors", (50,)) in rpc.sidecar._calls

    def test_get_machine_info_delegates(self):
        rpc = _make_rpc()
        ctx = MockServicerContext()
        result = rpc.GetMachineInfo(MachineId(id="test"), ctx)
        assert isinstance(result, MachineInfo)
        assert ("get_machine_info", ()) in rpc.sidecar._calls

    def test_set_mode_delegates_correctly(self):
        from linuxcnc_fleet.fleet_pb2 import Mode
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("operator")
        request = _AuthRequest(SetModeRequest(mode=Mode.MODE_AUTO), auth_ctx)
        result = rpc.SetMode(request, ctx)
        assert result.success is True
        assert ("set_mode", (2,)) in rpc.sidecar._calls

    def test_send_mdi_delegates_correctly(self):
        from linuxcnc_fleet.fleet_pb2 import MdiCommand
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("programmer")
        request = _AuthRequest(MdiCommand(command="G0 X1.0"), auth_ctx)
        result = rpc.SendMdiCommand(request, ctx)
        assert result.success is True
        assert ("send_mdi", ("G0 X1.0",)) in rpc.sidecar._calls

    def test_load_program_delegates_correctly(self):
        from linuxcnc_fleet.fleet_pb2 import ProgramPath
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("programmer")
        request = _AuthRequest(ProgramPath(path="/path/file.ngc"), auth_ctx)
        result = rpc.LoadProgram(request, ctx)
        assert result.success is True
        assert ("load_program", ("/path/file.ngc",)) in rpc.sidecar._calls

    def test_list_programs_delegates_correctly(self):
        from linuxcnc_fleet.fleet_pb2 import ListProgramsRequest
        rpc = _make_rpc()
        ctx = MockServicerContext()
        auth_ctx = _MockAuthContext("viewer")
        request = _AuthRequest(ListProgramsRequest(directory="/", max_depth=1), auth_ctx)
        result = rpc.ListPrograms(request, ctx)
        assert isinstance(result, ProgramList)
        assert ("list_programs", ("/", 1)) in rpc.sidecar._calls

    def test_get_position_joint(self):
        from linuxcnc_fleet.fleet_pb2 import PositionRequest, PositionResponse
        rpc = _make_rpc()
        ctx = MockServicerContext()
        request = PositionRequest(id=MachineId(id="test"), type=PositionRequest.JOINT)
        result = rpc.GetPosition(request, ctx)
        assert isinstance(result, PositionResponse)
        assert ("get_status", ()) in rpc.sidecar._calls

    def test_get_position_device(self):
        from linuxcnc_fleet.fleet_pb2 import PositionRequest, PositionResponse
        rpc = _make_rpc()
        ctx = MockServicerContext()
        request = PositionRequest(id=MachineId(id="test"), type=PositionRequest.DEVICE)
        result = rpc.GetPosition(request, ctx)
        assert isinstance(result, PositionResponse)

    def test_get_position_world(self):
        from linuxcnc_fleet.fleet_pb2 import PositionRequest, PositionResponse
        rpc = _make_rpc()
        ctx = MockServicerContext()
        request = PositionRequest(id=MachineId(id="test"), type=PositionRequest.WORLD)
        result = rpc.GetPosition(request, ctx)
        assert isinstance(result, PositionResponse)
