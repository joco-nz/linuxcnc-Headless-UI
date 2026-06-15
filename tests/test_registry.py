"""Tests for MachineRegistry with TTL-based heartbeat expiry."""

import time
import threading

import pytest

from gateway.registry import MachineEntry, MachineRegistry, create_test_registry


class TestMachineEntry:
    """Tests for the frozen MachineEntry dataclass."""

    def test_defaults(self):
        entry = MachineEntry(id="m1", address="192.168.1.10", port=5007)
        assert entry.id == "m1"
        assert entry.address == "192.168.1.10"
        assert entry.port == 5007
        assert entry.facility == ""
        assert entry.tags == []
        assert entry.version == ""
        assert entry.git_hash == ""
        assert entry.last_heartbeat == 0.0
        assert entry.registered_at == 0.0

    def test_all_fields(self):
        now = time.time()
        entry = MachineEntry(
            id="m1",
            address="192.168.1.10",
            port=5007,
            facility="shop-1",
            tags=["mill", "cnc"],
            version="2.8.0",
            git_hash="abc123",
            last_heartbeat=now,
            registered_at=now - 100,
        )
        assert entry.id == "m1"
        assert entry.facility == "shop-1"
        assert entry.tags == ["mill", "cnc"]
        assert entry.version == "2.8.0"
        assert entry.git_hash == "abc123"

    def test_frozen_cannot_modify(self):
        entry = MachineEntry(id="m1", address="192.168.1.10", port=5007)
        with pytest.raises(Exception):
            entry.id = "m2"


class TestMachineRegistry:
    """Tests for the MachineRegistry CRUD operations."""

    def test_create_test_registry(self):
        registry = create_test_registry()
        assert isinstance(registry, MachineRegistry)

    def test_create_test_registry_custom_ttl(self):
        registry = create_test_registry(heartbeat_ttl=10.0)
        assert registry._heartbeat_ttl == 10.0

    def test_register_machine(self):
        registry = create_test_registry()
        entry = registry.register("m1", "192.168.1.10", 5007, facility="shop-1")
        assert entry.id == "m1"
        assert entry.address == "192.168.1.10"
        assert entry.port == 5007
        assert entry.facility == "shop-1"

    def test_register_machine_with_tags(self):
        registry = create_test_registry()
        entry = registry.register("m1", "192.168.1.10", 5007, tags=["mill", "cnc"])
        assert entry.tags == ["mill", "cnc"]

    def test_register_machine_with_version(self):
        registry = create_test_registry()
        entry = registry.register("m1", "192.168.1.10", 5007, version="2.8.0", git_hash="abc123")
        assert entry.version == "2.8.0"
        assert entry.git_hash == "abc123"

    def test_register_sets_timestamps(self):
        registry = create_test_registry()
        before = time.time()
        entry = registry.register("m1", "192.168.1.10", 5007)
        after = time.time()
        assert entry.last_heartbeat >= before
        assert entry.last_heartbeat <= after
        assert entry.registered_at >= before
        assert entry.registered_at <= after

    def test_register_updates_existing(self):
        registry = create_test_registry()
        entry1 = registry.register("m1", "192.168.1.10", 5007)
        time.sleep(0.01)
        entry2 = registry.register("m1", "192.168.1.11", 5008)
        assert entry2.address == "192.168.1.11"
        assert entry2.port == 5008

    def test_heartbeat_updates_timestamp(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007)
        time.sleep(0.01)
        entry = registry.heartbeat("m1")
        assert entry is not None
        assert entry.last_heartbeat > 0

    def test_heartbeat_nonexistent_machine(self):
        registry = create_test_registry()
        result = registry.heartbeat("nonexistent")
        assert result is None

    def test_unregister_existing_machine(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007)
        result = registry.unregister("m1")
        assert result is True

    def test_unregister_nonexistent_machine(self):
        registry = create_test_registry()
        result = registry.unregister("nonexistent")
        assert result is False

    def test_lookup_existing_machine(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007)
        entry = registry.lookup("m1")
        assert entry is not None
        assert entry.id == "m1"

    def test_lookup_nonexistent_machine(self):
        registry = create_test_registry()
        entry = registry.lookup("nonexistent")
        assert entry is None

    def test_list_all(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007)
        registry.register("m2", "192.168.1.11", 5008)
        entries = registry.list_all()
        assert len(entries) == 2

    def test_list_by_facility(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")
        registry.register("m2", "192.168.1.11", 5008, facility="shop-2")
        registry.register("m3", "192.168.1.12", 5009, facility="shop-1")
        entries = registry.list_by_facility("shop-1")
        assert len(entries) == 2
        ids = {e.id for e in entries}
        assert ids == {"m1", "m3"}

    def test_list_by_facility_no_match(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")
        entries = registry.list_by_facility("shop-3")
        assert len(entries) == 0

    def test_count(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007)
        registry.register("m2", "192.168.1.11", 5008)
        assert registry.count() == 2

    def test_clear(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007)
        registry.clear()
        assert registry.count() == 0

    # --- TTL expiry tests ---

    def test_expired_machine_lookup(self):
        registry = create_test_registry(heartbeat_ttl=0.1)
        entry = registry.register("m1", "192.168.1.10", 5007)
        assert entry is not None
        time.sleep(0.15)
        expired = registry.lookup("m1")
        assert expired is None

    def test_expired_machine_list_all(self):
        registry = create_test_registry(heartbeat_ttl=0.1)
        registry.register("m1", "192.168.1.10", 5007)
        time.sleep(0.15)
        entries = registry.list_all()
        assert len(entries) == 0

    def test_expired_machine_removed_from_store(self):
        """Expired machines should be removed from the internal store."""
        registry = create_test_registry(heartbeat_ttl=0.1)
        registry.register("m1", "192.168.1.10", 5007)
        time.sleep(0.15)
        registry.list_all()  # Triggers cleanup
        assert "m1" not in registry._machines

    def test_heartbeat_refreshes_ttl(self):
        """Heartbeat should extend the TTL for a machine."""
        registry = create_test_registry(heartbeat_ttl=0.2)
        registry.register("m1", "192.168.1.10", 5007)
        time.sleep(0.15)
        # Refresh heartbeat before expiry
        registry.heartbeat("m1")
        time.sleep(0.1)
        # Should still be valid
        entry = registry.lookup("m1")
        assert entry is not None

    def test_mixed_expired_and_valid(self):
        """Registry should return only non-expired machines."""
        registry = MachineRegistry(heartbeat_ttl=0.1)
        registry.register("m1", "192.168.1.10", 5007)
        time.sleep(0.05)
        registry.register("m2", "192.168.1.11", 5008)
        time.sleep(0.08)  # m1 should be expired (0.13s > 0.1s TTL), m2 still valid (0.08s < 0.1s)
        entries = registry.list_all()
        assert len(entries) == 1
        assert entries[0].id == "m2"

    # --- Scope resolution tests ---

    def test_resolve_scope_all(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")
        registry.register("m2", "192.168.1.11", 5008, facility="shop-2")
        entries = registry.resolve_scope("ALL")
        assert len(entries) == 2

    def test_resolve_scope_facility(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")
        registry.register("m2", "192.168.1.11", 5008, facility="shop-2")
        entries = registry.resolve_scope("FACILITY", facility="shop-1")
        assert len(entries) == 1
        assert entries[0].id == "m1"

    def test_resolve_scope_facility_no_match(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007, facility="shop-1")
        entries = registry.resolve_scope("FACILITY", facility="shop-3")
        assert len(entries) == 0

    def test_resolve_scope_facility_no_facility_arg(self):
        """Resolve FACILITY without facility arg returns empty."""
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007)
        entries = registry.resolve_scope("FACILITY")
        assert len(entries) == 0

    def test_resolve_scope_tag_match(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007, tags=["mill", "cnc"])
        registry.register("m2", "192.168.1.11", 5008, tags=["lathe"])
        registry.register("m3", "192.168.1.12", 5009, tags=["mill"])
        entries = registry.resolve_scope("TAG", tags=["mill"])
        assert len(entries) == 2
        ids = {e.id for e in entries}
        assert ids == {"m1", "m3"}

    def test_resolve_scope_tag_no_match(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007, tags=["mill"])
        entries = registry.resolve_scope("TAG", tags=["lathe"])
        assert len(entries) == 0

    def test_resolve_scope_tag_no_tags_arg(self):
        """Resolve TAG without tags arg returns empty."""
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007, tags=["mill"])
        entries = registry.resolve_scope("TAG")
        assert len(entries) == 0

    def test_resolve_scope_unknown_type(self):
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007)
        entries = registry.resolve_scope("UNKNOWN")
        assert len(entries) == 0

    # --- Start/Stop cleanup thread tests ---

    def test_start_stop_cleanup_thread(self):
        registry = MachineRegistry(heartbeat_ttl=30.0, cleanup_interval=1.0)
        registry.start()
        assert registry._running is True
        assert registry._cleanup_thread is not None
        assert registry._cleanup_thread.is_alive()
        thread = registry._cleanup_thread
        registry.stop()
        assert registry._running is False
        assert not thread.is_alive()

    def test_start_twice_no_error(self):
        registry = create_test_registry()
        registry.start()
        registry.start()  # Should be safe
        registry.stop()

    def test_stop_without_start_no_error(self):
        registry = create_test_registry()
        registry.stop()  # Should be safe

    def test_background_cleanup_removes_expired(self):
        """Background thread should remove expired entries."""
        registry = MachineRegistry(heartbeat_ttl=0.1, cleanup_interval=0.2)
        registry.register("m1", "192.168.1.10", 5007)
        registry.start()
        time.sleep(0.4)
        registry.stop()
        assert registry.count() == 0

    def test_background_cleanup_preserves_valid(self):
        """Background thread should not remove valid entries."""
        registry = MachineRegistry(heartbeat_ttl=1.0, cleanup_interval=0.2)
        registry.register("m1", "192.168.1.10", 5007)
        registry.start()
        time.sleep(0.4)
        registry.stop()
        assert registry.count() == 1

    # --- Thread safety tests ---

    def test_concurrent_register_and_lookup(self):
        """Multiple threads registering and looking up should not crash."""
        registry = create_test_registry()
        errors = []

        def register_machine(i):
            try:
                registry.register(f"m{i}", f"192.168.1.{i}", 5007 + i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=register_machine, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert registry.count() == 10

    def test_concurrent_heartbeat_and_unregister(self):
        """Concurrent heartbeats and unregisters should not crash."""
        registry = create_test_registry()
        registry.register("m1", "192.168.1.10", 5007)
        errors = []

        def heartbeat_loop():
            for _ in range(100):
                try:
                    registry.heartbeat("m1")
                except Exception as e:
                    errors.append(e)

        def unregister_after_delay():
            time.sleep(0.05)
            try:
                registry.unregister("m1")
            except Exception as e:
                errors.append(e)

        hb_threads = [threading.Thread(target=heartbeat_loop) for _ in range(5)]
        unreg_thread = threading.Thread(target=unregister_after_delay)

        for t in hb_threads + [unreg_thread]:
            t.start()
        for t in hb_threads + [unreg_thread]:
            t.join()

        assert len(errors) == 0
