"""Being found by DHCP, and being moved to an area.

Two things in config_flow.py that nothing drove. `async_step_dhcp` is how a device gets
offered without anybody typing an address, and `_async_move_device` is how an area chosen
in the options actually reaches the device registry -- it is only ever stubbed out
elsewhere, never run.

Each has a "must not" at its centre:

  * the DHCP step probes the device unauthenticated, and only one of the five devices it
    was written against answers, so nothing in it may depend on a reply
  * the area move is a no-op when the value has not changed, so opening options and
    pressing submit does not re-file a device somebody moved by hand
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import config_flow as flow_module
from custom_components.dahua.config_flow import (
    DahuaFlowHandler,
    DahuaOptionsFlowHandler,
)
from custom_components.dahua.const import CONF_ADDRESS, CONF_AREA, CONF_PORT

SERIAL = "SERIAL1"


class _Discovery:
    def __init__(
        self, ip="10.0.0.5", macaddress="aa:bb:cc:dd:ee:ff", hostname="doorbell"
    ):
        self.ip = ip
        self.macaddress = macaddress
        self.hostname = hostname


def _dhcp_handler(monkeypatch, identity=None, serial_entries=(), address_match=None):
    """A flow handler with only the pieces async_step_dhcp uses.

    `identity` is what the unauthenticated probe answers; {} is the common case, because
    most devices do not answer it at all.
    """
    handler = object.__new__(DahuaFlowHandler)
    handler.context = {}
    handler.seen = SimpleNamespace(
        unique_id=None, aborted=None, matched=None, reached_user=False
    )

    async def _set_unique_id(unique_id, **kwargs):
        handler.seen.unique_id = unique_id

    def _entries_for_serial(serial):
        return list(serial_entries) if serial in [SERIAL] else []

    def _abort_entries_match(match):
        handler.seen.matched = match
        if address_match is not None:
            raise _Aborted("already_configured")

    def _abort(reason=None):
        handler.seen.aborted = reason
        return {"type": "abort", "reason": reason}

    async def _step_user(user_input=None):
        handler.seen.reached_user = True
        return {"type": "form", "step_id": "user"}

    handler.async_set_unique_id = _set_unique_id
    handler._async_entries_for_serial = _entries_for_serial
    handler._async_abort_entries_match = _abort_entries_match
    handler.async_abort = _abort
    handler.async_step_user = _step_user

    async def _probe(address):
        return dict(identity or {})

    monkeypatch.setattr(flow_module, "async_probe_identity", _probe)
    return handler


class _Aborted(Exception):
    """Home Assistant's own abort helper raises; this stands in for that."""


# --- being found --------------------------------------------------------------


async def test_a_device_that_says_nothing_is_still_offered(monkeypatch):
    """The property the comment in the source insists on: only one of five devices
    answers the probe, so a silent one must still reach the form."""
    handler = _dhcp_handler(monkeypatch, identity={})

    await handler.async_step_dhcp(_Discovery())

    assert handler.seen.reached_user, "a silent device was not offered at all"
    assert handler._discovered[CONF_ADDRESS] == "10.0.0.5"


async def test_a_silent_device_gets_no_invented_port(monkeypatch):
    """Absent rather than guessed. The form's own default applies, and writing a port the
    device never reported would be worse than leaving it."""
    handler = _dhcp_handler(monkeypatch, identity={})

    await handler.async_step_dhcp(_Discovery())

    assert CONF_PORT not in handler._discovered


async def test_a_device_that_answers_prefills_its_port(monkeypatch):
    """The whole point of probing: a device on 8000 is offered with 8000 in the form
    rather than the user finding out by failing to connect."""
    handler = _dhcp_handler(monkeypatch, identity={"HttpPort": 8000})

    await handler.async_step_dhcp(_Discovery())

    assert handler._discovered[CONF_PORT] == "8000"


async def test_a_device_already_configured_by_serial_is_not_offered_again(monkeypatch):
    """The serial is the identity. An address can change with a DHCP lease, so matching on
    it alone would offer a device somebody already has every time it renews."""
    handler = _dhcp_handler(
        monkeypatch, identity={"SerialNo": SERIAL}, serial_entries=[object()]
    )

    result = await handler.async_step_dhcp(_Discovery())

    assert result["reason"] == "already_configured"
    assert not handler.seen.reached_user


async def test_a_serial_with_whitespace_still_matches(monkeypatch):
    """Devices pad these fields."""
    handler = _dhcp_handler(
        monkeypatch, identity={"SerialNo": "  %s  " % SERIAL}, serial_entries=[object()]
    )

    result = await handler.async_step_dhcp(_Discovery())

    assert result["reason"] == "already_configured"


async def test_an_address_already_configured_is_not_nagged_about(monkeypatch):
    """The fallback when there is no serial to go on: an entry on this address is reason
    enough. Checked by asking Home Assistant, which raises rather than returning."""
    handler = _dhcp_handler(monkeypatch, identity={}, address_match=True)

    with pytest.raises(_Aborted):
        await handler.async_step_dhcp(_Discovery())

    assert handler.seen.matched == {CONF_ADDRESS: "10.0.0.5"}


async def test_repeated_announcements_do_not_become_repeated_cards(monkeypatch):
    """A provisional unique id off the MAC. The real one is the serial and is set later by
    the step that logs in; this only stops three announcements becoming three cards."""
    handler = _dhcp_handler(monkeypatch, identity={})

    await handler.async_step_dhcp(_Discovery(macaddress="aa:bb:cc:dd:ee:ff"))

    assert handler.seen.unique_id, "no unique id was set, so announcements would stack"


@pytest.mark.parametrize(
    "identity,hostname,expected",
    [
        ({"DeviceType": "VTO2211G-WP"}, "doorbell", "VTO2211G-WP"),
        ({"MachineName": "Front Door"}, "doorbell", "Front Door"),
        ({}, "doorbell", "doorbell"),
        ({}, None, "Dahua device"),
    ],
)
async def test_the_card_is_named_by_the_best_thing_available(
    monkeypatch, identity, hostname, expected
):
    """Four fallbacks deep, because the card is what somebody has to recognise their own
    camera from in a list of discoveries."""
    handler = _dhcp_handler(monkeypatch, identity=identity)

    await handler.async_step_dhcp(_Discovery(hostname=hostname))

    assert handler.context["title_placeholders"]["name"] == expected


async def test_the_card_also_carries_the_address(monkeypatch):
    """Two identical models on one network are told apart by it."""
    handler = _dhcp_handler(monkeypatch, identity={})

    await handler.async_step_dhcp(_Discovery(ip="10.0.0.9"))

    assert handler.context["title_placeholders"]["address"] == "10.0.0.9"


# --- being moved to an area ---------------------------------------------------


class _Registry:
    def __init__(self, device=None):
        self._device = device
        self.updates = []

    def async_get_device(self, identifiers=None):
        return self._device

    def async_update_device(self, device_id, **kwargs):
        self.updates.append((device_id, kwargs))


def _options_handler(monkeypatch, stored=None, options=None, loaded=True, device=None):
    coordinator = SimpleNamespace(get_serial_number=lambda: SERIAL)
    entry = SimpleNamespace(
        data={CONF_AREA: stored} if stored is not None else {},
        options={},
        entry_id="e",
        runtime_data={0: coordinator} if loaded else {},
    )
    monkeypatch.setattr(
        DahuaOptionsFlowHandler, "config_entry", property(lambda self: entry)
    )
    handler = DahuaOptionsFlowHandler()
    handler.options = dict(options or {})
    handler.hass = object()
    registry = _Registry(device)
    monkeypatch.setattr(flow_module.dr, "async_get", lambda _h: registry)
    return handler, registry


async def test_choosing_an_area_moves_the_device(monkeypatch):
    handler, registry = _options_handler(monkeypatch, device=SimpleNamespace(id="dev1"))

    await handler._async_move_device("kitchen")

    assert registry.updates == [("dev1", {"area_id": "kitchen"})]


async def test_submitting_the_same_area_again_moves_nothing(monkeypatch):
    """The no-op the docstring is about. Somebody who moved the device by hand and then
    opens options for an unrelated setting must not have it re-filed under them."""
    handler, registry = _options_handler(
        monkeypatch, stored="kitchen", device=SimpleNamespace(id="dev1")
    )

    await handler._async_move_device("kitchen")

    assert registry.updates == []


async def test_the_stored_option_wins_over_the_entry_data(monkeypatch):
    """Options are where a later choice lives, so a value already chosen there is what
    "unchanged" is measured against."""
    handler, registry = _options_handler(
        monkeypatch,
        stored="hall",
        options={CONF_AREA: "kitchen"},
        device=SimpleNamespace(id="dev1"),
    )

    await handler._async_move_device("kitchen")

    assert registry.updates == []


async def test_choosing_no_area_moves_nothing(monkeypatch):
    handler, registry = _options_handler(monkeypatch, device=SimpleNamespace(id="dev1"))

    await handler._async_move_device("")
    await handler._async_move_device(None)

    assert registry.updates == []


async def test_an_entry_that_is_not_loaded_is_left_for_later(monkeypatch):
    """There is no device to move yet. The option is still stored, and the config flow's
    suggested_area applies whenever the device is next created, so this is a deferral
    rather than a loss."""
    handler, registry = _options_handler(monkeypatch, loaded=False)

    await handler._async_move_device("kitchen")

    assert registry.updates == []


async def test_a_device_not_in_the_registry_is_not_invented(monkeypatch):
    handler, registry = _options_handler(monkeypatch, device=None)

    await handler._async_move_device("kitchen")

    assert registry.updates == []
