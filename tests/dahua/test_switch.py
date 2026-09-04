"""switch.py had no tests. These pin the call each switch actually makes."""

import pytest

from custom_components.dahua.client import SECURITY_LIGHT_TYPE, SIREN_TYPE
from custom_components.dahua.switch import (
    DahuaDisarmingEventNotificationsLinkageBinarySwitch,
    DahuaDisarmingLinkageBinarySwitch,
    DahuaMotionDetectionBinarySwitch,
    DahuaSirenBinarySwitch,
    DahuaSmartMotionDetectionBinarySwitch,
)


class _Client:
    def __init__(self):
        self.calls = []

    def _record(self, name):
        async def call(*args):
            self.calls.append((name,) + args)
        return call

    def __getattr__(self, name):
        return self._record(name)


class _Coordinator:
    def __init__(self, channel=4, amcrest=False):
        self.client = _Client()
        self._channel = channel
        self._amcrest = amcrest
        self.refreshed = 0
        self.states = {}

    def get_channel(self):
        return self._channel

    def get_serial_number(self):
        return "SERIAL1"

    def get_device_name(self):
        return "Garage"

    def supports_smart_motion_detection_amcrest(self):
        return self._amcrest

    def is_motion_detection_enabled(self):
        return self.states.get("motion", False)

    def is_disarming_linkage_enabled(self):
        return self.states.get("disarming", False)

    def is_event_notifications_enabled(self):
        return self.states.get("notifications", False)

    def is_smart_motion_detection_enabled(self):
        return self.states.get("smart", False)

    def is_siren_on(self):
        return self.states.get("siren", False)

    async def async_refresh(self):
        self.refreshed += 1


def _switch(cls, coordinator):
    entity = object.__new__(cls)
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    return entity


ALL = [
    DahuaMotionDetectionBinarySwitch,
    DahuaDisarmingLinkageBinarySwitch,
    DahuaDisarmingEventNotificationsLinkageBinarySwitch,
    DahuaSmartMotionDetectionBinarySwitch,
    DahuaSirenBinarySwitch,
]


# --- each switch drives its own API ----------------------------------------

@pytest.mark.parametrize("cls,method", [
    (DahuaMotionDetectionBinarySwitch, "enable_motion_detection"),
    (DahuaDisarmingLinkageBinarySwitch, "async_set_disarming_linkage"),
    (DahuaDisarmingEventNotificationsLinkageBinarySwitch, "async_set_event_notifications"),
])
async def test_channel_switches_send_channel_and_state(cls, method):
    c = _Coordinator(channel=4)
    s = _switch(cls, c)

    await s.async_turn_on()
    await s.async_turn_off()

    assert c.client.calls == [(method, 4, True), (method, 4, False)]
    assert c.refreshed == 2, "the UI would show a stale state without a refresh"


async def test_siren_asks_for_the_siren_not_the_security_light():
    """Both ride the same CGI; the type is the only thing separating them."""
    c = _Coordinator(channel=4)
    s = _switch(DahuaSirenBinarySwitch, c)

    await s.async_turn_on()

    assert c.client.calls == [("async_set_coaxial_control_state", 4, SIREN_TYPE, True)]
    assert SIREN_TYPE != SECURITY_LIGHT_TYPE


async def test_siren_turn_off_keeps_the_siren_type():
    c = _Coordinator(channel=1)
    await _switch(DahuaSirenBinarySwitch, c).async_turn_off()
    assert c.client.calls == [("async_set_coaxial_control_state", 1, SIREN_TYPE, False)]


# --- smart motion picks an API by vendor -----------------------------------

async def test_smart_motion_uses_the_dahua_api_by_default():
    c = _Coordinator(amcrest=False)
    s = _switch(DahuaSmartMotionDetectionBinarySwitch, c)

    await s.async_turn_on()
    await s.async_turn_off()

    assert c.client.calls == [
        ("async_enabled_smart_motion_detection", True),
        ("async_enabled_smart_motion_detection", False),
    ]


async def test_smart_motion_uses_the_ivs_rule_on_amcrest():
    c = _Coordinator(amcrest=True)
    s = _switch(DahuaSmartMotionDetectionBinarySwitch, c)

    await s.async_turn_on()
    await s.async_turn_off()

    assert c.client.calls == [
        ("async_set_ivs_rule", 0, 0, True),
        ("async_set_ivs_rule", 0, 0, False),
    ]


# --- identity --------------------------------------------------------------

def test_every_switch_has_its_own_unique_id():
    """A collision would silently merge two switches into one entity."""
    c = _Coordinator()
    ids = [_switch(cls, c).unique_id for cls in ALL]

    assert len(set(ids)) == len(ids), "duplicate unique_id among %s" % ids
    assert all(i.startswith("SERIAL1_") for i in ids)


def test_every_switch_is_named_after_the_device():
    c = _Coordinator()
    for cls in ALL:
        assert _switch(cls, c).name.startswith("Garage "), cls.__name__


@pytest.mark.parametrize("cls,key", [
    (DahuaMotionDetectionBinarySwitch, "motion"),
    (DahuaDisarmingLinkageBinarySwitch, "disarming"),
    (DahuaDisarmingEventNotificationsLinkageBinarySwitch, "notifications"),
    (DahuaSmartMotionDetectionBinarySwitch, "smart"),
])
def test_is_on_reflects_the_coordinator(cls, key):
    c = _Coordinator()
    s = _switch(cls, c)

    assert s.is_on is False
    c.states[key] = True
    assert s.is_on is True
