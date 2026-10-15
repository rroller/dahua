"""The lighting tables belong to the device, so the lock has to as well.

`_lighting_scheme_lock` was built in `DahuaClient.__init__`, and a client is
built per coordinator, which is per channel (#827). Two channels of one
recorder therefore held two locks over one device's LightingScheme table and
excluded each other from nothing.

That was survivable only because each channel also held its own stale copy of
the recovery store, which hid the other channels' keys from it. Sharing that
store (one file, one cache) takes the accident away and leaves the real
window: the turn-on saves the recovery mode *before* it writes the camera,
deliberately, so a write that half succeeds still has something to go back
to. In that gap the key exists while the camera still reads its old mode, and
another channel's poll runs `async_reconcile_lighting_scheme_restore_modes`,
sees a key whose row is not WhiteMode and discards it as stale. The write
then lands and the only record of what the camera was is gone.

So this file is about the pair: the store is shared per entry, and the lock
is shared per device.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.illuminator_restore import IlluminatorRestoreStore

ENTRY = "one-recorder"
DEVICE = "192.0.2.1:80"
CHANNEL = 2
OTHER_CHANNEL = 5
PROFILE = 0


class _Device:
    """One recorder's two lighting tables, as both channels' clients see them.

    Six channels, two profiles each, so the channel under test is not index
    zero: a table indexed by the wrong thing answers plausibly at zero.
    """

    def __init__(self):
        self.scheme = [
            [{"LightingMode": "AIMode"}, {"LightingMode": "AIMode"}] for _ in range(6)
        ]
        self.lighting = [
            [
                [
                    {"LightType": "InfraredLight", "Mode": "Auto"},
                    {
                        "LightType": "WhiteLight",
                        "Mode": "ZoomPrio",
                        "PercentOfMaxBrightness": 20,
                        "NearLight": [{"Light": 50}],
                        "MiddleLight": [],
                        "FarLight": [{"Light": 50}],
                    },
                ]
                for _ in range(2)
            ]
            for _ in range(6)
        ]
        self.writing = asyncio.Event()
        self.may_finish = asyncio.Event()
        self.may_finish.set()

    def table(self, name):
        return self.scheme if name == "LightingScheme" else self.lighting

    async def get_config(self, params):
        return {"table": self.table(params["name"])}

    async def set_configs(self, pairs):
        # The window the turn-on deliberately leaves open: the recovery mode
        # is already saved and the camera has not changed yet.
        self.writing.set()
        await self.may_finish.wait()
        for name, table in pairs:
            if name == "LightingScheme":
                self.scheme = table
            else:
                self.lighting = table
        return {"result": True}


def _client(device, store):
    """A client for one channel of that device, as the coordinator builds it."""
    client = object.__new__(DahuaClient)
    client._address = "192.0.2.1"
    client._device = DEVICE
    client._host_limit = asyncio.Semaphore(2)
    # From the same registry the constructor uses, rather than a lock of its
    # own: which lock a channel gets is the thing under test.
    client._lighting_scheme_lock = client_module._lighting_scheme_lock(DEVICE)
    client._illuminator_restore_store = store
    client._shared_rpc2 = AsyncMock(
        return_value=SimpleNamespace(client=SimpleNamespace(**_rpc2_of(device)))
    )
    return client


def _rpc2_of(device):
    return {"get_config": device.get_config, "set_configs": device.set_configs}


async def test_a_poll_on_another_channel_does_not_discard_a_key_mid_write(hass):
    """The key saved a moment ago is not stale, it is the point.

    Driven through the real store so the claim is about what is on disk, and
    through two clients because one client's lock would serialise this by
    itself and prove nothing.
    """
    device = _Device()
    turning_on = _client(device, IlluminatorRestoreStore(hass, ENTRY))
    polling = _client(device, IlluminatorRestoreStore(hass, ENTRY))
    device.may_finish.clear()

    on = asyncio.create_task(
        turning_on.async_set_lighting_scheme_illuminator(
            CHANNEL, True, 50, PROFILE, light_index=1
        )
    )
    await device.writing.wait()

    poll = asyncio.create_task(polling.async_reconcile_lighting_scheme_restore_modes())
    for _ in range(50):
        await asyncio.sleep(0)

    device.may_finish.set()
    await asyncio.gather(on, poll)

    after_restart = IlluminatorRestoreStore(hass, ENTRY)
    assert await after_restart.async_get(CHANNEL, PROFILE) == "AIMode", (
        "the only record of what this camera was set to was discarded by "
        "another channel's poll while the write was in flight"
    )


async def test_a_key_whose_camera_is_not_in_whitemode_is_still_discarded(hass):
    """The guard for the above. Reconcile exists to drop recovery state the
    camera has moved past, and sharing the lock must not stop it doing that."""
    device = _Device()
    store = IlluminatorRestoreStore(hass, ENTRY)
    await store.async_set(OTHER_CHANNEL, PROFILE, "AIMode")

    await _client(device, store).async_reconcile_lighting_scheme_restore_modes()

    assert await IlluminatorRestoreStore(hass, ENTRY).async_keys() == []
