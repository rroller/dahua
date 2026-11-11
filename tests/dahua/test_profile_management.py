"""Profile Management is separate from the active profile index."""

from copy import deepcopy

import pytest

from custom_components.dahua.profile import (
    PROFILE_MANAGEMENT_OPTIONS,
    profile_management_table_with_option,
    resolve_profile_management_mode,
    resolve_profile_management_table,
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
def test_resolves_the_four_measured_management_modes(mode, config, expected):
    data = {"table.VideoInMode[3].Mode": str(mode)}
    data.update(
        {
            f"table.VideoInMode[3].Config[{index}]": str(value)
            for index, value in enumerate(config)
        }
    )
    assert resolve_profile_management_mode(data, 3) == expected


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
def test_writer_changes_only_config_and_mode(option):
    original = [
        {
            "Config": [2],
            "Mode": 0,
            "TimeSection": [["1 06:00:00-17:00:00"]],
            "FutureFirmwareField": {"keep": True},
        },
        {"Config": [1], "Mode": 0, "OtherChannel": True},
    ]
    before = deepcopy(original)
    wanted = profile_management_table_with_option(original, 0, option)

    assert original == before
    assert wanted[0]["TimeSection"] == before[0]["TimeSection"]
    assert wanted[0]["FutureFirmwareField"] == before[0]["FutureFirmwareField"]
    assert wanted[1] == before[1]
    assert resolve_profile_management_table(wanted, 0) == option


def test_writer_rejects_a_missing_channel():
    with pytest.raises(ConnectionError):
        profile_management_table_with_option([{"Config": [2], "Mode": 0}], 3, "normal")
