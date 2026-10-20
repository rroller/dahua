"""switch.py had no tests. These pin the call each switch actually makes."""

import pytest

from custom_components.dahua.client import SECURITY_LIGHT_TYPE, SIREN_TYPE
from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.switch import (
    DahuaAlarmOutputSwitch,
    DahuaDisarmingEventNotificationsLinkageBinarySwitch,
    DahuaDisarmingLinkageBinarySwitch,
    DahuaMotionDetectionBinarySwitch,
    DahuaPrivacyModeBinarySwitch,
    DahuaSirenBinarySwitch,
    DahuaSmartMotionDetectionBinarySwitch,
)

from . import adds_entities


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
    # The platforms file each channel's entities under its own subentry, so they
    # read this on every entity they add. None is a single camera, and is what
    # `async_add_entities` wants for an entry that has no subentries.
    subentry_id = None

    # The real rule rather than a copy of it. switch.py now asks the coordinator whether
    # to create the siren, because the poll needs the same answer to decide whether to
    # fetch the status that entity reads, and a hand-written second copy here would be
    # the same drift that change removes. It derives from uses_recorder_deterrence and
    # supports_siren below, exactly as production does.
    creates_siren_entity = DahuaDataUpdateCoordinator.creates_siren_entity

    def __init__(self, channel=4, amcrest=False):
        self.client = _Client()
        self._channel = channel
        self._amcrest = amcrest
        self.refreshed = 0
        self.states = {}
        # CoordinatorEntity.available reads this, and the siren switch now builds
        # on super().available so it can go unavailable after a refusal (#942).
        self.last_update_success = True

    def get_channel(self):
        return self._channel

    def get_channel_number(self):
        return self._channel + 1

    def uses_recorder_deterrence(self):
        return False

    def is_recorder_host(self):
        return False

    def is_nvr_channel(self):
        return False

    def supports_nvr_active_deterrence(self):
        return False

    def uses_rpc2_deterrence(self, dahua_type=None):
        return False

    def supports_event_notifications(self):
        """Its own table now, rather than riding on the linkage answer.

        A VTO2000A answers HTTP 500 to both of those getConfig reads while
        serving MotionDetect, so the two switches it was given could only ever
        read off. switch.py asks about each table separately since.
        """
        return False

    def supports_cloud_connection(self):
        """Whether the device reports VSP_PaaS, which the cloud switch writes.

        Off here: that switch is the box's rather than this channel's, and the
        setup this double drives is about the per-channel ones. Its own file
        covers which channel carries it (#591).
        """
        return self.states.get("has_cloud_connection", False)

    def supports_vth_phone_setting(self, field):
        """Whether this device reported an indoor monitor's call setting (#949).

        Driven from `states` like the rest, so a case can turn the monitor's
        do-not-disturb switch on without the double growing a second shape.
        Off by default: these are cameras and recorders.
        """
        return field in self.states.get("vth_phone_fields", ())

    def supports_intelli_tour(self):
        """Whether this channel reports IntelliTour, which the tour switch writes.

        Off unless a test says otherwise: the tour belongs to a PTZ channel and
        most channels do not have one. Its own file covers the rest (#1051).
        """
        return self.states.get("has_intelli_tour", False)

    def uses_separate_host_device(self):
        """Only read when the cloud switch is created, to decide whether it is
        filed under the channel's subentry or the entry itself."""
        return False

    def get_ivs_rules(self):
        return []

    def get_serial_number(self):
        return "SERIAL1"

    def get_device_serial_number(self):
        """The box's serial, with no channel on it. On channel 0 of anything
        it is the same string, which is this double."""
        return "SERIAL1"

    def get_address(self):
        """Read by CoordinatorEntity.available, which the siren now builds on, and
        by refusals.key_for."""
        return "192.168.0.213"

    def get_device_name(self):
        return "Garage"

    def supports_smart_motion_detection_amcrest(self):
        return self._amcrest

    def supports_privacy_mode(self):
        return self.states.get("has_privacy_mode", False)

    def is_indoor_monitor_without_video(self):
        return self.states.get("indoor_monitor_without_video", False)

    def supports_alarm_output(self):
        return self.states.get("has_alarm_output", False)

    def is_alarm_output_on(self, output=0):
        return output in self.states.get("alarm_outputs_on", set())

    def get_alarm_output_slots(self):
        return self.states.get("alarm_output_slots", 1)

    def get_alarm_output_name(self, output=0):
        return self.states.get("alarm_output_names", {}).get(
            output, "Alarm output %d" % (output + 1)
        )

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


@pytest.mark.parametrize(
    "cls,method",
    [
        (DahuaMotionDetectionBinarySwitch, "enable_motion_detection"),
        (DahuaDisarmingLinkageBinarySwitch, "async_set_disarming_linkage"),
        (
            DahuaDisarmingEventNotificationsLinkageBinarySwitch,
            "async_set_event_notifications",
        ),
    ],
)
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

    # The channel goes with it. Without it every channel of an NVR wrote
    # SmartMotionDetect[0], setting channel one's option from any camera.
    assert c.client.calls == [
        ("async_enabled_smart_motion_detection", 4, True),
        ("async_enabled_smart_motion_detection", 4, False),
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


def test_every_switch_declares_a_translation_key():
    """All five name themselves through translations/en.json now.

    The docstring here used to say the siren's name was handed in by the
    platform because there could be several told apart by index. The first half
    stopped being true and the second half was never true: it varied by whether
    the host is a recorder, where it is called "Alarm".

    The siren picks between two keys, so the platform passes one and this reads
    it off the class list instead. Those two literals, and the strings behind
    them, are pinned in test_entity_names_come_from_translations.py."""
    c = _Coordinator()
    for cls in ALL:
        if cls is DahuaSirenBinarySwitch:
            # Its key is chosen by the platform and set on the instance, and
            # `_switch` builds with object.__new__, so there is nothing on the
            # class to read. Built properly here instead, both ways round.
            for key in ("siren", "alarm"):
                built = object.__new__(cls)
                built._attr_translation_key = key
                assert built.translation_key == key
            continue
        # Off the instance, not the class: `_attr_translation_key` read from a
        # class is the metaclass's property object, which is truthy, so a
        # class-level assertion passes for a class that declares nothing.
        assert _switch(cls, c).translation_key, cls.__name__


@pytest.mark.parametrize(
    "cls,key",
    [
        (DahuaMotionDetectionBinarySwitch, "motion"),
        (DahuaDisarmingLinkageBinarySwitch, "disarming"),
        (DahuaDisarmingEventNotificationsLinkageBinarySwitch, "notifications"),
        (DahuaSmartMotionDetectionBinarySwitch, "smart"),
    ],
)
def test_is_on_reflects_the_coordinator(cls, key):
    c = _Coordinator()
    s = _switch(cls, c)

    assert s.is_on is False
    c.states[key] = True
    assert s.is_on is True


# --- platform setup must not talk to the device -----------------------------


async def test_setup_asks_the_device_nothing():
    """A network call here spends the entry's setup budget.

    The coordinator already read the disarming linkage during its own setup and
    kept the answer; asking again put a round trip inside platform setup, where
    a slow device could take the whole entry down (issue #513).
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    from custom_components.dahua import switch as switch_module
    from custom_components.dahua.const import DOMAIN

    coordinator = _Coordinator()
    coordinator.supports_siren = lambda: False
    coordinator.supports_smart_motion_detection = lambda: False
    coordinator.supports_disarming_linkage = lambda: True
    coordinator.supports_event_notifications = lambda: True

    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(
        entry_id="e1",
        options={},
        runtime_data={0: coordinator},
        # Setup keeps a listener for capabilities that turn up on a later poll.
        async_on_unload=lambda unsub: None,
    )
    added = []

    # The decision is what changed here, not how the entities are built.
    with patch.multiple(
        switch_module,
        DahuaMotionDetectionBinarySwitch=lambda *a, **k: "motion",
        DahuaDisarmingLinkageBinarySwitch=lambda *a, **k: "disarming",
        DahuaDisarmingEventNotificationsLinkageBinarySwitch=lambda *a, **k: "notifications",
    ):
        await switch_module.async_setup_entry(hass, entry, adds_entities(added))

    assert coordinator.client.calls == [], (
        "platform setup made a network call: %s" % coordinator.client.calls
    )
    assert "disarming" in added and "notifications" in added


async def test_the_disarming_switches_follow_what_the_device_answered():
    """A device that refused the read must not get switches it cannot serve."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from custom_components.dahua import switch as switch_module
    from custom_components.dahua.const import DOMAIN

    coordinator = _Coordinator()
    coordinator.supports_siren = lambda: False
    coordinator.supports_smart_motion_detection = lambda: False
    coordinator.supports_disarming_linkage = lambda: False

    hass = SimpleNamespace(data={})
    added = []

    with patch.multiple(
        switch_module,
        DahuaMotionDetectionBinarySwitch=lambda *a, **k: "motion",
        DahuaDisarmingLinkageBinarySwitch=lambda *a, **k: "disarming",
        DahuaDisarmingEventNotificationsLinkageBinarySwitch=lambda *a, **k: "notifications",
    ):
        await switch_module.async_setup_entry(
            hass,
            SimpleNamespace(
                entry_id="e1",
                options={},
                runtime_data={0: coordinator},
                async_on_unload=lambda unsub: None,
            ),
            adds_entities(added),
        )

    assert "disarming" not in added
    assert "notifications" not in added
    assert coordinator.client.calls == []


async def test_each_disarming_switch_follows_its_own_table():
    """DisableLinkage and DisableEventNotify are two tables, and a device may
    serve one of them. Both switches were created from the linkage answer, so
    a device that serves only that one got a notifications switch reading a
    key it never reports.

    Measured on the hardware here: a VTO2000A answers HTTP 500 to both reads
    while serving MotionDetect, and a DHI-NVR5464-16P-EI answers 200 to both.
    Neither is the mixed case, which is why it is asserted rather than
    assumed.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    from custom_components.dahua import switch as switch_module

    coordinator = _Coordinator()
    coordinator.supports_siren = lambda: False
    coordinator.supports_smart_motion_detection = lambda: False
    coordinator.supports_disarming_linkage = lambda: True
    coordinator.supports_event_notifications = lambda: False

    added = []
    with patch.multiple(
        switch_module,
        DahuaMotionDetectionBinarySwitch=lambda *a, **k: "motion",
        DahuaDisarmingLinkageBinarySwitch=lambda *a, **k: "disarming",
        DahuaDisarmingEventNotificationsLinkageBinarySwitch=lambda *a, **k: "notifications",
    ):
        await switch_module.async_setup_entry(
            SimpleNamespace(data={}),
            SimpleNamespace(
                entry_id="e1",
                options={},
                runtime_data={0: coordinator},
                async_on_unload=lambda unsub: None,
            ),
            adds_entities(added),
        )

    assert "disarming" in added
    assert "notifications" not in added


# --- the alarm output relay ---------------------------------------------------
#
# A physical relay rather than a setting, so what it writes matters more than most:
# AlarmOut.Mode=1 forces it on and Mode=2 forces it off. Its three methods had no tests.


async def test_the_alarm_output_forces_on_and_off():
    c = _Coordinator()
    s = _switch(DahuaAlarmOutputSwitch, c)
    s._output = 0

    await s.async_turn_on()
    await s.async_turn_off()

    assert c.client.calls == [
        ("async_set_alarm_output_state", 0, True),
        ("async_set_alarm_output_state", 0, False),
    ]


async def test_the_alarm_output_refreshes_so_the_state_is_read_back():
    """It reports the physical state from alarm.cgi, so without the refresh the entity
    shows the old one until the next poll."""
    c = _Coordinator()
    s = _switch(DahuaAlarmOutputSwitch, c)
    s._output = 0

    await s.async_turn_on()

    assert c.refreshed == 1


async def test_the_alarm_output_writes_the_output_it_was_given():
    """A device with more than one relay gets an entity each, so the index cannot be
    assumed to be zero."""
    c = _Coordinator()
    s = _switch(DahuaAlarmOutputSwitch, c)
    s._output = 2

    await s.async_turn_on()

    assert c.client.calls == [("async_set_alarm_output_state", 2, True)]


def test_two_alarm_outputs_do_not_share_an_entity():
    """The index is in the unique id. Without it a second relay would claim the entity
    belonging to the first, which is the shape of #850."""
    c = _Coordinator()
    first = _switch(DahuaAlarmOutputSwitch, c)
    second = _switch(DahuaAlarmOutputSwitch, c)
    first._output, second._output = 0, 1

    assert first.unique_id != second.unique_id
    assert first.unique_id.endswith("_alarm_output_0")
    assert second.unique_id.endswith("_alarm_output_1")


def test_the_alarm_output_reports_the_physical_state():
    """Read back from the device, never from what was last written: these
    boxes accept a write, answer OK and drive nothing."""
    c = _Coordinator()
    s = _switch(DahuaAlarmOutputSwitch, c)
    s._output = 0

    assert s.is_on is False
    c.states["alarm_outputs_on"] = {0}
    assert s.is_on is True


def test_each_relay_reports_its_own_state():
    """`getOutState` is one bitmask covering every relay, so a switch that
    ignored its own index would show all six as whatever the first one is."""
    c = _Coordinator()
    c.states["alarm_outputs_on"] = {5}
    first = _switch(DahuaAlarmOutputSwitch, c)
    first._output = 0
    sixth = _switch(DahuaAlarmOutputSwitch, c)
    sixth._output = 5

    assert first.is_on is False
    assert sixth.is_on is True


# --- privacy mode -------------------------------------------------------------


async def test_privacy_mode_covers_and_uncovers_the_lens():
    """This one moves a motorised cover, so it is the only switch here whose off state
    the camera cannot see past."""
    c = _Coordinator()
    s = _switch(DahuaPrivacyModeBinarySwitch, c)

    await s.async_turn_on()
    await s.async_turn_off()

    assert c.client.calls == [
        ("async_set_privacy_mode", True),
        ("async_set_privacy_mode", False),
    ]
    assert c.refreshed == 2


# --- the siren, built the way the platform builds it --------------------------


def test_the_siren_takes_its_key_from_the_platform():
    """Called Alarm on a recorder and Siren otherwise, which the platform decides. The
    test above reads the key off a class; this one builds a siren properly."""
    c = _Coordinator()

    siren = DahuaSirenBinarySwitch(c, object(), translation_key="alarm")

    assert siren.translation_key == "alarm"


def test_the_siren_key_must_be_passed_by_name():
    """Keyword only on purpose: a call site still passing the old display name
    positionally would set a translation key that is not a slug and would then never
    match, silently. This makes it fail loudly instead."""
    c = _Coordinator()

    with pytest.raises(TypeError):
        DahuaSirenBinarySwitch(c, object(), "Siren")


def test_the_siren_reports_what_the_coordinator_read():
    c = _Coordinator()
    s = _switch(DahuaSirenBinarySwitch, c)

    assert s.is_on is False
    c.states["siren"] = True
    assert s.is_on is True


# --- which switches setup creates --------------------------------------------
#
# The two tests above about setup are there because it must not talk to the device. These
# are about the decision it makes: three of the appends had no test, so three switches
# were only ever built by hand.


async def _added_for(coordinator):
    """Run the real platform setup with every entity replaced by its name."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from custom_components.dahua import switch as switch_module

    for name in (
        "supports_siren",
        "supports_smart_motion_detection",
        "supports_disarming_linkage",
        "supports_event_notifications",
    ):
        if not hasattr(coordinator, name):
            setattr(coordinator, name, lambda: False)

    entry = SimpleNamespace(
        entry_id="e1",
        options={},
        runtime_data={0: coordinator},
        # Setup keeps a listener for capabilities that turn up on a later poll.
        async_on_unload=lambda unsub: None,
    )
    added = []
    with patch.multiple(
        switch_module,
        DahuaMotionDetectionBinarySwitch=lambda *a, **k: "motion",
        DahuaDisarmingLinkageBinarySwitch=lambda *a, **k: "disarming",
        DahuaDisarmingEventNotificationsLinkageBinarySwitch=lambda *a, **k: "notifications",
        DahuaSmartMotionDetectionBinarySwitch=lambda *a, **k: "smart",
        DahuaPrivacyModeBinarySwitch=lambda *a, **k: "privacy",
        DahuaAlarmOutputSwitch=lambda *a, **k: "alarm_output",
        # The multi-relay variant is a different class, because an
        # entity that names itself cannot also translate its name.
        DahuaNamedAlarmOutputSwitch=lambda *a, **k: "named_alarm_output",
    ):
        await switch_module.async_setup_entry(
            SimpleNamespace(data={}), entry, adds_entities(added)
        )
    return added


async def test_privacy_mode_is_only_offered_where_the_camera_has_a_cover():
    """Only cameras reporting a LeLensMask table have one, and on any other the call
    fails, so offering the switch would be offering something that cannot work."""
    without = _Coordinator()
    without.supports_smart_motion_detection = lambda: False
    assert "privacy" not in await _added_for(without)

    with_cover = _Coordinator()
    with_cover.supports_smart_motion_detection = lambda: False
    with_cover.states["has_privacy_mode"] = True
    assert "privacy" in await _added_for(with_cover)


async def test_the_alarm_output_is_only_offered_where_there_is_a_relay():
    without = _Coordinator()
    without.supports_smart_motion_detection = lambda: False
    assert "alarm_output" not in await _added_for(without)

    with_relay = _Coordinator()
    with_relay.supports_smart_motion_detection = lambda: False
    with_relay.states["has_alarm_output"] = True
    assert "alarm_output" in await _added_for(with_relay)


async def test_a_box_with_six_relays_gets_six_switches():
    """It used to get none: the multi-output `getOutState` encoding was
    unverified, so a recorder with relays was refused rather than given a
    switch that might report one as open while it is closed. It is a bitmask,
    measured, so they are offered now."""
    c = _Coordinator()
    c.supports_smart_motion_detection = lambda: False
    c.states["has_alarm_output"] = True
    c.states["alarm_output_slots"] = 6

    built = await _added_for(c)

    assert len([name for name in built if name == "named_alarm_output"]) == 6
    assert "alarm_output" not in built, (
        "six relays all called the same translated name is why the named "
        "variant exists"
    )


async def test_smart_motion_is_offered_for_either_flavour():
    """Dahua's own and Amcrest's are different APIs behind one switch, and a device needs
    only one of them for the switch to be worth having."""
    neither = _Coordinator()
    neither.supports_smart_motion_detection = lambda: False
    assert "smart" not in await _added_for(neither)

    dahua = _Coordinator()
    dahua.supports_smart_motion_detection = lambda: True
    assert "smart" in await _added_for(dahua)

    amcrest = _Coordinator(amcrest=True)
    amcrest.supports_smart_motion_detection = lambda: False
    assert "smart" in await _added_for(amcrest)


async def _switches_for(coordinator):
    from types import SimpleNamespace
    from unittest.mock import patch

    from custom_components.dahua import switch as switch_module

    coordinator.supports_siren = lambda: False
    coordinator.supports_smart_motion_detection = lambda: False
    coordinator.supports_disarming_linkage = lambda: False
    entry = SimpleNamespace(
        entry_id="e1",
        options={},
        runtime_data={0: coordinator},
        # Setup keeps a listener for capabilities that turn up on a later poll.
        async_on_unload=lambda unsub: None,
    )
    added = []
    with patch.multiple(
        switch_module,
        DahuaMotionDetectionBinarySwitch=lambda *a, **k: "motion",
    ):
        await switch_module.async_setup_entry(
            SimpleNamespace(data={}), entry, adds_entities(added)
        )
    return added


async def test_an_indoor_monitor_without_a_camera_gets_no_motion_detection_switch():
    """A VTH2421F-P has no camera: there is no picture to detect motion in."""
    coordinator = _Coordinator()
    coordinator.states["indoor_monitor_without_video"] = True

    assert "motion" not in await _switches_for(coordinator)


async def test_a_camera_still_gets_one():
    assert "motion" in await _switches_for(_Coordinator())


# --- naming several relays ----------------------------------------------------


def _named(slots, names, output):
    """The real coordinator's naming rule, with only what it reads set."""
    from custom_components.dahua import DahuaDataUpdateCoordinator

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._alarm_output_slots = slots
    coordinator._alarm_output_names = names
    return coordinator.get_alarm_output_name(output)


def test_a_relay_name_shared_with_another_is_not_used():
    """Every one of the six on the recorder measured is called "Beep" out of
    the factory. That is the default for all of them rather than a description
    of any one, and six entities named "Beep" are worse than six numbered."""
    beeps = {n: "Beep" for n in range(6)}

    assert _named(6, beeps, 0) == "Alarm output 1"
    assert _named(6, beeps, 5) == "Alarm output 6"


def test_a_relay_somebody_renamed_keeps_its_name():
    names = {0: "Beep", 1: "Beep", 2: "Gate", 3: "Beep", 4: "Beep", 5: "Beep"}

    assert _named(6, names, 2) == "Gate"


def test_its_still_default_neighbours_stay_numbered():
    """The rule is per relay, not per device: one renamed relay must not stop
    the other five being told apart."""
    names = {0: "Beep", 1: "Beep", 2: "Gate", 3: "Beep", 4: "Beep", 5: "Beep"}

    assert _named(6, names, 0) == "Alarm output 1"
    assert _named(6, names, 3) == "Alarm output 4"


def test_the_number_counts_from_one():
    """The device's own interface numbers its ports from one while the config
    array is indexed from zero, the same off-by-one as the alarm inputs."""
    assert _named(6, {}, 0) == "Alarm output 1"
    assert _named(6, {}, 5) == "Alarm output 6"


def test_a_relay_the_device_did_not_name_is_numbered():
    assert _named(6, {0: ""}, 0) == "Alarm output 1"
    assert _named(6, {}, 2) == "Alarm output 3"


def test_a_single_relay_is_still_translated_not_named():
    """A device with one relay keeps the name it has always had, in every
    language it has always had it in. That is the whole reason there are two
    classes rather than a branch."""
    c = _Coordinator()
    c.states["alarm_output_slots"] = 1
    single = _switch(DahuaAlarmOutputSwitch, c)

    assert single.translation_key == "alarm_output"
    assert getattr(single, "_attr_name", None) is None
