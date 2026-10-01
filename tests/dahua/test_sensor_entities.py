"""The three diagnostic sensors, and the plate sensor's subscription.

test_firmware_version_sensor.py covers the coordinator's getters. These are the entities
that read them, whose attributes and identity had no tests, and one of which was leaking
a callback.

The leak is the point of this file. `DahuaLicensePlateSensor.async_added_to_hass` threw
away the remover that `add_plate_listener` returns:

    self._coordinator.add_plate_listener(self.schedule_update_ha_state)

The authorized vehicle sensor next door hands it to `async_on_remove`, and
`add_plate_listener`'s own docstring says it returns one because "a removed one kept being
called". This caller was missed by that change. Since `_plate_listeners` is a list that
only ever appends, every reload left another dead callback in it, and the dispatch catches
each one and logs "Error calling plate listener" -- so one ANPR plate produced one warning
per reload the entry had ever had.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import sensor as sensor_module
from custom_components.dahua.sensor import (
    DahuaFirmwareVersionSensor,
    DahuaLicensePlateSensor,
    DahuaProfileSensor,
)


class _Coordinator:
    # The real one, so the remover it hands back is the real remover.
    add_plate_listener = DahuaDataUpdateCoordinator.add_plate_listener

    def __init__(self, firmware="4.300.0", build_date="2021-01-01", profile="1",
                 plate="unknown", plate_data=None):
        self._plate_listeners = []
        self._firmware = firmware
        self._build_date = build_date
        self._profile = profile
        self._plate = plate
        self._plate_data = plate_data or {}
        self.data = {"id": 7}

    def get_firmware_version(self):
        return self._firmware

    def get_build_date(self):
        return self._build_date

    def get_profile_mode(self):
        return self._profile

    def get_last_plate(self):
        return self._plate

    def get_last_plate_data(self):
        return self._plate_data

    def get_serial_number(self):
        return "SERIAL1"

    def get_device_name(self):
        return "Front Door"


@pytest.fixture
def real_init(monkeypatch):
    """Run a sensor's own __init__, skipping only Home Assistant's.

    The same shape as test_entity_identity.py: DahuaBaseEntity.__init__ is what
    reaches CoordinatorEntity, and replacing it with something that sets _coordinator
    leaves the subclass constructor -- the part under test -- running for real.
    """
    monkeypatch.setattr(
        sensor_module.DahuaBaseEntity, "__init__",
        lambda self, c, e: setattr(self, "_coordinator", c))


def _sensor(cls, coordinator):
    entity = object.__new__(cls)
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    return entity


# --- the firmware sensor's build date ----------------------------------------

def test_the_build_date_is_exposed_as_an_attribute():
    """Templatable alongside the version, which is what makes "tell me when a camera is
    behind" possible."""
    s = _sensor(DahuaFirmwareVersionSensor, _Coordinator(build_date="2021-01-01"))

    assert s.extra_state_attributes["build_date"] == "2021-01-01"


def test_a_device_that_reports_no_build_date_gets_no_empty_attribute():
    """Omitted rather than present and blank: an attribute that exists but says nothing
    reads as a device that reported something."""
    s = _sensor(DahuaFirmwareVersionSensor, _Coordinator(build_date=None))

    assert "build_date" not in s.extra_state_attributes


def test_the_build_date_does_not_replace_the_usual_attributes():
    """It adds to what the base entity exposes rather than standing in for it."""
    s = _sensor(DahuaFirmwareVersionSensor, _Coordinator())

    attrs = s.extra_state_attributes
    assert attrs["build_date"] == "2021-01-01"
    assert attrs["integration"] == "dahua"


# --- the profile sensor's raw number -----------------------------------------

def test_the_raw_profile_number_is_exposed_alongside_the_name():
    """The state is the readable name; an automation comparing profiles wants the number
    the device actually uses, and `Lighting_V2[channel][profile]` is indexed by it."""
    s = _sensor(DahuaProfileSensor, _Coordinator(profile="1"))

    assert s.extra_state_attributes["profile_number"] == "1"


def test_the_profile_attribute_keeps_the_base_attributes_too():
    s = _sensor(DahuaProfileSensor, _Coordinator(profile="2"))

    assert s.extra_state_attributes["integration"] == "dahua"


# --- the licence plate sensor ------------------------------------------------

def test_the_plate_sensor_has_its_own_unique_id(real_init):
    """Built with its real constructor, so the id is derived rather than asserted
    against a value the test set itself."""
    s = DahuaLicensePlateSensor(_Coordinator(), object())

    assert s.unique_id == "SERIAL1_license_plate"


def test_no_plate_yet_reads_as_nothing_rather_than_the_word_unknown():
    """The coordinator's own placeholder is the string "unknown", and passing that
    through would put it on a dashboard as though the camera had read a plate called
    unknown."""
    s = _sensor(DahuaLicensePlateSensor, _Coordinator(plate="unknown"))

    assert s.native_value is None


def test_a_recognised_plate_is_the_state():
    s = _sensor(DahuaLicensePlateSensor, _Coordinator(plate="ABC1234"))

    assert s.native_value == "ABC1234"


def test_what_the_camera_said_about_the_vehicle_is_the_attributes():
    data = {"plate": "ABC1234", "vehicle_color": "Black", "confidence": 92}
    s = _sensor(DahuaLicensePlateSensor, _Coordinator(plate="ABC1234", plate_data=data))

    attrs = s.extra_state_attributes

    assert {key: attrs[key] for key in data} == data
    # On top of the base's rather than instead of them. This used to be an
    # equality assertion, which is what let the sensor ship without `id` or
    # `integration` while reading as fully covered.
    assert attrs["integration"] == "dahua"
    assert attrs["id"] == "7"


def test_a_sensor_with_no_plate_yet_still_reports_the_base_attributes():
    """The real coordinator returns None before the first plate, and `**None` is a
    TypeError -- so the merge has to tolerate it.

    `_plate_data` is set to None by hand because this file's double coerces it to
    `{}` in its constructor, and `{}` merges fine. Testing against the double's
    coercion rather than the shape production actually produces would pass
    whatever the code did.
    """
    coordinator = _Coordinator()
    coordinator._plate_data = None
    assert coordinator.get_last_plate_data() is None, "the None path must be reached"

    attrs = _sensor(DahuaLicensePlateSensor, coordinator).extra_state_attributes

    assert attrs["integration"] == "dahua"
    assert attrs["id"] == "7"


def test_the_plate_sensor_is_pushed_not_polled():
    assert _sensor(DahuaLicensePlateSensor, _Coordinator()).should_poll is False


# --- and the leak --------------------------------------------------------------

async def test_the_plate_sensor_subscribes_when_added():
    c = _Coordinator()
    s = _sensor(DahuaLicensePlateSensor, c)

    await s.async_added_to_hass()

    assert len(c._plate_listeners) == 1


async def test_the_plate_sensor_lets_go_when_removed():
    """The bug. Without the remover the callback outlives the entity, and because
    `_plate_listeners` only appends, a reloaded entry accumulates one dead listener per
    reload. Each plate then logs "Error calling plate listener" that many times."""
    c = _Coordinator()
    s = _sensor(DahuaLicensePlateSensor, c)
    await s.async_added_to_hass()

    assert s._on_remove, "registered nothing to undo the subscription"
    for undo in list(s._on_remove):
        undo()

    assert c._plate_listeners == [], "the callback outlived the entity"


async def test_reloading_does_not_accumulate_dead_listeners():
    """Three times round, which is what a few options changes look like."""
    c = _Coordinator()

    for _ in range(3):
        s = _sensor(DahuaLicensePlateSensor, c)
        await s.async_added_to_hass()
        for undo in list(s._on_remove):
            undo()

    assert c._plate_listeners == [], (
        "%d dead listeners left behind, one per reload" % len(c._plate_listeners))
