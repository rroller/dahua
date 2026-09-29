"""Which camera entities an entry creates, and how often its services are registered.

`async_setup_entry` is the largest block in camera.py and had nothing on it. What it
contains is two decisions:

- how many camera entities a channel gets, and under what names and unique-id
  suffixes. One model, the SDT4E425, is a single config entry carrying two physical
  sensors, so it gets a set of streams per sensor rather than one set. That is a
  capability gated on a model string, which in this integration is a recurring source
  of bugs (#570, #676, #690), so the branch is worth pinning on both sides.
- registering the entity services once for the platform rather than once per channel.
  The code has a comment saying it "sits outside the loop above" because registering
  the same service name twice raises, and an NVR walks that loop once per channel. A
  comment explaining a past failure is exactly the thing to hold in place with a test.

`DahuaCamera` is replaced by a recorder here rather than constructed for real. That is
deliberate and it limits what these can claim: they check the arguments
`async_setup_entry` decides to pass, not what the entity then does with them. The
entity's own derivation of names and unique ids is covered elsewhere -- notably
test_entity_identity.py. Building real ones would need the full Home Assistant entity
machinery and would say nothing more about the decisions made here.
"""

import pytest

from custom_components.dahua import camera as camera_module


class _Client:
    @staticmethod
    def to_stream_name(subtype):
        """The real mapping, which the unique-id suffixes are built from."""
        if subtype == 0:
            return "Main"
        if subtype == 1:
            return "Sub"
        return "Sub_{0}".format(subtype)

    @staticmethod
    def get_rtsp_stream_url(channel, subtype):
        return "rtsp://10.0.0.1:554/ch%s/%s" % (channel, subtype)


class _Coordinator:
    def __init__(self, model="IPC-HDW1234", max_streams=2, channel=0, number=1):
        self.client = _Client()
        self._model = model
        self._max_streams = max_streams
        self._channel = channel
        self._number = number

    def get_model(self):
        return self._model

    def get_max_streams(self):
        return self._max_streams

    def get_channel(self):
        return self._channel

    def get_channel_number(self):
        return self._number

    def get_serial_number(self):
        return "SERIAL%d" % self._channel

    def channel_option(self, name, default=None):
        return default


class _Entry:
    def __init__(self, coordinators):
        self.runtime_data = coordinators
        self.entry_id = "entry"
        self.data = {}
        self.options = {}


class _Platform:
    """Stands in for the platform async_setup_entry registers services on."""

    def __init__(self):
        self.registered = []

    def async_register_entity_service(self, name, *args, **kwargs):
        self.registered.append(name)


@pytest.fixture
def setup(monkeypatch):
    """Run the real async_setup_entry with the entity and the platform recorded."""
    built = []
    platform = _Platform()

    class _Recorder:
        def __init__(self, coordinator, stream_index, config_entry, **kwargs):
            built.append({
                "coordinator": coordinator,
                "stream_index": stream_index,
                "logical_channel": kwargs.get("logical_channel"),
                "media_channel": kwargs.get("media_channel"),
                "display_name": kwargs.get("display_name"),
                "unique_suffix": kwargs.get("unique_suffix"),
            })

    monkeypatch.setattr(camera_module, "DahuaCamera", _Recorder)
    monkeypatch.setattr(
        camera_module.entity_platform, "async_get_current_platform",
        lambda: platform)

    async def run(*coordinators):
        entry = _Entry({i: c for i, c in enumerate(coordinators)})
        added = []
        await camera_module.async_setup_entry(
            None, entry, lambda entities: added.extend(entities))
        return added

    run.built = built
    run.platform = platform
    return run


# --- an ordinary camera -----------------------------------------------------

async def test_a_camera_gets_one_entity_per_stream(setup):
    await setup(_Coordinator(max_streams=3))

    assert [b["stream_index"] for b in setup.built] == [0, 1, 2]
    # Nothing overrides the naming for an ordinary camera; the entity derives it.
    assert all(b["display_name"] is None for b in setup.built)
    assert all(b["unique_suffix"] is None for b in setup.built)


async def test_a_camera_with_one_stream_gets_one_entity(setup):
    await setup(_Coordinator(max_streams=1))

    assert len(setup.built) == 1
    assert setup.built[0]["stream_index"] == 0


async def test_every_channel_of_a_recorder_gets_its_streams(setup):
    """An NVR entry owns several coordinators, and each is a channel with its own
    cameras."""
    await setup(_Coordinator(max_streams=2, channel=0),
                _Coordinator(max_streams=2, channel=1),
                _Coordinator(max_streams=2, channel=2))

    assert len(setup.built) == 6
    assert sorted({b["coordinator"].get_channel() for b in setup.built}) == [0, 1, 2]


# --- the two-sensor model ---------------------------------------------------

async def test_the_sdt4e425_gets_a_set_of_streams_for_each_of_its_two_sensors(setup):
    """One config entry, two physical sensors. Panorama is media channel 1 and PTZ is
    media channel 2, and each gets every stream."""
    await setup(_Coordinator(model="DH-SDT4E425-4F-GB-A-PV1", max_streams=2))

    assert len(setup.built) == 4
    assert [(b["logical_channel"], b["media_channel"], b["display_name"],
             b["unique_suffix"]) for b in setup.built] == [
        (0, 1, "Panorama", "Main"),
        (0, 1, "Panorama Sub", "Sub"),
        (1, 2, "PTZ", "1_Main"),
        (1, 2, "PTZ Sub", "1_Sub"),
    ]


async def test_the_two_sensors_cannot_collide_on_a_unique_id(setup):
    """Both sensors share one serial number, so the suffix is the only thing keeping
    their entities apart. The PTZ sensor's `1_` prefix is what does it, and without it
    the second sensor's Main stream would claim the first one's entity."""
    await setup(_Coordinator(model="SDT4E425-4F-GB-A-PV1", max_streams=3))

    suffixes = [b["unique_suffix"] for b in setup.built]
    assert len(set(suffixes)) == len(suffixes), suffixes
    assert set(suffixes) == {"Main", "Sub", "Sub_2", "1_Main", "1_Sub", "1_Sub_2"}


async def test_the_model_match_is_not_a_loose_prefix(setup):
    """The recurring bug class here is a capability gated on a model string matching
    more than it should. A different Dahua model that merely starts the same way must
    take the ordinary path, not be given two sensors it does not have."""
    await setup(_Coordinator(model="SDT4E425-OTHER", max_streams=2))

    assert len(setup.built) == 2, "a different model was treated as the two-sensor one"
    assert all(b["media_channel"] is None for b in setup.built)


async def test_a_camera_reporting_no_model_takes_the_ordinary_path(setup):
    """get_model can be None before the first successful poll."""
    await setup(_Coordinator(model=None, max_streams=2))

    assert len(setup.built) == 2
    assert all(b["display_name"] is None for b in setup.built)


# --- the services, registered once ------------------------------------------

async def test_the_entity_services_are_registered(setup):
    await setup(_Coordinator())

    assert setup.platform.registered, "no entity services were registered at all"


async def test_a_recorders_channels_do_not_register_the_services_again(setup):
    """The property the comment in the source exists for: registering the same entity
    service name twice raises, and this loop runs once per channel. Eleven channels
    must still register each service once."""
    await setup(*[_Coordinator(channel=i) for i in range(11)])

    names = setup.platform.registered
    duplicated = sorted({n for n in names if names.count(n) > 1})
    assert duplicated == [], "registered twice, which raises on a real platform: %s" % (
        duplicated,)


async def test_an_entry_with_nothing_set_up_still_registers_its_services(setup):
    """`entry_coordinators` returns an empty mapping for an entry whose setup failed or
    which is being torn down. That must not stop the platform being set up, and must
    not add entities for channels that are not there."""
    entry = _Entry({})
    added = []

    await camera_module.async_setup_entry(
        None, entry, lambda entities: added.extend(entities))

    assert added == []
    assert setup.built == []
    assert setup.platform.registered, "the platform lost its services"
