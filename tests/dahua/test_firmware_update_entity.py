"""The firmware update entity reports; it does not install.

The device learns of a newer image from its own cloud OTA check and leaves the
record in a local config table. These tests cover the two halves that are easy
to get wrong: joining the two version fields the record carries, and ordering
Dahua firmware strings, which Home Assistant's own ``AwesomeVersion`` cannot do
-- it calls ``2.800.0000016.0.R`` unknown and raises, and the update component
reads a raised comparison as "an update is available".
"""

import time
from types import SimpleNamespace

import pytest
from homeassistant.components.update import UpdateEntityFeature

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import entity as entity_module
from custom_components.dahua import update as update_module
from custom_components.dahua.const import FIRMWARE_UPGRADE_REFRESH_SECONDS
from custom_components.dahua.dahua_utils import (
    clean_firmware_version,
    cloud_upgrade_version,
    firmware_is_newer,
)
from custom_components.dahua.rpc2 import DahuaRpc2Client, Rpc2MethodRefused
from custom_components.dahua.update import DahuaFirmwareUpdateEntity

from . import adds_entities

# --- reading the two version strings -----------------------------------------


def test_the_build_date_is_not_part_of_the_version():
    assert (
        clean_firmware_version("2.800.0000016.0.R,build:2020-06-05")
        == "2.800.0000016.0.R"
    )


def test_a_firmware_without_a_build_date_is_returned_as_is():
    assert clean_firmware_version("3.120.0000.0.R") == "3.120.0000.0.R"


def test_nothing_is_not_a_version():
    assert clean_firmware_version("") == ""
    assert clean_firmware_version(None) == ""


def test_the_cloud_record_names_a_complete_version():
    """LastVersion and LastSubVersion are two halves of one string."""
    assert (
        cloud_upgrade_version({"LastVersion": "2.800.0000016.0", "LastSubVersion": "R"})
        == "2.800.0000016.0.R"
    )


def test_a_sub_version_already_inside_the_main_one_is_not_doubled():
    assert (
        cloud_upgrade_version(
            {"LastVersion": "2.800.0000016.0.R", "LastSubVersion": "R"}
        )
        == "2.800.0000016.0.R"
    )


def test_a_record_without_a_version_is_unknown():
    assert cloud_upgrade_version({"AutoCheck": True}) is None
    assert cloud_upgrade_version(None) is None


def test_a_build_date_in_the_record_is_not_part_of_the_version():
    """It would compare as extra numeric components and read as newer."""
    assert (
        cloud_upgrade_version({"LastVersion": "2.800.0000016.0.R,build:2020-06-05"})
        == "2.800.0000016.0.R"
    )


# --- the comparison Home Assistant cannot make -------------------------------


def test_the_same_version_is_not_newer():
    assert firmware_is_newer("2.800.0000016.0.R", "2.800.0000016.0.R") is False


def test_a_cloud_record_missing_the_trailing_component_is_not_newer():
    """``2.800.0000016.0`` must not read as an update over ``2.800.0000016.0.R``."""
    assert firmware_is_newer("2.800.0000016.0", "2.800.0000016.0.R") is False


def test_a_higher_build_is_newer():
    assert firmware_is_newer("2.820.0000000.32.R", "2.800.0000016.0.R") is True


def test_an_unknown_version_is_never_newer():
    assert firmware_is_newer(None, "2.800.0000016.0.R") is False
    assert firmware_is_newer("2.800.0000016.0.R", None) is False
    assert firmware_is_newer("", "2.800.0000016.0.R") is False


# --- the coordinator's accessors ---------------------------------------------


def test_the_accessors_carry_the_flag_and_the_version():
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._supports_cloud_upgrade = True
    coordinator._cloud_firmware_version = "2.820.0000000.32.R"

    assert coordinator.supports_cloud_upgrade() is True
    assert coordinator.get_cloud_firmware_version() == "2.820.0000000.32.R"


# --- the entity is gated on the capability -----------------------------------


@pytest.fixture(autouse=True)
def _skip_ha_plumbing(monkeypatch):
    monkeypatch.setattr(
        entity_module.DahuaBaseEntity, "__init__", lambda self, c, e: None
    )


def _coordinator(
    supports=True, installed="2.800.0000016.0.R,build:2020-06-05", latest=None
):
    return SimpleNamespace(
        get_device_name=lambda: "Front Door",
        get_serial_number=lambda: "SERIAL1",
        get_firmware_version=lambda: installed,
        get_cloud_firmware_version=lambda: latest,
        supports_cloud_upgrade=lambda: supports,
        subentry_id=None,
    )


def _setup(coordinator):
    hass = type("H", (), {"data": {}})()
    # One entry owns one coordinator per channel, and setup walks them all.
    entry = type("E", (), {"entry_id": "e1", "runtime_data": {0: coordinator}})()
    added = []
    return hass, entry, added


async def test_no_update_entity_without_a_cloud_record():
    """A device with no record could only ever read unknown, which is worse
    than no entity -- the same reasoning as the profile sensor gate."""
    hass, entry, added = _setup(_coordinator(supports=False))

    await update_module.async_setup_entry(hass, entry, adds_entities(added))

    assert not any(isinstance(e, DahuaFirmwareUpdateEntity) for e in added)


async def test_the_update_entity_is_added_when_the_device_has_one():
    hass, entry, added = _setup(
        _coordinator(supports=True, latest="2.820.0000000.32.R")
    )

    await update_module.async_setup_entry(hass, entry, adds_entities(added))

    assert any(isinstance(e, DahuaFirmwareUpdateEntity) for e in added)


def _entity(coordinator):
    entity = object.__new__(DahuaFirmwareUpdateEntity)
    entity._coordinator = coordinator
    return entity


def test_installed_version_drops_the_build_date():
    assert _entity(_coordinator()).installed_version == "2.800.0000016.0.R"


def test_installed_version_is_unknown_when_the_device_reported_none():
    assert _entity(_coordinator(installed="")).installed_version is None


def test_latest_version_is_what_the_cloud_record_named():
    entity = _entity(_coordinator(latest="2.820.0000000.32.R"))

    assert entity.latest_version == "2.820.0000000.32.R"


def test_the_entity_has_a_translatable_name():
    """Home Assistant composes "<device> <entity>" from has_entity_name, so the
    entity declares only its own half, which the language files can reach."""
    entity = _entity(_coordinator())

    assert entity.translation_key == "firmware_update"
    assert entity.unique_id == "SERIAL1_firmware_update"


def test_it_reports_and_installs_nothing():
    """No INSTALL feature: a wrong image bricks the camera."""
    assert _entity(_coordinator()).supported_features == UpdateEntityFeature(0)


def test_ordering_does_not_take_the_awesomeversion_route():
    entity = _entity(_coordinator(latest="2.820.0000000.32.R"))

    assert entity.version_is_newer("2.820.0000000.32.R", "2.800.0000016.0.R") is True
    assert entity.version_is_newer("2.800.0000016.0.R", "2.800.0000016.0.R") is False


# --- the probe, where a bad device must not take setup down ------------------


class _ProbeClient:
    def __init__(self, info=None, error=None):
        self._info = info
        self._error = error

    async def async_get_cloud_upgrade_info(self):
        if self._error is not None:
            raise self._error
        return self._info


def _probe_coordinator(client):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = client
    coordinator._probe_refusals = {}
    return coordinator


async def test_the_probe_records_the_version_it_found():
    coordinator = _probe_coordinator(
        _ProbeClient(info={"LastVersion": "2.800.0000016.0", "LastSubVersion": "R"})
    )

    await coordinator._async_probe_cloud_upgrade()

    assert coordinator.supports_cloud_upgrade() is True
    assert coordinator.get_cloud_firmware_version() == "2.800.0000016.0.R"


async def test_a_record_without_a_version_is_not_enough_for_an_entity():
    """A table that exists but has not been filled in could only read unknown."""
    coordinator = _probe_coordinator(_ProbeClient(info={"AutoCheck": True}))

    await coordinator._async_probe_cloud_upgrade()

    assert coordinator.supports_cloud_upgrade() is False
    assert coordinator.get_cloud_firmware_version() is None


async def test_a_probe_refusal_is_recorded_rather_than_raised():
    coordinator = _probe_coordinator(
        _ProbeClient(error=ConnectionError("no _DHCloudUpgrade_ table"))
    )

    await coordinator._async_probe_cloud_upgrade()

    assert coordinator.supports_cloud_upgrade() is False
    assert coordinator._probe_refusals["cloud_upgrade"]["error"] == "ConnectionError"


async def test_an_rpc2_refusal_is_recorded_as_an_answer():
    """Rpc2MethodRefused carries a code, not a status, and it is still the
    device answering: this firmware does not serve the table."""
    coordinator = _probe_coordinator(
        _ProbeClient(
            error=Rpc2MethodRefused(
                "Dahua RPC2 method configManager.getConfig returned result=false",
                code=268959743,
                message="Unknown error",
            )
        )
    )

    await coordinator._async_probe_cloud_upgrade()

    refusal = coordinator._probe_refusals["cloud_upgrade"]
    assert coordinator.supports_cloud_upgrade() is False
    assert refusal["answered"] is True
    assert refusal["status"] == 268959743


# --- the reuse window --------------------------------------------------------


def test_the_record_is_reused_until_the_window_expires():
    """The device rewrites it only after its own OTA check, so the answer is
    reused for hours -- and read again once the window has passed."""
    coordinator = object.__new__(DahuaDataUpdateCoordinator)

    coordinator._cloud_upgrade_checked_at = None
    assert coordinator._cloud_upgrade_read_is_due() is True, "never read"

    coordinator._cloud_upgrade_checked_at = time.monotonic()
    assert coordinator._cloud_upgrade_read_is_due() is False, "just read"

    coordinator._cloud_upgrade_checked_at = (
        time.monotonic() - FIRMWARE_UPGRADE_REFRESH_SECONDS - 1
    )
    assert coordinator._cloud_upgrade_read_is_due() is True, "window passed"


# --- the RPC2 read -----------------------------------------------------------


def _rpc2_returning(payload):
    client = object.__new__(DahuaRpc2Client)
    client._session_id = "session"

    async def get_config(params):
        assert params == {"name": "_DHCloudUpgrade_"}
        return payload

    client.get_config = get_config
    return client


async def test_the_rpc2_read_returns_the_first_record():
    client = _rpc2_returning({"table": [{"LastVersion": "2.8", "LastSubVersion": "R"}]})

    assert await client.get_cloud_upgrade_info() == {
        "LastVersion": "2.8",
        "LastSubVersion": "R",
    }


@pytest.mark.parametrize("payload", [{"table": []}, {"table": "x"}, {}])
async def test_the_rpc2_read_rejects_a_table_it_cannot_use(payload):
    client = _rpc2_returning(payload)

    with pytest.raises(ValueError):
        await client.get_cloud_upgrade_info()
