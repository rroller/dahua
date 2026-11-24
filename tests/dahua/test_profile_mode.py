"""VideoInMode is one host-wide read with a row per channel."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.coordinator import active_profile_index
from custom_components.dahua.profile import (
    canonicalize_video_in_mode,
    resolve_profile_management_mode,
    resolve_profile_schedule_times,
    video_in_mode_channel_is_rejected,
)

# What an eleven channel NVR returns. Channel 0 is on the day profile, most of
# the rest are on night, and channel 4 has been put on the scene profile.
NVR_TABLE = {
    "table.VideoInMode[0].Config[0]": "0",
    "table.VideoInMode[0].Mode": "0",
    "table.VideoInMode[1].Config[0]": "1",
    "table.VideoInMode[2].Config[0]": "1",
    "table.VideoInMode[3].Config[0]": "1",
    "table.VideoInMode[4].Config[0]": "2",
    "table.VideoInMode[5].Config[0]": "1",
    "table.VideoInMode[6].Config[0]": "1",
    "table.VideoInMode[7].Config[0]": "1",
    "table.VideoInMode[8].Config[0]": "1",
    "table.VideoInMode[9].Config[0]": "1",
    "table.VideoInMode[10].Config[0]": "0",
}

# A single camera returns one row, and that row is row 0.
SINGLE_CAMERA_TABLE = {
    "table.VideoInMode[0].Config[0]": "1",
    "table.VideoInMode[0].Mode": "0",
}


def _coordinator(channel):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = channel
    return coordinator


@pytest.mark.parametrize(
    "channel,expected",
    [
        (0, "0"),
        (1, "1"),
        (4, "2"),
        (10, "0"),
    ],
)
def test_each_channel_reads_its_own_row(channel, expected):
    """Reading row 0 for every channel gave the whole NVR channel 1's profile."""
    assert _coordinator(channel).read_profile_mode(NVR_TABLE) == expected


def test_the_profile_is_not_the_same_for_every_channel():
    """The bug stated as a property: eleven channels are not one answer."""
    modes = {_coordinator(c).read_profile_mode(NVR_TABLE) for c in range(11)}

    assert len(modes) > 1


# --- a single camera must be untouched --------------------------------------


def test_a_single_camera_still_reads_row_zero():
    assert _coordinator(0).read_profile_mode(SINGLE_CAMERA_TABLE) == "1"


def test_a_channel_with_no_row_of_its_own_does_not_borrow_row_zero():
    """This assertion used to read the other way round.

    Row 0 belongs to whichever camera reported it. A channel that has no row of
    its own must not adopt another camera's day/night profile -- the profile
    decides which Lighting_V2[channel][profile] every light command for this
    camera is written to, so borrowing one sends those writes to a profile the
    camera is not using, where the device accepts and ignores them.

    Falling through to the documented default instead makes the answer depend
    on this channel alone.
    """
    assert _coordinator(3).read_profile_mode(SINGLE_CAMERA_TABLE) == "0"


def test_a_recorder_does_not_give_every_channel_camera_ones_profile():
    """The field case. Camera 1 is on day, channel 6 reports no row at all."""
    table = {
        "table.VideoInMode[0].Config[0]": "1",
        "table.VideoInMode[1].Config[0]": "1",
    }

    assert _coordinator(0).read_profile_mode(table) == "1"
    assert _coordinator(1).read_profile_mode(table) == "1"
    assert (
        _coordinator(6).read_profile_mode(table) == "0"
    ), "channel 6 has no row and took camera 1's night profile"


def test_one_answer_is_never_assembled_from_two_cameras():
    """The fallback was per field, so Config[0] and ConfigEx could disagree.

    Channel 5 has a Config[0] of its own and no ConfigEx. Row 0's ConfigEx said
    night, and taking it turned this channel's day into night.
    """
    table = {
        "table.VideoInMode[0].ConfigEx": "night",
        "table.VideoInMode[0].Config[0]": "1",
        "table.VideoInMode[5].Config[0]": "0",
    }

    assert _coordinator(5).read_profile_mode(table) == "0"


# --- the old defaults ------------------------------------------------------


def test_an_empty_table_is_the_day_profile():
    assert _coordinator(0).read_profile_mode({}) == "0"
    assert _coordinator(7).read_profile_mode({}) == "0"


def test_a_blank_value_is_the_day_profile():
    """The device answering with nothing must not become an empty profile,
    which would then be interpolated into Lighting[channel][]."""
    assert (
        _coordinator(0).read_profile_mode({"table.VideoInMode[0].Config[0]": ""}) == "0"
    )


def test_a_blank_value_on_this_channel_does_not_fall_through_to_row_zero():
    """A row that exists and is blank is this channel's answer, not row 0's."""
    table = dict(NVR_TABLE)
    table["table.VideoInMode[4].Config[0]"] = ""

    assert _coordinator(4).read_profile_mode(table) == "0"


def test_the_profile_is_always_a_string():
    """It is interpolated straight into a config name, so it cannot be None."""
    for table in (NVR_TABLE, SINGLE_CAMERA_TABLE, {}):
        for channel in (0, 4, 10):
            assert isinstance(_coordinator(channel).read_profile_mode(table), str)


def test_videoinmode_nested_layouts_canonicalize_to_the_same_channel_row():
    expected = {
        "table.VideoInMode[0].Mode": "1",
        "table.VideoInMode[0].Config[0]": "0",
        "table.VideoInMode[0].Config[1]": "1",
        "table.VideoInMode[0].TimeSection[0][0]": "1 05:30:00-17:00:00",
    }
    for owner in (
        "table.VideoInMode[0]",
        "table.VideoInMode[0][0]",
        "table.VideoInMode[0][0][0]",
    ):
        physical = {
            owner + key[len("table.VideoInMode[0]") :]: value
            for key, value in expected.items()
        }
        assert canonicalize_video_in_mode(physical) == expected


def test_videoinmode_ambiguous_flat_owners_preserve_physical_evidence():
    data = {
        "table.VideoInMode[0].Mode": "0",
        "table.VideoInMode[0].Config[0]": "2",
        "table.VideoInMode[0][0].Mode": "1",
        "table.VideoInMode[0][0].Config[0]": "0",
    }

    canonical = canonicalize_video_in_mode(data)

    for key, value in data.items():
        assert canonical[key] == value

    assert video_in_mode_channel_is_rejected(canonical, 0)


def test_videoinmode_configex_only_preserves_physical_evidence():
    data = {
        "table.VideoInMode[0].ConfigEx": "Night",
    }

    canonical = canonicalize_video_in_mode(data)

    assert canonical == data
    assert not video_in_mode_channel_is_rejected(canonical, 0)


def test_videoinmode_nonzero_child_owner_is_not_promoted_over_its_sibling():
    data = {
        "table.VideoInMode[0][0].Mode": "0",
        "table.VideoInMode[0][1].Mode": "0",
        "table.VideoInMode[0][1].Config[0]": "1",
    }

    canonical = canonicalize_video_in_mode(data)

    for key, value in data.items():
        assert canonical[key] == value

    assert video_in_mode_channel_is_rejected(canonical, 0)


def test_videoinmode_complete_owner_is_not_promoted_over_incomplete_sibling():
    data = {
        "table.VideoInMode[0][0].Mode": "0",
        "table.VideoInMode[0][0].Config[0]": "1",
        "table.VideoInMode[0][1].Mode": "0",
    }

    canonical = canonicalize_video_in_mode(data)

    for key, value in data.items():
        assert canonical[key] == value

    assert video_in_mode_channel_is_rejected(canonical, 0)


@pytest.mark.parametrize(
    ("physical", "expected"),
    [
        (
            {
                "table.VideoInMode[3].Config[0]": "1",
            },
            {
                "table.VideoInMode[3].Config[0]": "1",
            },
        ),
        (
            {
                "table.VideoInMode[3][0].Config[0]": "1",
            },
            {
                "table.VideoInMode[3].Config[0]": "1",
            },
        ),
        (
            {
                "table.VideoInMode[3][0][0].Config[0]": "1",
            },
            {
                "table.VideoInMode[3].Config[0]": "1",
            },
        ),
        (
            {
                "table.VideoInMode[3][0].ConfigEx": "Night",
            },
            {
                "table.VideoInMode[3].ConfigEx": "Night",
            },
        ),
    ],
)
def test_cgi_flat_single_owner_paths_are_canonicalized(physical, expected):
    result = canonicalize_video_in_mode(physical)

    assert result == expected
    assert not video_in_mode_channel_is_rejected(result, 3)


@pytest.mark.parametrize(
    "physical",
    [
        {
            "table.VideoInMode[3][1].Config[0]": "0",
        },
        {
            "table.VideoInMode[3][0][1].Config[0]": "0",
        },
        {
            "table.VideoInMode[3].ConfigEx": "Night",
            "table.VideoInMode[3][0].Config[0]": "0",
        },
        {
            "table.VideoInMode[3].Config[0]": "1",
            "table.VideoInMode[3][0].Config[0]": "0",
        },
        {
            "table.VideoInMode[3][0].Config[0]": "0",
            "table.VideoInMode[3][1].Config[0]": "1",
        },
        {
            "table.VideoInMode[3][0].Mode": "0",
            "table.VideoInMode[3][1].Config[0]": "1",
        },
    ],
)
def test_cgi_flat_ambiguous_paths_are_rejected_for_all_consumers(physical):
    unrelated = {
        "table.VideoInMode[7].Config[0]": "1",
    }
    result = canonicalize_video_in_mode({**physical, **unrelated})

    for key, value in physical.items():
        assert result[key] == value

    assert video_in_mode_channel_is_rejected(result, 3)
    assert active_profile_index(result, 3) is None
    assert resolve_profile_management_mode(result, 3) is None
    assert resolve_profile_schedule_times(result, 3) is None

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = 3
    coordinator._profile_mode = "2"
    coordinator.data = {}
    coordinator.data.update(result)

    assert coordinator.describe_video_profile_shape() == "ambiguous"
    assert coordinator.video_profile_mode_is_writable() is False
    assert coordinator.get_profile_mode() == "2"

    assert not video_in_mode_channel_is_rejected(result, 7)
    assert active_profile_index(result, 7) == 1


def _rpc2_video_in_mode_client(table):
    client = object.__new__(DahuaClient)
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(
            return_value={
                "table": table,
            }
        )
    )
    holder = SimpleNamespace(
        client=rpc2,
        task=None,
    )

    async def shared_rpc2():
        return holder

    async def get(_url):
        return await client._rpc2_get_config("VideoInMode")

    client._shared_rpc2 = shared_rpc2
    client.get = get
    client._device = "rpc2-video-in-mode"
    client._username = "test"
    return client, rpc2


async def _read_rpc2_video_in_mode(table):
    client, rpc2 = _rpc2_video_in_mode_client(table)
    result = await client.async_get_video_in_mode()
    rpc2.get_config.assert_awaited_once_with({"name": "VideoInMode"})
    return result


async def test_rpc2_empty_sibling_before_owner_rejects_all_semantic_consumers():
    table = [
        [
            {},
            {
                "Mode": 0,
                "Config": [1],
                "TimeSection": [["1 05:30:00-17:00:00"]],
            },
        ]
    ]

    result = await _read_rpc2_video_in_mode(table)

    assert result["table.VideoInMode[0][1].Config[0]"] == "1"
    assert video_in_mode_channel_is_rejected(result, 0)
    assert active_profile_index(result, 0) is None
    assert resolve_profile_management_mode(result, 0) is None
    assert resolve_profile_schedule_times(result, 0) is None

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = 0
    coordinator._profile_mode = "2"
    coordinator.data = {}
    coordinator.data.update(result)

    assert coordinator.describe_video_profile_shape() == "ambiguous"
    assert coordinator.video_profile_mode_is_writable() is False
    assert coordinator.get_profile_mode() == "2"


async def test_rpc2_empty_sibling_after_owner_rejects_all_semantic_consumers():
    table = [
        [
            {
                "Mode": 0,
                "Config": [1],
                "TimeSection": [["1 05:30:00-17:00:00"]],
            },
            {},
        ]
    ]

    result = await _read_rpc2_video_in_mode(table)

    assert result["table.VideoInMode[0][0].Config[0]"] == "1"
    assert video_in_mode_channel_is_rejected(result, 0)
    assert active_profile_index(result, 0) is None
    assert resolve_profile_management_mode(result, 0) is None
    assert resolve_profile_schedule_times(result, 0) is None

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = 0
    coordinator._profile_mode = "2"
    coordinator.data = {}
    coordinator.data.update(result)

    assert coordinator.describe_video_profile_shape() == "ambiguous"
    assert coordinator.video_profile_mode_is_writable() is False
    assert coordinator.get_profile_mode() == "2"


def test_rejected_videoinmode_preserves_the_last_confirmed_profile():
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = 0
    coordinator._profile_mode = "2"

    rejected = canonicalize_video_in_mode(
        {
            "table.VideoInMode[0][1].Mode": "0",
            "table.VideoInMode[0][1].Config[0]": "1",
        },
        resolved_channels=set(),
        rejected_channels={0},
    )

    if not video_in_mode_channel_is_rejected(
        rejected,
        coordinator._channel,
    ):
        coordinator._profile_mode = coordinator.read_profile_mode(rejected)

    assert coordinator.get_profile_mode() == "2"


@pytest.mark.parametrize(
    ("row", "expected_profile", "expected_shape"),
    [
        ({"Config": [1]}, 1, "ordinary"),
        ({"ConfigEx": "Night"}, 1, "configex"),
    ],
)
async def test_rpc2_direct_owner_without_profile_management_fields_remains_readable(
    row,
    expected_profile,
    expected_shape,
):
    table = [
        {
            "Mode": 0,
            "Config": [2],
        },
        row,
    ]

    result = await _read_rpc2_video_in_mode(table)

    assert not video_in_mode_channel_is_rejected(result, 1)
    assert active_profile_index(result, 0) == 2
    assert active_profile_index(result, 1) == expected_profile
    assert resolve_profile_management_mode(result, 1) is None

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = 1
    coordinator.data = result

    assert coordinator.describe_video_profile_shape() == expected_shape


async def test_rpc2_singleton_deep_owner_remains_authoritative():
    table = [
        [
            [
                {
                    "Mode": 0,
                    "Config": [1],
                }
            ]
        ]
    ]

    result = await _read_rpc2_video_in_mode(table)

    assert not video_in_mode_channel_is_rejected(result, 0)
    assert result == {
        "table.VideoInMode[0].Mode": "0",
        "table.VideoInMode[0].Config[0]": "1",
    }
    assert active_profile_index(result, 0) == 1
    assert resolve_profile_management_mode(result, 0) == "full_time_night"
