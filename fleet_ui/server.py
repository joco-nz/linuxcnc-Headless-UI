"""LinuxCNC Fleet Dashboard — aiohttp web UI."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from typing import Any, Optional

import aiohttp
from aiohttp import web

try:
    from fleet_client import FleetClient, MachineEntry
except ImportError:
    FleetClient = None  # type: ignore
    MachineEntry = None  # type: ignore

log = logging.getLogger(__name__)


# ── SSE Stream Manager ───────────────────────────────────────────────────────

class _SSEStream:
    """Manages a single SSE client connection."""

    def __init__(self, request: web.Request | None, machine_id: str = "") -> None:
        self._request = request
        self.machine_id = machine_id
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
        self._done = False

    async def send(self, data: dict) -> None:
        if not self._done:
            try:
                self._queue.put_nowait(data)
            except asyncio.QueueFull:
                log.warning("SSE queue full for %s, dropping update", self.machine_id)

    async def _iter_lines(self) -> Any:
        while not self._done:
            try:
                data = await asyncio.wait_for(self._queue.get(), timeout=120.0)
                yield f"data: {json.dumps(data)}\n\n"
            except Exception:
                break

    def close(self) -> None:
        self._done = True


# ── Fleet App ────────────────────────────────────────────────────────────────

class FleetApp:
    """Application state — wraps FleetClient and manages streams."""

    def __init__(
        self,
        gateway_address: str,
        token: str,
        tls_enabled: bool = False,
        _mock_client: Any = None,
    ) -> None:
        self._gateway_address = gateway_address
        self._token = token
        self._tls_enabled = tls_enabled
        self._mock_client = _mock_client
        self._client: Optional[FleetClient] = None
        self._machines: list[MachineEntry] = []
        self._streams: dict[str, _SSEStream] = {}  # machine_id -> stream
        self._last_status: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        if self._mock_client is not None:
            return
        if FleetClient is None:
            raise RuntimeError("fleet_client package not installed")
        self._client = FleetClient(
            gateway_address=self._gateway_address,
            token=self._token,
            tls_enabled=self._tls_enabled,
        )

    async def close(self) -> None:
        client = self._mock_client if self._mock_client is not None else self._client
        if client:
            await client.close()

    async def _ensure_client(self) -> FleetClient:
        if self._mock_client is not None:
            return self._mock_client  # type: ignore[return-value]
        if self._client is None or getattr(self._client, '_closed', False):
            await self.init()
        return self._client

    # ── Machine Discovery ────────────────────────────────────────────────

    async def discover_machines(self) -> list[dict]:
        client = await self._ensure_client()
        try:
            machines = await client.get_machines()
            async with self._lock:
                self._machines = machines
            return [
                {
                    "machine_id": m.machine_id,
                    "machine_name": m.machine_name,
                    "host_address": m.host_address,
                    "version": m.version or "",
                    "num_joints": m.num_joints,
                    "num_hal_components": m.num_hal_components,
                }
                for m in machines
            ]
        except Exception as e:
            log.error("Discover failed: %s", e)
            return []

    async def get_status(self, machine_id: str) -> Optional[dict]:
        client = await self._ensure_client()
        try:
            status = await client.get_status(machine_id)
            result = _status_to_dict(status)
            async with self._lock:
                self._last_status[machine_id] = result
            return result
        except Exception as e:
            log.error("GetStatus %s failed: %s", machine_id, e)
            return None

    async def get_machine_info(self, machine_id: str) -> Optional[dict]:
        client = await self._ensure_client()
        try:
            info = await client.get_machine_info(machine_id)
            return _info_to_dict(info)
        except Exception as e:
            log.error("GetInfo %s failed: %s", machine_id, e)
            return None

    async def set_mode(self, machine_id: str, mode: str) -> dict:
        client = await self._ensure_client()
        try:
            result = await client.set_mode(machine_id, _mode_to_int(mode))
            return {"success": result.success, "message": result.message}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def send_mdi(self, machine_id: str, command: str) -> dict:
        client = await self._ensure_client()
        try:
            result = await client.send_mdi(machine_id, command)
            return {"success": result.success, "message": result.message}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def load_program(self, machine_id: str, path: str) -> dict:
        client = await self._ensure_client()
        try:
            result = await client.load_program(machine_id, path)
            return {"success": result.success, "message": result.message}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def broadcast_load_program(self, scope: str, path: str,
                                      facility: str = "", tags: list[str] | None = None) -> dict:
        client = await self._ensure_client()
        try:
            results = await client.broadcast_load_program(scope, path, facility, tags or [])
            return {"results": {mid: {"success": s, "message": m} for mid, (s, m) in results.items()}}
        except Exception as e:
            return {"error": str(e)}

    async def list_programs(self, machine_id: str, directory: str = "", max_depth: int = 0) -> dict:
        client = await self._ensure_client()
        try:
            pl = await client.list_programs(machine_id, directory, max_depth)
            return {
                "programs": [
                    {"path": p.path, "name": p.name, "size_bytes": p.size_bytes, "modified_time": p.modified_time}
                    for p in pl.programs
                ],
                "total": pl.total_count,
            }
        except Exception as e:
            return {"error": str(e)}

    async def control(self, machine_id: str, cmd: str) -> dict:
        client = await self._ensure_client()
        try:
            if cmd == "start":
                result = await client.start(machine_id)
            elif cmd == "stop":
                result = await client.stop(machine_id)
            elif cmd == "feed_hold":
                result = await client.feed_hold(machine_id)
            elif cmd == "continue_exec":
                result = await client.continue_exec(machine_id)
            elif cmd == "home_all":
                result = await client.home_all(machine_id)
            else:
                return {"success": False, "message": f"Unknown control: {cmd}"}
            return {"success": result.success, "message": result.message}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def list_hal(self, machine_id: str) -> Optional[list[dict]]:
        client = await self._ensure_client()
        try:
            hal_list = await client.list_hal_components(machine_id)
            components = []
            for comp in hal_list.components:
                pins = [
                    {
                        "name": p.name,
                        "type": _hal_type_to_str(p.type),
                        "is_output": p.is_output,
                        "value_f": p.value_f,
                        "value_u32": p.value_u32,
                        "value_s32": p.value_s32,
                        "value_bit": p.value_bit,
                    }
                    for p in comp.pins
                ]
                components.append({
                    "name": comp.name,
                    "update_period_ns": comp.update_period_ns,
                    "pins": pins,
                })
            return components
        except Exception as e:
            if "_hal" in str(e):
                return None
            log.error("ListHAL %s failed: %s", machine_id, e)
            return None

    async def read_hal_pin(self, machine_id: str, pin_name: str) -> Optional[dict]:
        client = await self._ensure_client()
        try:
            pin = await client.read_hal_pin(machine_id, pin_name)
            return {
                "pin_name": pin.pin_name,
                "type": _hal_type_to_str(pin.type),
                "value_f": pin.value_f,
                "value_u32": pin.value_u32,
                "value_s32": pin.value_s32,
                "value_bit": pin.value_bit,
            }
        except Exception as e:
            log.error("ReadPin %s failed: %s", pin_name, e)
            return None

    async def write_hal_pin(self, machine_id: str, pin_name: str, **kwargs) -> dict:
        client = await self._ensure_client()
        try:
            bit_val = kwargs.get("bit")
            float_val = kwargs.get("float")
            u32_val = kwargs.get("u32")
            s32_val = kwargs.get("s32")
            result = await client.write_hal_pin(
                machine_id, pin_name,
                bit_value=bit_val,
                float_value=float_val,
                u32_value=u32_val,
                s32_value=s32_val,
            )
            return {"success": result.success, "message": result.message}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def get_errors(self, machine_id: str) -> list[dict]:
        client = await self._ensure_client()
        try:
            err_list = await client.get_errors(machine_id, limit=50)
            return [
                {"message": e.message, "timestamp": e.timestamp}
                for e in err_list.errors
            ]
        except Exception as e:
            log.error("GetErrors %s failed: %s", machine_id, e)
            return []

    # ── SSE Streaming ────────────────────────────────────────────────────

    async def stream_status(self, machine_id: str) -> _SSEStream:
        app = self
        client = await self._ensure_client()

        async def _stream_loop() -> None:
            try:
                async for status in client.subscribe_status(machine_id):
                    data = _status_to_dict(status)
                    async with self._lock:
                        self._last_status[machine_id] = data
                    stream = app._streams.get(machine_id)
                    if stream is not None:
                        await stream.send(data)
            except Exception as e:
                log.error("Stream %s error: %s", machine_id, e)

        stream = _SSEStream(None, machine_id=machine_id)
        async with self._lock:
            self._streams[machine_id] = stream

        asyncio.create_task(_stream_loop())
        return stream

    def remove_stream(self, machine_id: str) -> None:
        if machine_id in self._streams:
            self._streams[machine_id].close()
            del self._streams[machine_id]

    async def stream_all_machines(self) -> _SSEStream:
        app = self
        client = await self._ensure_client()

        # Discover machines first
        machines_data = await self.discover_machines()
        machine_ids = [m["machine_id"] for m in machines_data]

        async def _stream_loop() -> None:
            try:
                async for machine_id, status in client.subscribe_all_status():
                    data = _status_to_dict(status)
                    async with self._lock:
                        self._last_status[machine_id] = data
                    stream = app._streams.get("__all__")
                    if stream is not None:
                        await stream.send(data)
            except Exception as e:
                log.error("StreamAll error: %s", e)

        stream = _SSEStream(None, machine_id="__all__")
        async with self._lock:
            self._streams["__all__"] = stream

        asyncio.create_task(_stream_loop())
        return stream

    def remove_all_stream(self) -> None:
        if "__all__" in self._streams:
            self._streams["__all__"].close()
            del self._streams["__all__"]

    async def get_last_status(self, machine_id: str) -> Optional[dict]:
        async with self._lock:
            return self._last_status.get(machine_id)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _status_to_dict(status: Any) -> dict:
    from linuxcnc_fleet.fleet_pb2 import Mode, ExecutionState, MachineState, InterpState, EstopState

    return {
        "machine_id": status.machine_id,
        "state": _enum_name(MachineState, status.state),
        "execution": _enum_name(ExecutionState, status.execution),
        "interp_state": _enum_name(InterpState, status.interp_state),
        "estop_state": _enum_name(EstopState, status.estop_state),
        "mode": _enum_name(Mode, status.mode),
        "joint_actual": {
            "x": status.joint_actual.x,
            "y": status.joint_actual.y,
            "z": status.joint_actual.z,
            "a": status.joint_actual.a,
            "b": status.joint_actual.b,
            "c": status.joint_actual.c,
        },
        "joint_commanded": {
            "x": status.joint_commanded.x,
            "y": status.joint_commanded.y,
            "z": status.joint_commanded.z,
            "a": status.joint_commanded.a,
            "b": status.joint_commanded.b,
            "c": status.joint_commanded.c,
        },
        "world_actual": {
            "x": status.world_actual.x,
            "y": status.world_actual.y,
            "z": status.world_actual.z,
            "a": status.world_actual.a,
            "b": status.world_actual.b,
            "c": status.world_actual.c,
        },
        "interp_line": status.interp_line,
        "program_file": status.program_file,
        "remaining_time": status.remaining_time,
        "feedrate": status.feedrate,
        "feedrate_override": status.feedrate_override,
        "spindle_speed": status.spindle_speed,
        "spindle_speed_override": status.spindle_speed_override,
        "coolant_mist": status.coolant_mist,
        "coolant_flood": status.coolant_flood,
        "coolant_mazak": status.coolant_mazak,
        "active_errors": list(status.active_errors),
        "cycle_time": status.cycle_time,
    }


def _info_to_dict(info: Any) -> dict:
    return {
        "machine_id": info.machine_id,
        "machine_name": info.machine_name,
        "host_address": info.host_address,
        "version": info.version.version_string if info.version else "",
        "build_type": info.version.build_type if info.version else "",
        "git_hash": info.version.git_hash if info.version else "",
        "num_joints": info.num_joints,
        "num_hal_components": info.num_hal_components,
    }


def _enum_name(enum_cls: Any, value: int) -> str:
    try:
        return enum_cls.Name(value)
    except ValueError:
        return f"UNKNOWN({value})"


def _mode_to_int(mode: str) -> int:
    from linuxcnc_fleet.fleet_pb2 import Mode

    mapping = {
        "MANUAL": Mode.MODE_MANUAL,
        "AUTO": Mode.MODE_AUTO,
        "MDA": Mode.MODE_MDA,
    }
    return mapping.get(mode.upper(), 0)


def _hal_type_to_str(t: int) -> str:
    try:
        from linuxcnc_fleet.fleet_pb2 import HalPinType
        return HalPinType.Name(t)
    except Exception:
        return f"UNKNOWN({t})"


# ── HTML Template ────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LinuxCNC Fleet Dashboard</title>
<style>
:root {
  --bg-primary: #0d1117;
  --bg-secondary: #161b22;
  --bg-tertiary: #21262d;
  --border: #30363d;
  --text-primary: #e6edf3;
  --text-secondary: #8b949e;
  --accent-blue: #58a6ff;
  --accent-green: #3fb950;
  --accent-red: #f85149;
  --accent-yellow: #d29922;
  --accent-purple: #bc8cff;
  --running: #3fb950;
  --paused: #d29922;
  --stopped: #8b949e;
  --estop: #f85149;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg-primary);
  color: var(--text-primary);
  height: 100vh;
  display: flex;
  flex-direction: column;
}

header {
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
  padding: 12px 20px;
  display: flex;
  align-items: center;
  gap: 16px;
}

header h1 { font-size: 18px; font-weight: 600; }
header .subtitle { color: var(--text-secondary); font-size: 13px; }

main {
  flex: 1;
  display: flex;
  overflow: hidden;
}

/* Sidebar */
.sidebar {
  width: 240px;
  min-width: 240px;
  background: var(--bg-secondary);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
}

.sidebar-header {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.machine-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.machine-item {
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;
  transition: background 0.15s;
}

.machine-item:hover { background: var(--bg-tertiary); }
.machine-item.active { background: var(--bg-tertiary); border: 1px solid var(--accent-blue); }

.machine-checkbox { width: 16px; height: 16px; accent-color: var(--accent-blue); cursor: pointer; flex-shrink: 0; }
.selected-count { padding: 8px 12px; font-size: 12px; color: var(--text-secondary); border-top: 1px solid var(--border); text-align: center; }
.selected-count span { color: var(--accent-blue); font-weight: 600; }

.program-load-row { display: flex; gap: 8px; align-items: center; }
.program-load-row input[type="text"] { flex: 1; min-width: 0; }
.broadcast-result-item { padding: 4px 0; font-size: 13px; border-bottom: 1px solid var(--border); }
.broadcast-result-item.success { color: var(--accent-green); }
.broadcast-result-item.error { color: var(--accent-red); }
.program-browser { max-height: 300px; overflow-y: auto; background: var(--bg-tertiary); border-radius: 6px; padding: 8px; margin-top: 8px; }
.program-entry { padding: 6px 8px; cursor: pointer; border-radius: 4px; font-size: 13px; display: flex; justify-content: space-between; align-items: center; }
.program-entry:hover { background: var(--bg-secondary); }
.program-name { font-family: 'SF Mono', 'Fira Code', monospace; color: var(--accent-blue); }
.program-meta { font-size: 11px; color: var(--text-secondary); }

.machine-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}

.machine-dot.running { background: var(--running); box-shadow: 0 0 6px var(--running); }
.machine-dot.paused { background: var(--paused); }
.machine-dot.stopped { background: var(--stopped); }
.machine-dot.estop { background: var(--estop); box-shadow: 0 0 8px var(--estop); animation: pulse 1s infinite; }

@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }

.machine-name { font-size: 14px; font-weight: 500; }
.machine-detail { font-size: 11px; color: var(--text-secondary); margin-top: 2px; }

/* Content */
.content {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
}

.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-secondary);
  font-size: 16px;
}

/* Tabs */
.tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0;
}

.tab-btn {
  background: none;
  border: none;
  color: var(--text-secondary);
  padding: 8px 16px;
  cursor: pointer;
  font-size: 14px;
  border-bottom: 2px solid transparent;
  transition: all 0.15s;
}

.tab-btn:hover { color: var(--text-primary); }
.tab-btn.active { color: var(--accent-blue); border-bottom-color: var(--accent-blue); }

.tab-panel { display: none; }
.tab-panel.active { display: block; }

/* Modal */
.modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 1000; display: flex; align-items: center; justify-content: center; }
.modal-overlay.hidden { display: none; }
.modal { background: var(--bg-primary); border-radius: 12px; width: 90%; max-width: 600px; max-height: 80vh; display: flex; flex-direction: column; box-shadow: 0 20px 60px rgba(0,0,0,0.3); }
.modal-header { padding: 16px 20px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
.modal-body { padding: 20px; overflow-y: auto; flex: 1; }
.close-btn { background: none; border: none; font-size: 24px; cursor: pointer; color: var(--text-secondary); padding: 0 4px; }
.close-btn:hover { color: var(--text-primary); }

/* Status cards */
.status-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 16px;
}

.status-card {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
}

.status-card .label {
  font-size: 12px;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 6px;
}

.status-card .value {
  font-size: 22px;
  font-weight: 600;
}

.status-card .value.running { color: var(--running); }
.status-card .value.paused { color: var(--paused); }
.status-card .value.estop { color: var(--estop); }

/* Controls */
.control-group {
  margin-bottom: 24px;
}

.control-group h3 {
  font-size: 14px;
  color: var(--text-secondary);
  margin-bottom: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.mode-buttons, .control-buttons {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.btn {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  color: var(--text-primary);
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.15s;
}

.btn:hover { background: var(--border); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }

.btn.primary { background: #1f6feb; border-color: #1f6feb; }
.btn.primary:hover { background: #388bfd; }
.btn.danger { background: rgba(248,81,73,0.15); border-color: var(--accent-red); color: var(--accent-red); }
.btn.danger:hover { background: rgba(248,81,73,0.25); }

.mdi-input-row {
  display: flex;
  gap: 8px;
}

.mdi-input-row input {
  flex: 1;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  color: var(--text-primary);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 14px;
  font-family: 'SF Mono', 'Fira Code', monospace;
}

.mdi-input-row input:focus { outline: none; border-color: var(--accent-blue); }

/* HAL table */
.hal-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.hal-table th {
  text-align: left;
  padding: 8px 12px;
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  font-weight: 500;
  border-bottom: 1px solid var(--border);
}

.hal-table td {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
}

.hal-component-header {
  background: var(--bg-tertiary);
  font-weight: 600;
  color: var(--accent-purple);
}

/* Error log */
.error-log {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  max-height: 400px;
  overflow-y: auto;
}

.error-entry {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 13px;
  color: var(--accent-red);
}

.error-entry:last-child { border-bottom: none; }

/* Toast */
.toast-container {
  position: fixed;
  bottom: 20px;
  right: 20px;
  z-index: 1000;
}

.toast {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 20px;
  margin-top: 8px;
  font-size: 14px;
  animation: slideIn 0.3s ease;
}

.toast.success { border-left: 3px solid var(--accent-green); }
.toast.error { border-left: 3px solid var(--accent-red); }

@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

/* Position display */
.position-row {
  display: flex;
  gap: 2px;
  font-family: 'SF Mono', 'Fira Code', monospace;
}

.pos-val {
  background: var(--bg-tertiary);
  padding: 6px 10px;
  border-radius: 4px;
  font-size: 13px;
  min-width: 60px;
  text-align: center;
}

.pos-label {
  font-size: 10px;
  color: var(--text-secondary);
  text-align: center;
  margin-top: 2px;
}

/* Config form */
.config-form {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 20px;
  max-width: 600px;
}

.config-form label {
  display: block;
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 4px;
  margin-top: 12px;
}

.config-form input, .config-form select {
  width: 100%;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  color: var(--text-primary);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 14px;
}

.config-form input:focus, .config-form select:focus { outline: none; border-color: var(--accent-blue); }

.hidden { display: none !important; }
</style>
</head>
<body>

<header id="header">
  <h1>LinuxCNC Fleet</h1>
  <span class="subtitle" id="gateway-status">Connecting...</span>
</header>

<main id="main-view">
  <!-- Config view -->
  <div id="config-view" class="content">
    <div class="config-form">
      <h2 style="margin-bottom: 8px;">Connect to Fleet Gateway</h2>
      <label>Gateway Address</label>
      <input id="cfg-gateway" type="text" value="localhost:50052" placeholder="host:port">

      <label>JWT Token</label>
      <input id="cfg-token" type="text" placeholder="eyJhbGciOiJIUzI1NiIs...">

      <label>TLS Enabled</label>
      <select id="cfg-tls">
        <option value="false">No (insecure)</option>
        <option value="true">Yes</option>
      </select>

      <button class="btn primary" onclick="connect()" style="margin-top: 20px; width: 100%;">Connect</button>
    </div>
  </div>

  <!-- Dashboard view -->
  <div id="dashboard-view" class="hidden">
   <aside class="sidebar">
      <div class="sidebar-header">Machines (<span id="machine-count">0</span>)</div>
      <div class="machine-list" id="machine-list"></div>
      <div class="selected-count" id="selected-count" style="display:none">
        <span id="selected-count-num">0</span> machines selected
      </div>
    </aside>

    <div class="content" id="content-area">
      <div class="empty-state" id="no-machine-selected">Select a machine to view details</div>

      <div id="machine-detail" class="hidden">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
          <h2 id="detail-title">Machine Name</h2>
          <span id="detail-status" style="font-size: 14px;"></span>
        </div>

        <div class="tabs" id="tab-bar">
          <button class="tab-btn active" data-tab="status-tab">Status</button>
          <button class="tab-btn" data-tab="controls-tab">Controls</button>
          <button class="tab-btn" data-tab="hal-tab">HAL Pins</button>
          <button class="tab-btn" data-tab="errors-tab">Errors</button>
        </div>

        <!-- Status Tab -->
        <div class="tab-panel active" id="status-tab">
          <div class="status-grid" id="status-cards"></div>
          <h3 style="margin: 20px 0 10px; font-size: 14px; color: var(--text-secondary);">Joint Positions</h3>
          <div class="position-row" id="joint-positions"></div>
          <h3 style="margin: 20px 0 10px; font-size: 14px; color: var(--text-secondary);">World Coordinates</h3>
          <div class="position-row" id="world-coords"></div>
        </div>

        <!-- Controls Tab -->
        <div class="tab-panel" id="controls-tab">
          <div class="control-group">
            <h3>Mode Selection</h3>
            <div class="mode-buttons" id="mode-buttons"></div>
          </div>
          <div class="control-group">
            <h3>MDI Command</h3>
            <div class="mdi-input-row">
              <input id="mdi-input" type="text" placeholder="G0 X10 Y5 Z2" onkeydown="if(event.key==='Enter')sendMdi()">
              <button class="btn primary" onclick="sendMdi()">Execute</button>
            </div>
          </div>
          <div class="control-group">
            <h3>Program Load</h3>
            <div class="mdi-input-row">
              <input id="program-path" type="text" placeholder="/path/to/file.ngc" onkeydown="if(event.key==='Enter')loadProgram()">
              <button class="btn" onclick="loadProgram()">Load</button>
            </div>
          </div>
          <div class="control-group">
            <h3>Motion Controls</h3>
            <div class="control-buttons" id="motion-buttons"></div>
          </div>
          <div class="control-group">
            <h3>Broadcast Program Load</h3>
            <div class="program-load-row">
              <input id="broadcast-program-path" type="text" placeholder="/path/to/file.ngc" onkeydown="if(event.key==='Enter')broadcastLoadProgram()">
              <button class="btn primary" onclick="broadcastLoadProgram()">Broadcast</button>
            </div>
            <div style="margin-top: 8px;">
              <button class="btn" onclick="openProgramBrowser()" style="font-size: 13px;">Browse Programs...</button>
            </div>
            <div id="broadcast-results" class="hidden" style="margin-top: 12px;"></div>
          </div>
        </div>

        <!-- HAL Pins Tab -->
        <div class="tab-panel" id="hal-tab">
          <div id="hal-content">
            <div class="empty-state">Loading...</div>
          </div>
        </div>

        <!-- Errors Tab -->
        <div class="tab-panel" id="errors-tab">
          <div class="error-log" id="error-log">
            <div class="empty-state">No errors</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</main>

<!-- Program Browser Modal -->
<div id="program-modal" class="modal-overlay hidden" onclick="if(event.target===this)closeProgramBrowser()">
  <div class="modal">
    <div class="modal-header">
      <h3 style="margin:0;font-size:18px;">Program Browser</h3>
      <button class="close-btn" onclick="closeProgramBrowser()">&times;</button>
    </div>
    <div class="modal-body">
      <div style="display:flex;gap:8px;margin-bottom:12px;">
        <input id="program-browser-target" type="text" placeholder="Machine ID" style="flex:1;">
        <button class="btn" onclick="refreshProgramBrowser()">Refresh</button>
      </div>
      <div id="program-browser-content">
        <div class="empty-state">Select a machine and click Refresh</div>
      </div>
    </div>
  </div>
</div>

<div class="toast-container" id="toast-container"></div>

<script>
// ── State ────────────────────────────────────────────────────────────────────
let fleet = { connected: false, client: null };
let selectedMachine = null;
let selectedMachines = new Set();
let lastSelectedIndex = -1;
let eventSources = {};

// ── Config ───────────────────────────────────────────────────────────────────
async function connect() {
  const gateway = document.getElementById('cfg-gateway').value.trim();
  const token = document.getElementById('cfg-token').value.trim();
  const tls = document.getElementById('cfg-tls').value === 'true';

  if (!gateway || !token) {
    showToast('Enter gateway address and JWT token', 'error');
    return;
  }

  try {
    const resp = await fetch(`/api/connect?gateway=${encodeURIComponent(gateway)}&tls=${tls}`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${token}` },
    });
    if (!resp.ok) throw new Error(await resp.text());
    fleet.connected = true;
    document.getElementById('config-view').classList.add('hidden');
    document.getElementById('dashboard-view').classList.remove('hidden');
    document.getElementById('gateway-status').textContent = `Gateway: ${gateway}`;
    await refreshMachines();
  } catch (e) {
    showToast(`Connection failed: ${e.message}`, 'error');
  }
}

// ── Machine List ─────────────────────────────────────────────────────────────
async function refreshMachines() {
  if (!fleet.connected) return;
  try {
    const resp = await fetch('/api/machines');
    const machines = await resp.json();
    document.getElementById('machine-count').textContent = machines.length;

    const list = document.getElementById('machine-list');
    list.innerHTML = '';
    machines.forEach((m, idx) => {
      const lastStatus = m._last_status || {};
      const state = (lastStatus.state || '').toLowerCase();
      const dotClass = state === 'running' ? 'running' : state === 'paused' ? 'paused' : state === 'e_stopped' ? 'estop' : 'stopped';
      const isChecked = selectedMachines.has(m.machine_id);

      const item = document.createElement('div');
      item.className = `machine-item${m.machine_id === selectedMachine ? ' active' : ''}`;
      item.setAttribute('data-index', idx);
      item.setAttribute('data-id', m.machine_id);
      item.onclick = (e) => handleMachineClick(e, m.machine_id, idx);
      item.innerHTML = `
        <input type="checkbox" class="machine-checkbox" ${isChecked ? 'checked' : ''} onclick="event.stopPropagation();toggleSelect('${m.machine_id}', event)" onkeydown="event.stopPropagation()">
        <div class="machine-dot ${dotClass}"></div>
        <div>
          <div class="machine-name">${m.machine_name || m.machine_id}</div>
          <div class="machine-detail">${lastStatus.mode || 'unknown'} · ${lastStatus.estop_state === 'E_STOPPED' ? 'E-STOP' : 'OK'}</div>
        </div>`;
      list.appendChild(item);
    });
  } catch (e) {
    console.error('refreshMachines:', e);
  }
}

// ── Multi-Select ─────────────────────────────────────────────────────────────
function handleMachineClick(e, machineId, idx) {
  if (e.ctrlKey || e.metaKey) {
    toggleSelect(machineId);
  } else if (e.shiftKey && lastSelectedIndex >= 0) {
    rangeSelect(lastSelectedIndex, idx);
  } else {
    selectMachine(machineId);
    selectedMachines.clear();
    lastSelectedIndex = idx;
    updateSelectedCount();
    refreshMachineListCheckboxes();
  }
}

function toggleSelect(machineId, e) {
  if (selectedMachines.has(machineId)) {
    selectedMachines.delete(machineId);
  } else {
    selectedMachines.add(machineId);
  }
  lastSelectedIndex = parseInt(e?.target?.closest('.machine-item')?.getAttribute('data-index') || '-1');
  updateSelectedCount();
  refreshMachineListCheckboxes();
}

function rangeSelect(fromIdx, toIdx) {
  if (fromIdx === -1 || lastSelectedIndex === -1) return;
  const start = Math.min(fromIdx, lastSelectedIndex);
  const end = Math.max(fromIdx, lastSelectedIndex);
  const list = document.getElementById('machine-list');
  const items = list.querySelectorAll('.machine-item');
  for (let i = start; i <= end; i++) {
    if (items[i]) {
      const id = items[i].getAttribute('data-id');
      selectedMachines.add(id);
    }
  }
  updateSelectedCount();
  refreshMachineListCheckboxes();
}

function refreshMachineListCheckboxes() {
  const list = document.getElementById('machine-list');
  const items = list.querySelectorAll('.machine-item');
  items.forEach(item => {
    const id = item.getAttribute('data-id');
    const cb = item.querySelector('.machine-checkbox');
    if (cb) cb.checked = selectedMachines.has(id);
  });
}

function updateSelectedCount() {
  const countEl = document.getElementById('selected-count');
  const numEl = document.getElementById('selected-count-num');
  if (selectedMachines.size > 0) {
    countEl.style.display = 'block';
    numEl.textContent = selectedMachines.size;
  } else {
    countEl.style.display = 'none';
  }
}

// ── Select Machine ───────────────────────────────────────────────────────────
async function selectMachine(machineId) {
  selectedMachine = machineId;
  document.getElementById('no-machine-selected').classList.add('hidden');
  document.getElementById('machine-detail').classList.remove('hidden');
  document.getElementById('detail-title').textContent = machineId;

  // Start streaming
  startStream(machineId);

  // Load initial data
  await refreshStatus();
  await refreshHAL();
  await refreshErrors();

  // Update sidebar selection
  document.querySelectorAll('.machine-item').forEach(el => {
    el.classList.toggle('active', el.querySelector('.machine-name')?.textContent === machineId ||
      el.querySelector('.machine-detail')?.textContent.includes(machineId));
  });

  // Build controls
  buildControls();
}

function startStream(machineId) {
  // Close existing stream for this machine
  if (eventSources[machineId]) {
    eventSources[machineId].close();
    delete eventSources[machineId];
  }

  const es = new EventSource(`/api/stream/status/${machineId}`);
  eventSources[machineId] = es;

  es.onmessage = (evt) => {
    try {
      const status = JSON.parse(evt.data);
      updateStatusDisplay(status);
    } catch (e) {}
  };

  es.onerror = () => {
    console.error(`Stream error for ${machineId}`);
    es.close();
    delete eventSources[machineId];
  };
}

// ── Status Display ───────────────────────────────────────────────────────────
async function refreshStatus() {
  if (!selectedMachine) return;
  try {
    const resp = await fetch(`/api/status/${selectedMachine}`);
    const status = await resp.json();
    updateStatusDisplay(status);

    // Update sidebar machine list with latest status
    refreshMachines();
  } catch (e) {
    console.error('refreshStatus:', e);
  }
}

function updateStatusDisplay(status) {
  if (!status) return;

  const cards = document.getElementById('status-cards');
  cards.innerHTML = `
    <div class="status-card"><div class="label">State</div><div class="value ${getStatusClass(status.state)}">${status.state}</div></div>
    <div class="status-card"><div class="label">Mode</div><div class="value">${status.mode}</div></div>
    <div class="status-card"><div class="label">Execution</div><div class="value">${status.execution}</div></div>
    <div class="status-card"><div class="label">E-Stop</div><div class="value ${status.estop_state === 'E_STOPPED' ? 'estop' : ''}">${status.estop_state}</div></div>
    <div class="status-card"><div class="label">Interp State</div><div class="value">${status.interp_state}</div></div>
    <div class="status-card"><div class="label">Program</div><div class="value" style="font-size:14px;word-break:break-all;">${status.program_file || '(none)'}</div></div>
    <div class="status-card"><div class="label">Feedrate</div><div class="value">${status.feedrate.toFixed(0)} mm/min</div></div>
    <div class="status-card"><div class="label">Spindle</div><div class="value">${status.spindle_speed.toFixed(0)} RPM</div></div>
  `;

  // Joint positions
  const jp = document.getElementById('joint-positions');
  jp.innerHTML = buildPositionRow(status.joint_actual, 'J');

  // World coords
  const wc = document.getElementById('world-coords');
  wc.innerHTML = buildPositionRow(status.world_actual, 'W');

  // Detail header
  document.getElementById('detail-status').innerHTML = `
    <span class="machine-dot ${getStatusClassDot(status.state)}" style="display:inline-block;margin-right:8px;"></span>
    ${status.state} · ${status.estop_state === 'E_STOPPED' ? '<span style="color:var(--accent-red)">E-STOP</span>' : 'Ready'}
  `;

  // Update coolant indicators
  const coolantInfo = document.createElement('div');
  coolantInfo.id = 'coolant-indicator';
  coolantInfo.innerHTML = [
    status.coolant_mist ? '💧Mist' : '',
    status.coolant_flood ? '🌊Flood' : '',
    status.coolant_mazak ? '🔥Mazak' : ''
  ].filter(Boolean).join(' · ') || '';

  const existing = document.getElementById('coolant-indicator');
  if (existing) existing.remove();
  cards.parentElement.insertBefore(coolantInfo, cards.nextSibling);
}

function buildPositionRow(pos, prefix) {
  return ['x', 'y', 'z'].map(axis => `
    <div>
      <div class="pos-val">${(pos[axis] || 0).toFixed(3)}</div>
      <div class="pos-label">${prefix}${axis.toUpperCase()}</div>
    </div>`).join('');
}

function getStatusClass(state) {
  const s = (state || '').toLowerCase();
  if (s === 'running') return 'running';
  if (s === 'paused' || s === 'hold') return 'paused';
  if (s === 'e_stopped') return 'estop';
  return '';
}

function getStatusClassDot(state) {
  const s = (state || '').toLowerCase();
  if (s === 'running') return 'running';
  if (s === 'paused' || s === 'hold') return 'paused';
  if (s === 'e_stopped') return 'estop';
  return 'stopped';
}

// ── Controls ─────────────────────────────────────────────────────────────────
function buildControls() {
  if (!selectedMachine) return;

  const modeBtns = document.getElementById('mode-buttons');
  modeBtns.innerHTML = ['MANUAL', 'AUTO', 'MDA'].map(m =>
    `<button class="btn" onclick="setMode('${m}')">${m}</button>`
  ).join('');

  const motionBtns = document.getElementById('motion-buttons');
  motionBtns.innerHTML = `
    <button class="btn primary" onclick="doControl('start')">▶ Start</button>
    <button class="btn danger" onclick="doControl('stop')">⏹ Stop</button>
    <button class="btn" onclick="doControl('feed_hold')">⏸ Hold</button>
    <button class="btn" onclick="doControl('continue_exec')">▶ Continue</button>
    <button class="btn" onclick="doControl('home_all')">⌂ Home All</button>
  `;
}

async function setMode(mode) {
  if (!selectedMachine) return;
  try {
    const resp = await fetch(`/api/mode/${selectedMachine}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    const result = await resp.json();
    showToast(result.message, result.success ? 'success' : 'error');
  } catch (e) {
    showToast(`Failed: ${e.message}`, 'error');
  }
}

async function sendMdi() {
  if (!selectedMachine) return;
  const input = document.getElementById('mdi-input');
  const cmd = input.value.trim();
  if (!cmd) return;

  try {
    const resp = await fetch(`/api/mdi/${selectedMachine}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: cmd }),
    });
    const result = await resp.json();
    showToast(result.message, result.success ? 'success' : 'error');
    input.value = '';
  } catch (e) {
    showToast(`Failed: ${e.message}`, 'error');
  }
}

async function loadProgram() {
  if (!selectedMachine) return;
  const input = document.getElementById('program-path');
  const path = input.value.trim();
  if (!path) return;

  try {
    const resp = await fetch(`/api/program/${selectedMachine}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    const result = await resp.json();
    showToast(result.message, result.success ? 'success' : 'error');
  } catch (e) {
    showToast(`Failed: ${e.message}`, 'error');
  }
}

async function broadcastLoadProgram() {
  if (selectedMachines.size === 0) {
    showToast('No machines selected', 'error');
    return;
  }
  const input = document.getElementById('broadcast-program-path');
  const path = input.value.trim();
  if (!path) {
    showToast('Enter a program path', 'error');
    return;
  }

  const resultsDiv = document.getElementById('broadcast-results');
  resultsDiv.classList.remove('hidden');
  resultsDiv.innerHTML = '<div style="color:var(--text-secondary);font-size:13px;">Broadcasting...</div>';

  try {
    const resp = await fetch('/api/programs/broadcast', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope: 'SELECTED', path, facility: '', tags: [] }),
    });
    const result = await resp.json();
    if (result.error) throw new Error(result.error);

    let html = '';
    for (const [machineId, res] of Object.entries(result.results)) {
      const cls = res.success ? 'success' : 'error';
      html += `<div class="broadcast-result-item ${cls}">${machineId}: ${res.message}</div>`;
    }
    resultsDiv.innerHTML = html;

    const successCount = Object.values(result.results).filter(r => r.success).length;
    showToast(`Loaded on ${successCount}/${result.results.length} machines`, 'success');
  } catch (e) {
    resultsDiv.innerHTML = `<div class="broadcast-result-item error">${e.message}</div>`;
    showToast(`Broadcast failed: ${e.message}`, 'error');
  }
}

function openProgramBrowser() {
  document.getElementById('program-modal').classList.remove('hidden');
  const targetInput = document.getElementById('program-browser-target');
  if (selectedMachine) {
    targetInput.value = selectedMachine;
  } else {
    targetInput.value = '';
    targetInput.placeholder = 'Enter machine ID';
  }
  refreshProgramBrowser();
}

function closeProgramBrowser() {
  document.getElementById('program-modal').classList.add('hidden');
}

async function refreshProgramBrowser() {
  const machineId = document.getElementById('program-browser-target').value.trim();
  if (!machineId) {
    document.getElementById('program-browser-content').innerHTML = '<div class="empty-state">Enter a machine ID</div>';
    return;
  }

  document.getElementById('program-browser-content').innerHTML = '<div style="color:var(--text-secondary);font-size:13px;">Loading programs...</div>';

  try {
    const resp = await fetch(`/api/programs/${machineId}`);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);

    const programs = data.programs || [];
    if (programs.length === 0) {
      document.getElementById('program-browser-content').innerHTML = '<div class="empty-state">No programs found</div>';
      return;
    }

    let html = '<div class="program-browser">';
    programs.forEach(p => {
      const sizeStr = p.size_bytes > 0 ? ` · ${formatBytes(p.size_bytes)}` : '';
      const timeStr = p.modified_time ? ` · ${new Date(p.modified_time).toLocaleString()}` : '';
      html += `<div class="program-entry" onclick="useProgramPath('${p.path.replace(/'/g, "\\'")}')">
        <span class="program-name">${p.name}</span>
        <span class="program-meta">${sizeStr}${timeStr}</span>
      </div>`;
    });
    html += '</div>';
    document.getElementById('program-browser-content').innerHTML = html;
  } catch (e) {
    document.getElementById('program-browser-content').innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
  }
}

function useProgramPath(path) {
  const input = document.getElementById('broadcast-program-path');
  input.value = path;
  closeProgramBrowser();
  showToast(`Path loaded: ${path}`);
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

async function doControl(cmd) {
  if (!selectedMachine) return;
  try {
    const resp = await fetch(`/api/control/${selectedMachine}/${cmd}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const result = await resp.json();
    showToast(result.message, result.success ? 'success' : 'error');
  } catch (e) {
    showToast(`Failed: ${e.message}`, 'error');
  }
}

// ── HAL Pins ─────────────────────────────────────────────────────────────────
async function refreshHAL() {
  if (!selectedMachine) return;
  try {
    const resp = await fetch(`/api/hal/${selectedMachine}`);
    const components = await resp.json();

    const container = document.getElementById('hal-content');
    if (components === null) {
      container.innerHTML = '<div class="empty-state">HAL not available on this machine</div>';
      return;
    }
    if (!components.length) {
      container.innerHTML = '<div class="empty-state">No HAL components found</div>';
      return;
    }

    let html = '';
    components.forEach(comp => {
      html += `<table class="hal-table"><tr class="hal-component-header"><td colspan="5">${comp.name}</td></tr>`;
      comp.pins.forEach(pin => {
        const val = pin.type === 'PIN_TYPE_BIT' ? (pin.value_bit ? '1' : '0') :
                    pin.type === 'PIN_TYPE_U32' ? pin.value_u32 :
                    pin.type === 'PIN_TYPE_S32' ? pin.value_s32 :
                    pin.value_f.toFixed(4);
        const dir = pin.is_output ? '<span style="color:var(--accent-green)">OUT</span>' : '<span style="color:var(--text-secondary)">IN</span>';
        html += `<tr><td>${pin.name}</td><td>${pin.type.replace('PIN_TYPE_', '')}</td><td>${dir}</td><td>${val}</td>
          ${pin.is_output ? `<td><button class="btn" style="padding:4px 8px;font-size:12px;" onclick="readPin('${pin.name}')">Read</button></td>` : '<td></td>'}
        </tr>`;
      });
      html += '</table>';
    });

    container.innerHTML = html;
  } catch (e) {
    console.error('refreshHAL:', e);
  }
}

async function readPin(pinName) {
  if (!selectedMachine) return;
  try {
    const resp = await fetch(`/api/hal/pin/${selectedMachine}/${encodeURIComponent(pinName)}`);
    const pin = await resp.json();
    showToast(`${pin.pin_name}: ${pin.value_bit !== undefined ? (pin.value_bit ? '1' : '0') : pin.value_f.toFixed(4)}`, 'success');
  } catch (e) {
    showToast(`Failed to read: ${e.message}`, 'error');
  }
}

// ── Errors ───────────────────────────────────────────────────────────────────
async function refreshErrors() {
  if (!selectedMachine) return;
  try {
    const resp = await fetch(`/api/errors/${selectedMachine}`);
    const errors = await resp.json();

    const log = document.getElementById('error-log');
    if (!errors.length) {
      log.innerHTML = '<div class="empty-state">No errors</div>';
      return;
    }
    log.innerHTML = errors.map(e => `<div class="error-entry">${e.message}</div>`).join('');
  } catch (e) {
    console.error('refreshErrors:', e);
  }
}

// ── Toast ────────────────────────────────────────────────────────────────────
function showToast(message, type = 'success') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

// ── Tabs ─────────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.add('active');

    // Refresh tab data on first click
    const tab = btn.dataset.tab;
    if (tab === 'hal-tab') refreshHAL();
    if (tab === 'errors-tab') refreshErrors();
  });
});

// ── Init ─────────────────────────────────────────────────────────────────────
setInterval(refreshMachines, 5000);
</script>
</body>
</html>"""


# ── aiohttp Handlers ─────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=HTML_TEMPLATE, content_type='text/html')


async def handle_connect(request: web.Request) -> web.Response:
    """Initialize FleetClient with provided token."""
    gateway = request.query.get('gateway', 'localhost:50052')
    tls = request.query.get('tls', 'false').lower() == 'true'
    token = request.headers.get('Authorization', '').replace('Bearer ', '')

    if not token:
        return web.json_response({'error': 'No token provided'}, status=401)

    try:
        app_state = request.app['fleet']
        await app_state.close()  # close previous connection
        app_state._gateway_address = gateway
        app_state._tls_enabled = tls
        app_state._token = token
        await app_state.init()

        machines = await app_state.discover_machines()
        return web.json_response({'status': 'connected', 'machines': len(machines)})
    except Exception as e:
        log.error("Connect failed: %s", e)
        return web.json_response({'error': str(e)}, status=500)


async def handle_machines(request: web.Request) -> web.Response:
    """Get all machines with their latest status."""
    app_state = request.app['fleet']
    machines = await app_state.discover_machines()

    # Enrich each machine with its last known status
    for m in machines:
        last = await app_state.get_last_status(m['machine_id'])
        if last:
            m['_last_status'] = last

    return web.json_response(machines)


async def handle_status(request: web.Request) -> web.Response:
    """Get full status for a machine."""
    app_state = request.app['fleet']
    machine_id = request.match_info['id']
    status = await app_state.get_status(machine_id)

    if status is None:
        return web.json_response({'error': f'Machine {machine_id} not found'}, status=404)
    return web.json_response(status)


async def handle_stream(request: web.Request) -> web.Response:
    """SSE stream for machine status updates."""
    app_state = request.app['fleet']
    machine_id = request.match_info['id']

    # Get last known status to send immediately
    last_status = await app_state.get_last_status(machine_id)

    stream = await app_state.stream_status(machine_id)

    async def _stream_generator():
        if last_status:
            yield f"data: {json.dumps(last_status)}\n\n"
        try:
            async for line in stream._iter_lines():
                yield line
        except Exception as e:
            log.debug("Stream client disconnected: %s", e)
        finally:
            app_state.remove_stream(machine_id)

    return web.AsyncIterableResponse(
        _stream_generator(),
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*',
        },
    )


async def handle_stream_all(request: web.Request) -> web.Response:
    """SSE stream for all machines."""
    app_state = request.app['fleet']

    # Get last known statuses
    machines = await app_state.discover_machines()
    initial_data = []
    for m in machines:
        last = await app_state.get_last_status(m['machine_id'])
        if last:
            initial_data.append(f"data: {json.dumps(last)}\n\n")

    stream = await app_state.stream_all_machines()

    async def _stream_generator():
        for data in initial_data:
            yield data
        try:
            async for line in stream._iter_lines():
                yield line
        except Exception as e:
            log.debug("StreamAll client disconnected: %s", e)
        finally:
            app_state.remove_all_stream()

    return web.AsyncIterableResponse(
        _stream_generator(),
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*',
        },
    )


async def handle_info(request: web.Request) -> web.Response:
    """Get machine info (version, joints)."""
    app_state = request.app['fleet']
    machine_id = request.match_info['id']
    info = await app_state.get_machine_info(machine_id)

    if info is None:
        return web.json_response({'error': f'Machine {machine_id} not found'}, status=404)
    return web.json_response(info)


async def handle_mode(request: web.Request) -> web.Response:
    """Set machine mode."""
    app_state = request.app['fleet']
    machine_id = request.match_info['id']
    body = await request.json()
    mode = body.get('mode', '').upper()

    result = await app_state.set_mode(machine_id, mode)
    return web.json_response(result)


async def handle_mdi(request: web.Request) -> web.Response:
    """Send MDI command."""
    app_state = request.app['fleet']
    machine_id = request.match_info['id']
    body = await request.json()
    command = body.get('command', '')

    result = await app_state.send_mdi(machine_id, command)
    return web.json_response(result)


async def handle_program(request: web.Request) -> web.Response:
    """Load a G-code program."""
    app_state = request.app['fleet']
    machine_id = request.match_info['id']
    body = await request.json()
    path = body.get('path', '')

    result = await app_state.load_program(machine_id, path)
    return web.json_response(result)


async def handle_program_broadcast(request: web.Request) -> web.Response:
    """Broadcast load a G-code program to multiple machines."""
    app_state = request.app['fleet']
    body = await request.json()
    result = await app_state.broadcast_load_program(
        scope=body.get('scope', 'ALL'),
        path=body.get('path', ''),
        facility=body.get('facility', ''),
        tags=body.get('tags', []),
    )
    return web.json_response(result)


async def handle_list_programs(request: web.Request) -> web.Response:
    """List available G-code programs on a machine."""
    app_state = request.app['fleet']
    machine_id = request.match_info['id']
    directory = request.query.get('directory', '')
    max_depth = int(request.query.get('max_depth', '0'))
    result = await app_state.list_programs(machine_id, directory, max_depth)
    return web.json_response(result)


async def handle_control(request: web.Request) -> web.Response:
    """Execute a motion control command."""
    app_state = request.app['fleet']
    machine_id = request.match_info['id']
    cmd = request.match_info['cmd']

    result = await app_state.control(machine_id, cmd)
    return web.json_response(result)


async def handle_hal_list(request: web.Request) -> web.Response:
    """List HAL components and pins."""
    app_state = request.app['fleet']
    machine_id = request.match_info['id']
    components = await app_state.list_hal(machine_id)

    if components is None:
        return web.json_response(None, status=200)  # HAL not available
    return web.json_response(components)


async def handle_hal_pin(request: web.Request) -> web.Response:
    """Read a single HAL pin."""
    app_state = request.app['fleet']
    machine_id = request.match_info['id']
    pin_name = request.match_info['pin']

    pin = await app_state.read_hal_pin(machine_id, pin_name)
    if pin is None:
        return web.json_response({'error': f'Pin {pin_name} not found'}, status=404)
    return web.json_response(pin)


async def handle_hal_write(request: web.Request) -> web.Response:
    """Write to a HAL output pin."""
    app_state = request.app['fleet']
    machine_id = request.match_info['id']
    pin_name = request.match_info['pin']
    body = await request.json()

    result = await app_state.write_hal_pin(
        machine_id, pin_name,
        bit=body.get('bit'),
        float=body.get('float'),
        u32=body.get('u32'),
        s32=body.get('s32'),
    )
    return web.json_response(result)


async def handle_errors(request: web.Request) -> web.Response:
    """Get error log for a machine."""
    app_state = request.app['fleet']
    machine_id = request.match_info['id']
    errors = await app_state.get_errors(machine_id)
    return web.json_response(errors)


# ── Router Setup ─────────────────────────────────────────────────────────────

def create_routes(app: web.Application) -> None:
    """Register all API routes."""
    app.router.add_get('/', handle_index)
    app.router.add_post('/api/connect', handle_connect)
    app.router.add_get('/api/machines', handle_machines)
    app.router.add_get('/api/status/{id}', handle_status)
    app.router.add_get('/api/stream/status/{id}', handle_stream)
    app.router.add_get('/api/stream/all', handle_stream_all)
    app.router.add_get('/api/info/{id}', handle_info)
    app.router.add_post('/api/mode/{id}', handle_mode)
    app.router.add_post('/api/mdi/{id}', handle_mdi)
    app.router.add_post('/api/program/{id}', handle_program)
    app.router.add_post('/api/programs/broadcast', handle_program_broadcast)
    app.router.add_get('/api/programs/{id}', handle_list_programs)
    app.router.add_post('/api/control/{id}/{cmd}', handle_control)
    app.router.add_get('/api/hal/{id}', handle_hal_list)
    app.router.add_get('/api/hal/pin/{id}/{pin}', handle_hal_pin)
    app.router.add_post('/api/hal/write/{id}/{pin}', handle_hal_write)
    app.router.add_get('/api/errors/{id}', handle_errors)


# ── CLI Entry Point ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='LinuxCNC Fleet Dashboard UI')
    parser.add_argument('--gateway', default='localhost:50052', help='Gateway address (host:port)')
    parser.add_argument('--token', default=None, help='JWT token for authentication')
    parser.add_argument('--port', type=int, default=8080, help='HTTP listen port')
    parser.add_argument('--tls-cert', default=None, help='TLS certificate path (PEM)')
    parser.add_argument('--tls-key', default=None, help='TLS private key path (PEM)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable debug logging')
    return parser.parse_args()


async def on_startup(app: web.Application) -> None:
    """Initialize FleetClient on server start."""
    args = app['args']
    fleet_app = FleetApp(
        gateway_address=args.gateway,
        token=args.token or '',
        tls_enabled=bool(args.tls_cert),
    )
    try:
        await fleet_app.init()
        log.info("FleetClient initialized — connected to %s", args.gateway)
    except Exception as e:
        log.warning("Initial connection failed (UI will work once connected via config form): %s", e)

    app['fleet'] = fleet_app


async def on_shutdown(app: web.Application) -> None:
    """Cleanup on server stop."""
    fleet_app = app.get('fleet')
    if fleet_app:
        await fleet_app.close()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
    )

    app = web.Application()
    app['args'] = args
    app.router.add_get('/', handle_index)
    create_routes(app)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # CORS middleware
    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        response = await handler(request)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        if request.method == 'OPTIONS':
            return web.Response(status=204)
        return response

    app.middlewares.append(cors_middleware)

    # TLS setup
    ssl_context = None
    if args.tls_cert and args.tls_key:
        import ssl as _ssl
        ssl_ctx = _ssl.create_default_context(_ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(args.tls_cert, args.tls_key)
        ssl_context = ssl_ctx

    log.info("Starting Fleet Dashboard on :%d", args.port)
    web.run_app(app, host='0.0.0.0', port=args.port, ssl_context=ssl_context)


if __name__ == '__main__':
    main()
