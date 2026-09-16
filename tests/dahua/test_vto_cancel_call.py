"""The cancel-call service is offered on every camera, not just doorbells.

`vto_cancel_call` is registered on the camera platform and documented in
services.yaml, so it appears in the service picker against every Dahua camera
and NVR channel a user has. Only a doorbell ever has a VTO client, so on
anything else the call reached `None.cancel_call()` and raised AttributeError --
a traceback, with nothing in it to say the wrong entity had been picked.

A doorbell between reconnects lands in the same place.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeassistant.exceptions import HomeAssistantError

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
