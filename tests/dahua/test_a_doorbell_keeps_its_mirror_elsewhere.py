"""A doorbell has the mirror hardware and keeps it in another table.

The mirror switch and the rotation select read `VideoImageControl`, which is
where a camera keeps `Flip`, `Rotate90` and `Mirror`. A VTO2000A answers that
table empty and keeps the same three fields among its picture settings in
`VideoInOptions`, so neither entity was ever built for it although the
hardware does the job.

Measured on a VTO2000A at firmware 3.120.0000.0.R on 2026-11-01, against the
device rather than through the integration's 300-second config cache:

    table.VideoInOptions[0].Flip=false
    table.VideoInOptions[0].Mirror=false
    table.VideoInOptions[0].Rotate90=0

and `VideoImageControl` on the same device answers HTTP 500. That is this
firmware refusing a table it does not have, so the probe has to move on from a
refusal as well as from an empty answer -- a device says "I do not have that"
in both voices, and which one it uses is not something to guess at. Writing
`VideoInOptions[0].Mirror=true` answered `OK`, changed that one key of the
thirty-two and left the other thirty-one byte-identical, and the next snapshot
came back with the porch and the garden swapped side for side. Writing it back
to `false` restored both the table and the picture. So the write is honoured by
the hardware and not merely stored, which is the half a read-back cannot show.

`Flip` was measured the same way and is honoured too, with the picture turning
top for bottom. That one needed the snapshots judged by correlating each
against the four orientations of the other rather than by eye: the first pair
was taken while the camera was still recovering its exposure after the write,
and scored +0.366 for a flip against +0.316 for no change, which is not a
result. A settled pair a minute apart scored +0.980 against +0.559, matching
the mirror's own +0.982. An unreadable frame is not evidence of anything, and
the margin is what says which.

The order is the load-bearing part: `VideoImageControl` is asked first and, when
it reports a field, is the only table read, so a camera is read exactly where it
was read before.
"""

from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

from custom_components.dahua.client import DahuaClient
from custom_components.dahua.const import (
    VIDEO_IMAGE_CONTROL_FIELDS,
    VIDEO_IMAGE_CONTROL_TABLE,
    VIDEO_IMAGE_CONTROL_TABLES,
)
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

# What each device serves, keyed by table name. A double that answers the same
# rows whatever it is asked for cannot tell the two tables apart.
CAMERA = {
    "VideoImageControl": {
        "table.VideoImageControl[0].Flip": "false",
        "table.VideoImageControl[0].Rotate90": "0",
        "table.VideoImageControl[0].Mirror": "false",
    }
}

# The doorbell, as measured: `VideoImageControl` refuses with a 500.
REFUSED = ClientResponseError(
    request_info=SimpleNamespace(real_url="http://doorbell/cgi-bin/configManager.cgi"),
    history=(),
    status=500,
)

DOORBELL = {
    "VideoImageControl": REFUSED,
    "VideoInOptions": {
        "table.VideoInOptions[0].Flip": "false",
        "table.VideoInOptions[0].Mirror": "false",
        "table.VideoInOptions[0].Rotate90": "0",
        "table.VideoInOptions[0].WhiteBalance": "Auto",
        "table.VideoInOptions[0].Gain": "95",
    },
}


def _coordinator(served, *, channel=0, raises=None):
    asked = []

    async def read(table=VIDEO_IMAGE_CONTROL_TABLE):
        asked.append(table)
        if raises is not None:
            raise raises
        answer = served.get(table, {})
        if isinstance(answer, Exception):
            raise answer
        return answer

    written = []

    async def write(channel_index, field, value, table=VIDEO_IMAGE_CONTROL_TABLE):
        written.append((channel_index, field, value, table))

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = SimpleNamespace(
        async_get_video_image_control=read,
        async_set_video_image_control=write,
    )
    coordinator._channel = channel
    coordinator.get_channel = lambda: coordinator._channel
    coordinator._probe_refusals = {}
    coordinator.data = {}
    coordinator.asked = asked
    coordinator.written = written
    return coordinator


async def test_a_doorbell_reports_its_controls_from_the_other_table():
    coordinator = _coordinator(DOORBELL)

    assert await coordinator._async_probe_video_image_control()

    assert coordinator.video_image_control_table() == "VideoInOptions"
    for field in VIDEO_IMAGE_CONTROL_FIELDS:
        assert coordinator.supports_video_image_control(field), field


async def test_a_doorbells_state_is_read_from_the_table_it_was_found_in():
    coordinator = _coordinator(DOORBELL)
    await coordinator._async_probe_video_image_control()
    coordinator.data = dict(DOORBELL["VideoInOptions"])

    assert coordinator.get_video_image_control("Mirror") is False
    assert coordinator.get_video_image_control("Flip") is False
    assert coordinator.get_video_image_control("Rotate90") == 0


async def test_a_camera_is_still_read_from_the_table_it_always_was():
    coordinator = _coordinator(CAMERA)

    assert await coordinator._async_probe_video_image_control()

    assert coordinator.video_image_control_table() == "VideoImageControl"
    assert coordinator.asked == ["VideoImageControl"]


async def test_a_camera_serving_both_tables_is_not_moved_to_the_other_one():
    """The fallback is a fallback. A camera that happens to serve picture
    settings as well is read where it was read before, and the second table is
    never asked for, so nothing about its state or its write path changes."""
    both = {
        "VideoImageControl": CAMERA["VideoImageControl"],
        "VideoInOptions": DOORBELL["VideoInOptions"],
    }
    coordinator = _coordinator(both)

    assert await coordinator._async_probe_video_image_control()

    assert coordinator.video_image_control_table() == "VideoImageControl"
    assert coordinator.asked == ["VideoImageControl"]


async def test_the_first_table_is_asked_first():
    """Named separately because the order is the whole safety argument."""
    assert VIDEO_IMAGE_CONTROL_TABLES[0] == "VideoImageControl"
    assert VIDEO_IMAGE_CONTROL_TABLE == "VideoImageControl"

    coordinator = _coordinator(DOORBELL)
    await coordinator._async_probe_video_image_control()

    assert coordinator.asked == ["VideoImageControl", "VideoInOptions"]


async def test_a_device_with_neither_table_gets_no_entities():
    coordinator = _coordinator({})

    assert not await coordinator._async_probe_video_image_control()

    assert coordinator._video_image_control_fields == frozenset()
    assert coordinator.video_image_control_table() == VIDEO_IMAGE_CONTROL_TABLE
    assert coordinator.asked == ["VideoImageControl", "VideoInOptions"]


async def test_an_empty_answer_is_not_recorded_as_a_refusal():
    """Two tables answered, both empty. The device said what it has, so there
    is nothing for the late retry to come back for."""
    coordinator = _coordinator({})

    await coordinator._async_probe_video_image_control()

    assert coordinator._probe_refusals.get("video_image_control") is None


async def test_both_tables_refusing_is_recorded_as_a_refusal():
    """The other half: nothing answered, so the device never said. That is the
    case the late retry exists for, and it must not be filed as an answer."""
    coordinator = _coordinator(DOORBELL, raises=TimeoutError("no answer"))

    assert not await coordinator._async_probe_video_image_control()

    assert coordinator._probe_refusals.get("video_image_control") is not None
    assert coordinator.asked == ["VideoImageControl", "VideoInOptions"]


async def test_the_measured_doorbell_path_is_not_chased_by_the_late_retry():
    """The doorbell refuses the first table and answers the second. The probe
    got what it came for, so there is nothing left unanswered to come back
    for, and filing a refusal here would have the late retry re-ask a question
    that has been answered on every poll for the life of the entry."""
    coordinator = _coordinator(DOORBELL)

    assert await coordinator._async_probe_video_image_control()

    assert coordinator._probe_refusals.get("video_image_control") is None


async def test_a_refusal_then_an_empty_answer_is_still_an_answer():
    """A 500 for one table and nothing in the other is the device saying it
    has neither, in its two voices. Both are answers."""
    coordinator = _coordinator({"VideoImageControl": REFUSED, "VideoInOptions": {}})

    assert not await coordinator._async_probe_video_image_control()

    assert coordinator._probe_refusals.get("video_image_control") is None


async def test_the_channel_row_is_read_from_the_other_table_too():
    """A recorder channel, to prove the row index is not pinned to zero when
    the fallback is the table in use."""
    served = {
        "VideoImageControl": {},
        "VideoInOptions": {
            "table.VideoInOptions[0].Mirror": "false",
            "table.VideoInOptions[3].Mirror": "true",
        },
    }
    coordinator = _coordinator(served, channel=3)

    assert await coordinator._async_probe_video_image_control()
    coordinator.data = dict(served["VideoInOptions"])

    assert coordinator.get_video_image_control("Mirror") is True


async def test_a_missing_channel_row_in_the_other_table_builds_nothing():
    served = {
        "VideoImageControl": {},
        "VideoInOptions": {"table.VideoInOptions[0].Mirror": "true"},
    }
    coordinator = _coordinator(served, channel=3)

    assert not await coordinator._async_probe_video_image_control()
    assert not coordinator.supports_video_image_control("Mirror")


async def test_the_poll_reads_the_table_the_probe_found():
    coordinator = _coordinator(DOORBELL)
    await coordinator._async_probe_video_image_control()
    coordinator.asked.clear()

    answer = await coordinator._async_fetch_video_image_control()

    assert coordinator.asked == ["VideoInOptions"]
    assert answer == DOORBELL["VideoInOptions"]


async def test_a_refused_poll_carries_the_other_tables_rows_forward():
    """The carried-forward filter is keyed on the table name, so on the
    doorbell it has to keep `VideoInOptions` rows. Keyed on the camera's table
    it would carry nothing and the entities would go unknown on one refusal."""
    coordinator = _coordinator(DOORBELL)
    await coordinator._async_probe_video_image_control()
    coordinator.client.async_get_video_image_control = _raising(TimeoutError("nope"))
    coordinator.data = {
        "table.VideoInOptions[0].Mirror": "true",
        "table.VideoInOptions[0].Flip": "false",
        "table.VideoInOptions[0].Rotate90": "0",
        "table.VideoColor[0][0].Gamma": "50",
    }

    assert await coordinator._async_fetch_video_image_control() == {
        "table.VideoInOptions[0].Mirror": "true",
        "table.VideoInOptions[0].Flip": "false",
        "table.VideoInOptions[0].Rotate90": "0",
    }


def _raising(exception):
    async def read(table=VIDEO_IMAGE_CONTROL_TABLE):
        raise exception

    return read


@pytest.mark.parametrize("field", ["Mirror", "Flip"])
@pytest.mark.parametrize("value", [True, False])
async def test_a_write_goes_back_to_the_table_it_was_read_from(field, value):
    coordinator = _coordinator(DOORBELL)
    await coordinator._async_probe_video_image_control()

    await coordinator.async_set_video_image_control(field, value)

    assert coordinator.written == [(0, field, value, "VideoInOptions")]


@pytest.mark.parametrize("field", ["Mirror", "Flip"])
async def test_a_cameras_write_still_goes_where_it_always_did(field):
    coordinator = _coordinator(CAMERA, channel=3)
    await coordinator._async_probe_video_image_control()

    await coordinator.async_set_video_image_control(field, True)

    assert coordinator.written == [(3, field, True, "VideoImageControl")]


async def test_a_write_before_any_probe_uses_the_cameras_table():
    """`object.__new__` skips `__init__`, which is how much of the suite and
    every diagnostics path builds one, so the table has to be a class default
    rather than only an instance attribute."""
    coordinator = object.__new__(DahuaDataUpdateCoordinator)

    assert coordinator.video_image_control_table() == "VideoImageControl"


@pytest.mark.parametrize(
    "table,expected",
    [
        ("VideoImageControl", "VideoImageControl[2].Mirror=true"),
        ("VideoInOptions", "VideoInOptions[2].Mirror=true"),
    ],
)
async def test_the_write_url_names_the_table_it_was_given(table, expected):
    client = object.__new__(DahuaClient)
    urls = []

    async def get(url):
        urls.append(url)

    client.get = get

    await client.async_set_video_image_control(2, "Mirror", True, table)

    assert urls == ["/cgi-bin/configManager.cgi?action=setConfig&" + expected]


@pytest.mark.parametrize(
    "table,expected",
    [
        ("VideoImageControl", "name=VideoImageControl"),
        ("VideoInOptions", "name=VideoInOptions"),
    ],
)
async def test_the_read_url_names_the_table_it_was_given(table, expected):
    client = object.__new__(DahuaClient)
    urls = []

    async def get(url):
        urls.append(url)
        return {}

    client.get = get

    await client.async_get_video_image_control(table)

    assert urls == ["/cgi-bin/configManager.cgi?action=getConfig&" + expected]


async def test_a_table_nothing_measured_is_refused():
    """The backup covers the two tables named in `const.py`. A write to some
    third table would leave `backup_config` unable to report what it changed,
    so the client will not build the URL at all."""
    client = object.__new__(DahuaClient)

    with pytest.raises(ValueError, match="Unsupported image control table"):
        await client.async_set_video_image_control(0, "Mirror", True, "VideoInMode")
    with pytest.raises(ValueError, match="Unsupported image control table"):
        await client.async_get_video_image_control("VideoInMode")
