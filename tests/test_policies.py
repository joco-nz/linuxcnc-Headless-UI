"""Tests for RBAC policy engine with attribute-based scoping."""

import pytest

from gateway.policies import (
    Permission,
    PolicyEngine,
    PolicyResult,
    Role,
    create_test_policy_engine,
)


class TestRoleEnum:
    """Tests for the Role enum."""

    def test_all_roles_present(self):
        roles = [r.value for r in Role]
        assert "viewer" in roles
        assert "operator" in roles
        assert "programmer" in roles
        assert "maintainer" in roles
        assert "admin" in roles

    def test_role_count(self):
        assert len(Role) == 5


class TestPermissionEnum:
    """Tests for the Permission enum."""

    def test_all_permissions_present(self):
        perms = [p.value for p in Permission]
        assert "read_status" in perms
        assert "control_start" in perms
        assert "control_stop" in perms
        assert "write_hal_pin" in perms
        assert "load_program" in perms


class TestPolicyResult:
    """Tests for the PolicyResult frozen dataclass."""

    def test_allowed_result(self):
        result = PolicyResult(allowed=True)
        assert result.allowed is True
        assert result.reason == ""

    def test_denied_result_with_reason(self):
        result = PolicyResult(allowed=False, reason="No permission")
        assert result.allowed is False
        assert result.reason == "No permission"


class TestPolicyEngine:
    """Tests for the PolicyEngine RBAC policy engine."""

    def test_create_test_policy_engine(self):
        engine = create_test_policy_engine()
        assert isinstance(engine, PolicyEngine)

    # --- has_permission ---

    def test_viewer_has_read_status(self):
        engine = PolicyEngine()
        result = engine.has_permission("viewer", Permission.READ_STATUS)
        assert result.allowed is True

    def test_operator_has_read_status(self):
        engine = PolicyEngine()
        result = engine.has_permission("operator", Permission.READ_STATUS)
        assert result.allowed is True

    def test_admin_has_read_status(self):
        engine = PolicyEngine()
        result = engine.has_permission("admin", Permission.READ_STATUS)
        assert result.allowed is True

    def test_viewer_lacks_control_start(self):
        engine = PolicyEngine()
        result = engine.has_permission("viewer", Permission.CONTROL_START)
        assert result.allowed is False

    def test_operator_has_control_start(self):
        editor = PolicyEngine()
        result = editor.has_permission("operator", Permission.CONTROL_START)
        assert result.allowed is True

    def test_programmer_has_load_program(self):
        engine = PolicyEngine()
        result = engine.has_permission("programmer", Permission.LOAD_PROGRAM)
        assert result.allowed is True

    def test_viewer_lacks_load_program(self):
        engine = PolicyEngine()
        result = engine.has_permission("viewer", Permission.LOAD_PROGRAM)
        assert result.allowed is False

    def test_maintainer_has_control_execution(self):
        engine = PolicyEngine()
        result = engine.has_permission("maintainer", Permission.CONTROL_EXECUTION)
        assert result.allowed is True

    def test_operator_lacks_control_execution(self):
        engine = PolicyEngine()
        result = engine.has_permission("operator", Permission.CONTROL_EXECUTION)
        assert result.allowed is False

    def test_programmer_has_control_step(self):
        engine = PolicyEngine()
        result = engine.has_permission("programmer", Permission.CONTROL_STEP)
        assert result.allowed is True

    def test_operator_lacks_control_step(self):
        engine = PolicyEngine()
        result = engine.has_permission("operator", Permission.CONTROL_STEP)
        assert result.allowed is False

    def test_viewer_has_read_hal_pin(self):
        engine = PolicyEngine()
        result = engine.has_permission("viewer", Permission.READ_HAL_PIN)
        assert result.allowed is True

    def test_viewer_lacks_write_hal_pin(self):
        engine = PolicyEngine()
        result = engine.has_permission("viewer", Permission.WRITE_HAL_PIN)
        assert result.allowed is False

    def test_operator_has_write_hal_pin(self):
        engine = PolicyEngine()
        result = engine.has_permission("operator", Permission.WRITE_HAL_PIN)
        assert result.allowed is True

    def test_admin_has_all_permissions(self):
        engine = PolicyEngine()
        for perm in Permission:
            result = engine.has_permission("admin", perm)
            assert result.allowed is True, f"Admin should have {perm.value}"

    def test_unknown_role(self):
        engine = PolicyEngine()
        result = engine.has_permission("unknown-role", Permission.READ_STATUS)
        assert result.allowed is False
        assert "Unknown role" in result.reason

    def test_denied_result_has_reason(self):
        engine = PolicyEngine()
        result = engine.has_permission("viewer", Permission.CONTROL_START)
        assert result.allowed is False
        assert "viewer" in result.reason
        assert "control_start" in result.reason

    # --- can_access_machine ---

    def test_admin_can_access_all_machines(self):
        engine = PolicyEngine()
        result = engine.can_access_machine("admin", ["mill", "shop-1"])
        assert result.allowed is True

    def test_viewer_can_access_tagged_machines(self):
        engine = PolicyEngine()
        result = engine.can_access_machine("viewer", ["mill"])
        assert result.allowed is True

    def test_unknown_role_cannot_access(self):
        engine = PolicyEngine()
        result = engine.can_access_machine("unknown-role", [])
        assert result.allowed is False
        assert "Unknown role" in result.reason

    # --- can_control_machine ---

    def test_operator_can_control(self):
        engine = PolicyEngine()
        result = engine.can_control_machine("operator")
        assert result.allowed is True
        assert "Control permissions granted" in result.reason

    def test_viewer_cannot_control(self):
        engine = PolicyEngine()
        result = engine.can_control_machine("viewer")
        assert result.allowed is False

    def test_admin_can_control(self):
        engine = PolicyEngine()
        result = engine.can_control_machine("admin")
        assert result.allowed is True

    # --- can_write_hal_pin ---

    def test_operator_can_write_hal_pin(self):
        engine = PolicyEngine()
        result = engine.can_write_hal_pin("operator")
        assert result.allowed is True
        assert "HAL write permissions granted" in result.reason

    def test_viewer_cannot_write_hal_pin(self):
        engine = PolicyEngine()
        result = engine.can_write_hal_pin("viewer")
        assert result.allowed is False

    # --- can_load_program ---

    def test_programmer_can_load_program(self):
        engine = PolicyEngine()
        result = engine.can_load_program("programmer")
        assert result.allowed is True
        assert "Program loading permissions granted" in result.reason

    def test_viewer_cannot_load_program(self):
        engine = PolicyEngine()
        result = engine.can_load_program("viewer")
        assert result.allowed is False

    def test_operator_cannot_load_program(self):
        engine = PolicyEngine()
        result = engine.can_load_program("operator")
        assert result.allowed is False

    def test_maintainer_can_load_program(self):
        engine = PolicyEngine()
        result = engine.can_load_program("maintainer")
        assert result.allowed is True

    # --- can_read_status ---

    def test_viewer_can_read_status(self):
        engine = PolicyEngine()
        result = engine.can_read_status("viewer")
        assert result.allowed is True
        assert "Status read permissions granted" in result.reason

    def test_admin_can_read_status(self):
        engine = PolicyEngine()
        result = engine.can_read_status("admin")
        assert result.allowed is True

    # --- can_subscribe ---

    def test_viewer_can_subscribe(self):
        engine = PolicyEngine()
        result = engine.can_subscribe("viewer")
        assert result.allowed is True
        assert "Subscription permissions granted" in result.reason

    def test_admin_can_subscribe(self):
        engine = PolicyEngine()
        result = engine.can_subscribe("admin")
        assert result.allowed is True

    # --- check_broadcast_authorization ---

    def test_admin_can_broadcast_mdi(self):
        engine = PolicyEngine()
        result = engine.check_broadcast_authorization("admin", "mdi")
        assert result.allowed is True
        assert "Admin can broadcast" in result.reason

    def test_admin_can_broadcast_execution(self):
        engine = PolicyEngine()
        result = engine.check_broadcast_authorization("admin", "execution")
        assert result.allowed is True

    def test_admin_can_broadcast_mode(self):
        engine = PolicyEngine()
        result = engine.check_broadcast_authorization("admin", "mode")
        assert result.allowed is True

    def test_operator_can_broadcast_execution(self):
        engine = PolicyEngine()
        result = engine.check_broadcast_authorization("operator", "execution")
        assert result.allowed is True

    def test_viewer_cannot_broadcast_mdi(self):
        engine = PolicyEngine()
        result = engine.check_broadcast_authorization("viewer", "mdi")
        assert result.allowed is False

    def test_viewer_cannot_broadcast_execution(self):
        engine = PolicyEngine()
        result = engine.check_broadcast_authorization("viewer", "execution")
        assert result.allowed is False

    def test_viewer_cannot_broadcast_mode(self):
        engine = PolicyEngine()
        result = engine.check_broadcast_authorization("viewer", "mode")
        assert result.allowed is False

    def test_programmer_can_broadcast_mode(self):
        engine = PolicyEngine()
        result = engine.check_broadcast_authorization("programmer", "mode")
        assert result.allowed is True

    def test_unknown_command_type_admin(self):
        """Admin should bypass all broadcast authorization checks including unknown types."""
        engine = PolicyEngine()
        result = engine.check_broadcast_authorization("admin", "unknown-cmd")
        assert result.allowed is True

    def test_unknown_command_type_non_admin(self):
        """Non-admin with unknown command type should be denied."""
        engine = PolicyEngine()
        result = engine.check_broadcast_authorization("operator", "unknown-cmd")
        assert result.allowed is False
        assert "Unknown command type" in result.reason

    def test_unknown_role_broadcast(self):
        engine = PolicyEngine()
        result = engine.check_broadcast_authorization("unknown-role", "mdi")
        assert result.allowed is False
        assert "Unknown role" in result.reason

    # --- filter_machines_by_scope ---

    def test_admin_sees_all_machines(self):
        engine = PolicyEngine()
        machines = [
            {"id": "m1", "facility": "shop-1", "tags": []},
            {"id": "m2", "facility": "shop-2", "tags": []},
        ]
        result = engine.filter_machines_by_scope("admin", None, machines)
        assert len(result) == 2

    def test_facility_scoped_viewer_sees_only_own_facility(self):
        engine = PolicyEngine()
        machines = [
            {"id": "m1", "facility": "shop-1", "tags": []},
            {"id": "m2", "facility": "shop-2", "tags": []},
        ]
        result = engine.filter_machines_by_scope("viewer", "shop-1", machines)
        assert len(result) == 1
        assert result[0]["id"] == "m1"

    def test_no_facility_viewer_sees_nothing(self):
        engine = PolicyEngine()
        machines = [
            {"id": "m1", "facility": "shop-1", "tags": []},
        ]
        result = engine.filter_machines_by_scope("viewer", None, machines)
        assert len(result) == 0

    def test_no_facility_admin_sees_all(self):
        engine = PolicyEngine()
        machines = [
            {"id": "m1", "facility": "shop-1", "tags": []},
            {"id": "m2", "facility": "shop-2", "tags": []},
        ]
        result = engine.filter_machines_by_scope("admin", None, machines)
        assert len(result) == 2

    def test_unknown_role_defaults_to_viewer(self):
        engine = PolicyEngine()
        machines = [
            {"id": "m1", "facility": "shop-1", "tags": []},
        ]
        result = engine.filter_machines_by_scope("unknown-role", None, machines)
        assert len(result) == 0

    def test_facility_scoped_operator(self):
        engine = PolicyEngine()
        machines = [
            {"id": "m1", "facility": "shop-1", "tags": ["mill"]},
            {"id": "m2", "facility": "shop-1", "tags": ["lathe"]},
            {"id": "m3", "facility": "shop-2", "tags": []},
        ]
        result = engine.filter_machines_by_scope("operator", "shop-1", machines)
        assert len(result) == 2
        ids = {m["id"] for m in result}
        assert ids == {"m1", "m2"}

    def test_empty_machines_list(self):
        engine = PolicyEngine()
        result = engine.filter_machines_by_scope("admin", None, [])
        assert result == []

    def test_machine_without_facility_field(self):
        engine = PolicyEngine()
        machines = [
            {"id": "m1", "tags": ["mill"]},  # No 'facility' key
        ]
        result = engine.filter_machines_by_scope("admin", None, machines)
        assert len(result) == 1

    def test_machine_without_tags_field(self):
        engine = PolicyEngine()
        machines = [
            {"id": "m1", "facility": "shop-1"},  # No 'tags' key
        ]
        result = engine.filter_machines_by_scope("admin", None, machines)
        assert len(result) == 1

    # --- Role hierarchy tests ---

    def test_admin_has_all_operator_permissions(self):
        """Admin should have every permission that operator has."""
        engine = PolicyEngine()
        for perm in Permission:
            op_result = engine.has_permission("operator", perm)
            admin_result = engine.has_permission("admin", perm)
            if op_result.allowed:
                assert admin_result.allowed, f"Admin should have {perm.value} if operator has it"

    def test_maintainer_has_all_programmer_permissions(self):
        """Maintainer should have every permission that programmer has."""
        engine = PolicyEngine()
        for perm in Permission:
            prog_result = engine.has_permission("programmer", perm)
            maint_result = engine.has_permission("maintainer", perm)
            if prog_result.allowed:
                assert maint_result.allowed, f"Maintainer should have {perm.value} if programmer has it"

    def test_programmer_has_all_viewer_permissions(self):
        """Programmer should have every permission that viewer has."""
        engine = PolicyEngine()
        for perm in Permission:
            view_result = engine.has_permission("viewer", perm)
            prog_result = engine.has_permission("programmer", perm)
            if view_result.allowed:
                assert prog_result.allowed, f"Programmer should have {perm.value} if viewer has it"
