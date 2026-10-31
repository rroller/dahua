"""A `dahua_event_received` event should say which device sent it.

The bus event carried `DeviceName` and nothing else that identifies the sender
on the CGI transport, so an automation listening to the bus could only tell two
devices apart by a name the user can change. #1121 is that exactly: a reporter
with two access controllers, "Dahua Gate" and "HA-LaQuinta", whose bus
automation fires for whichever one rang.

Home Assistant's own answer is `device_id`, holding the **device registry entry
id**, not a vendor serial. Read out of the installed core rather than assumed,
`rfxtrx/__init__.py`:

    device_entry = device_registry.async_get_device_by_identifier(
        (DOMAIN, device_id.unique_id), entry.entry_id,
    )
    if device_entry:
        event_data[ATTR_DEVICE_ID] = device_entry.id
    hass.bus.async_fire(EVENT_RFXTRX_EVENT, event_data)

deconz reads `event.data[ATTR_DEVICE_ID]` in its logbook, and zha, zwave_js,
tasmota, shelly, esphome and bthome all put the same key on events they fire. It
is also what a device trigger matches on, so this is the prerequisite for
offering one.

Measured on the recorder this was written against, read out of
`core.device_registry`: all sixteen of this integration's devices, the twelve
recorder channels included, are ordinary entries in `devices[]` and
`child_devices` is empty. So `async_get_device_by_identifier`, whose docstring
says it searches main devices only, does reach a channel.
"""

from unittest.mock import MagicMock

import pytest
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dahua.const import DOMAIN
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

SERIAL = "4E051BAPAZ8D724"


def _coordinator(hass, entry, channel=0, serial=SERIAL, name="Front Door"):
    """The double, with a real `hass` and a real config entry.

    `_remember_event` builds its own buffers through `getattr`, so nothing has
    to be pre-seeded for it.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator._channel = channel  # noqa: SLF001
    coordinator._serial_number = serial  # noqa: SLF001
    coordinator.get_device_name = lambda: name
    coordinator.get_address = lambda: "10.0.0.213"
    coordinator._handle_anpr_plate = lambda event: None  # noqa: SLF001
    coordinator._dispatch_event = lambda event, action: None  # noqa: SLF001
    return coordinator


def _listen(hass):
    seen = []
    hass.bus.async_listen(
        "dahua_event_received", lambda event: seen.append(dict(event.data))
    )
    return seen


async def _entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"address": "10.0.0.213"})
    entry.add_to_hass(hass)
    return entry


def _register(hass, entry, serial, name="Front Door"):
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, serial)},
        name=name,
    )


# --- the two transports ------------------------------------------------------


async def test_the_cgi_event_names_the_device_that_sent_it(hass):
    """The transport #1121 is on: an access controller or a camera."""
    entry = await _entry(hass)
    device = _register(hass, entry, SERIAL)
    coordinator = _coordinator(hass, entry)
    seen = _listen(hass)

    coordinator.handle_event({"Code": "_DoTalkAction_", "action": "Start"})
    await hass.async_block_till_done()

    assert len(seen) == 1
    assert seen[0][ATTR_DEVICE_ID] == device.id


async def test_the_doorbell_event_names_it_too(hass):
    """The DHIP transport. It already carried `serialNumber`, which is a device
    identifier but not the one Home Assistant matches on."""
    entry = await _entry(hass)
    device = _register(hass, entry, SERIAL)
    coordinator = _coordinator(hass, entry)
    seen = _listen(hass)

    coordinator.on_receive_vto_event(
        {"Code": "CallNoAnswered", "Action": "Start", "Index": 999}
    )
    await hass.async_block_till_done()

    assert len(seen) == 1
    assert seen[0][ATTR_DEVICE_ID] == device.id


# --- the case the whole thing is for ----------------------------------------


async def test_two_channels_on_one_host_name_different_devices(hass):
    """The neighbouring-but-wrong case, and the point of the feature.

    Channel 1's identifier is the serial with the channel appended, so the two
    coordinators must resolve to two registry ids. A lookup that fell back to
    the host's device would pass every test above and still leave #1121 broken.
    """
    entry = await _entry(hass)
    first = _register(hass, entry, SERIAL, "Dahua Gate")
    second = _register(hass, entry, f"{SERIAL}_1", "HA-LaQuinta")
    assert first.id != second.id, "two devices, two ids"

    gate = _coordinator(hass, entry, channel=0)
    laquinta = _coordinator(hass, entry, channel=1)
    seen = _listen(hass)

    gate.handle_event({"Code": "_DoTalkAction_", "action": "Start"})
    laquinta.handle_event({"Code": "_DoTalkAction_", "action": "Start"})
    await hass.async_block_till_done()

    assert [event[ATTR_DEVICE_ID] for event in seen] == [first.id, second.id]


# --- what it does when it cannot answer -------------------------------------


async def test_the_key_is_absent_before_the_device_is_registered(hass):
    """A device appears when its first entity is added, so an event can arrive
    before there is an id for it.

    Absent rather than None: an automation's `event_data` filter matches on the
    keys that are present, so a null would match nothing while reading as "sent
    by no device".
    """
    entry = await _entry(hass)
    coordinator = _coordinator(hass, entry)
    seen = _listen(hass)

    coordinator.handle_event({"Code": "VideoMotion", "action": "Start"})
    await hass.async_block_till_done()

    assert len(seen) == 1
    assert ATTR_DEVICE_ID not in seen[0]


async def test_a_doubles_mock_hass_puts_no_mock_on_the_bus(hass):
    """Much of this suite hands the coordinator a `MagicMock` for `hass`.

    A mock registry answers every lookup with a mock whose `.id` is another
    mock, so without the `str` guard the payload would carry that object and
    every assertion about the event would still pass.
    """
    entry = await _entry(hass)
    coordinator = _coordinator(hass, entry)
    coordinator.hass = MagicMock()

    coordinator.handle_event({"Code": "VideoMotion", "action": "Start"})

    fired = coordinator.hass.bus.fire.call_args[0][1]
    assert ATTR_DEVICE_ID not in fired


async def test_a_registry_that_raises_does_not_break_the_stream(hass):
    """This runs on the event stream, where an unguarded exception takes every
    camera on the host down until the stream reconnects (#705, #706)."""
    entry = await _entry(hass)
    coordinator = _coordinator(hass, entry)
    coordinator.config_entry = None  # reading entry_id off this raises
    seen = _listen(hass)

    coordinator.handle_event({"Code": "VideoMotion", "action": "Start"})
    await hass.async_block_till_done()

    assert len(seen) == 1
    assert ATTR_DEVICE_ID not in seen[0]


# --- the two couplings ------------------------------------------------------


async def test_the_remembered_event_is_what_the_device_sent(hass):
    """Diagnostics' buffer holds the payload as it arrived, which is why the
    device name is added after `_remember_event` and this must be too."""
    entry = await _entry(hass)
    device = _register(hass, entry, SERIAL)
    coordinator = _coordinator(hass, entry)
    seen = _listen(hass)

    coordinator.handle_event({"Code": "VideoMotion", "action": "Start"})
    await hass.async_block_till_done()

    remembered = list(coordinator._recent_events)[-1]["event"]  # noqa: SLF001
    assert seen[0][ATTR_DEVICE_ID] == device.id
    assert ATTR_DEVICE_ID not in remembered
    assert "DeviceName" not in remembered


async def test_the_identifier_is_the_one_entities_are_filed_against(hass):
    """Derived, not duplicated.

    `event_device_id` has to find the very device the entities are on. Asserting
    the expression twice would pass while both halves drifted together, so this
    registers a device under what `device_info` reports and looks it up with
    what the event path uses.
    """
    from custom_components.dahua.entity import DahuaBaseEntity

    entry = await _entry(hass)
    coordinator = _coordinator(hass, entry, channel=1)
    coordinator.stands_in_for_the_host = lambda: False
    coordinator.get_channel_model = lambda: "Coax"
    coordinator.get_model = lambda: "NVR"
    coordinator.get_configuration_url = lambda: None
    coordinator.get_channel_firmware_version = lambda: None
    coordinator.get_firmware_version = lambda: "4.0"
    coordinator.get_channel_serial_number = lambda: None
    coordinator.get_device_serial_number = lambda: SERIAL
    coordinator.configured_area_name = lambda: None

    entity = object.__new__(DahuaBaseEntity)
    entity._coordinator = coordinator  # noqa: SLF001

    (identifier,) = entity.device_info["identifiers"]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={identifier},
        name="Channel 2",
    )

    assert coordinator.event_device_id() == device.id


@pytest.mark.parametrize(
    "channel,expected_suffix",
    [(0, ""), (1, "_1"), (11, "_11")],
    ids=["host", "second-channel", "twelfth-channel"],
)
async def test_the_identifier_follows_the_serial_numbering(
    hass, channel, expected_suffix
):
    """Measured in `core.device_registry` on the recorder: the host is the bare
    serial and every other channel appends its index."""
    entry = await _entry(hass)
    coordinator = _coordinator(hass, entry, channel=channel)

    assert coordinator.device_identifier() == (DOMAIN, f"{SERIAL}{expected_suffix}")
