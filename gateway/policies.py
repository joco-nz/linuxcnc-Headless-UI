"""RBAC policy engine with attribute-based scoping for fleet management."""

from __future__ import annotations

import dataclasses
import logging
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


class Role(str, Enum):
    viewer = "viewer"
    operator = "operator"
    programmer = "programmer"
    maintainer = "maintainer"
    admin = "admin"


class Permission(str, Enum):
    READ_STATUS = "read_status"
    CONTROL_START = "control_start"
    CONTROL_STOP = "control_stop"
    CONTROL_HOLD = "control_hold"
    CONTROL_CONTINUE = "control_continue"
    CONTROL_HOME = "control_home"
    CONTROL_MODE = "control_mode"
    CONTROL_EXECUTION = "control_execution"
    CONTROL_STEP = "control_step"
    WRITE_HAL_PIN = "write_hal_pin"
    READ_HAL_PIN = "read_hal_pin"
    LOAD_PROGRAM = "load_program"
    SUBSCRIBE_STATUS = "subscribe_status"


# Role hierarchy: each role inherits permissions from roles below it
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.viewer: {
        Permission.READ_STATUS,
        Permission.READ_HAL_PIN,
        Permission.SUBSCRIBE_STATUS,
    },
    Role.operator: {
        Permission.READ_STATUS,
        Permission.CONTROL_START,
        Permission.CONTROL_STOP,
        Permission.CONTROL_HOLD,
        Permission.CONTROL_CONTINUE,
        Permission.CONTROL_HOME,
        Permission.CONTROL_MODE,
        Permission.READ_HAL_PIN,
        Permission.WRITE_HAL_PIN,
        Permission.SUBSCRIBE_STATUS,
    },
    Role.programmer: {
        Permission.READ_STATUS,
        Permission.CONTROL_START,
        Permission.CONTROL_STOP,
        Permission.CONTROL_HOLD,
        Permission.CONTROL_CONTINUE,
        Permission.CONTROL_HOME,
        Permission.CONTROL_MODE,
        Permission.CONTROL_STEP,
        Permission.READ_HAL_PIN,
        Permission.WRITE_HAL_PIN,
        Permission.LOAD_PROGRAM,
        Permission.SUBSCRIBE_STATUS,
    },
    Role.maintainer: {
        Permission.READ_STATUS,
        Permission.CONTROL_START,
        Permission.CONTROL_STOP,
        Permission.CONTROL_HOLD,
        Permission.CONTROL_CONTINUE,
        Permission.CONTROL_HOME,
        Permission.CONTROL_MODE,
        Permission.CONTROL_EXECUTION,
        Permission.CONTROL_STEP,
        Permission.READ_HAL_PIN,
        Permission.WRITE_HAL_PIN,
        Permission.LOAD_PROGRAM,
        Permission.SUBSCRIBE_STATUS,
    },
    Role.admin: {
        Permission.READ_STATUS,
        Permission.CONTROL_START,
        Permission.CONTROL_STOP,
        Permission.CONTROL_HOLD,
        Permission.CONTROL_CONTINUE,
        Permission.CONTROL_HOME,
        Permission.CONTROL_MODE,
        Permission.CONTROL_EXECUTION,
        Permission.CONTROL_STEP,
        Permission.WRITE_HAL_PIN,
        Permission.READ_HAL_PIN,
        Permission.LOAD_PROGRAM,
        Permission.SUBSCRIBE_STATUS,
    },
}


@dataclasses.dataclass(frozen=True)
class PolicyResult:
    """Result of a policy evaluation."""

    allowed: bool
    reason: str = ""


class PolicyEngine:
    """Evaluates RBAC policies with attribute-based scoping.

    Permissions are checked against the user's role, then scoped by facility
    and machine tags. Admin users bypass all scoping restrictions.
    """

    def __init__(self) -> None:
        self._valid_roles = set(Role)

    def _get_effective_permissions(self, role: Role) -> set[Permission]:
        """Get the effective permission set for a given role."""
        return ROLE_PERMISSIONS.get(role, set())

    def has_permission(self, user_role: str, permission: Permission) -> PolicyResult:
        """Check if a role has a specific permission.

        Returns PolicyResult with allowed=True/False and reason.
        """
        try:
            role = Role(user_role)
        except ValueError:
            return PolicyResult(
                allowed=False,
                reason=f"Unknown role: {user_role}",
            )

        effective_perms = self._get_effective_permissions(role)
        if permission in effective_perms:
            return PolicyResult(allowed=True)

        return PolicyResult(
            allowed=False,
            reason=f"Role '{user_role}' does not have permission '{permission.value}'",
        )

    def can_access_machine(self, user_role: str, machine_tags: list[str]) -> PolicyResult:
        """Check if a user role can access machines with given tags.

        Admin can access all machines. Other roles are limited by their scope
        and require the target machine to have at least one matching tag.
        """
        try:
            role = Role(user_role)
        except ValueError:
            return PolicyResult(
                allowed=False,
                reason=f"Unknown role: {user_role}",
            )

        if role == Role.admin:
            return PolicyResult(allowed=True)

        if not machine_tags:
            return PolicyResult(
                allowed=False,
                reason=f"Role '{user_role}' cannot access untagged machines",
            )

        return PolicyResult(
            allowed=True,
            reason=f"Role '{user_role}' scope allows machine access",
        )

    def can_control_machine(self, user_role: str) -> PolicyResult:
        """Check if a role can control machines (start/stop/home/etc.)."""
        result = self.has_permission(user_role, Permission.CONTROL_START)
        if result.allowed:
            return PolicyResult(allowed=True, reason="Control permissions granted")
        return PolicyResult(
            allowed=False,
            reason=f"Role '{user_role}' cannot control machines",
        )

    def can_write_hal_pin(self, user_role: str) -> PolicyResult:
        """Check if a role can write to HAL pins."""
        result = self.has_permission(user_role, Permission.WRITE_HAL_PIN)
        if result.allowed:
            return PolicyResult(allowed=True, reason="HAL write permissions granted")
        return PolicyResult(
            allowed=False,
            reason=f"Role '{user_role}' cannot write HAL pins",
        )

    def can_load_program(self, user_role: str) -> PolicyResult:
        """Check if a role can load programs."""
        result = self.has_permission(user_role, Permission.LOAD_PROGRAM)
        if result.allowed:
            return PolicyResult(allowed=True, reason="Program loading permissions granted")
        return PolicyResult(
            allowed=False,
            reason=f"Role '{user_role}' cannot load programs",
        )

    def can_read_status(self, user_role: str) -> PolicyResult:
        """Check if a role can read machine status."""
        result = self.has_permission(user_role, Permission.READ_STATUS)
        if result.allowed:
            return PolicyResult(allowed=True, reason="Status read permissions granted")
        return PolicyResult(
            allowed=False,
            reason=f"Role '{user_role}' cannot read status",
        )

    def can_subscribe(self, user_role: str) -> PolicyResult:
        """Check if a role can subscribe to streaming updates."""
        result = self.has_permission(user_role, Permission.SUBSCRIBE_STATUS)
        if result.allowed:
            return PolicyResult(allowed=True, reason="Subscription permissions granted")
        return PolicyResult(
            allowed=False,
            reason=f"Role '{user_role}' cannot subscribe",
        )

    def check_broadcast_authorization(
        self,
        user_role: str,
        command_type: str,
    ) -> PolicyResult:
        """Check authorization for broadcast commands.

        Args:
            user_role: The user's role string.
            command_type: One of 'mdi', 'execution', 'mode', 'program'.

        Returns PolicyResult with allowed/False and reason.
        """
        try:
            role = Role(user_role)
        except ValueError:
            return PolicyResult(
                allowed=False,
                reason=f"Unknown role: {user_role}",
            )

        if role == Role.admin:
            return PolicyResult(allowed=True, reason="Admin can broadcast any command")

        if command_type == "mdi":
            return self.has_permission(user_role, Permission.CONTROL_STEP)
        elif command_type == "execution":
            return self.can_control_machine(user_role)
        elif command_type == "mode":
            result = self.has_permission(user_role, Permission.CONTROL_MODE)
            if result.allowed:
                return PolicyResult(allowed=True, reason="Mode change permissions granted")
            return PolicyResult(
                allowed=False,
                reason=f"Role '{user_role}' cannot change mode",
            )
        elif command_type == "program":
            result = self.has_permission(user_role, Permission.LOAD_PROGRAM)
            if result.allowed:
                return PolicyResult(allowed=True, reason="Program load permissions granted")
            return PolicyResult(
                allowed=False,
                reason=f"Role '{user_role}' cannot load programs",
            )

        return PolicyResult(
            allowed=False,
            reason=f"Unknown command type: {command_type}",
        )

    def filter_machines_by_scope(
        self,
        user_role: str,
        user_facility: Optional[str],
        all_machines: list[dict],
    ) -> list[dict]:
        """Filter machines based on user role and facility scope.

        Args:
            user_role: The user's role string.
            user_facility: User's assigned facility (from OIDC claims).
            all_machines: List of machine dicts with 'facility' and 'tags' keys.

        Returns filtered list of machines the user can access.
        """
        try:
            role = Role(user_role)
        except ValueError:
            role = Role.viewer

        if role == Role.admin:
            return all_machines  # Admin sees everything

        filtered = []
        for machine in all_machines:
            machine_facility = machine.get("facility", "")
            machine_tags = machine.get("tags", [])

            # Facility-scoped roles can only see machines in their facility
            if user_facility and machine_facility == user_facility:
                filtered.append(machine)

        return filtered


def create_test_policy_engine() -> PolicyEngine:
    """Create a policy engine for testing."""
    return PolicyEngine()
