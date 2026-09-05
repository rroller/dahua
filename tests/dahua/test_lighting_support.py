"""The IR lighting probe could never fail, so every device claimed one.

async_get_config catches aiohttp.ClientResponseError and returns {}, so the
coordinator's `except ClientError` around the lighting probe was unreachable.
_supports_lighting was therefore True on every device, and an NVR channel with
no infrared light fetched Lighting[ch][mode] on every poll forever -- which on
a device that closes the connection and reissues its digest nonce per request
costs two TCP connections and a rejected login each time.
"""

import pytest
from aiohttp import ClientError

from custom_components.dahua import DahuaDataUpdateCoordinator

LIGHTING_PRESENT = {
    "table.Lighting[0][0].Correction": "50",
    "table.Lighting[0][0].Mode": "Auto",
    "table.Lighting[0][0].Sensitive": "3",
}


class _Client:
    def __init__(self, result=None, raises=None):
        self.result, self.raises = result, raises
        self.calls = 0

    async def async_get_config_lighting(self, channel, profile_mode):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.result


async def _probe(client):
    """Call the coordinator's own detection, not a copy of it.

    The first version of this file reimplemented the probe here, so reverting
    the production code to `= True` left four of five tests passing. Only the
    source-inspection test noticed. Now it runs the real method.
    """
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = 0
    c._profile_mode = "0"
    c.client = client
    return await c.async_detect_lighting_support()


async def test_a_device_that_reports_lighting_is_supported():
    assert await _probe(_Client(result=LIGHTING_PRESENT)) is True


async def test_a_device_that_returns_nothing_is_not_supported():
    """This is the case that was broken: {} used to be read as success."""
    assert await _probe(_Client(result={})) is False


async def test_an_error_is_still_unsupported():
    assert await _probe(_Client(raises=ClientError("boom"))) is False


async def test_the_probe_is_what_the_coordinator_actually_uses():
    """Guard against the detection being inlined again somewhere else."""
    import inspect

    from custom_components.dahua import DahuaDataUpdateCoordinator as C

    src = inspect.getsource(C._async_update_data)
    assert "async_detect_lighting_support()" in src
    assert "self._supports_lighting = True" not in src


async def test_it_asks_the_device_only_once():
    client = _Client(result=LIGHTING_PRESENT)

    await _probe(client)

    assert client.calls == 1


def test_async_get_config_still_swallows_so_the_probe_must_not_rely_on_it():
    """If this ever changes, the probe above can be simplified -- and this
    test is how you find out."""
    import inspect

    from custom_components.dahua.client import DahuaClient

    src = inspect.getsource(DahuaClient.async_get_config)
    assert "return {}" in src and "except aiohttp.ClientResponseError" in src
