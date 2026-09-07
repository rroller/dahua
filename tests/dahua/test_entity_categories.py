"""Configuration entities should not be dressed up as controls.

An entity with no category is a primary control: it lands in auto-generated
dashboards and sits at the top of the device page. On an eleven channel NVR that
put dozens of "disarming linkage" style toggles in front of the cameras someone
actually wanted to look at.
"""

from types import SimpleNamespace

from homeassistant.const import EntityCategory

from custom_components.dahua.sensor import (
    DahuaFirmwareVersionSensor,
    DahuaSerialNumberSensor,
)
from custom_components.dahua.switch import (
    DahuaDisarmingEventNotificationsLinkageBinarySwitch,
    DahuaDisarmingLinkageBinarySwitch,
    DahuaMotionDetectionBinarySwitch,
    DahuaSirenBinarySwitch,
    DahuaSmartMotionDetectionBinarySwitch,
)

CONFIGURATION_SWITCHES = [
    DahuaMotionDetectionBinarySwitch,
    DahuaDisarmingLinkageBinarySwitch,
    DahuaDisarmingEventNotificationsLinkageBinarySwitch,
    DahuaSmartMotionDetectionBinarySwitch,
]


def _declared_category(cls):
    """What the class itself declares, not what it inherits."""
    return cls.__dict__.get("_attr_entity_category")


# --- switches ---------------------------------------------------------------

def test_the_configuration_switches_say_they_are_configuration():
    for cls in CONFIGURATION_SWITCHES:
        assert _declared_category(cls) is EntityCategory.CONFIG, cls.__name__


def test_the_siren_is_left_as_a_control():
    """Sounding a siren is an action someone wants on a dashboard."""
    assert _declared_category(DahuaSirenBinarySwitch) is None


# --- diagnostic sensors -----------------------------------------------------

def _coordinator():
    c = SimpleNamespace(
        get_device_name=lambda: "Garage",
        get_serial_number=lambda: "SERIAL1_4",   # channel-suffixed entity key
        get_device_serial_number=lambda: "SERIAL1",  # what the device reports
        get_firmware_version=lambda: "2.800.0",
    )
    return c


def _sensor(cls, coordinator):
    entity = object.__new__(cls)
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    return entity


def test_both_sensors_are_diagnostics():
    for cls in (DahuaFirmwareVersionSensor, DahuaSerialNumberSensor):
        assert _declared_category(cls) is EntityCategory.DIAGNOSTIC, cls.__name__


def test_the_firmware_sensor_reports_the_firmware():
    assert _sensor(DahuaFirmwareVersionSensor, _coordinator()).native_value == "2.800.0"


def test_the_serial_sensor_reports_the_device_serial_not_the_entity_key():
    """Every channel of one NVR is the same box and must say the same serial."""
    value = _sensor(DahuaSerialNumberSensor, _coordinator()).native_value

    assert value == "SERIAL1"
    assert not value.endswith("_4"), "this is the entity key, not a serial number"


def test_the_sensors_do_not_collide():
    c = _coordinator()
    ids = [
        _sensor(cls, c).unique_id
        for cls in (DahuaFirmwareVersionSensor, DahuaSerialNumberSensor)
    ]

    assert len(set(ids)) == len(ids)
    assert all(i.startswith("SERIAL1_4_") for i in ids)


def test_the_sensors_are_named_after_the_device():
    c = _coordinator()
    for cls in (DahuaFirmwareVersionSensor, DahuaSerialNumberSensor):
        assert _sensor(cls, c).name.startswith("Garage "), cls.__name__
