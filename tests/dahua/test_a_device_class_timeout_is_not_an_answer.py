"""A device that did not answer what it is has not said it is a camera.

`getDeviceClass` is the one question that removes a guess rather than adding
one. `is_recorder_host()` reads it, and through it a recorder whose model string
does not say NVR -- a Lorex N843A8, most OEM rebrands -- is recognised as a
recorder anyway. `is_nvr_channel`'s docstring spells out what rides on it: which
IVS table a channel reads (`RemoteVideoAnalyseRule` or `VideoAnalyseRule`), which
path a deterrence write takes, and whether the disk and configured-channel
sensors exist at all.

The probe caught `PROBE_FAILED`, which is `(ClientError, TimeoutError)`, and
stored `""` either way. That block runs once, so **one slow answer at startup
decided the question for the life of the entry**: a recorder quietly became a
camera, read the wrong IVS table and routed deterrence down the camera path, for
as long as Home Assistant stayed up.

It is the #724 shape. There, a timeout on the channel-numbering probe was read
as a definite no and renumbered a working channel; the fix was to decide nothing
and leave it for the next entry or the next restart. `_note_probe_refusal`'s own
docstring states the rule: an HTTP status is the device answering and
generalises, a timeout is a fact about that moment and generalises to nothing.
This probe recorded the refusal and then cached the non-answer regardless.

So the two are told apart now. A status settles it -- the firmware does not serve
`getDeviceClass`, and asking again cannot change that. No status leaves the
question open and the poll asks again until the device answers something.

**What the retry deliberately does not do is rebuild the entity set.** The disk
sensors are decided once, from `is_recorder_host()`, before platforms are
forwarded. A class arriving on the fourth poll fixes what is re-decided every
cycle and not what exists, which is why the log line asks for a reload. Half the
recovery beats none, and rebuilding platforms from inside a poll is not something
to attempt here.

The harness is `test_what_setup_probes.py`'s, trimmed, with a client that can
fail this one call either way and answer on a later attempt.
"""

from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import DahuaClient

ADDRESS = "10.0.0.5"

DEVICE_CLASS = "async_get_device_class"


def _status_error(status=404):
    """The device answering that it does not serve this action."""
    return ClientResponseError(
        request_info=SimpleNamespace(real_url="http://%s/cgi-bin/x.cgi" % ADDRESS),
        history=(),
        status=status,
    )


class _Client:
    """Answers everything emptily, and fails `getDeviceClass` to order.

    `fail_with` is the exception for each attempt in turn; `None` in the list
    means answer with `answer`. Running off the end answers too, so a test says
    only as much as it needs to.
    """

    use_rpc2 = False
    device_key = "%s:80" % ADDRESS

    SHAPED = {
        "get_max_extra_streams": 1,
        "get_software_version": {"version": "1.0"},
        "get_device_type": {"type": "IPC-HFW1234"},
    }

    def __init__(self, fail_with=(), answer="NVR"):
        self._fail_with = list(fail_with)
        self._answer = answer
        self.class_asks = 0

    async def async_get_device_class(self):
        self.class_asks += 1
        if self._fail_with:
            failure = self._fail_with.pop(0)
            if failure is not None:
                raise failure
        return self._answer

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        assert hasattr(DahuaClient, name), (
            "setup asked the client for %s, which DahuaClient does not have" % name
        )

        async def call(*args, **kwargs):
            return self.SHAPED.get(name, {})

        return call


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
)

# is_recorder_host is deliberately absent: it is the method under test, and the
# whole point is what it reads.
SELF_METHODS = (
    "is_doorbell",
    "is_flood_light",
    "is_nvr_channel",
    "supports_floodlightmode",
    "supports_infrared_light",
    "uses_rpc2_deterrence",
)


def _coordinator(hass, client):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.hass = hass
    c.client = client
    c.initialized = False
    c._address = ADDRESS
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
    c._video_color_fields = frozenset()
    c.machine_name = ""
    c.model = ""
    c.config_entry = SimpleNamespace(entry_id="e1")
    for flag in FLAGS:
        setattr(c, flag, False)
    for name in SELF_METHODS:
        setattr(c, name, lambda *a, **k: False)
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


# --- a device that answers: nothing changes ---------------------------------


async def test_an_answered_class_is_asked_once(hass):
    client = _Client(answer="NVR")
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert coordinator.reported_device_class() == "NVR"
    assert client.class_asks == 1, "it was re-asked after answering"


# --- a device that refuses: settled, and not asked again --------------------


@pytest.mark.parametrize("status", [400, 404, 501])
async def test_a_refusal_is_an_answer_and_is_not_retried(hass, status):
    """A status is the firmware saying it does not serve this action. Asking
    again cannot change its mind, and every poll would pay for the question."""
    client = _Client(fail_with=[_status_error(status)])
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert coordinator.reported_device_class() == ""
    assert client.class_asks == 1, "a refusal was treated as worth retrying"


# --- a device that did not answer: asked again ------------------------------


async def test_a_timeout_is_asked_again(hass):
    client = _Client(fail_with=[TimeoutError()])
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    assert client.class_asks == 1

    await coordinator._async_update_data()

    assert client.class_asks == 2, "a timeout was cached as though it were an answer"


async def test_the_recovered_class_is_used(hass):
    """The point of it. Until the class arrives this recorder is a camera to
    is_recorder_host, which decides the IVS table and the deterrence path."""
    client = _Client(fail_with=[TimeoutError()], answer="NVR")
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    assert coordinator.is_recorder_host() is False, "nothing was known yet"

    await coordinator._async_update_data()

    assert coordinator.reported_device_class() == "NVR"
    assert coordinator.is_recorder_host() is True


async def test_it_keeps_asking_while_the_device_stays_quiet(hass):
    client = _Client(fail_with=[TimeoutError(), TimeoutError(), TimeoutError()])
    coordinator = _coordinator(hass, client)

    for _ in range(3):
        await coordinator._async_update_data()

    assert client.class_asks == 3


async def test_it_stops_asking_once_anything_is_answered(hass):
    """Including an empty answer: a device that replies without a `class` field
    has still answered, and re-asking it every poll for ever would be the cost
    this guards against."""
    client = _Client(fail_with=[TimeoutError()], answer="")
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    await coordinator._async_update_data()
    assert client.class_asks == 2

    await coordinator._async_update_data()

    assert client.class_asks == 2, "an empty answer was not treated as an answer"


async def test_a_refusal_after_a_timeout_also_settles_it(hass):
    """The device came back and said it does not serve this. That is an answer,
    so the retry stops rather than continuing for the life of the entry."""
    client = _Client(fail_with=[TimeoutError(), _status_error(404)])
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    await coordinator._async_update_data()
    assert client.class_asks == 2

    await coordinator._async_update_data()

    assert client.class_asks == 2


async def test_the_recovery_is_said_once_and_asks_for_a_reload(hass, caplog):
    """The entity set is decided before platforms are forwarded, so the log has
    to say what the retry cannot fix."""
    client = _Client(fail_with=[TimeoutError()], answer="NVR")
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    said = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "INFO" and ADDRESS in record.getMessage()
    ]
    assert len(said) == 1, said
    assert "reload" in said[0], said[0]
