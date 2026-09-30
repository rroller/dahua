"""The thin wrappers round the illuminator, and two listeners that must not take a poll down.

`illuminator_light_index` and `illuminator_brightness_bank` are both tested: each exists
because a hardcoded value was wrong on real hardware, #647 for the index and #652 for the
bank. The one-line methods that *call* them were not, and those are where the arguments
are chosen.

That matters more than the line count suggests. On an HFW3449E-S-IL, index 0 is the
infrared emitter and the white light is at 1, so a wrapper passing the wrong index turns
the **infrared** emitter up and down: the write is accepted, the configuration changes,
and the user sees nothing, because infrared is invisible. That is #647 exactly, and the
helper cannot protect against it, because by then the index has already been chosen.

Also here, two things on the event path that must fail quietly:

* **`async_start_event_listener` joins this host's existing stream** rather than opening a
  second one, because the device sends every channel's events down any stream. A channel
  with no events configured must not register at all.
* **A plate listener that raises must not stop the others.** They are entity callbacks,
  one per ANPR sensor, and an exception in the first would leave the rest of a recorder's
  plate sensors unnotified for that read.
"""

import asyncio

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator

CHANNEL = 2
PROFILE = "1"


def _coordinator(data=None, **attrs):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.data = {} if data is None else data
    coordinator._channel = CHANNEL
    coordinator._profile_mode = PROFILE
    coordinator._supports_lighting_scheme_illuminator = False
    for name, value in attrs.items():
        setattr(coordinator, name, value)
    return coordinator


def _light(index, field, value, *, channel=CHANNEL, profile=PROFILE):
    return {"table.Lighting_V2[{0}][{1}][{2}].{3}".format(
        channel, profile, index, field): value}


# --- which bank the wrapper asks about --------------------------------------


@pytest.mark.parametrize("bank", ["MiddleLight", "NearLight", "FarLight"])
def test_the_bank_the_device_names_is_the_one_returned(bank):
    coordinator = _coordinator(_light(0, "%s[0].Light" % bank, "50"))

    assert coordinator.get_illuminator_bank() == bank


def test_a_device_that_names_no_bank_falls_back_to_middlelight():
    """Which is what every caller did before the bank was looked up at all, so a
    device reporting none keeps exactly the behaviour it had."""
    assert _coordinator().get_illuminator_bank() == "MiddleLight"


def test_the_bank_is_read_for_the_white_light_index_not_index_zero():
    """#647. Index 0 is the infrared emitter on some models and the white light is at
    1, so asking about 0 aims brightness at a bank the white row may not even have.

    The device here says 0 is infrared and 1 is white, and only the white row has a
    NearLight bank. Getting MiddleLight back would mean index 0 was used.
    """
    data = {}
    data.update(_light(0, "LightType", "InfraredLight"))
    data.update(_light(0, "MiddleLight[0].Light", "50"))
    data.update(_light(1, "LightType", "WhiteLight"))
    data.update(_light(1, "NearLight[0].Light", "80"))

    assert _coordinator(data).get_illuminator_bank() == "NearLight"


def test_the_bank_is_read_for_this_channel_and_profile():
    """A recorder polls every channel into one table, and the profile changes between
    day and night, so both have to reach the lookup."""
    data = {}
    data.update(_light(0, "NearLight[0].Light", "80"))
    data.update(_light(0, "FarLight[0].Light", "80", channel=9))
    data.update(_light(0, "FarLight[0].Light", "80", profile="0"))

    assert _coordinator(data).get_illuminator_bank() == "NearLight"


# --- and whether it is on ---------------------------------------------------


def test_manual_means_the_illuminator_is_on():
    assert _coordinator(_light(0, "Mode", "Manual")).is_illuminator_on() is True


@pytest.mark.parametrize("mode", ["Off", "Auto", "", "manual"])
def test_anything_else_means_it_is_off(mode):
    """Compared exactly, so `manual` in the wrong case reads as off. Pinned as
    measured rather than tidied, since widening it changes what the light reports on
    whatever firmware spells it that way."""
    assert _coordinator(_light(0, "Mode", mode)).is_illuminator_on() is False


def test_nothing_polled_yet_is_off_rather_than_an_error():
    assert _coordinator().is_illuminator_on() is False


def test_the_mode_is_read_for_the_white_light_index():
    """Same #647 problem on the read side: reading index 0's mode on a device whose
    white light is at 1 reports the infrared emitter's state as the illuminator's."""
    data = {}
    data.update(_light(0, "LightType", "InfraredLight"))
    data.update(_light(0, "Mode", "Off"))
    data.update(_light(1, "LightType", "WhiteLight"))
    data.update(_light(1, "Mode", "Manual"))

    assert _coordinator(data).is_illuminator_on() is True


def test_a_scheme_device_needs_both_the_mode_and_whitemode():
    """The other branch, for completeness: where a lighting scheme is in play, Manual
    alone does not light it."""
    data = {}
    data.update(_light(0, "Mode", "Manual"))
    coordinator = _coordinator(data, _supports_lighting_scheme_illuminator=True)

    assert coordinator.is_illuminator_on() is False

    data["table.LightingScheme[{0}][{1}].LightingMode".format(
        CHANNEL, PROFILE)] = "WhiteMode"

    assert coordinator.is_illuminator_on() is True


# --- joining the host's event stream ---------------------------------------


async def test_a_channel_with_events_joins_the_hosts_stream():
    """One stream per host, not per channel: the device sends every channel's events
    down any stream, so a second connection buys nothing and costs a socket."""
    registered = []

    coordinator = _coordinator(
        events=["VideoMotion"], hass=object(), _address="10.0.0.5")

    class _Stream:
        def register(self, who):
            registered.append(who)

    import custom_components.dahua.coordinator as module
    original = module._host_stream
    module._host_stream = lambda hass, address: _Stream()
    try:
        await coordinator.async_start_event_listener()
    finally:
        module._host_stream = original

    assert registered == [coordinator]


async def test_a_channel_with_no_events_does_not_register():
    """`None` means nothing was selected, and registering would hold a stream open for
    a channel that has nothing to receive."""
    called = []

    coordinator = _coordinator(events=None, hass=object(), _address="10.0.0.5")

    import custom_components.dahua.coordinator as module
    original = module._host_stream

    def _should_not_run(hass, address):
        called.append(address)
        raise AssertionError("a stream was opened for a channel with no events")

    module._host_stream = _should_not_run
    try:
        await coordinator.async_start_event_listener()
    finally:
        module._host_stream = original

    assert called == []


async def test_the_doorbell_listener_runs_as_a_task_it_can_be_stopped_by():
    """A doorbell streams events over its own long-lived connection rather than joining
    the host stream, so it is held as a task on the coordinator. Keeping the handle is
    what lets unload stop it: without it the connection outlives the entry and keeps
    reconnecting to a device Home Assistant has forgotten.
    """
    ran = []

    async def _stream():
        ran.append("started")
        await asyncio.sleep(0)

    coordinator = _coordinator(_async_stream_vto_events=_stream, _vto_task=None)

    await coordinator.async_start_vto_event_listener()

    assert coordinator._vto_task is not None, "no handle was kept, so nothing can stop it"
    await coordinator._vto_task
    assert ran == ["started"]


# --- a plate listener that raises ------------------------------------------


def test_one_failing_plate_listener_does_not_stop_the_others():
    """They are entity callbacks, one per ANPR sensor. An exception in the first would
    leave the rest of a recorder's plate sensors unnotified for that read, and the
    event has already been put on the bus by then, so there is nothing to retry."""
    notified = []

    def _raises():
        raise RuntimeError("that sensor has gone")

    coordinator = _coordinator(
        _plate_listeners=[_raises, lambda: notified.append("second")],
        _last_plate_data=None,
        _last_plate_timestamp=0,
        get_device_name=lambda: "Gate",
        # Stubbed rather than driven: this test is about the listener loop, and
        # whether a plate is authorised is a separate contract with its own tests.
        is_plate_authorized=lambda plate: False,
        hass=type("_Hass", (), {"bus": type("_Bus", (), {"fire": lambda *a: None})()})(),
    )

    coordinator._handle_anpr_plate(
        {"Code": "TrafficJunction",
         "data": {"TrafficCar": {"PlateNumber": "ABC123"}}})

    assert notified == ["second"], (
        "the second listener was skipped because the first raised")
