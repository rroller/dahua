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

    def __init__(self, events=(), doorbell=False, channel=0, no_video=False):
        self._events = list(events)
        self._doorbell = doorbell
        self._channel = channel
        self._no_video = no_video

    def get_event_list(self):
        return self._events

    def get_storage_disks(self):
        return []

    def is_doorbell(self):
        return self._doorbell

    def is_indoor_monitor_without_video(self):
        return self._no_video

    def get_channel(self):
        return self._channel


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

    monkeypatch.setattr(bs, "DahuaEventSensor", _Event)
    monkeypatch.setattr(bs, "DahuaAuthorizedVehicleBinarySensor", _Vehicle)

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
