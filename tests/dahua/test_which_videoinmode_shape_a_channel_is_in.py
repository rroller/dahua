"""Which VideoInMode shape a channel is in, and whether the profile can be written.

#458 has sat since February 2025: `dahua.set_video_profile_mode` answers
`Unknown error`. The cause is not the service, it is that `VideoInMode` comes in three
shapes and the service writes `Config[0]`, which only selects the profile in one of them.

Measured on a single DHI-NVR5464, reading `VideoInMode` once for the host:

    ch0       Config[0]=0   ConfigEx=None    ordinary, Config[0] is the profile
    ch1       Config[0]=0   ConfigEx=Day     the IL shape, Config[0] is static
    ch9       Config[0]=2   ConfigEx=None    general profile management, profile 2

**All three on one recorder at the same time.** That is what makes this per channel
rather than per model, and it is why a model name cannot answer it: the same device is
two different answers depending on which camera you ask about.

`read_profile_mode` already tells these apart, because the profile decides which
`Lighting[channel][profile]` row a light command is written to. This says the same thing
in a form a log line can use, so a write that cannot work says why instead of failing
opaquely.

An unpolled channel counts as writable on purpose. Refusing on an unknown would be worse
than trying: writing `Config[0]` is what the service has always done, and plenty of
devices this has never been measured on may answer it perfectly well.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator

CHANNEL = 4


def _coordinator(fields=None, *, channel=CHANNEL):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = channel
    coordinator.data = {}
    for name, value in (fields or {}).items():
        coordinator.data["table.VideoInMode[%d].%s" % (channel, name)] = value
    return coordinator


# --- the three shapes, as measured -----------------------------------------

MEASURED = [
    ("ordinary", {"Config[0]": "0"}),
    ("ordinary", {"Config[0]": "1"}),
    ("ordinary", {"Config[0]": "0", "ConfigEx": "None"}),
    ("general", {"Config[0]": "2"}),
    ("general", {"Config[0]": "2", "ConfigEx": "Day"}),
    ("configex", {"Config[0]": "0", "ConfigEx": "Day"}),
    ("configex", {"Config[0]": "0", "ConfigEx": "Night"}),
]


@pytest.mark.parametrize("expected, fields", MEASURED)
def test_the_shape_is_named_from_the_fields(expected, fields):
    assert _coordinator(fields).describe_video_profile_shape() == expected


def test_general_profile_management_wins_over_configex():
    """`Config[0]=2` means one profile covers everything and `ConfigEx` still echoes
    day/night while selecting nothing. Preferring `ConfigEx` there sent every write to
    the day profile while the camera rendered from 2, which was #605."""
    coordinator = _coordinator({"Config[0]": "2", "ConfigEx": "Night"})

    assert coordinator.describe_video_profile_shape() == "general"


def test_an_unrecognised_configex_is_not_the_configex_shape():
    """Only a value we understand should override `Config[0]`, which is very likely
    right. A string nobody has seen before is not evidence of anything."""
    coordinator = _coordinator({"Config[0]": "0", "ConfigEx": "Something"})

    assert coordinator.describe_video_profile_shape() == "ordinary"


@pytest.mark.parametrize("spelling", ["Day", "day", "DAY", " Night ", "night"])
def test_the_configex_value_is_read_however_the_device_spells_it(spelling):
    coordinator = _coordinator({"Config[0]": "0", "ConfigEx": spelling})

    assert coordinator.describe_video_profile_shape() == "configex"


def test_nothing_polled_yet_is_unknown_rather_than_ordinary():
    """Told apart from ordinary because they lead to different decisions below, and
    because reporting a shape for a channel nothing has been read from would be an
    invention."""
    assert _coordinator().describe_video_profile_shape() == "unknown"


def test_the_shape_is_read_from_this_channels_row():
    """The read is host wide, one row per channel, so a recorder carrying three shapes
    at once must not answer with its neighbour's."""
    coordinator = _coordinator({"Config[0]": "2"}, channel=9)
    coordinator.data["table.VideoInMode[0].Config[0]"] = "0"

    assert coordinator.describe_video_profile_shape() == "general"

    ordinary = _coordinator({"Config[0]": "0"}, channel=0)
    ordinary.data["table.VideoInMode[9].Config[0]"] = "2"

    assert ordinary.describe_video_profile_shape() == "ordinary"


# --- and what that means for the write --------------------------------------


@pytest.mark.parametrize(
    "fields, writable",
    [
        ({"Config[0]": "0"}, True),
        ({"Config[0]": "2"}, False),
        ({"Config[0]": "0", "ConfigEx": "Day"}, False),
        ({}, True),
    ],
)
def test_whether_writing_config_zero_can_select_the_profile(fields, writable):
    assert _coordinator(fields).video_profile_mode_is_writable() is writable


def test_an_unpolled_channel_is_still_attempted():
    """Deliberate. Writing Config[0] is what the service has always done, and refusing
    because nothing has been read yet would remove the feature from every device this
    has not been measured on."""
    assert _coordinator().video_profile_mode_is_writable() is True


def test_the_two_shapes_that_cannot_work_are_both_refused():
    """Named together because they fail differently and look the same from outside: the
    general shape has the device reject the write, and the IL shape has it accepted and
    ignored. #458 is the first, #582 was the second."""
    assert _coordinator({"Config[0]": "2"}).video_profile_mode_is_writable() is False
    assert (
        _coordinator(
            {"Config[0]": "0", "ConfigEx": "Day"}
        ).video_profile_mode_is_writable()
        is False
    )


def test_the_shape_and_the_verdict_agree():
    """One is derived from the other, so a change to either that forgets the other
    shows up here rather than as a wrong log line."""
    for _expected, fields in MEASURED:
        coordinator = _coordinator(fields)
        shape = coordinator.describe_video_profile_shape()
        assert coordinator.video_profile_mode_is_writable() is (shape == "ordinary"), (
            fields,
            shape,
        )
