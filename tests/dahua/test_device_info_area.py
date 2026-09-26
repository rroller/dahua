"""The area chosen while adding a device is where that device lands.

`device_info` is the single chokepoint for every entity on every platform here:
nothing overrides it, and every entity class inherits `DahuaBaseEntity` directly
or through `DahuaEventDrivenEntity`. It also had **no tests at all** before this
file, so the identifier tuple, the name source and the rest were free to change
silently. These pin the new key and the six that were already there.

**Why `suggested_area` and not a registry write.** Home Assistant honours
`suggested_area` only when it *creates* the device, which is exactly the moment
this is for, and means a device the user later moves is never argued with.
Changing the area of a device that already exists is the options flow's job,
through `async_update_device`, because `suggested_area` would be ignored there.

**Why a name and not the id.** The picker in the config flow returns an area_id;
`suggested_area` is matched on the area's *name*, and an unrecognised string makes
Home Assistant create a new area called that. So passing the id straight through
would quietly produce a second area named `front_garden` beside the real one.
"""
from types import SimpleNamespace

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.const import CONF_AREA, DOMAIN
from custom_components.dahua.entity import DahuaBaseEntity

SERIAL = "4L03CB4PAZC9E8F"


class _Coordinator:
    """Everything device_info reads, and nothing else."""

    def __init__(self, area_name=None):
        self._area_name = area_name

    def get_serial_number(self):
        return SERIAL

    def get_device_name(self):
        return "Front Door"

    def get_model(self):
        return "IPC-HDW5831R-ZE"

    def get_address(self):
        return "10.0.0.5"

    def get_firmware_version(self):
        return "2.800.0"

    def configured_area_name(self):
        return self._area_name


def _info(area_name=None):
    entity = object.__new__(DahuaBaseEntity)
    entity._coordinator = _Coordinator(area_name)
    return entity.device_info


# --- the new key ---------------------------------------------------------------

def test_the_chosen_area_is_suggested():
    assert _info("Front Garden")["suggested_area"] == "Front Garden"


def test_a_device_with_no_area_suggests_nothing():
    """Absent, not None. Every key here is handed to async_get_or_create as
    given, and a None would be a value rather than a silence."""
    assert "suggested_area" not in _info()


def test_an_area_that_no_longer_exists_suggests_nothing():
    """configured_area_name returns None when the id no longer resolves, which
    is what stops a deleted area being recreated from a stale id."""
    assert "suggested_area" not in _info(None)


# --- and the six that were already there --------------------------------------

def test_the_identity_is_unchanged():
    info = _info("Front Garden")

    assert info["identifiers"] == {(DOMAIN, SERIAL)}
    assert info["name"] == "Front Door"
    assert info["model"] == "IPC-HDW5831R-ZE"
    assert info["manufacturer"] == "Dahua"
    assert info["sw_version"] == "2.800.0"
    assert info["configuration_url"] == "http://10.0.0.5"


def test_an_area_adds_exactly_one_key():
    """Nothing else moves when an area is set."""
    without = _info()
    with_area = _info("Front Garden")

    assert set(with_area) - set(without) == {"suggested_area"}
    assert all(with_area[key] == without[key] for key in without)


# --- where the name comes from ------------------------------------------------

def _coordinator_with(options=None, data=None, areas=None):
    """A real coordinator, with only the config entry and hass stood in for."""
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.config_entry = SimpleNamespace(
        options=dict(options or {}), data=dict(data or {}))
    coordinator.hass = SimpleNamespace(data={}, _areas=dict(areas or {}))
    return coordinator


def test_the_area_id_comes_from_the_options_first():
    """Options win over data, like every other setting that can change after
    setup."""
    coordinator = _coordinator_with(
        options={CONF_AREA: "chosen_later"}, data={CONF_AREA: "chosen_at_setup"})

    assert coordinator.get_configured_area() == "chosen_later"


def test_the_setup_time_area_is_used_when_no_option_was_set():
    coordinator = _coordinator_with(data={CONF_AREA: "chosen_at_setup"})

    assert coordinator.get_configured_area() == "chosen_at_setup"


def test_no_area_anywhere_reads_as_none():
    assert _coordinator_with().get_configured_area() is None


def test_a_blank_area_reads_as_none_not_as_an_empty_string():
    """A cleared picker stores "", which must not be passed on as a value."""
    assert _coordinator_with(options={CONF_AREA: ""}).get_configured_area() is None
