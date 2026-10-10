"""What setup asks a device, and what it concludes when the device refuses.

The one-time initialisation asks a camera thirteen separate questions about what it can
do, each in its own handler, each turning one capability on or off. That block is the
largest single piece of the integration and almost none of it was executed.

The property that matters is the same for all thirteen: **a refusal is an answer, not a
failure.** A camera without a siren, without PTZ, without an illuminator is a perfectly
good camera, and a probe that took setup down with it would make one missing feature into
no integration at all. #854 was exactly that, reported by three people: an RPC2 refusal of
the coaxial status stopped their AD410s ever finishing setup.

So each probe is driven twice over, once refusing and once not, and the refusal case
asserts three things: setup finished, that capability is off, and **the other capabilities
are untouched**. The last is the one a per-probe test would miss. These share a try block
and a sequence, so a handler that swallows too much turns one refusal into several
features quietly disappearing.

The client and coordinator surfaces the block touches were taken from it by walking its
AST rather than by reading it, so nothing it asks for is missing from the fake by
oversight.
"""

import pytest
from aiohttp import ClientConnectionError, ClientResponseError
from types import SimpleNamespace

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.const import UPDATE

# Every question setup asks, and the capability it decides. Taken from the source:
# each of these is one try block whose handler sets exactly this attribute.
PROBES = [
    ("async_get_coaxial_control_io_status", "_supports_coaxial_control", None),
    ("async_get_disarming_linkage", "_supports_disarming_linkage", None),
    ("async_get_event_notifications", "_supports_event_notifications", None),
    ("async_get_ptz_position", "_supports_ptz_position", None),
    ("async_get_smart_motion_detection", "_supports_smart_motion_detection", None),
    ("async_get_video_in_options", "_supports_day_night_color", None),
    ("async_get_lighting_v2", "_supports_lighting_v2", None),
    # Gated on a model name whitelist, which is the bug class #570, #676 and
    # #690 were all instances of. Named here so the probe actually runs.
    (
        "async_get_lighting_scheme",
        "_supports_lighting_scheme_illuminator",
        "IPC-COLOR4M-TZ",
    ),
    ("async_get_privacy_mode", "_supports_privacy_mode", None),
    ("async_get_cloud_upgrade_info", "_supports_cloud_upgrade", None),
    # The picture adjustments (#1006). This one was added without a probe at
    # all: the read sat in the poll's fan-out gated on nothing but the platform,
    # so a device that refuses the table failed every refresh. Listed here so it
    # inherits the three properties this file is about, which is exactly what it
    # did not have.
    ("async_get_video_color", "_video_color_fields", None),
]

# The probes whose capability is a set of field names rather than a bool, so
# "off" for them is an empty set. Only the picture adjustments: a device can
# serve three of the four, and a single yes could not say which.
PROBE_SETS = {"_video_color_fields"}


# What setup asks the coordinator about itself. Off, so the fan-out afterwards is
# empty and each test is about the probe it names.
SELF_METHODS = (
    "is_doorbell",
    "is_flood_light",
    "is_nvr_channel",
    "supports_floodlightmode",
    "supports_infrared_light",
    "uses_rpc2_deterrence",
)

FLAGS = (
    "_supports_cloud_upgrade",
    "_supports_coaxial_control",
    "_supports_day_night_color",
    "_supports_disarming_linkage",
    "_supports_event_notifications",
    "_supports_floodlightmode",
    "_supports_lighting",
    "_supports_lighting_scheme_illuminator",
    "_supports_lighting_v2",
    "_supports_privacy_mode",
    "_supports_profile_mode",
    "_supports_ptz_position",
    "_supports_smart_motion_detection",
    "_video_color_fields",
)


def _refused():
    """What a device that does not serve an endpoint answers with.

    An HTTP error response, which is what the handlers catch: they are typed
    `PROBE_REFUSED = (ClientResponseError, TimeoutError)` and
    `PROBE_FAILED = (ClientError, TimeoutError)`, not bare `Exception`. Raising
    something else here would escape to the outer handler and make every one of
    these report that a refusal stops setup, which is not what the code does.
    """
    return ClientResponseError(
        request_info=SimpleNamespace(real_url="http://10.0.0.5/cgi-bin/x.cgi"),
        history=(),
        status=400,
    )


class _Client:
    """Answers anything the initialisation asks, and refuses one thing.

    Generic rather than a list of methods, because a scan for `self.client.x()`
    call sites misses attributes that are read rather than called and methods
    reached any other way. Two CI runs went on finding one more of those, and the
    list was never going to be provably complete.

    Every name is still checked against the real `DahuaClient`, so a fake that
    answers something the client does not have fails here rather than passing.
    """

    use_rpc2 = False
    device_key = "10.0.0.5:80"

    # The few answers that have to be a particular shape rather than empty.
    SHAPED = {
        "get_max_extra_streams": 1,
        "get_software_version": {"version": "1.0"},
    }

    def __init__(self, refusing=None, model="IPC-HFW1234"):
        self._refusing = refusing
        self.asked = []
        self.SHAPED = dict(self.SHAPED, **{"get_device_type": {"type": model}})

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        assert hasattr(DahuaClient, name), (
            "setup asked the client for %s, which DahuaClient does not have" % name
        )

        async def call(*args, **kwargs):
            self.asked.append(name)
            if name == self._refusing:
                raise _refused()
            return self.SHAPED.get(name, {})

        return call


def _coordinator(hass, refusing=None, model="IPC-HFW1234"):
    """A coordinator about to run its one-time initialisation.

    `refusing` names the one client call that fails. Everything else answers
    emptily, which is what a device that serves an endpoint and has nothing to
    report looks like.
    """
    client = _Client(refusing, model)

    c = object.__new__(DahuaDataUpdateCoordinator)
    c.hass = hass
    c.client = client
    c.initialized = False
    c._address = "10.0.0.5"
    c._channel = 0
    c._channel_number = 1
    c._serial_number = "SERIAL1"
    c._firmware_version = ""
    c._device_class = ""
    c._channel_model = None
    c._max_streams = 1
    c._alarm_output_slots = 0
    c._ivs_rules = []
    c._profile_mode = "0"
    c._preset_position = "0"
    c._camera_reboot_generation = 0
    c.machine_name = ""
    c.model = ""
    c.config_entry = SimpleNamespace(entry_id="e1")
    for flag in FLAGS:
        setattr(c, flag, False)
    for name in SELF_METHODS:
        setattr(c, name, lambda *a, **k: False)
    # True, or the probes gated on it never run and every assertion about them
    # passes because nothing happened. The coaxial one is gated this way and was
    # doing exactly that until a test asserted it had been asked.
    c._wanted_by = lambda *a, **k: True
    c.channel_option = lambda key, default=None: default
    c.get_coaxial_status_channel = lambda: 0
    c.get_rpc2_coaxial_status_channel = lambda: 1
    c.read_profile_mode = lambda data: "0"
    c._note_probe_refusal = lambda *a, **k: None
    c._back_off_poll_interval = lambda n: None
    c._restore_poll_interval = lambda: None

    async def _nothing(*a, **k):
        return None

    c.async_start_event_listener = _nothing
    c.async_start_vto_event_listener = _nothing
    c.async_detect_lighting_support = _nothing
    c._async_probe_direct_deterrence = _nothing
    c._async_coaxial_status = _nothing
    c._async_fetch_privacy_mode = _nothing
    return c


# --- a device that answers everything ---------------------------------------


async def test_setup_finishes(hass):
    """The control. Every assertion below is about a run that completed, so a
    setup that fell over for its own reasons would make all of them vacuous."""
    coordinator = _coordinator(hass)

    await coordinator._async_update_data()

    assert coordinator.initialized is True


async def test_setup_only_runs_its_probes_once(hass):
    """`initialized` is what stops the whole block running on every poll. Asking a
    camera thirteen capability questions every thirty seconds is most of a poll
    budget spent on answers that do not change."""
    coordinator = _coordinator(hass)
    await coordinator._async_update_data()

    asked = []

    async def _record(*args, **kwargs):
        asked.append(1)
        return {}

    coordinator.client.async_get_privacy_mode = _record

    await coordinator._async_update_data()

    assert asked == [], "the capability probes ran again on the second poll"


async def test_the_cloud_upgrade_probe_is_not_asked_without_the_update_platform(hass):
    """The record feeds only the informational update entity, so setup must not
    spend a read on a device that will never show one. The poll half of the same
    gate is pinned in test_poll_skips_unused.py; this is the setup half."""
    coordinator = _coordinator(hass)
    coordinator._wanted_by = lambda *platforms: UPDATE not in platforms

    await coordinator._async_update_data()

    assert "async_get_cloud_upgrade_info" not in coordinator.client.asked


# --- a capability that worked and then stopped -------------------------------


async def test_a_ptz_read_that_starts_failing_keeps_the_last_preset(hass):
    """The preset select reads `status.PresetID`, and a camera that refuses the read on
    one poll should not reset the select to "0".

    This is the second poll, not the probe: the probe decides whether the camera has PTZ
    at all, and this is what happens afterwards when a camera that said yes stops
    answering. Some firmware drops this API intermittently, and dropping the value with
    it would make the select flicker between the real preset and nothing.
    """
    coordinator = _coordinator(hass)
    await coordinator._async_update_data()
    coordinator.data = {"status.PresetID": "3"}

    coordinator.client._refusing = "async_get_ptz_position"
    result = await coordinator._async_update_data()

    assert (
        result.get("status.PresetID") == "3"
    ), "a refused read dropped the preset the select is showing"


async def test_a_ptz_read_that_fails_with_nothing_remembered_says_nothing(hass):
    """With no previous value there is nothing to carry, and inventing one would make
    the select claim a preset the camera never reported."""
    coordinator = _coordinator(hass)
    await coordinator._async_update_data()
    coordinator.data = {}

    coordinator.client._refusing = "async_get_ptz_position"
    result = await coordinator._async_update_data()

    assert "status.PresetID" not in result, result.get("status.PresetID")


async def test_a_refused_ptz_read_does_not_fail_the_poll(hass):
    """The whole point. This runs inside the fan-out that gathers every per-poll read,
    so an exception here would take the rest of the poll with it and leave every entity
    on the channel unavailable over one optional API."""
    coordinator = _coordinator(hass)
    await coordinator._async_update_data()

    coordinator.client._refusing = "async_get_ptz_position"
    result = await coordinator._async_update_data()

    assert isinstance(result, dict), "the poll did not complete"


# --- and one that refuses ---------------------------------------------------


@pytest.mark.parametrize("method, flag, model", PROBES)
async def test_every_probe_is_actually_asked(hass, method, flag, model):
    """The guard against the rest of this file passing for the wrong reason.

    Several probes are gated on `_wanted_by`, and with that answering no they
    never run at all: setup finishes, the capability is off, and every assertion
    below holds without the code under test having been reached. That is what was
    happening to the coaxial probe until this test existed.
    """
    coordinator = _coordinator(hass, model=model or "IPC-HFW1234")

    await coordinator._async_update_data()

    assert method in coordinator.client.asked, (
        "%s was never asked, so the tests about it prove nothing" % method
    )


@pytest.mark.parametrize("method, flag, model", PROBES)
async def test_a_refused_probe_does_not_stop_setup(hass, method, flag, model):
    """#854, generalised. Three people had AD410s that never finished setup
    because one RPC2 call was refused, and every one of these is the same shape."""
    coordinator = _coordinator(hass, refusing=method, model=model or "IPC-HFW1234")

    await coordinator._async_update_data()

    assert coordinator.initialized is True, (
        "a refused %s stopped the device finishing setup" % method
    )


@pytest.mark.parametrize("method, flag, model", PROBES)
async def test_a_refused_probe_turns_its_own_capability_off(hass, method, flag, model):
    coordinator = _coordinator(hass, refusing=method, model=model or "IPC-HFW1234")

    await coordinator._async_update_data()

    got = getattr(coordinator, flag)
    # `is False` for the bools rather than `== False`, which would also accept a
    # 0 or an empty string from a handler that had quietly started storing
    # something else. The picture adjustments are the one capability that is a
    # set of field names, so theirs is compared as an empty set.
    if flag in PROBE_SETS:
        assert got == frozenset()
    else:
        assert got is False


@pytest.mark.parametrize("method, flag, model", PROBES)
async def test_a_refused_probe_leaves_the_others_alone(hass, method, flag, model):
    """The one a per-probe test would miss. They share a sequence, so a handler
    that reaches too far turns one refusal into several features disappearing, and
    nothing about that is visible in a log.
    """
    answering = _coordinator(hass, model=model or "IPC-HFW1234")
    await answering._async_update_data()
    expected = {name: getattr(answering, name) for name in FLAGS if name != flag}

    refusing = _coordinator(hass, refusing=method, model=model or "IPC-HFW1234")
    await refusing._async_update_data()

    got = {name: getattr(refusing, name) for name in FLAGS if name != flag}
    assert got == expected, "refusing %s also changed another capability" % method
