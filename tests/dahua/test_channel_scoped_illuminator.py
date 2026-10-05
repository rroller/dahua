"""The channel-scoped white-light override's orchestration (#959).

Drives the real DahuaClient method against a fake device seeded with the tables
a DHI-NVR5464 returned for channel 11, with the snapshot store and the shared
RPC2 call faked. Pins the things that keep a camera from being stranded in
WhiteMode: write order, first-ON-only snapshot, disarm-first restore, and that
a failed row write aborts before the scene is armed.
"""

import copy
import json
from pathlib import Path

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua import white_light_override as wlo
from custom_components.dahua.client import DahuaClient

FIXTURE = Path(__file__).parent / "fixtures" / "nvr5464_ch11_lighting.json"


@pytest.fixture(autouse=True)
def _no_host_cache(monkeypatch):
    monkeypatch.setattr(client_module, "clear_host_cache", lambda scope: None)


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class _Store:
    def __init__(self):
        self.d = {}

    async def async_get(self, channel):
        return copy.deepcopy(self.d.get(channel))

    async def async_set(self, channel, snapshot):
        self.d[channel] = copy.deepcopy(snapshot)

    async def async_remove(self, channel):
        self.d.pop(channel, None)


class _Device:
    """Applies channel-scoped writes verbatim; records the order of writes."""

    def __init__(self, drop=None):
        f = _fixture()
        self.t = {
            11: {"LightingScheme": f["LightingScheme"], "Lighting_V2": f["Lighting_V2"]}
        }
        self.writes = []
        self._drop = drop  # a table name whose write is accepted but ignored

    async def get_config(self, params):
        return {"table": copy.deepcopy(self.t[params["channel"]][params["name"]])}

    async def set_config(self, name, table, channel=None):
        self.writes.append(name)
        if name != self._drop:
            self.t[channel][name] = copy.deepcopy(table)
        return {"result": True}


class _ACM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _client(device, store):
    c = object.__new__(DahuaClient)
    c._channel_snapshot_store = store
    c._device = "dev"
    c._host_limit = _ACM()
    import asyncio

    c._lighting_scheme_lock = asyncio.Lock()

    async def shared(action):
        return await action(device)

    c._rpc2_shared_call = shared
    return c


async def test_on_writes_the_row_then_arms_the_scene():
    dev, store = _Device(), _Store()
    await _client(dev, store).async_force_channel_scoped_white_light(11, True, 100)

    assert dev.writes == ["Lighting_V2", "LightingScheme"]
    assert wlo.is_forced_on(dev.t[11]["LightingScheme"], dev.t[11]["Lighting_V2"])


async def test_on_saves_the_true_original_as_the_snapshot():
    dev, store = _Device(), _Store()
    await _client(dev, store).async_force_channel_scoped_white_light(11, True, 100)

    snap = store.d[11]
    assert wlo.scheme_modes(snap["LightingScheme"]) == ["AIMode"] * 9
    assert wlo.white_light_modes(snap["Lighting_V2"]) == ["Off"] + ["ZoomPrio"] * 8


async def test_a_second_on_does_not_overwrite_the_snapshot():
    """The camera is already forced after the first ON; re-saving would record
    that forced state as the thing to restore, losing the way back."""
    dev, store = _Device(), _Store()
    c = _client(dev, store)
    await c.async_force_channel_scoped_white_light(11, True, 100)
    await c.async_force_channel_scoped_white_light(11, True, 50)

    assert wlo.scheme_modes(store.d[11]["LightingScheme"]) == ["AIMode"] * 9


async def test_off_disarms_then_restores_the_exact_row_and_purges():
    dev, store = _Device(), _Store()
    c = _client(dev, store)
    await c.async_force_channel_scoped_white_light(11, True, 100)
    dev.writes.clear()

    await c.async_force_channel_scoped_white_light(11, False, 0)

    assert dev.writes == ["LightingScheme", "Lighting_V2"]
    assert wlo.scheme_modes(dev.t[11]["LightingScheme"]) == ["AIMode"] * 9
    assert wlo.white_light_modes(dev.t[11]["Lighting_V2"]) == ["Off"] + ["ZoomPrio"] * 8
    assert 11 not in store.d


async def test_off_with_no_snapshot_is_a_no_op():
    dev, store = _Device(), _Store()
    await _client(dev, store).async_force_channel_scoped_white_light(11, False, 0)
    assert dev.writes == []


async def test_a_failed_row_write_aborts_before_arming_the_scene():
    """If the Lighting_V2 write does not take, the scene is never switched to
    WhiteMode, so the camera is left in its untouched original mode."""
    dev, store = _Device(drop="Lighting_V2"), _Store()

    with pytest.raises(ConnectionError):
        await _client(dev, store).async_force_channel_scoped_white_light(11, True, 100)

    assert "LightingScheme" not in dev.writes
    assert wlo.scheme_modes(dev.t[11]["LightingScheme"]) == ["AIMode"] * 9


async def test_off_preserves_a_scene_the_user_changed_while_on():
    """The merge restore: a scene switched away from WhiteMode while the light
    was on is left as the camera reports it, while the owned scenes revert and
    the snapshot is still purged once the write verifies."""
    dev, store = _Device(), _Store()
    c = _client(dev, store)
    await c.async_force_channel_scoped_white_light(11, True, 100)

    dev.t[11]["LightingScheme"][5]["LightingMode"] = "ColorMode"
    dev.writes.clear()

    await c.async_force_channel_scoped_white_light(11, False, 0)

    assert dev.writes == ["LightingScheme", "Lighting_V2"]
    modes = wlo.scheme_modes(dev.t[11]["LightingScheme"])
    assert modes[5] == "ColorMode"  # the user's change survived
    assert modes[0] == "AIMode"  # an owned scene reverted
    assert 11 not in store.d  # snapshot purged after the write verified
