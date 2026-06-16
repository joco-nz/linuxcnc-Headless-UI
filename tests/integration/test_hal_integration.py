"""HAL integration tests against real LinuxCNC simulation (axis_mm.ini).

These tests verify that the sidecar correctly interfaces with the hal module
to read/write HAL pins on a running LinuxCNC instance loaded with core_sim.hal.

The axis_mm.ini configuration loads:
  - core_sim.hal (3-axis joint simulation)
  - sim_spindle_encoder.hal
  - axis_manualtoolchange.hal
  - simulated_home.hal
  - cooling.hal
"""

import grpc
import pytest
from unittest.mock import MagicMock

pytest.importorskip("linuxcnc")

from gateway.auth import create_test_token
from linuxcnc_fleet.fleet_pb2 import (
    HalPinRead,
    HalPinSubscribe,
    HalPinWrite,
    ListHalRequest,
    MachineId,
)
from linuxcnc_fleet.fleet_pb2_grpc import FleetGatewayServiceStub, FleetServiceStub
from linuxcnc_fleet.headless import LinuxCncSidecar


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_admin_token():
    return create_test_token({
        "sub": "test-admin",
        "name": "Test Admin",
        "role": "admin",
    })


def _make_hal_mock():
    """Create a MagicMock configured with hal module constants."""
    m = MagicMock()
    m.HAL_BIT = 0
    m.HAL_U32 = 1
    m.HAL_S32 = 2
    m.HAL_FLOAT = 3
    m.HAL_IN = 16
    m.HAL_OUT = 32
    m.HAL_IO = 48
    return m


# ---------------------------------------------------------------------------
# Tests: HAL component discovery and pin operations (real LinuxCNC)
# ---------------------------------------------------------------------------

class TestHALDiscovery:
    """Test HAL component enumeration against real LinuxCNC (axis_mm.ini)."""

    def test_list_hal_components(self, gateway_server):
        """Sidecar returns HAL components discovered via get_info_pins().

        axis_mm.ini loads core_sim.hal which creates motion/io/halui components
        with pins like joint.0.motor-pos-fb, iocontrol.0.user-enable-out, etc.
        """
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        gateway_channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        gateway_stub = FleetGatewayServiceStub(gateway_channel)

        token = _make_admin_token()
        metadata = [("authorization", f"Bearer {token}")]

        route_resp = gateway_stub.RouteMachine(
            MachineId(id="integration-real-machine"),
            metadata=metadata,
        )

        sidecar_channel = grpc.insecure_channel(
            f"{route_resp.instance_address}:{route_resp.instance_port}"
        )
        sidecar_stub = FleetServiceStub(sidecar_channel)

        hal_list = sidecar_stub.ListHalComponents(
            ListHalRequest(id=MachineId(id="integration-real-machine")),
        )

        component_names = [c.name for c in hal_list.components]

        # axis_mm.ini loads core_sim.hal which creates motion, io, halui components.
        # At minimum we should see some components (not empty).
        assert len(component_names) > 0, \
            f"Expected HAL components from axis_mm.ini but got none: {component_names}"

        # Verify each component has pins
        for comp in hal_list.components:
            assert isinstance(comp.pins, list) or hasattr(comp.pins, '__iter__')

        sidecar_channel.close()
        gateway_channel.close()


class TestHALPinRead:
    """Test reading HAL pins via ReadHalPin RPC against real LinuxCNC."""

    def test_read_hal_float_pin(self, gateway_server, discovered_hal_pins):
        """Read a float position feedback pin → returns a value from hal.get_value()."""
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        target_pin = discovered_hal_pins.get("float_position")
        if not target_pin:
            pytest.skip("No float position (pos-fb) pin available")

        gateway_channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        gateway_stub = FleetGatewayServiceStub(gateway_channel)

        token = _make_admin_token()
        metadata = [("authorization", f"Bearer {token}")]

        route_resp = gateway_stub.RouteMachine(
            MachineId(id="integration-real-machine"),
            metadata=metadata,
        )

        sidecar_channel = grpc.insecure_channel(
            f"{route_resp.instance_address}:{route_resp.instance_port}"
        )
        sidecar_stub = FleetServiceStub(sidecar_channel)

        hal_read = HalPinRead(
            id=MachineId(id="integration-real-machine"),
            pin_name=target_pin,
        )

        result = sidecar_stub.ReadHalPin(hal_read)

        assert result.value is not None, f"ReadHalPin should return a value for {target_pin}"
        assert result.pin_name == target_pin

        sidecar_channel.close()
        gateway_channel.close()

    def test_read_hal_bit_pin(self, gateway_server, discovered_hal_pins):
        """Read an enable-in bit pin → returns a boolean from hal.get_value()."""
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        target_pin = discovered_hal_pins.get("bit_enable_in")
        if not target_pin:
            pytest.skip("No bit enable-in pin available")

        gateway_channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        gateway_stub = FleetGatewayServiceStub(gateway_channel)

        token = _make_admin_token()
        metadata = [("authorization", f"Bearer {token}")]

        route_resp = gateway_stub.RouteMachine(
            MachineId(id="integration-real-machine"),
            metadata=metadata,
        )

        sidecar_channel = grpc.insecure_channel(
            f"{route_resp.instance_address}:{route_resp.instance_port}"
        )
        sidecar_stub = FleetServiceStub(sidecar_channel)

        hal_read = HalPinRead(
            id=MachineId(id="integration-real-machine"),
            pin_name=target_pin,
        )

        result = sidecar_stub.ReadHalPin(hal_read)

        assert result.value is not None, f"ReadHalPin should return a value for {target_pin}"

        sidecar_channel.close()
        gateway_channel.close()


class TestHALPinWrite:
    """Test writing to HAL output pins via WriteHalPin RPC."""

    def test_write_hal_output_pin(self, gateway_server, discovered_hal_pins):
        """Write an enable-out bit pin → hal.set_p() succeeds."""
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        target_pin = discovered_hal_pins.get("bit_enable_out")
        if not target_pin:
            pytest.skip("No bit enable-out pin available")

        gateway_channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        gateway_stub = FleetGatewayServiceStub(gateway_channel)

        token = _make_admin_token()
        metadata = [("authorization", f"Bearer {token}")]

        route_resp = gateway_stub.RouteMachine(
            MachineId(id="integration-real-machine"),
            metadata=metadata,
        )

        sidecar_channel = grpc.insecure_channel(
            f"{route_resp.instance_address}:{route_resp.instance_port}"
        )
        sidecar_stub = FleetServiceStub(sidecar_channel)

        hal_write = HalPinWrite(
            id=MachineId(id="integration-real-machine"),
            pin_name=target_pin,
            value_bit=True,
        )

        result = sidecar_stub.WriteHalPin(hal_write)

        assert result.success is True, f"WriteHalPin should succeed: {result.message}"

        # Verify writeback — read the pin again
        hal_read = HalPinRead(
            id=MachineId(id="integration-real-machine"),
            pin_name=target_pin,
        )
        read_result = sidecar_stub.ReadHalPin(hal_read)
        assert read_result.value is not None, "Should be able to read back the written value"
        assert read_result.value_bit is True, f"Expected True, got {read_result.value_bit}"

        sidecar_channel.close()
        gateway_channel.close()


class TestHALPinSubscription:
    """Test server-streaming HAL pin updates via SubscribeHalPins."""

    def test_hal_pin_subscription_stream(self, gateway_server, discovered_hal_pins):
        """Subscribe to a position feedback pin → stream returns periodic updates."""
        gw_port, registry, stop_sidecar, stop_gw = gateway_server

        target_pin = discovered_hal_pins.get("float_position")
        if not target_pin:
            pytest.skip("No float position (pos-fb) pin available")

        gateway_channel = grpc.insecure_channel(f"127.0.0.1:{gw_port}")
        gateway_stub = FleetGatewayServiceStub(gateway_channel)

        token = _make_admin_token()
        metadata = [("authorization", f"Bearer {token}")]

        route_resp = gateway_stub.RouteMachine(
            MachineId(id="integration-real-machine"),
            metadata=metadata,
        )

        sidecar_channel = grpc.insecure_channel(
            f"{route_resp.instance_address}:{route_resp.instance_port}"
        )
        sidecar_stub = FleetServiceStub(sidecar_channel)

        request = HalPinSubscribe(
            id=MachineId(id="integration-real-machine"),
            pin_names=[target_pin],
            poll_interval_seconds=0.5,
        )

        received_count = 0
        try:
            for update in sidecar_stub.SubscribeHalPins(request):
                assert len(update.pins) > 0 or update.pin_name != ""
                received_count += 1
                if received_count >= 3:
                    break
        except grpc.RpcError:
            pass

        assert received_count >= 3, f"Expected at least 3 updates, got {received_count}"


# ---------------------------------------------------------------------------
# Tests: HAL operations with mocked hal module (unit-style)
# ---------------------------------------------------------------------------

class TestReadHalPinUnit:
    """Test read_hal_pin logic with mocked hal module."""

    def test_raises_when_hal_module_missing(self):
        """read_hal_pin raises RuntimeError when hal is None."""
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=None)
        with pytest.raises(RuntimeError, match="hal module not available"):
            sidecar.read_hal_pin("some_pin")

    def test_returns_bit_value(self):
        """read_hal_pin returns HalPinValue with value_bit for HAL_BIT type."""
        hal = _make_hal_mock()
        hal.get_info_pins.return_value = [
            {'NAME': 'spindle.on', 'TYPE': 0, 'VALUE': 1.0},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.read_hal_pin("spindle.on")

        assert result.pin_name == "spindle.on"
        from linuxcnc_fleet.fleet_pb2 import HalPinType
        assert result.type == HalPinType.PIN_TYPE_BIT
        assert result.value_bit is True

    def test_returns_u32_value(self):
        """read_hal_pin returns HalPinValue with value_u32 for HAL_U32 type."""
        hal = _make_hal_mock()
        hal.get_info_pins.return_value = [
            {'NAME': 'feed.rate', 'TYPE': 1, 'VALUE': 0.0},
        ]
        hal.get_value.return_value = 42.0

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.read_hal_pin("feed.rate")

        from linuxcnc_fleet.fleet_pb2 import HalPinType
        assert result.type == HalPinType.PIN_TYPE_U32
        assert result.value_u32 == 42

    def test_returns_s32_value(self):
        """read_hal_pin returns HalPinValue with value_s32 for HAL_S32 type."""
        hal = _make_hal_mock()
        hal.get_info_pins.return_value = [
            {'NAME': 'offset.z', 'TYPE': 2, 'VALUE': 0.0},
        ]
        hal.get_value.return_value = -100.0

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.read_hal_pin("offset.z")

        from linuxcnc_fleet.fleet_pb2 import HalPinType
        assert result.type == HalPinType.PIN_TYPE_S32
        assert result.value_s32 == -100

    def test_returns_float_value(self):
        """read_hal_pin returns HalPinValue with value_f for HAL_FLOAT type."""
        hal = _make_hal_mock()
        hal.get_info_pins.return_value = [
            {'NAME': 'spindle.speed-cmd', 'TYPE': 3, 'VALUE': 0.0},
        ]
        hal.get_value.return_value = 3.14159

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.read_hal_pin("spindle.speed-cmd")

        from linuxcnc_fleet.fleet_pb2 import HalPinType
        assert result.type == HalPinType.PIN_TYPE_FLOAT
        assert abs(result.value_f - 3.14159) < 0.0001

    def test_raises_value_error_on_get_info_pins_failure(self):
        """read_hal_pin raises ValueError when get_info_pins fails."""
        hal = _make_hal_mock()
        hal.get_info_pins.side_effect = RuntimeError("HAL unavailable")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        with pytest.raises(ValueError, match="not found"):
            sidecar.read_hal_pin("missing.pin")

    def test_raises_value_error_on_unknown_pin(self):
        """read_hal_pin raises ValueError when pin is not in get_info_pins."""
        hal = _make_hal_mock()
        hal.get_info_pins.return_value = [
            {'NAME': 'other.pin', 'TYPE': 3, 'VALUE': 1.0},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        with pytest.raises(ValueError, match="not found"):
            sidecar.read_hal_pin("unknown.pin")


class TestWriteHalPinUnit:
    """Test write_hal_pin logic with mocked hal module."""

    def test_returns_error_when_hal_missing(self):
        """write_hal_pin returns error Result when hal is None."""
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=None)
        result = sidecar.write_hal_pin("some.pin")
        assert result.success is False
        assert "not available" in result.message.lower()

    def test_returns_write_protected_for_input_pin(self):
        """write_hal_pin returns HAL_WRITE_PROTECTED when pin is not an output."""
        hal = _make_hal_mock()
        hal.pin_has_writer.return_value = False

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.write_hal_pin("input.pin")

        assert result.success is False
        from linuxcnc_fleet.fleet_pb2 import ErrorCode
        assert result.error_code == ErrorCode.HAL_WRITE_PROTECTED
        assert "not an output" in result.message.lower()

    def test_returns_pin_not_found_when_type_lookup_fails(self):
        """write_hal_pin returns HAL_PIN_NOT_FOUND when pin is not found."""
        hal = _make_hal_mock()
        hal.pin_has_writer.return_value = True
        hal.get_info_pins.return_value = []

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.write_hal_pin("unknown.pin")

        from linuxcnc_fleet.fleet_pb2 import ErrorCode
        assert result.success is False
        assert result.error_code == ErrorCode.HAL_PIN_NOT_FOUND
        assert "not found" in result.message.lower()

    def test_writes_bit_true(self):
        """write_hal_pin writes '1' for bit pin when value_bit=True."""
        hal = _make_hal_mock()
        hal.pin_has_writer.return_value = True
        hal.get_info_pins.return_value = [
            {'NAME': 'relay.on', 'TYPE': 0, 'VALUE': 0.0},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.write_hal_pin("relay.on", value_bit=True)

        assert result.success is True
        hal.set_p.assert_called_with("relay.on", "1")

    def test_writes_bit_false(self):
        """write_hal_pin writes '0' for bit pin when value_bit=False."""
        hal = _make_hal_mock()
        hal.pin_has_writer.return_value = True
        hal.get_info_pins.return_value = [
            {'NAME': 'relay.off', 'TYPE': 0, 'VALUE': 1.0},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.write_hal_pin("relay.off", value_bit=False)

        assert result.success is True
        hal.set_p.assert_called_with("relay.off", "0")

    def test_writes_u32_value(self):
        """write_hal_pin writes string(value_u32) for HAL_U32 pin."""
        hal = _make_hal_mock()
        hal.pin_has_writer.return_value = True
        hal.get_info_pins.return_value = [
            {'NAME': 'counter.val', 'TYPE': 1, 'VALUE': 0.0},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.write_hal_pin("counter.val", value_u32=99)

        assert result.success is True
        hal.set_p.assert_called_with("counter.val", "99")

    def test_writes_s32_value(self):
        """write_hal_pin writes string(value_s32) for HAL_S32 pin."""
        hal = _make_hal_mock()
        hal.pin_has_writer.return_value = True
        hal.get_info_pins.return_value = [
            {'NAME': 'offset.x', 'TYPE': 2, 'VALUE': 0.0},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.write_hal_pin("offset.x", value_s32=-50)

        assert result.success is True
        hal.set_p.assert_called_with("offset.x", "-50")

    def test_writes_float_value(self):
        """write_hal_pin writes string(value_f) for HAL_FLOAT pin."""
        hal = _make_hal_mock()
        hal.pin_has_writer.return_value = True
        hal.get_info_pins.return_value = [
            {'NAME': 'spindle.speed-cmd', 'TYPE': 3, 'VALUE': 0.0},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.write_hal_pin("spindle.speed-cmd", value_f=1500.0)

        assert result.success is True
        hal.set_p.assert_called_with("spindle.speed-cmd", "1500.0")

    def test_returns_internal_error_on_set_p_failure(self):
        """write_hal_pin returns INTERNAL_ERROR when set_p raises."""
        hal = _make_hal_mock()
        hal.pin_has_writer.return_value = True
        hal.get_info_pins.return_value = [
            {'NAME': 'bad.pin', 'TYPE': 3, 'VALUE': 0.0},
        ]
        hal.set_p.side_effect = RuntimeError("hardware fault")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.write_hal_pin("bad.pin", value_f=1.0)

        from linuxcnc_fleet.fleet_pb2 import ErrorCode
        assert result.success is False
        assert result.error_code == ErrorCode.INTERNAL_ERROR


class TestListHalComponentsUnit:
    """Test list_hal_components logic with mocked hal module."""

    def test_raises_when_hal_module_missing(self):
        """list_hal_components raises RuntimeError when hal is None."""
        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=None)
        with pytest.raises(RuntimeError, match="hal module not available"):
            sidecar.list_hal_components()

    def test_returns_empty_when_get_info_raises(self):
        """list_hal_components returns empty list when get_info_pins raises."""
        hal = _make_hal_mock()
        hal.get_info_pins.side_effect = RuntimeError("HAL unavailable")

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.list_hal_components()

        assert len(result.components) == 0

    def test_returns_single_component_with_pins(self):
        """list_hal_components returns component with pins from get_info_pins."""
        hal = _make_hal_mock()
        hal.get_info_pins.return_value = [
            {'NAME': 'motion.joint-0.pos-fb', 'TYPE': 3, 'VALUE': 12.5, 'DIRECTION': 0},
            {'NAME': 'motion.joint-0.vel-fb', 'TYPE': 3, 'VALUE': 0.0, 'DIRECTION': 0},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.list_hal_components()

        assert len(result.components) == 1
        comp = result.components[0]
        assert comp.name == "motion"
        assert len(comp.pins) == 2
        assert comp.pins[0].name == "motion.joint-0.pos-fb"
        assert comp.update_period_ns == 0.0

    def test_groups_pins_by_component(self):
        """list_hal_components groups pins by component name (first segment of NAME)."""
        hal = _make_hal_mock()
        hal.get_info_pins.return_value = [
            {'NAME': 'motion.joint-0.pos-fb', 'TYPE': 3, 'VALUE': 1.0, 'DIRECTION': 0},
            {'NAME': 'halui.joint-0.pos-actual', 'TYPE': 3, 'VALUE': 2.0, 'DIRECTION': 0},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.list_hal_components()

        assert len(result.components) == 2
        names = {c.name for c in result.components}
        assert "motion" in names
        assert "halui" in names

    def test_sets_output_direction(self):
        """list_hal_components sets is_output=True for HAL_OUT direction."""
        hal = _make_hal_mock()
        hal.get_info_pins.return_value = [
            {'NAME': 'io.user-enable-out', 'TYPE': 0, 'VALUE': False, 'DIRECTION': 32},
            {'NAME': 'io.emc-enable-in', 'TYPE': 0, 'VALUE': True, 'DIRECTION': 16},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.list_hal_components()

        assert len(result.components) == 1
        comp = result.components[0]
        assert len(comp.pins) == 2
        out_pin = next(p for p in comp.pins if 'out' in p.name)
        in_pin = next(p for p in comp.pins if 'in' in p.name)
        assert out_pin.is_output is True
        assert in_pin.is_output is False

    def test_populates_params_from_get_info_params(self):
        """list_hal_components populates params from get_info_params."""
        hal = _make_hal_mock()
        hal.get_info_pins.return_value = []
        hal.get_info_params.return_value = [
            {'NAME': 'motion.max-velocity', 'VALUE': 100.0},
            {'NAME': 'motion.max-acceleration', 'VALUE': 5000.0},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.list_hal_components()

        assert len(result.components) == 1
        comp = result.components[0]
        assert "motion.max-velocity" in comp.params
        assert "motion.max-acceleration" in comp.params
        assert comp.params["motion.max-velocity"] == pytest.approx(100.0)

    def test_populates_signals(self):
        """list_hal_components includes signals from get_info_signals."""
        hal = _make_hal_mock()
        hal.get_info_pins.return_value = []
        hal.get_info_signals.return_value = [
            {'NAME': 'motion.joint-0.motor-pos-cmd', 'DRIVER': 'stepgen', 'TYPE': 3},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.list_hal_components()

        assert len(result.components) == 1
        comp = result.components[0]
        assert comp.name == "motion"

    def test_skips_pins_without_dot(self):
        """list_hal_components skips pins whose NAME has no dot (no component prefix)."""
        hal = _make_hal_mock()
        hal.get_info_pins.return_value = [
            {'NAME': 'no-component', 'TYPE': 3, 'VALUE': 1.0, 'DIRECTION': 0},
            {'NAME': 'valid.comp-pin', 'TYPE': 3, 'VALUE': 2.0, 'DIRECTION': 0},
        ]

        sidecar = LinuxCncSidecar(ini_path="/fake.ini", machine_id="test", hal_override=hal)
        result = sidecar.list_hal_components()

        assert len(result.components) == 1
        assert result.components[0].name == "valid"
