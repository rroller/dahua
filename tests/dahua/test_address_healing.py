"""A camera whose address changed should be fixable by re-adding it.

`_abort_if_unique_id_configured()` was called with no arguments, so re-adding a
device that had moved aborted with "already configured" and left the old address in
the entry. The entry stayed broken, and nothing pointed at Reconfigure as the way
out. #479 is somebody asking for the IP to be updatable at all.

Same serial is the same physical device, so the address we have just successfully
talked to is the right one, and passing it as `updates=` is the documented Home
Assistant way to heal it.

The wrinkle is that a recorder is **one entry per channel**. `updates=` heals the
entry whose unique_id matched and no more, so a sixteen channel recorder would come
back with one working channel and fifteen still pointing at an address the device no
longer has. `_async_heal_siblings` is that gap.
"""

from types import SimpleNamespace

from custom_components.dahua.config_flow import DahuaFlowHandler

SERIAL = "BC0A198PAJ779DF"
OLD = "192.168.0.213"
NEW = "192.168.0.99"


class _ConfigEntries:
    """Just enough of hass.config_entries to see what was rewritten."""

    def __init__(self):
        self.updated = []

    def async_update_entry(self, entry, data=None):
        self.updated.append((entry.unique_id, data.get("address")))
        entry.data = data


def _entry(unique_id, address=OLD):
    return SimpleNamespace(unique_id=unique_id,
                           data={"address": address, "channel": 0})


def _handler(entries):
    handler = DahuaFlowHandler()
    handler.hass = SimpleNamespace(config_entries=_ConfigEntries())
    handler._async_current_entries = lambda: list(entries)
    return handler


# --- the siblings ------------------------------------------------------------

def test_a_recorders_other_channels_follow_it():
    """The whole point: fifteen entries left on a dead address is not a fix."""
    entries = [_entry(SERIAL), _entry(SERIAL + "_1"), _entry(SERIAL + "_2")]
    handler = _handler(entries)

    handler._async_heal_siblings(SERIAL, NEW)

    assert handler.hass.config_entries.updated == [
        (SERIAL + "_1", NEW), (SERIAL + "_2", NEW)]


def test_the_matched_entry_is_left_to_updates():
    """Doing it here as well would rewrite the same entry twice."""
    entries = [_entry(SERIAL), _entry(SERIAL + "_1")]
    handler = _handler(entries)

    handler._async_heal_siblings(SERIAL, NEW)

    assert SERIAL not in [uid for uid, _addr in handler.hass.config_entries.updated]


def test_healing_from_a_channel_entry_finds_the_others():
    """The user may well re-add channel 3 rather than channel 0."""
    entries = [_entry(SERIAL), _entry(SERIAL + "_3"), _entry(SERIAL + "_4")]
    handler = _handler(entries)

    handler._async_heal_siblings(SERIAL + "_3", NEW)

    moved = sorted(uid for uid, _addr in handler.hass.config_entries.updated)
    assert moved == [SERIAL, SERIAL + "_4"]


def test_an_entry_already_on_the_right_address_is_not_rewritten():
    """Rewriting an entry reloads it, so doing it for nothing costs the user a
    reload of a camera that was working."""
    entries = [_entry(SERIAL), _entry(SERIAL + "_1", address=NEW)]
    handler = _handler(entries)

    handler._async_heal_siblings(SERIAL, NEW)

    assert handler.hass.config_entries.updated == []


def test_another_device_is_never_touched():
    entries = [_entry(SERIAL), _entry("OTHERSERIAL_1")]
    handler = _handler(entries)

    handler._async_heal_siblings(SERIAL, NEW)

    assert handler.hass.config_entries.updated == []


def test_a_serial_that_merely_starts_the_same_is_not_a_sibling():
    """BC0A198PAJ779DFX is a different recorder, not channel X of this one."""
    entries = [_entry(SERIAL), _entry(SERIAL + "X_1")]
    handler = _handler(entries)

    handler._async_heal_siblings(SERIAL, NEW)

    assert handler.hass.config_entries.updated == []


def test_a_bare_serial_is_not_mistaken_for_a_channel_suffix():
    """Only a trailing _<digits> is a channel. A serial ending in a digit, which
    Dahua serials do, must not be truncated."""
    entries = [_entry(SERIAL), _entry(SERIAL + "_1")]
    handler = _handler(entries)

    handler._async_heal_siblings(SERIAL, NEW)

    assert handler.hass.config_entries.updated == [(SERIAL + "_1", NEW)]


def test_only_a_numeric_tail_counts_as_a_channel():
    """A unique_id whose last segment is not a number is a whole serial, not a
    serial plus a channel. Splitting on the underscore alone would look for the
    siblings of a device that does not exist, and heal nothing."""
    entries = [_entry("ABC_XY"), _entry("ABC_XY_1"), _entry("ABC_9")]
    handler = _handler(entries)

    handler._async_heal_siblings("ABC_XY", NEW)

    moved = [uid for uid, _addr in handler.hass.config_entries.updated]
    assert moved == ["ABC_XY_1"], (
        "ABC_9 belongs to serial ABC, which is a different device")


def test_nothing_else_configured_is_no_work():
    handler = _handler([_entry(SERIAL)])

    handler._async_heal_siblings(SERIAL, NEW)

    assert handler.hass.config_entries.updated == []


# --- and the flow has to ask for it ------------------------------------------


def _stub_client(monkeypatch):
    """A device that answers identity and has no recorder table."""
    from custom_components.dahua import config_flow

    class _Device:
        def __init__(self, *args, **kwargs):
            self.identity_derived_from_credentials = False

        async def get_machine_name(self):
            return {"name": "Front"}

        async def async_get_system_info(self, strict_auth=False):
            return {"serialNumber": SERIAL}

        async def async_get_remote_devices(self):
            raise Exception("no table")

    class _Session:
        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(config_flow, "DahuaClient", _Device)
    monkeypatch.setattr(config_flow, "ClientSession", _Session)
    monkeypatch.setattr(config_flow, "TCPConnector", lambda **kwargs: None)


async def _driven(monkeypatch, entries):
    """Re-add the device through the real step, and report what happened."""
    _stub_client(monkeypatch)
    seen = {}
    handler = _handler(entries)

    async def _set_unique_id(unique_id):
        seen["unique_id"] = unique_id

    def _abort(**kwargs):
        seen["updates"] = kwargs.get("updates")

    async def _discover():
        return {"type": "progress"}

    handler.async_set_unique_id = _set_unique_id
    handler._abort_if_unique_id_configured = _abort
    handler.async_step_discover = _discover

    await handler.async_step_user({"username": "u", "password": "p",
                                   "address": NEW, "channel": 0})
    seen["moved"] = list(handler.hass.config_entries.updated)
    return seen


async def test_the_add_form_heals_the_siblings_too(monkeypatch):
    """Driving the real step with a recorder's other channels present. Without
    this, a mutation removing the sibling call passes everything: the tests above
    call the helper directly, and the one below has no siblings to move."""
    seen = await _driven(monkeypatch, [_entry(SERIAL), _entry(SERIAL + "_1"),
                                       _entry(SERIAL + "_2")])

    assert seen["moved"] == [(SERIAL + "_1", NEW), (SERIAL + "_2", NEW)]


async def test_the_add_form_offers_the_new_address_to_the_abort(monkeypatch):
    """The helper is only useful if `updates=` is actually passed, and a mutation
    that dropped it survived every test above."""
    from custom_components.dahua import config_flow

    seen = {}

    class _Device:
        def __init__(self, *args, **kwargs):
            self.identity_derived_from_credentials = False

        async def get_machine_name(self):
            return {"name": "Front"}

        async def async_get_system_info(self, strict_auth=False):
            return {"serialNumber": SERIAL}

        async def async_get_remote_devices(self):
            raise Exception("no table")

    class _Session:
        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(config_flow, "DahuaClient", _Device)
    monkeypatch.setattr(config_flow, "ClientSession", _Session)
    monkeypatch.setattr(config_flow, "TCPConnector", lambda **kwargs: None)

    handler = _handler([])

    async def _set_unique_id(unique_id):
        seen["unique_id"] = unique_id

    def _abort(**kwargs):
        seen["updates"] = kwargs.get("updates")

    handler.async_set_unique_id = _set_unique_id
    handler._abort_if_unique_id_configured = _abort
    async def _discover():
        return {"type": "progress"}

    handler.async_step_discover = _discover

    await handler.async_step_user({"username": "u", "password": "p",
                                   "address": NEW, "channel": 0})

    assert seen["unique_id"] == SERIAL
    assert seen["updates"] == {"address": NEW}, (
        "the address just confirmed has to reach the abort, or the entry keeps the "
        "old one")
