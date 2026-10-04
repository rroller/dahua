"""What the coordinator reports off the last poll, and which key each answer comes from.

These are the thin readers every platform goes through. They look too small to test,
which is why a third of them were never executed, and the reason to test them is not
the line count: **each one names an exact key in the polled table, and a key that is
wrong returns a plausible answer for ever.** `False` for a switch that is really on,
`"unknown"` for a plate that was read, a light that never appears to be lit. Nothing
raises and nothing is logged, so there is no sign of it but a user saying the entity is
stuck.

Three of them do something less obvious than reading a key, and those are the ones
worth reading this file for:

* `is_event_notifications_enabled` is **inverted**. The table key is
  `DisableEventNotify.Enable`, so notifications are enabled when the device says
  `false`. Anyone "fixing" the comparison to `true` would flip every one of these
  sensors and the change would look like a cleanup.
* `is_motion_detection_enabled` formats the **channel** into its key, so on a recorder
  it must read its own channel and not channel 0. This is the shape of #676 and #690.
* Some comparisons lower-case the device's answer and some do not, so `"True"` means
  enabled in one place and not in another. That asymmetry is pinned here as measured
  behaviour rather than tidied, because tidying it silently changes what several
  entities report.

`get_authorized_hold_time` is the other one with real behaviour: it swallows a
non-numeric setting rather than raising, which matters because the value comes from a
text field a user can type into.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.const import (
    CONF_AUTHORIZED_HOLD_TIME,
    DEFAULT_AUTHORIZED_HOLD_TIME,
)

CHANNEL = 2


def _coordinator(
    data=None,
    *,
    channel=CHANNEL,
    channel_config=None,
    options=None,
    entry_data=None,
    **attrs
):
    """Only the attributes these readers touch, so nothing passes on state it never set."""
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.data = {} if data is None else data
    coordinator._channel = channel
    coordinator._channel_config = {} if channel_config is None else channel_config
    coordinator.config_entry = SimpleNamespace(
        data={} if entry_data is None else entry_data,
        options={} if options is None else options,
    )
    for name, value in attrs.items():
        setattr(coordinator, name, value)
    return coordinator


# --- each reader, and the key it reads ---------------------------------------

# getter, the data that means "yes", and a key that is deliberately close but wrong.
READERS = [
    ("is_alarm_output_on", {"status.AlarmOut[0]": "1"}, {"status.AlarmOut[1]": "1"}),
    (
        "is_motion_detection_enabled",
        {"table.MotionDetect[2].Enable": "true"},
        {"table.MotionDetect[0].Enable": "true"},
    ),
    (
        "is_disarming_linkage_enabled",
        {"table.DisableLinkage.Enable": "true"},
        {"table.DisableLinkage": "true"},
    ),
    (
        "is_ring_light_on",
        {"table.LightGlobal[0].Enable": "true"},
        {"table.LightGlobal[1].Enable": "true"},
    ),
    ("is_siren_on", {"status.Speaker": "On"}, {"status.Speakers": "On"}),
    ("is_security_light_on", {"status.WhiteLight": "On"}, {"status.WhiteLights": "On"}),
]


@pytest.mark.parametrize("getter, yes, wrong_key", READERS)
def test_the_reader_says_yes_for_its_own_key(getter, yes, wrong_key):
    coordinator = _coordinator(yes)

    assert getattr(coordinator, getter)() is True


@pytest.mark.parametrize("getter, yes, wrong_key", READERS)
def test_a_neighbouring_key_is_not_mistaken_for_it(getter, yes, wrong_key):
    """The failure this file exists for. A key off by one index or one letter is not an
    error, it is a reader that answers no for ever."""
    coordinator = _coordinator(wrong_key)

    assert getattr(coordinator, getter)() is False


@pytest.mark.parametrize("getter, yes, wrong_key", READERS)
def test_nothing_polled_yet_is_not_yes(getter, yes, wrong_key):
    """`data` is empty until the first poll lands, and every one of these is read while
    the entity is being built."""
    coordinator = _coordinator({})

    assert getattr(coordinator, getter)() is False


# --- the inverted one -------------------------------------------------------


def test_event_notifications_are_enabled_when_the_device_says_disabled_is_false():
    """`DisableEventNotify.Enable` is a *disable* flag, so enabled is `false`.
    Reading this as a normal Enable key inverts every notification sensor."""
    assert (
        _coordinator(
            {"table.DisableEventNotify.Enable": "false"}
        ).is_event_notifications_enabled()
        is True
    )

    assert (
        _coordinator(
            {"table.DisableEventNotify.Enable": "true"}
        ).is_event_notifications_enabled()
        is False
    )


def test_event_notifications_are_not_enabled_before_the_first_poll():
    """The empty default is `""`, which is not `"false"`, so an unpolled device reports
    disabled rather than enabled. Worth pinning: the opposite default would announce a
    capability the device has not confirmed."""
    assert _coordinator({}).is_event_notifications_enabled() is False


# --- the channel goes into the key ------------------------------------------


def test_motion_detection_reads_its_own_channel():
    """On a recorder every channel shares one polled table, so the channel in the key
    is the only thing separating this channel's answer from channel 0's."""
    data = {
        "table.MotionDetect[0].Enable": "false",
        "table.MotionDetect[2].Enable": "true",
    }

    assert _coordinator(data, channel=2).is_motion_detection_enabled() is True
    assert _coordinator(data, channel=0).is_motion_detection_enabled() is False


# --- case, which is not handled the same way everywhere ----------------------


@pytest.mark.parametrize(
    "getter, data",
    [
        ("is_motion_detection_enabled", {"table.MotionDetect[2].Enable": "TRUE"}),
        ("is_disarming_linkage_enabled", {"table.DisableLinkage.Enable": "True"}),
        ("is_siren_on", {"status.Speaker": "ON"}),
        ("is_security_light_on", {"status.WhiteLight": "on"}),
    ],
)
def test_these_readers_ignore_the_case_the_device_used(getter, data):
    assert getattr(_coordinator(data), getter)() is True


def test_the_ring_light_is_case_sensitive_and_that_is_measured_not_intended():
    """`is_ring_light_on` compares to `"true"` without lowering, unlike its neighbours.
    Pinned rather than fixed: a device answering `True` here would read as off, and
    changing it is a behaviour change for whatever firmware does that, so it should be
    a deliberate commit rather than a tidy-up inside another one."""
    assert (
        _coordinator({"table.LightGlobal[0].Enable": "true"}).is_ring_light_on() is True
    )
    assert (
        _coordinator({"table.LightGlobal[0].Enable": "True"}).is_ring_light_on()
        is False
    )


# --- the two level status lookup --------------------------------------------


def test_a_status_value_is_looked_for_in_both_shapes():
    """Devices answer `status.status.X` and others answer `status.X`, and the doubled
    form wins. Both are real, which is why the fallback exists."""
    assert (
        _coordinator({"status.status.Speaker": "On"}).get_status_value("Speaker")
        == "On"
    )
    assert _coordinator({"status.Speaker": "On"}).get_status_value("Speaker") == "On"


def test_the_doubled_form_wins_over_the_single_one():
    coordinator = _coordinator({"status.status.Speaker": "On", "status.Speaker": "Off"})

    assert coordinator.get_status_value("Speaker") == "On"


def test_an_absent_status_value_is_empty_rather_than_none():
    """Callers do `.lower()` on it, so `None` here would be an AttributeError on any
    device that does not report the key."""
    assert _coordinator({}).get_status_value("Speaker") == ""


# --- the hold time, which a user can type into ------------------------------


def test_the_hold_time_prefers_this_channel_over_the_entry():
    coordinator = _coordinator(
        channel_config={CONF_AUTHORIZED_HOLD_TIME: 90},
        options={CONF_AUTHORIZED_HOLD_TIME: 30},
    )

    assert coordinator.get_authorized_hold_time() == 90


def test_the_hold_time_falls_back_to_the_entry_then_the_default():
    assert (
        _coordinator(
            entry_data={CONF_AUTHORIZED_HOLD_TIME: 45}
        ).get_authorized_hold_time()
        == 45
    )

    assert _coordinator().get_authorized_hold_time() == DEFAULT_AUTHORIZED_HOLD_TIME


@pytest.mark.parametrize("typed", ["", "ninety", None, "12x"])
def test_an_unusable_hold_time_falls_back_instead_of_raising(typed):
    """It reaches here from a text field. Raising would take down whichever platform
    read it, which is a whole device lost to one bad character."""
    coordinator = _coordinator(channel_config={CONF_AUTHORIZED_HOLD_TIME: typed})

    assert coordinator.get_authorized_hold_time() == DEFAULT_AUTHORIZED_HOLD_TIME


def test_a_numeric_string_is_accepted():
    """The options flow stores what the field gave it, so the digits can arrive as text."""
    coordinator = _coordinator(channel_config={CONF_AUTHORIZED_HOLD_TIME: "90"})

    assert coordinator.get_authorized_hold_time() == 90


# --- the plate and the name ------------------------------------------------


def test_the_last_plate_is_unknown_until_one_is_read():
    assert _coordinator(_last_plate_data=None).get_last_plate() == "unknown"
    assert _coordinator(_last_plate_data={}).get_last_plate() == "unknown"


def test_a_plate_record_with_no_plate_in_it_is_unknown():
    """The record is built from an event payload, so the key can be absent."""
    assert _coordinator(_last_plate_data={"other": "x"}).get_last_plate() == "unknown"


def test_the_last_plate_is_reported_when_there_is_one():
    assert (
        _coordinator(_last_plate_data={"plate": "ABC123"}).get_last_plate() == "ABC123"
    )


def test_the_device_name_prefers_the_configured_one():
    """`_name` is what the user called it; `machine_name` is what the device calls
    itself. Preferring the device's would rename everything on the next poll."""
    assert (
        _coordinator(_name="Front Door", machine_name="IPC-HFW1234").get_device_name()
        == "Front Door"
    )

    assert (
        _coordinator(_name=None, machine_name="IPC-HFW1234").get_device_name()
        == "IPC-HFW1234"
    )


# --- and the readers that just surface an attribute -------------------------

# Thin, but a getter wired to the wrong attribute is the same silent failure as a
# wrong key, and several of these sit next to an identically named neighbour
# (`get_model` beside `get_channel_model`, `_serial_number` beside the composite
# `get_serial_number` builds).
ATTRIBUTES = [
    ("get_model", "model", "IPC-HFW1234"),
    ("get_channel_model", "_channel_model", "H32_VSIPP"),
    ("get_device_serial_number", "_serial_number", "SERIAL1"),
    ("get_max_streams", "_max_streams", 3),
    ("get_event_list", "events", ["VideoMotion"]),
    ("supports_privacy_mode", "_supports_privacy_mode", True),
    ("supports_day_night_color", "_supports_day_night_color", True),
    ("supports_disarming_linkage", "_supports_disarming_linkage", True),
    ("supports_profile_mode", "_supports_profile_mode", True),
]


@pytest.mark.parametrize("getter, attribute, value", ATTRIBUTES)
def test_the_reader_surfaces_its_own_attribute(getter, attribute, value):
    coordinator = _coordinator(**{attribute: value})

    assert getattr(coordinator, getter)() == value


def test_the_channel_model_is_not_the_device_model():
    """Deliberately separate: `get_model` answers what the device reports and
    `get_channel_model` what the channel reports, and #690 is what conflating them
    looks like. A test that set both to the same string would not notice a swap."""
    coordinator = _coordinator(model="NVR5464", _channel_model="H32_VSIPP")

    assert coordinator.get_model() == "NVR5464"
    assert coordinator.get_channel_model() == "H32_VSIPP"
