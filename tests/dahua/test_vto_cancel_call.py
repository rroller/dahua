"""The cancel-call service is offered on every camera, not just doorbells.

`vto_cancel_call` is registered on the camera platform and documented in
services.yaml, so it appears in the service picker against every Dahua camera
and NVR channel a user has. Only a doorbell ever has a VTO client, so on
anything else the call reached `None.cancel_call()` and raised AttributeError --
a traceback, with nothing in it to say the wrong entity had been picked.

A doorbell between reconnects lands in the same place.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.camera import DahuaCamera


def _camera(vto_client):
    camera = object.__new__(DahuaCamera)
    camera._coordinator = SimpleNamespace(
        get_vto_client=lambda: vto_client,
        get_device_name=lambda: "Front Door",
    )
    return camera


async def test_cancelling_on_a_camera_says_what_is_wrong():
    with pytest.raises(HomeAssistantError) as err:
        await _camera(None).async_vto_cancel_call()

    assert "Front Door" in str(err.value), "the message does not name the entity"
    assert "doorbell" in str(err.value).lower()


async def test_it_is_not_an_attribute_error():
    """The point of the change: a service call on the wrong entity is user
    error, and should read as one rather than as a crash."""
    with pytest.raises(HomeAssistantError):
        await _camera(None).async_vto_cancel_call()

    with pytest.raises(Exception) as err:
        await _camera(None).async_vto_cancel_call()
    assert not isinstance(err.value, AttributeError)


async def test_a_connected_doorbell_still_cancels():
    vto_client = SimpleNamespace(cancel_call=AsyncMock())

    await _camera(vto_client).async_vto_cancel_call()

    vto_client.cancel_call.assert_awaited_once()


# --- the client the service is handed -----------------------------------------

def _coordinator(vto_client):
    """A coordinator with only what get_vto_client reads."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._vto_client = vto_client
    return c


def _protocol(connected):
    """A VTO protocol, with the future it resolves when its socket ends."""
    disconnected = asyncio.get_event_loop().create_future()
    if not connected:
        disconnected.set_result(True)
    return SimpleNamespace(disconnected=disconnected, cancel_call=AsyncMock())


async def test_a_device_that_never_had_a_doorbell_connection_has_no_client():
    assert _coordinator(None).get_vto_client() is None


async def test_a_client_whose_connection_has_gone_is_not_handed_out():
    """`_vto_client` is only assigned, never cleared, so after a drop it names a
    protocol whose socket has gone. A doorbell reconnects often enough for that
    to be reachable."""
    assert _coordinator(_protocol(connected=False)).get_vto_client() is None


async def test_a_live_client_is_handed_out():
    protocol = _protocol(connected=True)

    assert _coordinator(protocol).get_vto_client() is protocol


async def test_cancelling_between_reconnects_reads_as_an_error_not_a_crash():
    """The two halves together: the getter admits it has nothing, and the
    service says so rather than raising AttributeError on None."""
    coordinator = _coordinator(_protocol(connected=False))
    coordinator.get_device_name = lambda: "Front Door"
    camera = object.__new__(DahuaCamera)
    camera._coordinator = coordinator

    with pytest.raises(HomeAssistantError):
        await camera.async_vto_cancel_call()

