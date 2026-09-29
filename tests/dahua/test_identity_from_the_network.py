"""A camera should not change identity when its password does.

A device that will not answer `magicBox.cgi` gets an identity synthesised in three places
in client.py:

    md5("{address}_{rtsp_port}_{username}_{password}")

The fallback is what supports those cameras at all. What it is *derived from* is the bug:
change the password, or the RTSP port, or the username, and the same physical camera is a
different device. `_abort_if_unique_id_configured` cannot see the collision, so you get a
second entry with every entity suffixed `_2` and the first one unavailable for ever. #805
is that, and #320 is somebody living it.

**Measured, which is what makes this fixable:** `DHDiscover.search` on UDP 37810 needs no
credentials and returns `SerialNo`. On the recorder here it is byte for byte what
`magicBox.cgi` returns. The reply carries no MAC at all, which is what @rroller asked
about, so the serial is the better answer and it is already extracted by `parse_reply`.

Two limits, both deliberate:

- It is **UDP on the local subnet**, so a camera behind a router answers nothing and keeps
  the hash. Nothing that works today stops working.
- The migration **never merges or deletes**. If the serial is already held by another
  entry then the camera is configured twice, which is #320, and both entries have their
  own entities and history. Naming it and stopping is the only safe automatic move.
"""

import ast
import asyncio
import io
from types import SimpleNamespace

import pytest

import custom_components.dahua as dahua
import custom_components.dahua.config_flow as flow_module
import custom_components.dahua.discovery as discovery_module
from custom_components.dahua import (
    async_migrate_synthesised_unique_id,
    async_network_identity,
    is_synthesised_identity,
)
from custom_components.dahua.const import (
    CONF_ADDRESS,
    CONF_CHANNEL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_RTSP_PORT,
    CONF_USERNAME,
)

HASH = "4f3a9c8ecafe4f3a9c8ecafe4f3a9c8e"
SERIAL = "BC0A198PAJ779DF"
ADDRESS = "10.0.0.5"


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """Asked once per host and cached for the process, so it outlives a test too."""
    dahua._HOST_NETWORK_IDENTITY.clear()
    dahua._HOST_NETWORK_IDENTITY_LOCKS.clear()
    yield
    dahua._HOST_NETWORK_IDENTITY.clear()
    dahua._HOST_NETWORK_IDENTITY_LOCKS.clear()


# --- telling a hash from a serial -------------------------------------------

@pytest.mark.parametrize("value", [HASH, HASH + "_1", HASH + "_11"])
def test_a_credentials_hash_is_recognised(value):
    assert is_synthesised_identity(value) is True


@pytest.mark.parametrize("value", [
    SERIAL, SERIAL + "_3", "3C06520PAN00001", "", None, 0,
    HASH[:31], HASH + "abc", HASH.upper(), HASH + "_", HASH + "_x",
])
def test_anything_else_is_left_alone(value):
    """A real serial is shorter, upper case and not hex. `md5().hexdigest()` is lower
    case, so an upper case 32 character string is not one of ours."""
    assert is_synthesised_identity(value) is False


# --- asked once per host ----------------------------------------------------

def _stub_probe(monkeypatch, answer, record=None):
    async def _probe(address):
        if record is not None:
            record.append(address)
        return dict(answer) if answer else {}

    monkeypatch.setattr(discovery_module, "async_probe", _probe)


async def test_the_probe_runs_once_for_a_host(monkeypatch):
    """A recorder has one entry per channel and they all set up at once. Eleven probes
    for one answer would be eleven UDP timeouts on a device that does not reply."""
    calls = []
    _stub_probe(monkeypatch, {"SerialNo": SERIAL}, calls)

    await async_network_identity(SimpleNamespace(), ADDRESS)
    await async_network_identity(SimpleNamespace(), ADDRESS)

    assert calls == [ADDRESS]


async def test_a_silent_device_is_not_asked_again(monkeypatch):
    """The empty answer is cached deliberately: it will be empty for the next channel
    too, and the cost of finding that out is a timeout each time."""
    calls = []
    _stub_probe(monkeypatch, {}, calls)

    await async_network_identity(SimpleNamespace(), ADDRESS)
    await async_network_identity(SimpleNamespace(), ADDRESS)

    assert calls == [ADDRESS]


async def test_two_hosts_are_asked_separately(monkeypatch):
    calls = []
    _stub_probe(monkeypatch, {"SerialNo": SERIAL}, calls)

    await async_network_identity(SimpleNamespace(), ADDRESS)
    await async_network_identity(SimpleNamespace(), "10.0.0.9")

    assert sorted(calls) == ["10.0.0.5", "10.0.0.9"]


async def test_simultaneous_callers_share_one_probe(monkeypatch):
    """Eleven coordinators start together, so the check-then-probe has to be locked or
    they all miss the cache at the same instant."""
    calls = []

    async def _probe(address):
        calls.append(address)
        await asyncio.sleep(0)
        return {"SerialNo": SERIAL}

    monkeypatch.setattr(discovery_module, "async_probe", _probe)

    await asyncio.gather(*[
        async_network_identity(SimpleNamespace(), ADDRESS) for _ in range(5)])

    assert calls == [ADDRESS]


# --- new adds prefer the network serial too --------------------------------

class _Client:
    def __init__(self, serial, *args, **kwargs):
        self._serial = serial
        self.identity_derived_from_credentials = serial == HASH

    async def get_machine_name(self):
        return {"name": "hashy" if self._serial == HASH else "Front Door"}

    async def async_get_system_info(self, strict_auth=False):
        return {"serialNumber": self._serial}


def _flow(monkeypatch, serial, probe_answer, probed=None):
    class _Session:
        async def close(self):
            return None

    monkeypatch.setattr(flow_module, "ClientSession", lambda **kw: _Session())
    monkeypatch.setattr(flow_module, "TCPConnector", lambda **kw: None)
    monkeypatch.setattr(flow_module, "DahuaClient",
                        lambda *a, **kw: _Client(serial))

    async def _probe(address):
        if probed is not None:
            probed.append(address)
        return dict(probe_answer) if probe_answer else {}

    monkeypatch.setattr(flow_module, "async_probe_identity", _probe)
    return flow_module.DahuaFlowHandler()


async def test_a_new_add_prefers_the_network_serial(monkeypatch):
    handler = _flow(monkeypatch, HASH, {"SerialNo": SERIAL})

    data, error = await handler._test_credentials(
        "admin", "pw", ADDRESS, "80", "554", 0)

    assert error is None
    assert data["serialNumber"] == SERIAL, "a new entry took the credentials hash"


async def test_a_device_that_identifies_itself_is_not_probed(monkeypatch):
    probed = []
    handler = _flow(monkeypatch, SERIAL, {"SerialNo": "SOMETHING-ELSE"}, probed)

    data, _error = await handler._test_credentials(
        "admin", "pw", ADDRESS, "80", "554", 0)

    assert probed == [], "it probed a device that answered over HTTP"
    assert data["serialNumber"] == SERIAL


async def test_a_new_add_keeps_the_hash_when_the_probe_is_silent(monkeypatch):
    handler = _flow(monkeypatch, HASH, {})

    data, _error = await handler._test_credentials(
        "admin", "pw", ADDRESS, "80", "554", 0)

    assert data["serialNumber"] == HASH


# --- the migration ----------------------------------------------------------

def _entry(unique_id, channel=0, address=ADDRESS, entry_id="e1", title="Cam"):
    return SimpleNamespace(
        unique_id=unique_id, entry_id=entry_id, title=title,
        data={CONF_ADDRESS: address, CONF_CHANNEL: channel})


def _hass(entries=()):
    updated = []

    def async_update_entry(entry, **kwargs):
        updated.append((entry.entry_id, kwargs))
        if "unique_id" in kwargs:
            entry.unique_id = kwargs["unique_id"]

    return SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=lambda domain: list(entries),
            async_update_entry=async_update_entry),
        _updated=updated)


async def test_a_device_with_a_real_serial_is_never_even_probed(monkeypatch):
    """The cost guarantee. Every healthy entry runs this on every setup, so it has to
    stop before the network on the first line."""
    calls = []
    _stub_probe(monkeypatch, {"SerialNo": SERIAL}, calls)
    hass = _hass()

    await async_migrate_synthesised_unique_id(hass, _entry(SERIAL))

    assert calls == [], "it probed a device that already knows who it is"
    assert hass._updated == []


async def test_a_hashed_identity_is_moved_onto_the_serial(monkeypatch):
    _stub_probe(monkeypatch, {"SerialNo": SERIAL})
    hass = _hass()
    entry = _entry(HASH)

    await async_migrate_synthesised_unique_id(hass, entry)

    assert hass._updated == [("e1", {"unique_id": SERIAL})]


async def test_the_channel_survives_the_move(monkeypatch):
    """One entry per channel, and the index is part of the id. Dropping it would give
    every channel of a recorder the same identity."""
    _stub_probe(monkeypatch, {"SerialNo": SERIAL})
    hass = _hass()

    await async_migrate_synthesised_unique_id(hass, _entry(HASH + "_3", channel=3))

    assert hass._updated == [("e1", {"unique_id": SERIAL + "_3"})]


async def test_a_device_that_answers_nothing_keeps_its_hash(monkeypatch):
    """UDP on the local subnet, so a camera behind a router gets no answer. It has to
    keep working exactly as it does now."""
    _stub_probe(monkeypatch, {})
    hass = _hass()

    await async_migrate_synthesised_unique_id(hass, _entry(HASH))

    assert hass._updated == []


async def test_a_reply_with_no_serial_keeps_the_hash(monkeypatch):
    """The probe can identify a model without giving a serial; `parse_reply` keeps such
    a reply so the form can still be prefilled."""
    _stub_probe(monkeypatch, {"DeviceType": "IPC-HDW4300C"})
    hass = _hass()

    await async_migrate_synthesised_unique_id(hass, _entry(HASH))

    assert hass._updated == []


async def test_an_entry_with_no_address_is_left_alone(monkeypatch):
    calls = []
    _stub_probe(monkeypatch, {"SerialNo": SERIAL}, calls)
    entry = _entry(HASH)
    entry.data = {CONF_CHANNEL: 0}
    hass = _hass()

    await async_migrate_synthesised_unique_id(hass, entry)

    assert calls == [] and hass._updated == []


async def test_nothing_is_written_when_the_id_already_matches(monkeypatch):
    """Otherwise every restart writes the entry again for no reason."""
    _stub_probe(monkeypatch, {"SerialNo": HASH})
    hass = _hass()

    await async_migrate_synthesised_unique_id(hass, _entry(HASH))

    assert hass._updated == []


# --- the case it must refuse to resolve ------------------------------------

async def test_a_serial_already_held_by_another_entry_is_not_taken(monkeypatch):
    """#320: the camera is configured twice. Both entries have their own entities and
    history, so picking one would silently destroy somebody's data."""
    _stub_probe(monkeypatch, {"SerialNo": SERIAL})
    other = _entry(SERIAL, entry_id="e2", title="The same camera, added twice")
    hass = _hass([other])

    await async_migrate_synthesised_unique_id(hass, _entry(HASH))

    assert hass._updated == [], "it moved onto an id another entry already holds"


async def test_the_entry_being_migrated_is_in_the_list_and_does_not_block_itself(monkeypatch):
    """Its own row is in `async_entries`, and it must not read as a collision with
    itself. It cannot: its id is md5 shaped and the target is a device serial, and the
    one case where those are equal returns at the matching-id check above. That is why
    there is no `entry_id` comparison in the collision test, and a mutation removing one
    is how this was noticed."""
    _stub_probe(monkeypatch, {"SerialNo": SERIAL})
    entry = _entry(HASH)
    hass = _hass([entry])

    await async_migrate_synthesised_unique_id(hass, entry)

    assert hass._updated == [("e1", {"unique_id": SERIAL})]


async def test_a_collision_on_another_channel_does_not_block_this_one(monkeypatch):
    """Channel 3 moving to `SERIAL_3` is not blocked by channel 0 holding `SERIAL`."""
    _stub_probe(monkeypatch, {"SerialNo": SERIAL})
    hass = _hass([_entry(SERIAL, entry_id="e2")])

    await async_migrate_synthesised_unique_id(hass, _entry(HASH + "_3", channel=3))

    assert hass._updated == [("e1", {"unique_id": SERIAL + "_3"})]


# --- and the migration has to actually be called ---------------------------

def test_setup_calls_the_migration():
    """A helper that works and a setup that never calls it is the failure this repo keeps
    shipping, and driving the real `async_setup_entry` needs the whole of Home Assistant.

    So this reads the source instead: `async_setup_entry` must contain a call to
    `async_migrate_synthesised_unique_id`. Static, but it fails for the one mutation the
    tests above cannot see, and it names the function so a rename cannot slip past."""
    # async_setup_entry has not moved, and this passed through the split for
    # that reason alone rather than because it was asking the right question.
    from .integration_source import definition

    setup = definition("async_setup_entry")
    called = {ast.unparse(node.func) for node in ast.walk(setup)
              if isinstance(node, ast.Call)}

    assert "async_migrate_synthesised_unique_id" in called, (
        "async_setup_entry does not call the migration, so no existing entry is ever "
        "re-identified")
