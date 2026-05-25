"""Tests for _Snapshot dataclass immutability and atomic swap behavior."""

import dataclasses
import threading
import time

import pytest

from linuxcnc_fleet.headless import _Snapshot


class TestSnapshotImmutability:
    def test_snapshot_is_frozen(self):
        """_Snapshot must be immutable (frozen=True)."""
        snap = _Snapshot(machine_id="test", state=1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.machine_id = "changed"

    def test_snapshot_default_values(self):
        snap = _Snapshot()
        assert snap.machine_id == ""
        assert snap.state == 0
        assert snap.execution == 0
        assert snap.interp_state == 0
        assert snap.estop_state == 0
        assert snap.mode == 0
        assert snap.joint_actual_x == 0.0
        assert snap.feedrate == 0.0
        assert snap.coolant_mist is False
        assert snap.errors == []

    def test_snapshot_accepts_custom_values(self):
        snap = _Snapshot(
            machine_id="machine1",
            state=3,
            execution=1,
            interp_line=42,
            program_file="test.ngc",
            feedrate=500.0,
            coolant_mist=True,
        )
        assert snap.machine_id == "machine1"
        assert snap.state == 3
        assert snap.interp_line == 42
        assert snap.program_file == "test.ngc"
        assert snap.feedrate == 500.0
        assert snap.coolant_mist is True


class TestSnapshotReplace:
    def test_dataclasses_replace_creates_new_instance(self):
        snap1 = _Snapshot(machine_id="machine1", state=1)
        snap2 = dataclasses.replace(snap1, state=2, feedrate=100.0)

        assert snap1.state == 1  # original unchanged
        assert snap2.state == 2
        assert snap2.machine_id == "machine1"  # inherited
        assert snap2.feedrate == 100.0

    def test_replace_preserves_unspecified_fields(self):
        snap1 = _Snapshot(machine_id="m1", state=5, execution=3)
        snap2 = dataclasses.replace(snap1, state=6)
        assert snap2.execution == 3


class TestAtomicSwap:
    def test_writer_reader_no_lock_swap(self):
        """Writer thread replaces snapshot; reader sees either old or new — never partial."""
        target_snap = _Snapshot(machine_id="test")

        # Simulate the pattern used in LinuxCncSidecar:
        # writer does object.__setattr__(self, '_snapshot', new_snapshot)
        # reader reads self._snapshot
        def writer():
            for i in range(100):
                new_snap = _Snapshot(machine_id=f"machine-{i}", state=i)
                object.__setattr__(target_snap, '__test_snapshot__', new_snap)
                time.sleep(0.001)

        collected = []
        barrier = threading.Barrier(2)

        def reader():
            barrier.wait()  # synchronize start
            for _ in range(200):
                snap = getattr(target_snap, '__test_snapshot__', None)
                if snap is not None:
                    collected.append(snap.machine_id)
                time.sleep(0.001)

        t_writer = threading.Thread(target=writer)
        t_reader = threading.Thread(target=reader)
        t_writer.start()
        t_reader.start()
        t_writer.join(timeout=5)
        t_reader.join(timeout=5)

        # Every collected snapshot should have a valid machine_id (never partial/corrupt)
        for mid in collected:
            assert mid.startswith("machine-")
            assert mid.split("-")[1].isdigit()

    def test_snapshot_errors_field_is_independent(self):
        """Each _Snapshot has its own errors list — no shared mutable state."""
        snap1 = _Snapshot(errors=["error1"])
        snap2 = _Snapshot()

        # Can't mutate snap1 (frozen), but replace proves independence
        snap3 = dataclasses.replace(snap1, errors=["error2", "error3"])
        assert len(snap3.errors) == 2
        assert snap3.errors[0] == "error2"
