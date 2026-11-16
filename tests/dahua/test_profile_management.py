"""Profile Management is separate from the active profile index."""

from copy import deepcopy

import pytest

from custom_components.dahua.profile import (
    PROFILE_MANAGEMENT_OPTIONS,
    canonicalize_video_in_mode,
    profile_management_table_with_option,
    resolve_profile_management_mode,
    resolve_profile_management_table,
    video_in_mode_channel_is_rejected,
)


@pytest.mark.parametrize(
    ("mode", "config", "expected"),
    [
        (0, [2], "normal"),
        (0, [0], "full_time_day"),
        (0, [1], "full_time_night"),
        (1, [0, 1], "schedule"),
    ],
)
@pytest.mark.parametrize(
    "owner",
    [
        "table.VideoInMode[3]",
        "table.VideoInMode[3][0]",
        "table.VideoInMode[3][0][0]",
    ],
)
def test_resolves_the_four_measured_management_modes(mode, config, expected, owner):
    physical = {f"{owner}.Mode": str(mode)}
    physical.update(
        {f"{owner}.Config[{index}]": str(value) for index, value in enumerate(config)}
    )

    canonical = canonicalize_video_in_mode(physical)

    assert resolve_profile_management_mode(canonical, 3) == expected


def test_profile_management_rejects_multiple_complete_owners():
    physical = {
        "table.VideoInMode[0].Mode": "0",
        "table.VideoInMode[0].Config[0]": "2",
        "table.VideoInMode[0][0].Mode": "1",
        "table.VideoInMode[0][0].Config[0]": "0",
        "table.VideoInMode[0][0].Config[1]": "1",
    }

    canonical = canonicalize_video_in_mode(physical)

    for key, value in physical.items():
        assert canonical[key] == value

    assert video_in_mode_channel_is_rejected(canonical, 0)
    assert resolve_profile_management_mode(canonical, 0) is None


def test_does_not_borrow_row_zero_for_another_channel():
    data = {
        "table.VideoInMode[0].Mode": "1",
        "table.VideoInMode[0].Config[0]": "0",
        "table.VideoInMode[0].Config[1]": "1",
    }
    assert resolve_profile_management_mode(data, 3) is None


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"table.VideoInMode[0].Mode": "0"},
        {"table.VideoInMode[0].Config[0]": "2"},
        {
            "table.VideoInMode[0].Mode": "5",
            "table.VideoInMode[0].Config[0]": "2",
        },
        {
            "table.VideoInMode[0].Mode": "1",
            "table.VideoInMode[0].Config[0]": "0",
        },
    ],
)
def test_unknown_or_incomplete_shapes_stay_unknown(data):
    assert resolve_profile_management_mode(data, 0) is None


def test_config_ex_does_not_override_management_mode():
    data = {
        "table.VideoInMode[0].Mode": "0",
        "table.VideoInMode[0].Config[0]": "2",
        "table.VideoInMode[0].ConfigEx": "Night",
    }
    assert resolve_profile_management_mode(data, 0) == "normal"


@pytest.mark.parametrize("option", PROFILE_MANAGEMENT_OPTIONS)
@pytest.mark.parametrize("wrap_depth", [0, 1, 2])
def test_writer_preserves_the_reported_videoinmode_layout(option, wrap_depth):
    row = {
        "Config": [2],
        "Mode": 0,
        "TimeSection": [["1 05:30:00-17:00:00"]],
        "FutureFirmwareField": {"keep": True},
    }

    owner = row
    for _ in range(wrap_depth):
        owner = [owner]

    other_channel = {"Config": [1], "Mode": 0, "OtherChannel": True}
    original = [owner, other_channel]
    before = deepcopy(original)

    wanted = profile_management_table_with_option(original, 0, option)

    assert original == before
    assert wanted[1] == before[1]
    assert resolve_profile_management_table(wanted, 0) == option

    target_before = before[0]
    target_after = wanted[0]

    for _ in range(wrap_depth):
        assert isinstance(target_after, list)
        assert len(target_after) == 1
        target_before = target_before[0]
        target_after = target_after[0]

    assert target_after["TimeSection"] == target_before["TimeSection"]
    assert target_after["FutureFirmwareField"] == {"keep": True}


def test_writer_rejects_a_missing_channel():
    with pytest.raises(ConnectionError):
        profile_management_table_with_option(
            [{"Config": [2], "Mode": 0}],
            3,
            "normal",
        )


def test_structured_resolvers_reject_multiple_branches_before_the_owner():
    table = [
        [
            {"Config": [2], "Mode": 0},
            {"Config": [0, 1], "Mode": 1},
        ]
    ]

    assert resolve_profile_management_table(table, 0) is None
    with pytest.raises(ConnectionError, match="unambiguous configuration row"):
        profile_management_table_with_option(table, 0, "normal")
