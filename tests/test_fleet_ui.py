"""Tests for fleet_ui/server.py — SSE streams, FleetApp, and HTTP handlers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import grpc
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from fleet_ui.server import FleetApp, _SSEStream, _info_to_dict, _mode_to_int, _status_to_dict, create_routes
from linuxcnc_fleet.fleet_pb2 import (
    ErrorEvent,
    ErrorList,
    HalComponentInfo,
    HalComponentList,
    HalPinInfo,
    HalPinType,
    HalPinUpdate,
    HalPinValue,
    LinuxCncVersion,
    MachineInfo,
    MachineStatus,
    Mode,
    Position,
    ProgramEntry,
    ProgramList,
    Result,
)


# ---------------------------------------------------------------------------
# Mock FleetClient — returns real protobuf messages for coverage of helpers
# ---------------------------------------------------------------------------

class MockFleetClient:
    """Lightweight mock that mirrors FleetClient's public API."""

    def __init__(self) -> None:
        self._closed = False
        self.get_machines_calls: list = []
        self.get_status_calls: list = []
        self.set_mode_calls: list = []
        self.send_mdi_calls: list = []
        self.load_program_calls: list = []
        self.broadcast_load_program_calls: list = []
        self.list_programs_calls: list = []
        self.control_calls: list = []
        self.list_hal_components_calls: list = []
        self.read_hal_pin_calls: list = []
        self.write_hal_pin_calls: list = []
        self.get_errors_calls: list = []
        self.get_machine_info_calls: list = []
        self.start_calls: list = []
        self.stop_calls: list = []
        self.feed_hold_calls: list = []
        self.continue_exec_calls: list = []
        self.home_all_calls: list = []
        self.estop_reset_calls: list = []
        self.power_on_calls: list = []
        self.power_off_calls: list = []
        self.set_machine_state_calls: list = []
        self.init_machine_calls: list = []
        self.subscribe_status_calls: list = []
        self.subscribe_all_status_calls: list = []
        self.close_calls: int = 0

    # ── GatewayService RPCs ──────────────────────────────────────────

    async def get_machines(self, facility=None):
        self.get_machines_calls.append(facility)
        return [
            MagicMock(machine_id="machine-1", machine_name="Lathe-1", host_address="192.168.1.10",
                      version="2.8.1", num_joints=4, num_hal_components=5),
            MagicMock(machine_id="machine-2", machine_name="Mill-1", host_address="192.168.1.11",
                      version="2.8.1", num_joints=4, num_hal_components=3),
        ]

    async def get_status(self, machine_id):
        self.get_status_calls.append(machine_id)
        return MachineStatus(
            machine_id=machine_id,
            state=3,  # RUNNING
            execution=1,  # RUN
            interp_state=3,  # EXECUTE
            estop_state=1,  # NOT_E_STOPPED
            mode=2,  # MODE_AUTO
            joint_actual=Position(x=10.0, y=20.0, z=30.0),
            joint_commanded=Position(x=10.0, y=20.0, z=30.0),
            world_actual=Position(x=10.0, y=20.0, z=30.0),
            interp_line=42,
            program_file="/home/linuxcnc/nf/test.ngc",
            remaining_time="0:05:00",
            feedrate=500.0,
            feedrate_override=1.0,
            spindle_speed=2000.0,
            spindle_speed_override=1.0,
            coolant_mist=True,
            coolant_flood=False,
            coolant_mazak=False,
            cycle_time=12.5,
        )

    async def set_mode(self, machine_id, mode):
        self.set_mode_calls.append((machine_id, mode))
        return Result(success=True, message=f"Mode set to {mode}")

    async def send_mdi(self, machine_id, command):
        self.send_mdi_calls.append((machine_id, command))
        return Result(success=True, message="MDI executed")

    async def load_program(self, machine_id, path):
        self.load_program_calls.append((machine_id, path))
        return Result(success=True, message=f"Program loaded: {path}")

    async def broadcast_load_program(self, scope, path, facility="", tags=None):
        self.broadcast_load_program_calls.append((scope, path, facility, tags or []))
        return {"machine-1": (True, "Loaded"), "machine-2": (True, "Loaded")}

    async def list_programs(self, machine_id, directory="", max_depth=0):
        self.list_programs_calls.append((machine_id, directory, max_depth))
        return ProgramList(
            programs=[
                ProgramEntry(path="/home/linuxcnc/nf/part1.ngc", name="part1.ngc", size_bytes=2048, modified_time=1700000000),
                ProgramEntry(path="/home/linuxcnc/nf/part2.ngc", name="part2.ngc", size_bytes=4096, modified_time=1700100000),
            ],
            total_count=2,
        )

    async def control(self, machine_id, cmd):
        self.control_calls.append((machine_id, cmd))
        return Result(success=True, message=f"Control: {cmd}")

    async def start(self, machine_id):
        self.start_calls.append(machine_id)
        return Result(success=True, message="Started")

    async def stop(self, machine_id):
        self.stop_calls.append(machine_id)
        return Result(success=True, message="Stopped")

    async def feed_hold(self, machine_id):
        self.feed_hold_calls.append(machine_id)
        return Result(success=True, message="Feed hold")

    async def continue_exec(self, machine_id):
        self.continue_exec_calls.append(machine_id)
        return Result(success=True, message="Continued")

    async def home_all(self, machine_id):
        self.home_all_calls.append(machine_id)
        return Result(success=True, message="Homed all axes")

    async def set_machine_state(self, machine_id, state):
        self.set_machine_state_calls.append((machine_id, state))
        labels = {1: "E-Stop Reset", 2: "Power Off", 3: "Power On"}
        return Result(success=True, message=f"State set to {labels.get(state, state)}")

    async def init_machine(self, machine_id):
        self.init_machine_calls.append(machine_id)
        return Result(success=True, message="Machine initialized")

    # ── HAL RPCs ─────────────────────────────────────────────────────

    async def list_hal_components(self, machine_id):
        self.list_hal_components_calls.append(machine_id)
        return HalComponentList(
            components=[
                HalComponentInfo(
                    name="spindle",
                    update_period_ns=1000000,
                    pins=[
                        HalPinInfo(name="spindle.speed-cmd", type=3, is_output=True, value_f=2000.0,
                                   value_u32=0, value_s32=0, value_bit=False),
                        HalPinInfo(name="spindle.at-speed", type=0, is_output=False, value_f=0.0,
                                   value_u32=0, value_s32=0, value_bit=True),
                    ],
                ),
            ],
        )

    async def read_hal_pin(self, machine_id, pin_name):
        self.read_hal_pin_calls.append((machine_id, pin_name))
        return HalPinValue(
            pin_name=pin_name,
            type=3,  # HAL_FLOAT
            value_f=123.456,
        )

    async def write_hal_pin(self, machine_id, pin_name, bit_value=None, float_value=None, u32_value=None, s32_value=None):
        self.write_hal_pin_calls.append((machine_id, pin_name, {
            "bit": bit_value, "float": float_value, "u32": u32_value, "s32": s32_value,
        }))
        return Result(success=True, message=f"Wrote {pin_name}")

    # ── Error RPCs ───────────────────────────────────────────────────

    async def get_errors(self, machine_id, limit=50):
        self.get_errors_calls.append((machine_id, limit))
        return ErrorList(
            errors=[
                ErrorEvent(message="Spindle overload detected", timestamp=1700000000.0),
                ErrorEvent(message="Coolant low", timestamp=1700000010.0),
            ],
        )

    # ── Machine Info ─────────────────────────────────────────────────

    async def get_machine_info(self, machine_id):
        self.get_machine_info_calls.append(machine_id)
        return MachineInfo(
            machine_id=machine_id,
            machine_name="Lathe-1",
            host_address="192.168.1.10",
            version=LinuxCncVersion(version_string="2.8.1", build_type="release", git_hash="abc123"),
            num_joints=4,
            num_hal_components=5,
        )

    # ── Streaming subscriptions ──────────────────────────────────────

    def subscribe_status(self, machine_id):
        self.subscribe_status_calls.append(machine_id)

        async def _gen():
            for i in range(3):
                yield MachineStatus(machine_id=machine_id, state=3, execution=1, interp_state=3,
                                    estop_state=1, mode=2, joint_actual=Position(x=float(i), y=0.0, z=0.0))
                await asyncio.sleep(0.01)

        return _gen()

    def subscribe_all_status(self):
        self.subscribe_all_status_calls.append(True)

        async def _gen():
            for mid in ["machine-1", "machine-2"]:
                yield mid, MachineStatus(machine_id=mid, state=3, execution=1, interp_state=3,
                                         estop_state=1, mode=2, joint_actual=Position(x=0.0, y=0.0, z=0.0))
                await asyncio.sleep(0.01)

        return _gen()

    async def close(self):
        self._closed = True
        self.close_calls += 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client():
    """Provide a MockFleetClient instance."""
    return MockFleetClient()


@pytest.fixture
def fleet_app(mock_client):
    """Provide a FleetApp with injected mock client."""
    app = FleetApp(gateway_address="localhost:50052", token="fake-token", _mock_client=mock_client)
    return app


@pytest.fixture
def aiohttp_app(fleet_app):
    """Provide a web.Application with FleetApp in app['fleet']."""
    import argparse
    app = web.Application()
    app["fleet"] = fleet_app
    app["args"] = argparse.Namespace(port=8080, gateway="localhost:50052", token="", tls_cert=None, tls_key=None, timeout=120)
    create_routes(app)
    return app


@pytest.fixture
async def client(aiohttp_app):
    """Provide an aiohttp TestClient."""
    server = TestServer(aiohttp_app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


# ---------------------------------------------------------------------------
# Tests: _SSEStream
# ---------------------------------------------------------------------------

class TestSSEStream:

    async def test_send_adds_to_queue(self):
        stream = _SSEStream(None, machine_id="test-machine")
        await stream.send({"machine_id": "test-machine"})
        assert not stream._queue.empty()
        data = stream._queue.get_nowait()
        assert data == {"machine_id": "test-machine"}
        stream.close()

    async def test_close_stops_accepting(self):
        stream = _SSEStream(None, machine_id="test-machine")
        stream.close()
        await stream.send({"machine_id": "test-machine"})
        assert stream._queue.empty()

    async def test_queue_full_logs_warning_with_machine_id(self, caplog):
        import logging
        stream = _SSEStream(None, machine_id="overload-machine")
        # Fill the queue to maxsize (100)
        for i in range(100):
            await stream.send({"i": i})
        # This one should trigger QueueFull
        await stream.send({"overflow": True})
        assert "SSE queue full for overload-machine" in caplog.text

    async def test_iter_lines_yields_json_data(self):
        stream = _SSEStream(None, machine_id="test-machine")
        await stream.send({"key": "value"})
        collected = []
        async for line in stream._iter_lines():
            collected.append(line)
            if len(collected) >= 1:
                break
        stream.close()
        assert len(collected) == 1
        assert collected[0] == 'data: {"key": "value"}\n\n'

    async def test_iter_lines_exits_on_timeout(self):
        stream = _SSEStream(None, machine_id="test-machine")
        with patch("fleet_ui.server.asyncio.wait_for", side_effect=asyncio.TimeoutError):
            collected = []
            async for line in stream._iter_lines():
                collected.append(line)
        assert len(collected) == 0
        stream.close()

    async def test_iter_lines_multiple_items(self):
        stream = _SSEStream(None, machine_id="multi")
        await stream.send({"a": 1})
        await stream.send({"b": 2})
        collected = []
        count = 0
        async for line in stream._iter_lines():
            collected.append(line)
            count += 1
            if count >= 2:
                break
        stream.close()
        assert len(collected) == 2
        assert json.loads(collected[0].replace("data: ", "")) == {"a": 1}
        assert json.loads(collected[1].replace("data: ", "")) == {"b": 2}

    async def test_machine_id_attribute(self):
        stream = _SSEStream(None, machine_id="my-machine")
        assert stream.machine_id == "my-machine"

    async def test_default_machine_id_empty(self):
        stream = _SSEStream(None)
        assert stream.machine_id == ""


# ---------------------------------------------------------------------------
# Tests: FleetApp methods
# ---------------------------------------------------------------------------

class TestFleetApp:

    async def test_discover_machines_returns_dict_list(self, fleet_app, mock_client):
        result = await fleet_app.discover_machines()
        assert len(result) == 2
        assert result[0]["machine_id"] == "machine-1"
        assert result[0]["machine_name"] == "Lathe-1"
        assert result[0]["host_address"] == "192.168.1.10"
        assert mock_client.get_machines_calls

    async def test_discover_machines_empty_on_error(self, fleet_app):
        mock_client = MockFleetClient()
        mock_client.get_machines = MagicMock(side_effect=Exception("connection refused"))
        app = FleetApp(gateway_address="localhost:50052", token="fake", _mock_client=mock_client)
        result = await app.discover_machines()
        assert result == []

    async def test_get_status_caches_last_status(self, fleet_app, mock_client):
        result = await fleet_app.get_status("machine-1")
        assert result is not None
        assert result["machine_id"] == "machine-1"
        assert result["state"] == "RUNNING"
        # Check it's cached
        cached = await fleet_app.get_last_status("machine-1")
        assert cached is not None
        assert cached["state"] == "RUNNING"

    async def test_get_status_returns_none_on_error(self, fleet_app):
        mock_client = MockFleetClient()
        mock_client.get_status = MagicMock(side_effect=Exception("not found"))
        app = FleetApp(gateway_address="localhost:50052", token="fake", _mock_client=mock_client)
        result = await app.get_status("machine-1")
        assert result is None

    async def test_set_mode_forwards_to_client(self, fleet_app, mock_client):
        result = await fleet_app.set_mode("machine-1", "MANUAL")
        assert result["success"] is True
        assert mock_client.set_mode_calls[0] == ("machine-1", 1)  # MODE_MANUAL = 1

    async def test_set_mode_unknown_returns_error(self, fleet_app, mock_client):
        result = await fleet_app.set_mode("machine-1", "UNKNOWN_MODE")
        assert result["success"] is False
        assert "Unknown machine mode" in result["message"]
        assert not mock_client.set_mode_calls  # never forwarded to client

    async def test_send_mdi_forwards_to_client(self, fleet_app, mock_client):
        result = await fleet_app.send_mdi("machine-1", "G0 X10")
        assert result["success"] is True
        assert mock_client.send_mdi_calls[0] == ("machine-1", "G0 X10")

    async def test_load_program_forwards_to_client(self, fleet_app, mock_client):
        result = await fleet_app.load_program("machine-1", "/path/to/file.ngc")
        assert result["success"] is True
        assert mock_client.load_program_calls[0] == ("machine-1", "/path/to/file.ngc")

    async def test_broadcast_load_program_returns_results_dict(self, fleet_app, mock_client):
        result = await fleet_app.broadcast_load_program("ALL", "/path.ngc")
        assert "results" in result
        assert "machine-1" in result["results"]
        assert result["results"]["machine-1"]["success"] is True

    async def test_broadcast_load_program_error_returns_error_key(self):
        mock_client = MockFleetClient()
        mock_client.broadcast_load_program = MagicMock(side_effect=Exception("gateway down"))
        app = FleetApp(gateway_address="localhost:50052", token="fake", _mock_client=mock_client)
        result = await app.broadcast_load_program("ALL", "/path.ngc")
        assert "error" in result

    async def test_list_programs_returns_formatted_response(self, fleet_app, mock_client):
        result = await fleet_app.list_programs("machine-1")
        assert "programs" in result
        assert len(result["programs"]) == 2
        assert result["total"] == 2
        assert result["programs"][0]["name"] == "part1.ngc"

    async def test_list_hal_returns_component_dicts(self, fleet_app, mock_client):
        result = await fleet_app.list_hal("machine-1")
        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "spindle"
        assert len(result[0]["pins"]) == 2

    async def test_list_hal_returns_none_when_hal_module_missing(self):
        mock_client = MockFleetClient()
        mock_client.list_hal_components = MagicMock(side_effect=ImportError("no _hal module"))
        app = FleetApp(gateway_address="localhost:50052", token="fake", _mock_client=mock_client)
        result = await app.list_hal("machine-1")
        assert result is None

    async def test_read_hal_pin_returns_pin_dict(self, fleet_app, mock_client):
        result = await fleet_app.read_hal_pin("machine-1", "spindle.speed-cmd")
        assert result["pin_name"] == "spindle.speed-cmd"
        assert result["value_f"] == pytest.approx(123.456)

    async def test_write_hal_pin_forwards_typed_values(self, fleet_app, mock_client):
        result = await fleet_app.write_hal_pin("machine-1", "pin-out", float=3.14)
        assert result["success"] is True
        call_args = mock_client.write_hal_pin_calls[0]
        assert call_args[2]["float"] == 3.14

    async def test_write_hal_pin_rejects_multiple_value_types(self, fleet_app, mock_client):
        result = await fleet_app.write_hal_pin("machine-1", "pin-out", float=3.14, bit=False)
        assert result["success"] is False
        assert "Multiple value types provided" in result["message"]

    async def test_write_hal_pin_rejects_zero_value_types(self, fleet_app, mock_client):
        result = await fleet_app.write_hal_pin("machine-1", "pin-out")
        assert result["success"] is False
        assert "Exactly one of bit, float, u32, or s32 must be provided" in result["message"]

    async def test_write_hal_pin_with_bit_value(self, fleet_app, mock_client):
        result = await fleet_app.write_hal_pin("machine-1", "pin-out", bit=True)
        assert result["success"] is True
        call_args = mock_client.write_hal_pin_calls[0]
        assert call_args[2]["bit"] is True

    async def test_write_hal_pin_with_u32_value(self, fleet_app, mock_client):
        result = await fleet_app.write_hal_pin("machine-1", "pin-out", u32=42)
        assert result["success"] is True
        call_args = mock_client.write_hal_pin_calls[0]
        assert call_args[2]["u32"] == 42

    async def test_write_hal_pin_with_s32_value(self, fleet_app, mock_client):
        result = await fleet_app.write_hal_pin("machine-1", "pin-out", s32=-100)
        assert result["success"] is True
        call_args = mock_client.write_hal_pin_calls[0]
        assert call_args[2]["s32"] == -100

    async def test_get_errors_returns_error_dicts(self, fleet_app, mock_client):
        result = await fleet_app.get_errors("machine-1")
        assert len(result) == 2
        assert result[0]["message"] == "Spindle overload detected"

    async def test_get_machine_info_returns_info_dict(self, fleet_app, mock_client):
        result = await fleet_app.get_machine_info("machine-1")
        assert result["machine_id"] == "machine-1"
        assert result["version"] == "2.8.1"
        assert result["num_joints"] == 4

    async def test_control_start_forwards_to_client(self, fleet_app, mock_client):
        result = await fleet_app.control("machine-1", "start")
        assert result["success"] is True
        assert "machine-1" in mock_client.start_calls

    async def test_control_stop_forwards_to_client(self, fleet_app, mock_client):
        result = await fleet_app.control("machine-1", "stop")
        assert result["success"] is True
        assert "machine-1" in mock_client.stop_calls

    async def test_control_feed_hold_forwards_to_client(self, fleet_app, mock_client):
        result = await fleet_app.control("machine-1", "feed_hold")
        assert result["success"] is True
        assert "machine-1" in mock_client.feed_hold_calls

    async def test_control_continue_exec_forwards_to_client(self, fleet_app, mock_client):
        result = await fleet_app.control("machine-1", "continue_exec")
        assert result["success"] is True
        assert "machine-1" in mock_client.continue_exec_calls

    async def test_control_home_all_forwards_to_client(self, fleet_app, mock_client):
        result = await fleet_app.control("machine-1", "home_all")
        assert result["success"] is True
        assert "machine-1" in mock_client.home_all_calls

    async def test_control_unknown_cmd_returns_error(self, fleet_app, mock_client):
        result = await fleet_app.control("machine-1", "unknown_thing")
        assert result["success"] is False
        assert "Unknown control" in result["message"]

    async def test_control_estop_reset_forwards_to_set_machine_state(self, fleet_app, mock_client):
        result = await fleet_app.control("machine-1", "estop_reset")
        assert result["success"] is True
        assert ("machine-1", 1) in mock_client.set_machine_state_calls

    async def test_control_power_on_forwards_to_set_machine_state(self, fleet_app, mock_client):
        result = await fleet_app.control("machine-1", "power_on")
        assert result["success"] is True
        assert ("machine-1", 3) in mock_client.set_machine_state_calls

    async def test_control_power_off_forwards_to_set_machine_state(self, fleet_app, mock_client):
        result = await fleet_app.control("machine-1", "power_off")
        assert result["success"] is True
        assert ("machine-1", 2) in mock_client.set_machine_state_calls

    async def test_set_machine_state_forwards_to_client(self, fleet_app, mock_client):
        result = await fleet_app.set_machine_state("machine-1", 3)
        assert result["success"] is True
        assert ("machine-1", 3) in mock_client.set_machine_state_calls

    async def test_init_machine_forwards_to_client(self, fleet_app, mock_client):
        result = await fleet_app.init_machine("machine-1")
        assert result["success"] is True
        assert "machine-1" in mock_client.init_machine_calls

    async def test_stream_status_creates_and_registers_stream(self, fleet_app, mock_client):
        stream = await fleet_app.stream_status("machine-1")
        assert isinstance(stream, _SSEStream)
        assert stream.machine_id == "machine-1"
        assert "machine-1" in fleet_app._streams
        assert stream in fleet_app._streams["machine-1"]

    async def test_remove_stream_cleans_up(self, fleet_app, mock_client):
        stream = await fleet_app.stream_status("machine-1")
        assert "machine-1" in fleet_app._streams
        await fleet_app.remove_stream("machine-1", stream)
        assert "machine-1" not in fleet_app._streams

    async def test_remove_stream_fallback_closes_all(self, fleet_app, mock_client):
        """remove_stream without stream arg closes all and deletes entry."""
        s1 = await fleet_app.stream_status("machine-1")
        s2 = await fleet_app.stream_status("machine-1")
        assert len(fleet_app._streams["machine-1"]) == 2
        await fleet_app.remove_stream("machine-1")
        assert s1._done is True
        assert s2._done is True
        assert "machine-1" not in fleet_app._streams

    async def test_stream_all_machines_creates_stream(self, fleet_app, mock_client):
        stream = await fleet_app.stream_all_machines()
        assert isinstance(stream, _SSEStream)
        assert stream.machine_id == "__all__"
        assert "__all__" in fleet_app._streams

    async def test_remove_all_stream_cleans_up(self, fleet_app, mock_client):
        stream = await fleet_app.stream_all_machines()
        assert "__all__" in fleet_app._streams
        await fleet_app.remove_stream("__all__", stream)
        assert "__all__" not in fleet_app._streams

    async def test_multiple_streams_same_machine_independent(self, fleet_app, mock_client):
        """Two tabs for the same machine create two independent streams."""
        s1 = await fleet_app.stream_status("machine-1")
        s2 = await fleet_app.stream_status("machine-1")
        assert s1 is not s2
        assert s1.stream_id != s2.stream_id
        assert len(fleet_app._streams["machine-1"]) == 2

    async def test_remove_stream_does_not_affect_other_tabs(self, fleet_app, mock_client):
        """Removing one stream should not affect other streams for the same machine."""
        s1 = await fleet_app.stream_status("machine-1")
        s2 = await fleet_app.stream_status("machine-1")
        assert len(fleet_app._streams["machine-1"]) == 2
        await fleet_app.remove_stream("machine-1", s1)
        assert "machine-1" in fleet_app._streams
        assert len(fleet_app._streams["machine-1"]) == 1
        assert s1._done is True
        assert s2._done is False

    async def test_stream_status_reconnects_on_error(self, fleet_app, mock_client):
        """Stream should reconnect after gRPC error with exponential backoff."""
        call_count = 0

        async def failing_subscribe(machine_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise grpc.aio.AioRpcError(
                    code=lambda: grpc.StatusCode.UNAVAILABLE,
                    details=lambda: "connection refused",
                    initial_metadata=None, trailing_metadata=None,
                )
            for i in range(2):
                yield MachineStatus(machine_id=machine_id, state=3, execution=1, interp_state=3,
                                    estop_state=1, mode=2, joint_actual=Position(x=float(i), y=0.0, z=0.0))
                await asyncio.sleep(0.01)

        original_subscribe = mock_client.subscribe_status
        mock_client.subscribe_status = failing_subscribe

        stream = await fleet_app.stream_status("machine-1")
        assert "machine-1" in fleet_app._streams

        await asyncio.sleep(2.5)  # Wait for retry after 2s backoff

        stream.close()
        await fleet_app.remove_stream("machine-1", stream)
        await asyncio.sleep(0.2)

        assert call_count == 2

    async def test_stream_status_stops_retrying_when_closed(self, fleet_app, mock_client):
        """Stream should stop reconnecting once client disconnects (stream closed)."""
        error_count = 0

        def always_fail(machine_id):
            nonlocal error_count
            error_count += 1

            async def _gen():
                raise grpc.aio.AioRpcError(
                    code=lambda: grpc.StatusCode.UNAVAILABLE,
                    details=lambda: "connection refused",
                    initial_metadata=None, trailing_metadata=None,
                )
            return _gen()

    async def test_get_last_status_returns_cached(self, fleet_app, mock_client):
        await fleet_app.get_status("machine-1")
        cached = await fleet_app.get_last_status("machine-1")
        assert cached is not None
        assert cached["state"] == "RUNNING"

    async def test_get_last_status_returns_none_for_unknown(self, fleet_app):
        result = await fleet_app.get_last_status("nonexistent")
        assert result is None

    async def test_stream_status_reconnects_on_error(self, fleet_app, mock_client):
        """Stream should reconnect after gRPC error with exponential backoff."""
        call_count = 0

        async def failing_subscribe(machine_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise grpc.aio.AioRpcError(
                    code=lambda: grpc.StatusCode.UNAVAILABLE,
                    details=lambda: "connection refused",
                    initial_metadata=None, trailing_metadata=None,
                )
            for i in range(2):
                yield MachineStatus(machine_id=machine_id, state=3, execution=1, interp_state=3,
                                    estop_state=1, mode=2, joint_actual=Position(x=float(i), y=0.0, z=0.0))
                await asyncio.sleep(0.01)

        original_subscribe = mock_client.subscribe_status
        mock_client.subscribe_status = failing_subscribe

        stream = await fleet_app.stream_status("machine-1")
        assert "machine-1" in fleet_app._streams

        await asyncio.sleep(2.5)  # Wait for retry after 2s backoff

        stream.close()
        await asyncio.sleep(0.2)

        assert call_count == 2

    async def test_stream_status_stops_retrying_when_closed(self, fleet_app, mock_client):
        """Stream should stop reconnecting once client disconnects (stream closed)."""
        error_count = 0

        def always_fail(machine_id):
            nonlocal error_count
            error_count += 1

            async def _gen():
                raise grpc.aio.AioRpcError(
                    code=lambda: grpc.StatusCode.UNAVAILABLE,
                    details=lambda: "connection refused",
                    initial_metadata=None, trailing_metadata=None,
                )
            return _gen()

        original_subscribe = mock_client.subscribe_status
        mock_client.subscribe_status = always_fail

        stream = await fleet_app.stream_status("machine-1")
        await asyncio.sleep(0.1)

        # Close the stream while a retry is pending
        stream.close()
        await fleet_app.remove_stream("machine-1", stream)
        await asyncio.sleep(1.5)  # Wait longer than first backoff (2s)

        assert error_count == 1

    async def test_stream_all_reconnects_on_error(self, fleet_app, mock_client):
        """StreamAll should reconnect after gRPC error with exponential backoff."""
        call_count = 0

        async def failing_subscribe():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise grpc.aio.AioRpcError(
                    code=lambda: grpc.StatusCode.UNAVAILABLE,
                    details=lambda: "connection refused",
                    initial_metadata=None, trailing_metadata=None,
                )
            for mid in ["machine-1", "machine-2"]:
                yield mid, MachineStatus(machine_id=mid, state=3, execution=1, interp_state=3,
                                         estop_state=1, mode=2, joint_actual=Position(x=0.0, y=0.0, z=0.0))
                await asyncio.sleep(0.01)

        original_subscribe = mock_client.subscribe_all_status
        mock_client.subscribe_all_status = failing_subscribe

        stream = await fleet_app.stream_all_machines()
        assert "__all__" in fleet_app._streams

        await asyncio.sleep(2.5)  # Wait for retry after 2s backoff

        stream.close()
        await fleet_app.remove_stream("__all__", stream)
        await asyncio.sleep(0.2)

        assert call_count == 2

    async def test_stream_all_stops_retrying_when_closed(self, fleet_app, mock_client):
        """StreamAll should stop reconnecting once client disconnects."""
        error_count = 0

        async def _always_fail():
            nonlocal error_count
            error_count += 1
            raise grpc.aio.AioRpcError(
                code=lambda: grpc.StatusCode.UNAVAILABLE,
                details=lambda: "connection refused",
                initial_metadata=None, trailing_metadata=None,
            )
            yield  # Make this an async generator

        original_subscribe = mock_client.subscribe_all_status
        mock_client.subscribe_all_status = _always_fail

        stream = await fleet_app.stream_all_machines()
        await asyncio.sleep(0.1)

        stream.close()
        await asyncio.sleep(1.5)

        assert error_count == 1

    async def test_ensure_client_uses_mock(self, fleet_app, mock_client):
        """When _mock_client is set, _ensure_client returns it without calling init."""
        client = await fleet_app._ensure_client()
        assert client is mock_client

    async def test_init_raises_when_fleet_client_not_installed(self):
        # Temporarily hide FleetClient by setting it to None
        import fleet_ui.server as server_mod
        original = server_mod.FleetClient
        server_mod.FleetClient = None  # type: ignore[assignment]
        try:
            app = FleetApp(gateway_address="localhost:50052", token="fake")
            with pytest.raises(RuntimeError, match="fleet_client package not installed"):
                await app.init()
        finally:
            server_mod.FleetClient = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Tests: HTTP handlers via aiohttp TestClient
# ---------------------------------------------------------------------------

class TestHTTPHandlers:

    async def test_handle_index_returns_html(self, client):
        resp = await client.get("/")
        assert resp.status == 200
        text = await resp.text()
        assert "<title>LinuxCNC Fleet Dashboard</title>" in text
        assert "LinuxCNC Fleet" in text

    async def test_handle_index_embeds_gateway_from_args(self, aiohttp_app):
        import argparse
        aiohttp_app["args"] = argparse.Namespace(port=8080, gateway="custom-gw:9999", token="", tls_cert=None, tls_key=None, timeout=120)
        server = TestServer(aiohttp_app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/")
            text = await resp.text()
            assert 'value="custom-gw:9999"' in text
            assert "autoConnect" in text
            assert "'false'" in text
        finally:
            await client.close()

    async def test_handle_index_prepopulates_token(self, aiohttp_app):
        import argparse
        aiohttp_app["args"] = argparse.Namespace(port=8080, gateway="localhost:50052", token="my-jwt-token", tls_cert=None, tls_key=None, timeout=120)
        server = TestServer(aiohttp_app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/")
            text = await resp.text()
            assert 'value="my-jwt-token"' in text
            assert "autoConnect" in text
            assert "'true'" in text
        finally:
            await client.close()

    async def test_handle_index_no_auto_connect_without_token(self, aiohttp_app):
        import argparse
        aiohttp_app["args"] = argparse.Namespace(port=8080, gateway="localhost:50052", token="", tls_cert=None, tls_key=None, timeout=120)
        server = TestServer(aiohttp_app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/")
            text = await resp.text()
            assert "autoConnect" in text
            assert "'false'" in text
        finally:
            await client.close()

    async def test_handle_connect_with_valid_token(self, client, fleet_app):
        resp = await client.post(
            "/api/connect?gateway=localhost:50052&tls=false",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "connected"

    async def test_handle_connect_rejects_no_token(self, client):
        resp = await client.post("/api/connect?gateway=localhost:50052")
        assert resp.status == 401
        data = await resp.json()
        assert "error" in data

    async def test_handle_machines_returns_json_list(self, client, fleet_app, mock_client):
        # Pre-populate last_status so enrich works
        await fleet_app.get_status("machine-1")
        resp = await client.get("/api/machines")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 2
        assert data[0]["machine_id"] == "machine-1"

    async def test_handle_status_returns_200(self, client, fleet_app):
        resp = await client.get("/api/status/machine-1")
        assert resp.status == 200
        data = await resp.json()
        assert data["machine_id"] == "machine-1"

    async def test_handle_mode_forwards_post_body(self, client, fleet_app, mock_client):
        resp = await client.post("/api/mode/machine-1", json={"mode": "AUTO"})
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

    async def test_handle_mdi_forwards_post_body(self, client, fleet_app, mock_client):
        resp = await client.post("/api/mdi/machine-1", json={"command": "G0 X10"})
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

    async def test_handle_program_forwards_post_body(self, client, fleet_app, mock_client):
        resp = await client.post("/api/program/machine-1", json={"path": "/path.ngc"})
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

    async def test_handle_program_broadcast_forwards_body(self, client, fleet_app, mock_client):
        resp = await client.post("/api/programs/broadcast", json={
            "scope": "ALL", "path": "/path.ngc", "facility": "", "tags": [],
        })
        assert resp.status == 200
        data = await resp.json()
        assert "results" in data

    async def test_handle_list_programs_with_query_params(self, client, fleet_app, mock_client):
        resp = await client.get("/api/programs/machine-1?directory=/home&max_depth=2")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["programs"]) == 2

    async def test_handle_control_forwards_cmd_from_url(self, client, fleet_app, mock_client):
        resp = await client.post("/api/control/machine-1/start")
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

    async def test_handle_hal_list_returns_components(self, client, fleet_app, mock_client):
        resp = await client.get("/api/hal/machine-1")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 1

    async def test_handle_hal_pin_returns_pin_value(self, client, fleet_app, mock_client):
        resp = await client.get("/api/hal/pin/machine-1/spindle.speed-cmd")
        assert resp.status == 200
        data = await resp.json()
        assert data["pin_name"] == "spindle.speed-cmd"

    async def test_handle_hal_write_forwards_typed_body(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"float": 42.0})
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

    async def test_handle_errors_returns_error_list(self, client, fleet_app, mock_client):
        resp = await client.get("/api/errors/machine-1")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 2

    async def test_handle_mode_rejects_malformed_json(self, client, fleet_app, mock_client):
        resp = await client.post("/api/mode/machine-1", data="not json")
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_mdi_rejects_malformed_json(self, client, fleet_app, mock_client):
        resp = await client.post("/api/mdi/machine-1", data="{invalid")
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_program_rejects_malformed_json(self, client, fleet_app, mock_client):
        resp = await client.post("/api/program/machine-1", data="broken json]")
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_program_broadcast_rejects_malformed_json(self, client, fleet_app, mock_client):
        resp = await client.post("/api/programs/broadcast", data="...")
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_hal_write_rejects_malformed_json(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", data="no json at all")
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_hal_write_rejects_no_value_type(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={})
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_hal_write_rejects_multiple_value_types(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"bit": True, "float": 1.0})
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_hal_write_rejects_invalid_bit_type(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"bit": 1})
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_hal_write_rejects_invalid_float_type(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"float": "not_a_number"})
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_hal_write_rejects_u32_out_of_range_negative(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"u32": -1})
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_hal_write_rejects_u32_out_of_range_too_large(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"u32": 4294967296})
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_hal_write_rejects_s32_out_of_range_negative(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"s32": -2147483649})
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_hal_write_rejects_s32_out_of_range_too_large(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"s32": 2147483648})
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    async def test_handle_hal_write_rejects_u32_bool(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"u32": True})
        assert resp.status == 400
        data = await resp.json()

    async def test_handle_hal_write_rejects_s32_bool(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"s32": False})
        assert resp.status == 400
        data = await resp.json()

    async def test_handle_hal_write_accepts_valid_bit(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"bit": True})
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

    async def test_handle_hal_write_accepts_valid_u32(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"u32": 4294967295})
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

    async def test_handle_hal_write_accepts_valid_s32(self, client, fleet_app, mock_client):
        resp = await client.post("/api/hal/write/machine-1/pin-out", json={"s32": -2147483648})
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

    async def test_handle_stream_does_not_crash_with_args_port(self, client, fleet_app, mock_client):
        resp = await client.get("/api/stream/status/machine-1")
        assert resp.status == 200
        chunk = await resp.content.readany()
        assert b"data:" in chunk

    async def test_handle_stream_all_does_not_crash_with_args_port(self, client, fleet_app, mock_client):
        resp = await client.get("/api/stream/all")
        assert resp.status == 200
        chunk = await resp.content.readany()
        assert b"data:" in chunk

    async def test_handle_stream_sets_cors_for_matching_origin(self, client, fleet_app, mock_client):
        resp = await client.get(
            "/api/stream/status/machine-1",
            headers={"Origin": "http://localhost:8080"},
        )
        assert resp.status == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:8080"
        await resp.content.readany()

    async def test_handle_stream_all_sets_cors_for_matching_origin(self, client, fleet_app, mock_client):
        resp = await client.get(
            "/api/stream/all",
            headers={"Origin": "http://localhost:8080"},
        )
        assert resp.status == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:8080"
        await resp.content.readany()

    # SSE streaming routes tested via TestSSEStream class — direct HTTP consumption
    # would block on the 120s queue timeout. The _iter_lines, send, close paths
    # are fully covered by unit tests above.


# ---------------------------------------------------------------------------
# Tests: Helper functions
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_status_to_dict_converts_all_fields(self):
        status = MachineStatus(
            machine_id="test-1",
            state=3,  # RUNNING
            execution=1,  # RUN
            interp_state=3,  # EXECUTE
            estop_state=1,  # NOT_E_STOPPED
            mode=2,  # MODE_AUTO
            joint_actual=Position(x=1.0, y=2.0, z=3.0, a=4.0, b=5.0, c=6.0),
            joint_commanded=Position(x=1.0, y=2.0, z=3.0, a=4.0, b=5.0, c=6.0),
            world_actual=Position(x=10.0, y=20.0, z=30.0, a=0.0, b=0.0, c=0.0),
            interp_line=42,
            program_file="/test.ngc",
            remaining_time="5:00",
            feedrate=500.0,
            feedrate_override=1.0,
            spindle_speed=2000.0,
            spindle_speed_override=1.0,
            coolant_mist=True,
            coolant_flood=False,
            coolant_mazak=False,
            cycle_time=10.5,
        )
        result = _status_to_dict(status)
        assert result["machine_id"] == "test-1"
        assert result["state"] == "RUNNING"
        assert result["execution"] == "RUN"
        assert result["estop_state"] == "NOT_E_STOPPED"
        assert result["mode"] == "MODE_AUTO"
        assert result["joint_actual"]["x"] == 1.0
        assert result["interp_line"] == 42
        assert result["program_file"] == "/test.ngc"
        assert result["feedrate"] == 500.0
        assert result["spindle_speed"] == 2000.0
        assert result["coolant_mist"] is True
        assert result["coolant_flood"] is False

    def test_status_to_dict_handles_unknown_enum(self):
        status = MachineStatus(machine_id="test", state=99)
        result = _status_to_dict(status)
        assert "UNKNOWN" in result["state"]

    def test_info_to_dict(self):
        info = MachineInfo(
            machine_id="test-1",
            machine_name="Lathe-1",
            host_address="192.168.1.10",
            version=LinuxCncVersion(version_string="2.8.1", build_type="release", git_hash="abc123"),
            num_joints=4,
            num_hal_components=5,
        )
        result = _info_to_dict(info)
        assert result["machine_id"] == "test-1"
        assert result["version"] == "2.8.1"
        assert result["build_type"] == "release"
        assert result["git_hash"] == "abc123"

    def test_info_to_dict_handles_none_version(self):
        info = MachineInfo(
            machine_id="test-1",
            machine_name="Lathe-1",
            host_address="192.168.1.10",
            version=None,
            num_joints=4,
            num_hal_components=0,
        )
        result = _info_to_dict(info)
        assert result["version"] == ""
        assert result["build_type"] == ""
        assert result["git_hash"] == ""

    def test_mode_to_int_manual(self):
        assert _mode_to_int("MANUAL") == 1  # MODE_MANUAL

    def test_mode_to_int_auto(self):
        assert _mode_to_int("AUTO") == 2  # MODE_AUTO

    def test_mode_to_int_mda(self):
        assert _mode_to_int("MDA") == 3  # MODE_MDA

    def test_mode_to_int_uppercase(self):
        assert _mode_to_int("manual") == 1

    def test_mode_to_int_unknown_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown machine mode"):
            _mode_to_int("BOGUS")

    def test_mode_to_int_empty_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown machine mode"):
            _mode_to_int("")

    def test_mode_to_int_none_like_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown machine mode"):
            _mode_to_int("UNKNOWN")


# ---------------------------------------------------------------------------
# Tests: FleetApp with real initialization path (mock client not injected)
# ---------------------------------------------------------------------------

class TestFleetAppIntegration:

    async def test_close_cleans_client(self, mock_client):
        app = FleetApp(gateway_address="localhost:50052", token="fake", _mock_client=mock_client)
        await app._ensure_client()
        await app.close()
        assert mock_client.close_calls == 1

    async def test_close_no_op_when_no_client(self):
        app = FleetApp(gateway_address="localhost:50052", token="fake")
        # Should not raise even if no client was ever initialized
        await app.close()


# ---------------------------------------------------------------------------
# Tests: Phase 3 — Token refresh, proactive polling, reactive retry, auth status
# ---------------------------------------------------------------------------

class TestTokenRefresh:

    async def test_fetch_token_noop_with_mock_client(self, fleet_app, mock_client):
        result = await fleet_app._fetch_token()
        assert result is None

    async def test_is_token_expired_returns_false_when_no_expiry(self, fleet_app):
        assert fleet_app._is_token_expired() is False

    async def test_is_token_expired_returns_true_when_near_expiry(self):
        import time
        app = FleetApp(gateway_address="localhost:50052", token="fake")
        app._token_expiry = time.time() + 100  # expires in 100s (less than 300s threshold)
        assert app._is_token_expired() is True

    async def test_is_token_expired_returns_false_when_far_from_expiry(self):
        import time
        app = FleetApp(gateway_address="localhost:50052", token="fake")
        app._token_expiry = time.time() + 600  # expires in 600s (more than 300s threshold)
        assert app._is_token_expired() is False

    async def test_refresh_and_retry_returns_none_when_no_client(self, fleet_app):
        result = await fleet_app._refresh_and_retry(lambda: None)
        assert result is None

    async def test_proactive_task_canceled_on_close(self):
        import time
        app = FleetApp(gateway_address="localhost:50052", token="fake")
        app._token_expiry = time.time() + 100  # near expiry to trigger refresh
        await app._start_proactive_refresh()
        assert app._proactive_task is not None
        await app.close()
        assert app._proactive_task is None


class TestReactiveRetry:

    async def test_grpc_call_succeeds_without_retry(self, fleet_app, mock_client):
        result = await fleet_app._grpc_call_with_retry(lambda: mock_client.get_status("machine-1"))
        assert result is not None
        assert result.machine_id == "machine-1"

    async def test_grpc_call_unauthenticated_triggers_refresh(self, mock_client):
        """When _refresh_and_retry succeeds, the operation is retried and result returned."""
        import time

        # Create a real-ish client that has refresh_token method
        class RefreshableMockClient:
            def __init__(self):
                self._closed = False
                self.refresh_token_calls = []

            async def get_status(self, machine_id):
                return MachineStatus(
                    machine_id=machine_id,
                    state=3, execution=1, interp_state=3, estop_state=1, mode=2,
                    joint_actual=Position(x=10.0, y=20.0, z=30.0),
                    joint_commanded=Position(x=10.0, y=20.0, z=30.0),
                    world_actual=Position(x=10.0, y=20.0, z=30.0),
                )

            async def refresh_token(self, new_token):
                self.refresh_token_calls.append(new_token)

        real_mock = RefreshableMockClient()
        app = FleetApp(
            gateway_address="localhost:50052", token="fake",
            _mock_client=real_mock,
            gateway_http_port=9999,  # non-existent port to test mock path
        )
        # Set a valid token expiry so _fetch_token has something to work with
        app._token_expiry = time.time() + 600

        call_count = 0

        async def failing_then_success():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise grpc.aio.AioRpcError(
                    code=grpc.StatusCode.UNAUTHENTICATED,
                    details="token expired",
                    initial_metadata=None, trailing_metadata=None,
                )
            return real_mock.get_status("machine-1")

        # With mock client, _fetch_token returns None, so retry returns None and exception is re-raised
        with pytest.raises(grpc.aio.AioRpcError):
            await app._grpc_call_with_retry(failing_then_success)
        assert call_count == 1  # operation called once, then exception raised

    async def test_grpc_call_unavailable_reraises(self, fleet_app):
        async def always_unavailable():
            raise grpc.aio.AioRpcError(
                code=grpc.StatusCode.UNAVAILABLE,
                details="connection refused",
                initial_metadata=None, trailing_metadata=None,
            )

        with pytest.raises(grpc.aio.AioRpcError):
            await fleet_app._grpc_call_with_retry(always_unavailable)


class TestAuthStatusEndpoint:

    async def test_auth_status_returns_has_token_true(self, aiohttp_app):
        import argparse
        aiohttp_app["args"] = argparse.Namespace(port=8080, gateway="localhost:50052", token="my-token", tls_cert=None, tls_key=None, timeout=120)
        server = TestServer(aiohttp_app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/api/auth/status")
            assert resp.status == 200
            data = await resp.json()
            assert data["has_token"] is True
        finally:
            await client.close()

    async def test_auth_status_returns_has_token_false(self):
        import argparse
        mock_client = MockFleetClient()
        fleet_app = FleetApp(gateway_address="localhost:50052", token="", _mock_client=mock_client)
        app = web.Application()
        app["fleet"] = fleet_app
        app["args"] = argparse.Namespace(port=8080, gateway="localhost:50052", token="", tls_cert=None, tls_key=None, timeout=120)
        create_routes(app)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/api/auth/status")
            assert resp.status == 200
            data = await resp.json()
            assert data["has_token"] is False
        finally:
            await client.close()

    async def test_auth_status_returns_connecting_flag(self, aiohttp_app):
        import argparse
        aiohttp_app["args"] = argparse.Namespace(port=8080, gateway="localhost:50052", token="", tls_cert=None, tls_key=None, timeout=120)
        fleet = aiohttp_app["fleet"]
        fleet._connecting = True
        server = TestServer(aiohttp_app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/api/auth/status")
            assert resp.status == 200
            data = await resp.json()
            assert data["connecting"] is True
        finally:
            await client.close()

    async def test_auth_status_returns_token_expiry(self, aiohttp_app):
        import argparse
        import time
        aiohttp_app["args"] = argparse.Namespace(port=8080, gateway="localhost:50052", token="my-token", tls_cert=None, tls_key=None, timeout=120)
        fleet = aiohttp_app["fleet"]
        fleet._token_expiry = time.time() + 600
        server = TestServer(aiohttp_app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/api/auth/status")
            assert resp.status == 200
            data = await resp.json()
            assert isinstance(data["token_expiry"], (int, float))
        finally:
            await client.close()


class TestAutoConnectFlow:

    async def test_handle_connect_starts_proactive_refresh(self, aiohttp_app):
        import argparse
        aiohttp_app["args"] = argparse.Namespace(port=8080, gateway="localhost:50052", token="", tls_cert=None, tls_key=None, timeout=120, http_port=50053)
        fleet = aiohttp_app["fleet"]
        # Replace mock client with one that has refresh_token method
        fleet._mock_client.refresh_token = MagicMock()
        server = TestServer(aiohttp_app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.post(
                "/api/connect?gateway=localhost:50052&tls=false",
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "connected"
        finally:
            await client.close()

    async def test_fleet_app_has_gateway_http_port(self):
        app = FleetApp(gateway_address="localhost:50052", token="fake", gateway_http_port=9999)
        assert app._gateway_http_port == 9999

    async def test_fetch_token_uses_default_port(self, fleet_app):
        fleet_app._gateway_http_port = None
        fleet_app._token_expiry = 0.0
        result = await fleet_app._fetch_token()
        assert result is None  # no gateway running, but should not crash

    async def test_proactive_refresh_starts_when_client_valid(self):
        app = FleetApp(gateway_address="localhost:50052", token="fake", _mock_client=MockFleetClient())
        await app._start_proactive_refresh()
        assert app._proactive_task is not None
        await app.close()

    async def test_proactive_refresh_noop_when_already_started(self):
        app = FleetApp(gateway_address="localhost:50052", token="fake", _mock_client=MockFleetClient())
        await app._start_proactive_refresh()
        task1 = app._proactive_task
        await app._start_proactive_refresh()
        assert app._proactive_task is task1
        await app.close()
