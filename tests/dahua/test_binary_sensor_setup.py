"""Which binary sensors an entry creates.

`async_setup_entry` decides this and had no tests, which is how the README came to
describe the Authorized Vehicle sensor as "only created on cameras that report ANPR". It
is not: it is appended unconditionally, for every channel of every entry. That claim was
mine, in #861, and it is corrected in the same commit as these tests.

The distinction matters to somebody reading their entity list. The sensor existing says
nothing about whether the camera can do ANPR. What decides whether it ever turns on is
two things that are not this: the camera reporting a plate at all, and that plate being
on the authorized list.

`DahuaEventSensor` and `DahuaAuthorizedVehicleBinarySensor` are replaced by recorders
here, so these tests are about what setup decides to create, not about what the entities
then do. Their own behaviour is covered in test_binary_sensor.py.
"""

import pytest

from custom_components.dahua import binary_sensor as bs

from . import adds_entities


class _Coordinator:
    # The platforms file each channel's entities under its own subentry, so they
    # read this on every entity they add. None is a single camera, and is what
    # `async_add_entities` wants for an entry that has no subentries.
    subentry_id = None

    def __init__(
        self,
        events=(),
        doorbell=False,
        channel=0,
        no_video=False,
        disks=(),
        serial="SERIAL1",
        host_device=False,
        alarm_inputs=(),
    ):
        self._events = list(events)
        self._doorbell = doorbell
        self._channel = channel
        self._no_video = no_video
        self._disks = [{"name": name} for name in disks]
        self._serial = serial
        self._host_device = host_device
        self._alarm_inputs = list(alarm_inputs)

    def get_event_list(self):
        return self._events

    def get_ivs_rules(self):
        return []

    def get_storage_disks(self):
        return list(self._disks)

    def supports_alarm_inputs(self):
        return bool(self._alarm_inputs)

    def get_alarm_inputs(self):
        return list(self._alarm_inputs)

    def get_device_serial_number(self):
        """The serial without the channel suffix. What a disk is keyed on."""
        return self._serial

    def is_doorbell(self):
        return self._doorbell

    def is_indoor_monitor_without_video(self):
        return self._no_video

    def get_channel(self):
        return self._channel

    def uses_separate_host_device(self):
        """Whether the box has a device apart from its channels.

        False by default so that every test above this one describes what it
        always described: a recorder whose class was never answered keeps the
        old arrangement, with its disks on the lowest channel.
        """
        return self._host_device


class _Entry:
    def __init__(self, coordinators):
        self.runtime_data = {i: c for i, c in enumerate(coordinators)}
        self.entry_id = "entry"


@pytest.fixture
def setup(monkeypatch):
    """Run the real async_setup_entry with the entities recorded."""
    built = []

    class _Event:
        def __init__(self, coordinator, entry, event_name):
            built.append(("event", event_name, coordinator))

    class _Vehicle:
        def __init__(self, coordinator, entry):
            built.append(("vehicle", None, coordinator))

    class _Disk:
        def __init__(self, coordinator, entry, disk_name):
            built.append(("disk", disk_name, coordinator))

    class _AlarmInput:
        def __init__(self, coordinator, entry, alarm_input):
            built.append(("alarm_input", alarm_input["index"], coordinator))

    monkeypatch.setattr(bs, "DahuaEventSensor", _Event)
    monkeypatch.setattr(bs, "DahuaAuthorizedVehicleBinarySensor", _Vehicle)
    monkeypatch.setattr(bs, "DahuaDiskProblemBinarySensor", _Disk)
    monkeypatch.setattr(bs, "DahuaAlarmInputBinarySensor", _AlarmInput)

    async def run(*coordinators):
        added = []
        await bs.async_setup_entry(None, _Entry(coordinators), adds_entities(added))
        return added

    run.built = built
    return run


def _names(built, kind="event"):
    return [name for k, name, _ in built if k == kind]


# --- the claim the README got wrong -------------------------------------------


async def test_every_camera_gets_an_authorized_vehicle_sensor(setup):
    """Unconditional. Not gated on ANPR, not gated on plates being configured."""
    await setup(_Coordinator())

    assert _names(setup.built, "vehicle") == [
        None
    ], "expected exactly one authorized vehicle sensor"


async def test_every_channel_of_a_recorder_gets_one_too(setup):
    """An eleven channel recorder gets eleven of them, which is worth knowing before
    reading an entity list and concluding eleven cameras do ANPR."""
    await setup(*[_Coordinator(channel=i) for i in range(11)])

    assert len(_names(setup.built, "vehicle")) == 11


# --- the disks, which are the device's and not the channel's -------------------


async def test_a_recorders_disks_are_created_once(setup):
    """`storageDevice.cgi` takes no channel, so every coordinator of a recorder
    answers with the same disks. Creating one sensor each gave a twelve channel
    recorder with four bays forty eight entities for four disks, filed under
    devices named after cameras."""
    disks = ("/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/sdd")
    await setup(*[_Coordinator(channel=i, disks=disks) for i in range(12)])

    created = [(name, c.get_channel()) for k, name, c in setup.built if k == "disk"]

    assert len(created) == 4, created
    assert sorted(name for name, _ in created) == sorted(disks)


async def test_the_lowest_channel_carries_them(setup):
    disks = ("/dev/sda",)
    await setup(*[_Coordinator(channel=i, disks=disks) for i in (3, 0, 7)])

    carriers = {c.get_channel() for k, _n, c in setup.built if k == "disk"}

    assert carriers == {0}


async def test_a_recorder_without_channel_zero_still_gets_them(setup):
    """The lowest channel present, not channel 0 specifically. The ids do not
    depend on which one it is, because they are keyed on the device serial."""
    await setup(*[_Coordinator(channel=i, disks=("/dev/sda",)) for i in (5, 9)])

    carriers = {c.get_channel() for k, _n, c in setup.built if k == "disk"}

    assert carriers == {5}


class _DiskMarker:
    """Stands in for a disk sensor, and is recognisable in the add call."""

    def __init__(self, *args, **kwargs):
        pass


async def _disks_filed_under(coordinator):
    """Which subentry the *disk* sensors were handed to.

    Setup makes more than one add call: the disks once for the device, and then
    whatever the per-channel loop builds, which is correctly filed under the
    channel. Watching every call conflates the two and reads as a failure when
    the behaviour is right, so this answers only for the call carrying disks.

    `adds_entities` cannot stand in here: it accepts `config_subentry_id` and
    throws it away, which is right for every other test in this file.
    """
    seen = []

    def add(new_entities, update_before_add=False, *, config_subentry_id=None):
        if any(isinstance(entity, _DiskMarker) for entity in new_entities):
            seen.append(config_subentry_id)

    await bs.async_setup_entry(None, _Entry([coordinator]), add)
    return seen


def _stub_the_entities(monkeypatch):
    """Every class this setup builds, not just the one under test.

    The per-channel loop runs too, and the real
    DahuaAuthorizedVehicleBinarySensor reads `get_serial_number` off the
    coordinator in its constructor. Stubbing only the disk sensor left that one
    real, which is an AttributeError about a method these tests are not about.
    """
    monkeypatch.setattr(bs, "DahuaDiskProblemBinarySensor", _DiskMarker)
    for name in ("DahuaAuthorizedVehicleBinarySensor", "DahuaEventSensor"):
        monkeypatch.setattr(bs, name, lambda *a, **k: object())


async def test_the_recorders_disks_are_filed_under_the_entry(monkeypatch):
    """Not under a channel's subentry.

    A subentry is a channel. Filing the recorder's disks under one would mean
    that removing that channel took the recorder's disks with it, and the
    recorder still has them.
    """
    _stub_the_entities(monkeypatch)

    recorder = _Coordinator(disks=("sda", "sdb"), host_device=True)
    recorder.subentry_id = "channel-0"

    assert await _disks_filed_under(recorder) == [None]


async def test_a_camera_with_an_sd_card_keeps_its_disk_on_its_own_channel(monkeypatch):
    """There is no separate device to file it against, so nothing moves."""
    _stub_the_entities(monkeypatch)

    camera = _Coordinator(disks=("sda",), host_device=False)
    camera.subentry_id = "channel-0"

    assert await _disks_filed_under(camera) == ["channel-0"]


async def test_a_camera_gets_no_disk_sensors(setup):
    """Which is every device that is not a recorder: the read is gated on
    is_recorder_host(), so get_storage_disks() is empty."""
    await setup(_Coordinator())

    assert [k for k, _n, _c in setup.built if k == "disk"] == []


async def test_a_channel_that_reports_no_disks_is_not_chosen(setup):
    """Channels of one recorder share the answer, so in practice they all report
    the same disks. The sensors must still come from a coordinator that has
    them, or they would describe nothing."""
    await setup(
        _Coordinator(channel=0),
        _Coordinator(channel=1, disks=("/dev/sda",)),
    )

    carriers = {c.get_channel() for k, _n, c in setup.built if k == "disk"}

    assert carriers == {1}


# --- the event sensors --------------------------------------------------------


async def test_one_sensor_per_selected_event(setup):
    await setup(_Coordinator(events=["VideoMotion", "CrossLineDetection"]))

    assert _names(setup.built) == ["VideoMotion", "CrossLineDetection"]


async def test_selecting_no_events_still_leaves_the_vehicle_sensor(setup):
    """An empty selection is honoured and turns the event connection off for that
    channel, but the entry is still set up."""
    added = await setup(_Coordinator(events=[]))

    assert _names(setup.built) == []
    assert _names(setup.built, "vehicle") == [None]
    assert added, "nothing was added at all"


async def test_a_doorbell_gets_its_four_extra_sensors(setup):
    """Added without being selected, because most people adding a doorbell want them."""
    await setup(_Coordinator(events=["VideoMotion"], doorbell=True))

    assert _names(setup.built) == [
        "VideoMotion",
        "DoorbellPressed",
        "Invite",
        "DoorStatus",
        "CallNoAnswered",
    ]


async def test_a_camera_gets_none_of_the_doorbell_sensors(setup):
    """They would sit at off for ever on something with no button."""
    await setup(_Coordinator(events=["VideoMotion"], doorbell=False))

    assert _names(setup.built) == ["VideoMotion"]


async def test_a_doorbell_event_already_selected_is_built_twice(setup):
    """A doorbell whose event list already contains DoorbellPressed has it appended
    again, because the four are added without checking. Both carry the same unique id,
    which is derived from the event name, so only one entity can result.

    Pinned as the current behaviour rather than as a bug to fix: what Home Assistant
    does with two entities sharing a unique id is its business, and changing the
    construction is a judgement about somebody's existing entities.
    """
    await setup(_Coordinator(events=["DoorbellPressed"], doorbell=True))

    assert _names(setup.built).count("DoorbellPressed") == 2


# --- the loop over an entry's channels ----------------------------------------


async def test_each_channel_gets_its_own_sensors(setup):
    await setup(
        _Coordinator(events=["VideoMotion"], channel=0),
        _Coordinator(events=["AlarmLocal"], channel=1),
    )

    assert _names(setup.built) == ["VideoMotion", "AlarmLocal"]
    assert len(_names(setup.built, "vehicle")) == 2


async def test_an_entry_with_nothing_set_up_adds_nothing(setup):
    """`entry_coordinators` returns an empty mapping for a setup that failed or an entry
    being torn down, and this platform must not raise on it."""
    added = []

    await bs.async_setup_entry(None, _Entry([]), adds_entities(added))

    assert added == []
    assert setup.built == []


# --- an indoor monitor without a camera ---------------------------------------
#
# Its event list comes back empty from the coordinator (see
# test_vth_without_video.py); here, the vehicle sensor every camera gets.


async def test_an_indoor_monitor_without_a_camera_gets_no_vehicle_sensor(setup):
    """The one exception to "every camera gets one": a plate is read from a picture,
    and a VTH that says it has no camera has none."""
    await setup(_Coordinator(no_video=True))

    assert _names(setup.built, "vehicle") == []


async def test_a_camera_next_to_it_still_gets_one(setup):
    camera = _Coordinator(channel=1)
    await setup(_Coordinator(no_video=True), camera)

    assert [c for k, _, c in setup.built if k == "vehicle"] == [camera]


# --- the alarm terminals, which are the box's and not the channel's -----------

TERMINALS = (
    {"index": 0, "name": "Alarm-in Port1", "enabled": True},
    {"index": 3, "name": "Alarm-in Port4", "enabled": True},
)


async def test_a_recorders_alarm_terminals_are_created_once(setup):
    """`Alarm` and `alarm.cgi` take no channel, so every coordinator of a
    recorder that probed them answers with the same terminals. One set each
    would give a twelve channel recorder with sixteen terminals a hundred and
    ninety two entities for sixteen screw terminals (#1090)."""
    await setup(*[_Coordinator(channel=i, alarm_inputs=TERMINALS) for i in range(12)])

    created = [index for k, index, _c in setup.built if k == "alarm_input"]

    assert created == [0, 3], created


async def test_the_lowest_channel_carries_the_terminals(setup):
    """The same rule host.py routes with. The two have to agree: an AlarmLocal
    handed to a coordinator with no sensors on it updates nothing."""
    await setup(*[_Coordinator(channel=i, alarm_inputs=TERMINALS) for i in (5, 1, 9)])

    carriers = {c.get_channel() for k, _i, c in setup.built if k == "alarm_input"}

    assert carriers == {1}


async def test_a_camera_gets_no_alarm_terminals(setup):
    """Nothing but a recorder that answered the table gets these."""
    await setup(_Coordinator())

    assert [k for k, _i, _c in setup.built if k == "alarm_input"] == []


class _TerminalMarker:
    def __init__(self, coordinator, entry, alarm_input):
        self.index = alarm_input["index"]


async def _terminals_filed_under(coordinator):
    """Which subentry the alarm terminal sensors were handed to.

    Setup makes more than one add call, so this answers only for the one
    carrying terminals, the same reason `_disks_filed_under` exists.
    """
    seen = []

    def add(new_entities, update_before_add=False, *, config_subentry_id=None):
        if any(isinstance(entity, _TerminalMarker) for entity in new_entities):
            seen.append(config_subentry_id)

    await bs.async_setup_entry(None, _Entry([coordinator]), add)
    return seen


def _stub_for_terminals(monkeypatch):
    monkeypatch.setattr(bs, "DahuaAlarmInputBinarySensor", _TerminalMarker)
    for name in (
        "DahuaAuthorizedVehicleBinarySensor",
        "DahuaEventSensor",
        "DahuaDiskProblemBinarySensor",
    ):
        monkeypatch.setattr(bs, name, lambda *a, **k: object())


async def test_the_terminals_are_filed_under_the_entry_not_a_camera(monkeypatch):
    """A screw terminal on the back panel is not channel 0's screw terminal.

    Filing it under a channel's subentry would mean removing that camera took
    the recorder's alarm inputs with it, and the recorder still has them.
    """
    _stub_for_terminals(monkeypatch)

    recorder = _Coordinator(alarm_inputs=TERMINALS, host_device=True)
    recorder.subentry_id = "channel-0"

    assert await _terminals_filed_under(recorder) == [None]


async def test_a_recorder_with_no_host_device_keeps_them_on_its_channel(monkeypatch):
    """A recorder whose class was never answered has nowhere else to put them."""
    _stub_for_terminals(monkeypatch)

    recorder = _Coordinator(alarm_inputs=TERMINALS, host_device=False)
    recorder.subentry_id = "channel-0"

    assert await _terminals_filed_under(recorder) == ["channel-0"]


async def test_a_recorders_channels_lose_their_alarm_local_sensors(setup):
    """Superseded by the terminal sensors, so not created at all.

    On a multi-channel recorder these never showed their own channel's
    state: the only thing that ever fed them was the terminal number being
    read as a channel. Leaving them to sit at off for ever is worse than
    removing them, because Home Assistant marks an entity a platform has
    stopped providing as unavailable and offers to delete it, which somebody
    notices, where a stale "off" is indistinguishable from a quiet alarm.
    """
    await setup(
        *[
            _Coordinator(
                channel=i, events=["VideoMotion", "AlarmLocal"], alarm_inputs=TERMINALS
            )
            for i in range(3)
        ]
    )

    assert "AlarmLocal" not in _names(setup.built)
    assert (
        _names(setup.built).count("VideoMotion") == 3
    ), "only the superseded code is dropped, not the channel's other sensors"


async def test_a_camera_keeps_its_alarm_local_sensor(setup):
    """A camera's own alarm terminal has nowhere else to go, and #231's
    single-channel branch still delivers it. Taking this away would remove a
    working sensor from every doorbell and camera that has one."""
    await setup(_Coordinator(events=["AlarmLocal"]))

    assert "AlarmLocal" in _names(setup.built)


async def test_a_recorder_with_no_terminals_keeps_them_too():
    """Nothing supersedes them there, so nothing is removed. A recorder whose
    probe found no terminals must not quietly lose a sensor."""
    import pytest as _pytest  # noqa: F401

    coordinators = [_Coordinator(channel=i, events=["AlarmLocal"]) for i in range(3)]
    built = []

    class _Event:
        def __init__(self, coordinator, entry, event_name):
            built.append(event_name)

    import custom_components.dahua.binary_sensor as _bs
    from unittest.mock import patch

    with patch.multiple(
        _bs,
        DahuaEventSensor=_Event,
        DahuaAuthorizedVehicleBinarySensor=lambda *a, **k: object(),
        DahuaDiskProblemBinarySensor=lambda *a, **k: object(),
        DahuaAlarmInputBinarySensor=lambda *a, **k: object(),
    ):
        await _bs.async_setup_entry(None, _Entry(coordinators), adds_entities([]))

    assert built.count("AlarmLocal") == 3
