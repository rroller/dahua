"""`dahua.get_channel_title` reads back the channel title overlay (#980).

The companion to `set_channel_title`, the way `get_overlay_text` is to the
overlay setters: five services could set the title and none read it, and the
title can be changed on the camera, the NVR or the app, so remembering what Home
Assistant last wrote is not the same as knowing what is on the video.

A separate service rather than another key on `get_overlay_text`: the title has
no overlay group to pass, and adding a key would change #461's shipped response
shape. The read and the parser are the ones the add flow already runs in
production (`parse_channel_titles(async_get_config("ChannelTitle"))`).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import yaml

from custom_components.dahua.camera import DahuaCamera

# The flat keys configManager.cgi?getConfig&name=ChannelTitle answers with.
TITLES = {
    "table.ChannelTitle[0].Name": "Front Street",
    "table.ChannelTitle[2].Name": "Back Yard",
}


def _camera(table=None, channel=0):
    """The entity without Home Assistant's plumbing, as the other camera tests do."""
    camera = object.__new__(DahuaCamera)
    camera._logical_channel = channel
    camera._coordinator = SimpleNamespace(
        client=SimpleNamespace(
            async_get_config=AsyncMock(return_value=TITLES if table is None else table)
        )
    )
    return camera


# --- the service contract ---------------------------------------------------


async def test_it_reads_this_channels_title():
    result = await _camera(channel=0).async_get_channel_title()

    assert result == {"channel_title": "Front Street"}


async def test_a_channel_reads_its_own_row():
    """Every channel of a recorder is a row in one table, so reading the wrong
    one would report another camera's title as this camera's."""
    result = await _camera(channel=2).async_get_channel_title()

    assert result == {"channel_title": "Back Yard"}


async def test_a_channel_the_device_does_not_list_is_empty_not_null():
    """Returned as "" rather than None so a template can use it directly."""
    result = await _camera(channel=5).async_get_channel_title()

    assert result == {"channel_title": ""}


async def test_an_empty_table_is_empty_not_null():
    result = await _camera(table={}).async_get_channel_title()

    assert result == {"channel_title": ""}


# --- declared where a person can find it ------------------------------------


def test_the_service_is_declared_beside_the_one_that_sets_it():
    with open("custom_components/dahua/services.yaml", encoding="utf-8") as f:
        services = yaml.safe_load(f)

    assert "get_channel_title" in services
    assert "set_channel_title" in services, "the writer it mirrors went missing"
    # It reads a whole channel, so unlike get_overlay_text it takes no fields.
    assert "fields" not in services["get_channel_title"]
