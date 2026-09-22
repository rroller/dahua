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
"""

from types import SimpleNamespace

from custom_components.dahua import DahuaDataUpdateCoordinator


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

    c.handle_event({"Code": "AccessControl", "action": "Pulse",
                    "Data": {"State": 1}})

    assert c.fired == [key], "a Pulse event updated no sensor at all"
    assert c._dahua_event_timestamp[key] > 0


def test_an_access_control_card_is_scanned_on_the_cgi_path():
    c = _coordinator()
    _listening(c, "AccessControl")

    c.handle_event({"Code": "AccessControl", "action": "Pulse",
                    "Data": {"State": 1, "CardNo": "1234ABCD"}})

    assert len(c.scanned) == 1, "the NFC tag was never handed to async_scan_tag"


def test_a_pulse_that_is_not_a_press_leaves_the_sensor_off():
    c = _coordinator()
    key = _listening(c, "AccessControl")

    c.handle_event({"Code": "AccessControl", "action": "Pulse",
                    "Data": {"State": 0}})

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

    c.on_receive_vto_event({"Code": "DoorStatus", "Action": "Pulse",
                            "Data": {"Status": "Open"}, "Index": 1})

    assert c.fired == [], "door 2 wrote to door 1's sensor"

    c.on_receive_vto_event({"Code": "DoorStatus", "Action": "Pulse",
                            "Data": {"Status": "Open"}, "Index": 0})

    assert c.fired == [key]
    assert c._dahua_event_timestamp[key] > 0


def test_the_doorbell_path_still_reads_the_button_state():
    c = _coordinator()
    key = _listening(c, "DoorbellPressed")

    c.on_receive_vto_event({"Code": "BackKeyLight", "Action": "Pulse",
                            "Data": {"State": 1}})

    assert c._dahua_event_timestamp[key] > 0
