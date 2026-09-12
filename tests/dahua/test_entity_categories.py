"""Configuration entities should not be dressed up as controls.

An entity with no category is a primary control: it lands in auto-generated
dashboards and sits at the top of the device page. On an eleven channel NVR that
put dozens of "disarming linkage" style toggles in front of the cameras someone
actually wanted to look at.
"""

from types import SimpleNamespace

import pytest

from homeassistant.const import EntityCategory

from custom_components.dahua import sensor as sensor_module
from custom_components.dahua.sensor import (
    DahuaFirmwareVersionSensor,
    DahuaProfileSensor,
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


def _category(cls):
    """The category an instance of this class actually reports.

    Read from an instance rather than the class dict: Home Assistant's
    CachedProperties metaclass turns an `_attr_` class attribute into a property
    object, so the class dict holds the descriptor and not the value.
    """
    return object.__new__(cls).entity_category


# --- switches ---------------------------------------------------------------

def test_the_configuration_switches_say_they_are_configuration():
    for cls in CONFIGURATION_SWITCHES:
        assert _category(cls) is EntityCategory.CONFIG, cls.__name__


def test_the_siren_is_left_as_a_control():
    """Sounding a siren is an action someone wants on a dashboard."""
    assert _category(DahuaSirenBinarySwitch) is None


# --- diagnostic sensors -----------------------------------------------------

def _coordinator():
    c = SimpleNamespace(
        get_device_name=lambda: "Garage",
        get_serial_number=lambda: "SERIAL1_4",   # channel-suffixed entity key
        get_device_serial_number=lambda: "SERIAL1",  # what the device reports
        get_firmware_version=lambda: "2.800.0",
        get_profile_mode=lambda: "1",
    )
    return c


@pytest.fixture(autouse=True)
def _skip_ha_plumbing(monkeypatch):
    monkeypatch.setattr(sensor_module.DahuaBaseEntity, "__init__", lambda self, c, e: None)


def _sensor(cls, coordinator):
    entity = object.__new__(cls)
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    return entity


def test_both_sensors_are_diagnostics():
    for cls in (DahuaFirmwareVersionSensor, DahuaSerialNumberSensor, DahuaProfileSensor):
        assert _category(cls) is EntityCategory.DIAGNOSTIC, cls.__name__


def test_the_firmware_sensor_reports_the_firmware():
    assert _sensor(DahuaFirmwareVersionSensor, _coordinator()).native_value == "2.800.0"


def test_the_profile_sensor_reports_the_named_profile():
    assert _sensor(DahuaProfileSensor, _coordinator()).native_value == "Night"


def test_the_serial_sensor_reports_the_device_serial_not_the_entity_key():
    """Every channel of one NVR is the same box and must say the same serial."""
    value = _sensor(DahuaSerialNumberSensor, _coordinator()).native_value

    assert value == "SERIAL1"
    assert not value.endswith("_4"), "this is the entity key, not a serial number"


def test_the_sensors_do_not_collide():
    c = _coordinator()
    ids = [
        _sensor(cls, c).unique_id
        for cls in (DahuaFirmwareVersionSensor, DahuaSerialNumberSensor, DahuaProfileSensor)
    ]

    assert len(set(ids)) == len(ids)
    assert all(i.startswith("SERIAL1_4_") for i in ids)


def test_the_sensors_are_named_after_the_device():
    c = _coordinator()
    for cls in (DahuaFirmwareVersionSensor, DahuaSerialNumberSensor, DahuaProfileSensor):
        assert _sensor(cls, c).name.startswith("Garage "), cls.__name__


# --- the profile sensor is gated on the capability -------------------------------

def _setup_coordinator(profile_support):
    c = _coordinator()
    c.supports_profile_mode = lambda: profile_support
    return c


def _setup(coordinator):
    hass = type("H", (), {"data": {"dahua": {"e1": coordinator}}})()
    entry = type("E", (), {"entry_id": "e1"})()
    added = []
    return hass, entry, added


async def test_the_profile_sensor_is_only_added_when_profile_mode_is_supported():
    hass, entry, added = _setup(_setup_coordinator(profile_support=True))
    await sensor_module.async_setup_entry(hass, entry, added.extend)
    assert any(isinstance(s, DahuaProfileSensor) for s in added)


async def test_no_profile_sensor_without_profile_support():
    """The profile stays "0" (Day) forever on such a device; a wrong value
    looks like a working one, so no sensor is better."""
    hass, entry, added = _setup(_setup_coordinator(profile_support=False))
    await sensor_module.async_setup_entry(hass, entry, added.extend)
    assert not any(isinstance(s, DahuaProfileSensor) for s in added)


async def test_the_diagnostic_sensors_are_always_added():
    hass, entry, added = _setup(_setup_coordinator(profile_support=False))
    await sensor_module.async_setup_entry(hass, entry, added.extend)
    names = [type(s).__name__ for s in added]
    assert "DahuaFirmwareVersionSensor" in names
    assert "DahuaSerialNumberSensor" in names
