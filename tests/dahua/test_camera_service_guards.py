"""Two camera service handlers that did not do what their siblings do.

`async_set_video_profile_mode` was the only camera write handler that did not
refresh afterwards. The profile decides which `Lighting` row every light command
addresses, and only the poll reads it back, so a `light.turn_on` in the same
poll window after setting Night was written to the day row, where the device
accepts and ignores it.

`async_vto_open_door` is offered on every camera entity, while the Open Door
button is only created on a doorbell. Aimed at a camera, the CGI endpoint is not
there and the user gets a raw HTTP error instead of the sentence the sibling
cancel-call service gives.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua.camera import DahuaCamera


def _camera(*, doorbell=False):
    """The entity without Home Assistant's plumbing, as the other camera tests do."""
    camera = object.__new__(DahuaCamera)
    camera._logical_channel = 0
    camera._coordinator = SimpleNamespace(
        client=SimpleNamespace(
            async_set_video_profile_mode=AsyncMock(),
            async_set_night_switch_mode=AsyncMock(),
            async_access_control_open_door=AsyncMock(),
        ),
        get_model=lambda: "IPC-HDW5831R-ZE",
        # The profile write asks whether Config[0] can select the profile on this
        # channel before sending it. True keeps these tests about the refresh.
        video_profile_mode_is_writable=lambda: True,
        describe_video_profile_shape=lambda: "ordinary",
        is_doorbell=lambda: doorbell,
        get_device_name=lambda: "Front Door",
        async_refresh=AsyncMock(),
    )
    return camera, camera._coordinator


# --- the profile write refreshes ---------------------------------------------


async def test_setting_the_profile_refreshes_so_the_next_light_write_uses_it():
    camera, coordinator = _camera()

    await camera.async_set_video_profile_mode("Night")

    coordinator.client.async_set_video_profile_mode.assert_awaited_once_with(0, "Night")
    coordinator.async_refresh.assert_awaited_once()


async def test_the_nvr_night_switch_path_refreshes_too():
    camera, coordinator = _camera()
    coordinator.get_model = lambda: "DHI-NVR4108HS-8P-4KS2"

    await camera.async_set_video_profile_mode("Night")

    coordinator.client.async_set_night_switch_mode.assert_awaited_once_with(0, "Night")
    coordinator.async_refresh.assert_awaited_once()


# --- and opening a door on a camera says what it is for -----------------------


async def test_opening_a_door_on_a_camera_that_is_not_a_doorbell_says_so():
    camera, coordinator = _camera(doorbell=False)

    with pytest.raises(HomeAssistantError) as caught:
        await camera.async_vto_open_door(1)

    assert caught.value.translation_key == "open_door_needs_a_doorbell"
    assert caught.value.translation_placeholders == {"device": "Front Door"}
    coordinator.client.async_access_control_open_door.assert_not_awaited()


async def test_opening_a_door_on_a_doorbell_still_opens_it():
    camera, coordinator = _camera(doorbell=True)

    await camera.async_vto_open_door(1)

    coordinator.client.async_access_control_open_door.assert_awaited_once_with(1)
