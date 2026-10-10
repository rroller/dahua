"""The CGI event path lost Pulse handling and NFC tag scanning.

Two streams reach the same sensors. The DHIP one (doorbells) handled Start,
Stop *and* Pulse, and handed AccessControl cards to async_scan_tag. The CGI one
-- every camera and every NVR channel -- handled only Start and Stop:

    action = event.get("action")
    if action == "Start":  ...
    elif action == "Stop": ...
                                  # and nothing else

So on that transport a Pulse event reached the Home Assistant event bus, where
an automation could see it, and then updated no sensor at all. An AccessControl
card was never scanned. Both behaviours had existed on the doorbell path the
whole time, which is how the gap stayed invisible.

Found by comparing against myhomeiot/DahuaVTO, whose single event path has no
such split.

**The three CGI tests below used to hand the payload in as `Data`**, which is
the DHIP spelling. The CGI wire format is `Code=X;action=Y;index=Z;data={json}`
and `parse_event` splits it on `=`, so that path produces a lowercase `data` --
the example payloads in `on_receive`'s docstring show it. So these exercised the
lowercase `action` branch and then handed the payload in the one casing the code
happened to read, and passed while the real CGI path read nothing: the door
status stayed closed with the door open, the button never raised, and no card
ever reached async_scan_tag. They feed `data` now, and `event_payload` reads
both.
"""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator, event_payload


def _coordinator(channel=0):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c._dahua_event_timestamp = {}
    c._dahua_event_listeners = {}
    c.fired = []
    c.scanned = []
    c.hass = SimpleNamespace(
        bus=SimpleNamespace(fire=lambda *a, **k: None),
        async_create_task=lambda coro: c.scanned.append(coro),
    )
    c.get_device_name = lambda: "Side Gate"
    # handle_event logs the address before dispatching, and get_address reads
    # this. object.__new__ keeps the class's methods but none of the attributes
    # __init__ would have set, so anything they touch has to be named here.
    c._address = "10.0.0.5"
    c._handle_anpr_plate = lambda event: None
    return c


def _listening(c, code):
    key = c.get_event_key(code)
    # Through the public API: the dict holds a list of listeners per event
    # since #715, because two entities can want the same one.
    c.add_dahua_event_listener(code, lambda: c.fired.append(key))
    return key


# --- the CGI path: what it used to drop -------------------------------------


def test_a_pulse_on_the_cgi_path_now_reaches_the_sensor():
    c = _coordinator()
    key = _listening(c, "AccessControl")

    c.handle_event({"Code": "AccessControl", "action": "Pulse", "data": {"State": 1}})

    assert c.fired == [key], "a Pulse event updated no sensor at all"
    assert c._dahua_event_timestamp[key] > 0


async def test_an_access_control_card_is_scanned_on_the_cgi_path():
    c = _coordinator()
    _listening(c, "AccessControl")

    # The coordinator resolves async_scan_tag in its own module, so patching
    # the package rebound a name nothing reads and the real helper ran.
    with patch(
        "custom_components.dahua.coordinator.async_scan_tag", new_callable=AsyncMock
    ) as scan_tag:
        c.handle_event(
            {
                "Code": "AccessControl",
                "action": "Pulse",
                "data": {"State": 1, "CardNo": "1234ABCD"},
            }
        )

        # Execute every coroutine queued by the fake Home Assistant scheduler.
        for coroutine in c.scanned:
            await coroutine

        assert len(c.scanned) == 1, "the NFC tag was never handed to async_scan_tag"
        scan_tag.assert_awaited_once_with(
            c.hass, hashlib.md5(b"1234ABCD").hexdigest(), "Side Gate"
        )


def test_a_pulse_that_is_not_a_press_leaves_the_sensor_off():
    c = _coordinator()
    key = _listening(c, "AccessControl")

    c.handle_event({"Code": "AccessControl", "action": "Pulse", "data": {"State": 0}})

    assert c._dahua_event_timestamp[key] == 0
    assert c.fired == [key], "the entity is still told to re-read"


# --- and what it must keep doing --------------------------------------------


def test_start_and_stop_still_work_on_the_cgi_path():
    c = _coordinator()
    key = _listening(c, "VideoMotion")

    c.handle_event({"Code": "VideoMotion", "action": "Start"})
    assert c._dahua_event_timestamp[key] > 0

    c.handle_event({"Code": "VideoMotion", "action": "Stop"})
    assert c._dahua_event_timestamp[key] == 0
    assert len(c.fired) == 2


def test_an_unconfigured_code_is_still_ignored():
    c = _coordinator()

    c.handle_event({"Code": "VideoMotion", "action": "Start"})

    assert c.fired == []


# --- the doorbell path must behave exactly as before ------------------------


def test_the_doorbell_path_keeps_its_door_index_guard():
    """#488: only door 1 may write to the single Door Status sensor."""
    c = _coordinator()
    key = _listening(c, "DoorStatus")

    c.on_receive_vto_event(
        {
            "Code": "DoorStatus",
            "Action": "Pulse",
            "Data": {"Status": "Open"},
            "Index": 1,
        }
    )

    assert c.fired == [], "door 2 wrote to door 1's sensor"

    c.on_receive_vto_event(
        {
            "Code": "DoorStatus",
            "Action": "Pulse",
            "Data": {"Status": "Open"},
            "Index": 0,
        }
    )

    assert c.fired == [key]
    assert c._dahua_event_timestamp[key] > 0


def test_the_doorbell_path_still_reads_the_button_state():
    c = _coordinator()
    key = _listening(c, "DoorbellPressed")

    c.on_receive_vto_event(
        {"Code": "BackKeyLight", "Action": "Pulse", "Data": {"State": 1}}
    )

    assert c._dahua_event_timestamp[key] > 0


# --- the payload casing, which is what the CGI path was reading nothing from --
#
# `_dispatch_event` is shared by both transports, and four of its reads named
# only `Data`. DHIP sends that; the CGI path sends `data`. So on every camera,
# every recorder channel, and any doorbell that does not answer getDeviceClass
# and is not in is_doorbell's model-prefix list (the #690 rebadge class), those
# four reads found nothing at all.


def test_a_door_opening_on_the_cgi_path_raises_the_sensor():
    c = _coordinator()
    key = _listening(c, "DoorStatus")

    c.handle_event(
        {"Code": "DoorStatus", "action": "Pulse", "data": {"Status": "Open"}}
    )

    assert c._dahua_event_timestamp[key] > 0, "the door read closed while it was open"


def test_a_door_closing_on_the_cgi_path_clears_it():
    c = _coordinator()
    key = _listening(c, "DoorStatus")

    c.handle_event(
        {"Code": "DoorStatus", "action": "Pulse", "data": {"Status": "Open"}}
    )
    c.handle_event(
        {"Code": "DoorStatus", "action": "Pulse", "data": {"Status": "Close"}}
    )

    assert c._dahua_event_timestamp[key] == 0


def test_the_cgi_path_keeps_the_door_index_guard_too():
    """#488 again, on the other transport. The door number arrives as `index`
    here, so reading only `Index` made every door look like door 0 and a second
    door's Open wrote to the first door's sensor."""
    c = _coordinator()
    key = _listening(c, "DoorStatus")

    c.handle_event(
        {
            "Code": "DoorStatus",
            "action": "Pulse",
            "data": {"Status": "Open"},
            "index": 1,
        }
    )

    assert c._dahua_event_timestamp.get(key, 0) == 0, "door 2 wrote door 1's sensor"


def test_a_button_press_on_the_cgi_path_raises_the_sensor():
    c = _coordinator()
    key = _listening(c, "DoorbellPressed")

    c.handle_event({"Code": "BackKeyLight", "action": "Pulse", "data": {"State": 1}})

    assert c._dahua_event_timestamp[key] > 0, "the press never raised the sensor"


def test_a_quiet_call_state_on_the_cgi_path_clears_it():
    """The other half: State 0 is idle, and it has to be read as idle rather
    than as an absent payload, which also reads as 0."""
    c = _coordinator()
    key = _listening(c, "DoorbellPressed")

    c.handle_event({"Code": "BackKeyLight", "action": "Pulse", "data": {"State": 1}})
    c.handle_event({"Code": "BackKeyLight", "action": "Pulse", "data": {"State": 0}})

    assert c._dahua_event_timestamp[key] == 0


async def test_a_card_on_the_dhip_path_is_still_scanned():
    """The mirror of the CGI card test above, so neither casing can be fixed by
    breaking the other."""
    c = _coordinator()
    _listening(c, "AccessControl")

    with patch(
        "custom_components.dahua.coordinator.async_scan_tag", new_callable=AsyncMock
    ) as scan_tag:
        c.on_receive_vto_event(
            {
                "Code": "AccessControl",
                "Action": "Pulse",
                "Data": {"State": 1, "CardNo": "1234ABCD"},
            }
        )

        for coroutine in c.scanned:
            await coroutine

        scan_tag.assert_awaited_once_with(
            c.hass, hashlib.md5(b"1234ABCD").hexdigest(), "Side Gate"
        )


def test_a_truncated_payload_does_not_raise():
    """parse_event leaves the raw string when the JSON did not parse, and `.get`
    on a string raises out of the stream loop, which wraps on_receive in
    try/finally with no handler. That took every channel on a host down once
    already (#475)."""
    c = _coordinator()
    key = _listening(c, "DoorStatus")

    c.handle_event({"Code": "DoorStatus", "action": "Pulse", "data": '{"Status": "Op'})

    assert c._dahua_event_timestamp.get(key, 0) == 0


# --- the helper that settles it, as a function -------------------------------


@pytest.mark.parametrize(
    "event, expected",
    [
        # The two real shapes.
        ({"data": {"Status": "Open"}}, {"Status": "Open"}),
        ({"Data": {"Status": "Open"}}, {"Status": "Open"}),
        # No payload at all, which is every Start and Stop.
        ({"Code": "VideoMotion", "action": "Start"}, {}),
        # parse_event leaves the raw string when the JSON did not parse, and a
        # `.get` on it raises out of the stream loop (#475).
        ({"data": '{"Status": "Op'}, {}),
        # A device really does send nulls: "Track": None is in the coordinator's
        # own example payload.
        ({"data": None}, {}),
        ({"Data": []}, {}),
    ],
)
def test_the_payload_is_read_in_either_casing(event, expected):
    assert event_payload(event) == expected
