"""Machine registration store with TTL-based heartbeat expiry."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class MachineEntry:
    """Immutable record of a registered machine instance."""

    id: str
    address: str
    port: int
    facility: str = ""
    tags: list[str] = dataclasses.field(default_factory=list)
    version: str = ""
    git_hash: str = ""
    last_heartbeat: float = 0.0
    registered_at: float = 0.0


class MachineRegistry:
    """In-memory machine registry with TTL-based expiry.

    Sidecars register on startup and must heartbeat every `heartbeat_ttl` seconds.
    Expired entries are cleaned up lazily (on lookup) and periodically via a
    background cleanup thread.
    """

    def __init__(self, heartbeat_ttl: float = 30.0, cleanup_interval: float = 60.0) -> None:
        self._heartbeat_ttl = heartbeat_ttl
        self._cleanup_interval = cleanup_interval
        self._machines: dict[str, MachineEntry] = {}
        self._lock = threading.Lock()
        self._running = False
        self._cleanup_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the background cleanup thread."""
        if self._running:
            return
        self._running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="registry-cleanup"
        )
        self._cleanup_thread.start()

    def stop(self) -> None:
        """Stop the background cleanup thread."""
        self._running = False
        self._stop_event.set()
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=5.0)
            self._cleanup_thread = None

    def register(
        self,
        machine_id: str,
        address: str,
        port: int,
        facility: str = "",
        tags: Optional[list[str]] = None,
        version: str = "",
        git_hash: str = "",
    ) -> MachineEntry:
        """Register a new machine or update an existing one."""
        entry = MachineEntry(
            id=machine_id,
            address=address,
            port=port,
            facility=facility,
            tags=tags or [],
            version=version,
            git_hash=git_hash,
            last_heartbeat=time.time(),
            registered_at=time.time(),
        )
        with self._lock:
            self._machines[machine_id] = entry
        log.info("Registered machine %s at %s:%d", machine_id, address, port)
        return entry

    def heartbeat(self, machine_id: str) -> Optional[MachineEntry]:
        """Update heartbeat timestamp for a registered machine.

        Returns the updated entry, or None if the machine is not registered.
        """
        now = time.time()
        with self._lock:
            entry = self._machines.get(machine_id)
            if entry is None:
                return None
            # frozen dataclass — replace with updated timestamp
            new_entry = dataclasses.replace(entry, last_heartbeat=now)
            self._machines[machine_id] = new_entry
        return new_entry

    def unregister(self, machine_id: str) -> bool:
        """Remove a machine from the registry. Returns True if it existed."""
        with self._lock:
            if machine_id in self._machines:
                del self._machines[machine_id]
                log.info("Unregistered machine %s", machine_id)
                return True
        return False

    def lookup(self, machine_id: str) -> Optional[MachineEntry]:
        """Look up a machine by ID. Returns None if expired or not found."""
        now = time.time()
        with self._lock:
            entry = self._machines.get(machine_id)
            if entry is None:
                return None
            if now - entry.last_heartbeat > self._heartbeat_ttl:
                del self._machines[machine_id]
                log.warning("Machine %s expired (no heartbeat for %.0fs)", machine_id, now - entry.last_heartbeat)
                return None
            return entry

    def list_all(self) -> list[MachineEntry]:
        """List all non-expired machines."""
        now = time.time()
        with self._lock:
            valid = []
            expired_ids = []
            for mid, entry in self._machines.items():
                if now - entry.last_heartbeat <= self._heartbeat_ttl:
                    valid.append(entry)
                else:
                    expired_ids.append(mid)
            for eid in expired_ids:
                del self._machines[eid]
            return valid

    def list_by_facility(self, facility: str) -> list[MachineEntry]:
        """List machines matching a facility name."""
        all_machines = self.list_all()
        return [m for m in all_machines if m.facility == facility]

    def resolve_scope(
        self,
        scope_type: str,
        facility: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> list[MachineEntry]:
        """Resolve target machines based on broadcast scope.

        Args:
            scope_type: 'ALL', 'FACILITY', or 'TAG'.
            facility: Facility name (used when scope_type is 'FACILITY').
            tags: Tag list (used when scope_type is 'TAG').

        Returns matching machine entries.
        """
        all_machines = self.list_all()

        if scope_type == "ALL":
            return all_machines
        elif scope_type == "FACILITY":
            if not facility:
                return []
            return [m for m in all_machines if m.facility == facility]
        elif scope_type == "TAG":
            if not tags:
                return []
            return [
                m for m in all_machines
                if any(t in m.tags for t in tags)
            ]

        return []

    def count(self) -> int:
        """Return the number of registered (non-expired) machines."""
        return len(self.list_all())

    def _cleanup_loop(self) -> None:
        """Background loop that periodically removes expired entries."""
        while self._running:
            self._stop_event.wait(timeout=self._cleanup_interval)
            if not self._running:
                break
            now = time.time()
            with self._lock:
                expired_ids = [
                    mid for mid, entry in self._machines.items()
                    if now - entry.last_heartbeat > self._heartbeat_ttl
                ]
                for eid in expired_ids:
                    del self._machines[eid]
                if expired_ids:
                    log.info("Cleaned up %d expired machines", len(expired_ids))

    def clear(self) -> None:
        """Remove all entries (useful for testing)."""
        with self._lock:
            self._machines.clear()


def create_test_registry(heartbeat_ttl: float = 30.0) -> MachineRegistry:
    """Create a registry instance for testing."""
    return MachineRegistry(heartbeat_ttl=heartbeat_ttl)
