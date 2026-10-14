"""The last three capability questions that gate an entity are asked again.

Part of #1057. These three were left until now because each needed its
platform to listen as well as the coordinator to ask: nothing creates an
entity from a late answer unless the platform is watching for one, and only
light, select and switch were. number, sensor and update join them here.

| capability | what a lost answer costs |
| --- | --- |
| `video_color` | four sliders, one per field the channel reports |
| `profile_mode` | the profile sensor, and the poll stops reading VideoInMode |
| `cloud_upgrade` | the firmware update entity |

`profile_mode` is the one worth reading twice. The flag does not only gate a
sensor: with it False the poll never reads VideoInMode, so `_profile_mode`
stays "0", and a camera running Night has its lighting read from and written
to the Day row, where the device accepts the write and renders from somewhere
else. A probe that timed out cost that for the life of the entry.

The distinction the fix rests on is kept throughout: a device that *answers*
is not asked twice, and only silence stays open. All three judge on what came
back rather than on whether the read raised, which they already did:
`async_get_video_color` is read per channel row, `async_get_config_lighting`
turns a 400 into `{}` so more than one line back is the capability, and the
cloud record has to name a version.

Reintroduced one at a time against the real source:

| reintroduced | caught by |
| --- | --- |
| the three registry entries removed | every `arrives_late` test |
| a good read not recorded as an answer | `arrives_late`, `not_announced_again` |
| the retry reading the flag its own probe just wrote | `not_announced_again` |
| the setup probes inlined again | the three `in_exactly_one_place` tests |
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module

from .integration_source import source

ADDRESS = "10.0.0.88"
CHANNEL = 3
SERIAL = "AMR044032F4F06B33E_3"

# This channel's own row. Channel 0's would be the bug video_color_fields
# exists to prevent, so the fake answers about both.
PICTURE = {
    "table.VideoColor[0][0].Brightness": "50",
    "table.VideoColor[{0}][0].Brightness".format(CHANNEL): "50",
    "table.VideoColor[{0}][0].Contrast".format(CHANNEL): "50",
}
# A table that came back for the host and carries nothing for this channel.
PICTURE_FOR_ANOTHER_CHANNEL = {"table.VideoColor[0][0].Brightness": "50"}

# More than one line back is a camera with selectable profiles.
PROFILES = {
    "table.Lighting[{0}][2].Mode".format(CHANNEL): "Auto",
    "table.Lighting[{0}][2].Sensitive".format(CHANNEL): "3",
}
# What a camera without them answers, after async_get_config_lighting has
# turned the device's `Error -1 getting param` into an empty table.
NO_PROFILES = {}

CLOUD = {"LastVersion": "2.800.0000016.0.R", "LastSubVersion": "R"}
# The record exists and names nothing, which is the device answering.
CLOUD_EMPTY = {"LastVersion": ""}


class _Client:
    """The three reads these capabilities come from, as the test dictates.

    Each list is consumed in order and the last entry repeats, so a test says
    "silent, then it answers" without counting calls. An `Exception` is raised
    rather than returned.
    """

    def __init__(self, picture=(PICTURE,), profiles=(PROFILES,), cloud=(CLOUD,)):
        self._answers = {
            "picture": list(picture),
            "profiles": list(profiles),
            "cloud": list(cloud),
        }
        self.reads = {"picture": 0, "profiles": 0, "cloud": 0}
        self.profile_rows = []

    def _answer(self, name):
        self.reads[name] += 1
        answers = self._answers[name]
        answer = answers[0] if len(answers) == 1 else answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    async def async_get_video_color(self):
        return self._answer("picture")

    async def async_get_config_lighting(self, channel, profile):
        self.profile_rows.append((channel, profile))
        return self._answer("profiles")

    async def async_get_cloud_upgrade_info(self):
        return self._answer("cloud")


def _coordinator(**answers):
    """A coordinator carrying the probes, the retry machinery and nothing else.

    `object.__new__` rather than the constructor, as the rest of this suite
    does: building a real one wants a hass, an entry and a device.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _Client(**answers)
    coordinator._channel = CHANNEL
    coordinator._address = ADDRESS
    coordinator._video_color_fields = frozenset()
    coordinator._supports_profile_mode = False
    coordinator._supports_cloud_upgrade = False
    coordinator._cloud_firmware_version = None
    coordinator._cloud_upgrade_checked_at = None
    coordinator._probe_refusals = {}
    coordinator._capability_retries = {}
    coordinator.hass = None
    coordinator.config_entry = type("E", (), {"entry_id": "entry-1"})()
    coordinator.get_serial_number = lambda: SERIAL
    return coordinator


@pytest.fixture
def discoveries(monkeypatch):
    """What the coordinator announced, captured where it looks the name up.

    `coordinator.py` does `from .late import async_notify_capability_discovered`,
    so patching `late` would not reach the bound name and the real dispatcher
    would be called with `hass=None`.
    """
    announced = []
    monkeypatch.setattr(
        coordinator_module,
        "async_notify_capability_discovered",
        lambda hass, entry_id, channel_key: announced.append((entry_id, channel_key)),
    )
    return announced


# --- the picture adjustments -------------------------------------------------


async def test_a_timed_out_picture_probe_leaves_the_question_open():
    coordinator = _coordinator(picture=(TimeoutError(),))

    assert await coordinator._async_probe_video_color() is False
    assert coordinator.supports_video_color("Brightness") is False
    assert coordinator.probes_that_did_not_answer() == {"video_color"}


async def test_a_table_without_this_channels_row_is_an_answer():
    """A recorder answers one request for every channel, and the rows are
    sparse. Another channel's row is the device saying no about this one."""
    coordinator = _coordinator(
        picture=(TimeoutError(), PICTURE_FOR_ANOTHER_CHANNEL),
    )
    await coordinator._async_probe_video_color()

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_video_color("Brightness") is False
    assert coordinator.probes_that_did_not_answer() == set()


async def test_picture_adjustments_that_arrive_late_gate_their_sliders(discoveries):
    """Through the real registry, not a narrowed one: a test that monkeypatches
    `RETRYABLE_CAPABILITIES` down to this capability passes just as happily
    when the registry entry is deleted."""
    coordinator = _coordinator(picture=(TimeoutError(), PICTURE))
    await coordinator._async_probe_video_color()

    assert "video_color" in coordinator_module.RETRYABLE_CAPABILITIES

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_video_color("Brightness") is True
    assert coordinator.supports_video_color("Contrast") is True
    assert coordinator.supports_video_color("Hue") is False, (
        "per field, so a device reporting two of the four gets two sliders "
        "rather than a third that can only read unknown"
    )
    assert discoveries == [("entry-1", SERIAL)]


# --- the lighting profile ----------------------------------------------------


async def test_a_timed_out_profile_probe_leaves_the_question_open():
    coordinator = _coordinator(profiles=(TimeoutError(),))

    assert await coordinator._async_probe_profile_mode() is False
    assert coordinator.supports_profile_mode() is False
    assert coordinator.probes_that_did_not_answer() == {"profile_mode"}


async def test_a_camera_without_profiles_is_not_asked_again():
    """`async_get_config_lighting` turns the device's `Error -1 getting param`
    into an empty table, which is the device answering."""
    coordinator = _coordinator(profiles=(TimeoutError(), NO_PROFILES))
    await coordinator._async_probe_profile_mode()

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_profile_mode() is False
    assert coordinator.probes_that_did_not_answer() == set()


async def test_a_profile_that_arrives_late_gates_its_sensor(discoveries):
    coordinator = _coordinator(profiles=(TimeoutError(), PROFILES))
    await coordinator._async_probe_profile_mode()

    assert "profile_mode" in coordinator_module.RETRYABLE_CAPABILITIES

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_profile_mode() is True
    assert discoveries == [("entry-1", SERIAL)]


async def test_the_retry_asks_about_this_channels_row(discoveries):
    """The name was hardcoded to `Lighting[0][2]` once, so on a merged recorder
    every channel's answer came from camera 1's table. The retry must not
    reintroduce that by asking differently from setup."""
    coordinator = _coordinator(profiles=(TimeoutError(), PROFILES))
    await coordinator._async_probe_profile_mode()

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.client.profile_rows == [(CHANNEL, 2), (CHANNEL, 2)]


# --- the cloud firmware record -----------------------------------------------


async def test_a_timed_out_cloud_probe_leaves_the_question_open():
    coordinator = _coordinator(cloud=(TimeoutError(),))

    assert await coordinator._async_probe_cloud_upgrade() is False
    assert coordinator.supports_cloud_upgrade() is False
    assert coordinator.probes_that_did_not_answer() == {"cloud_upgrade"}


async def test_a_record_that_names_no_version_is_an_answer():
    """The table exists and has not been filled in. An update entity built on
    it could only ever read unknown."""
    coordinator = _coordinator(cloud=(TimeoutError(), CLOUD_EMPTY))
    await coordinator._async_probe_cloud_upgrade()

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_cloud_upgrade() is False
    assert coordinator.probes_that_did_not_answer() == set()


async def test_a_cloud_record_that_arrives_late_gates_the_update_entity(discoveries):
    coordinator = _coordinator(cloud=(TimeoutError(), CLOUD))
    await coordinator._async_probe_cloud_upgrade()

    assert "cloud_upgrade" in coordinator_module.RETRYABLE_CAPABILITIES

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_cloud_upgrade() is True
    assert coordinator.get_cloud_firmware_version() == "2.800.0000016.0.R"
    assert discoveries == [("entry-1", SERIAL)]


# --- asked again, but not for ever -------------------------------------------


async def test_a_second_answer_is_not_announced_again(discoveries):
    """A probe that answers on the retry has nothing left to discover, and a
    channel that keeps being rebuilt would re-read the device each time."""
    coordinator = _coordinator(
        picture=(TimeoutError(), PICTURE),
        profiles=(TimeoutError(), PROFILES),
        cloud=(TimeoutError(), CLOUD),
    )
    await coordinator._async_probe_video_color()
    await coordinator._async_probe_profile_mode()
    await coordinator._async_probe_cloud_upgrade()

    await coordinator._async_retry_unanswered_capabilities()
    await coordinator._async_retry_unanswered_capabilities()

    assert len(discoveries) == 1
    assert coordinator.client.reads == {"picture": 2, "profiles": 2, "cloud": 2}, (
        "once at setup and once on the retry. A third read of any of them "
        "means that answer was not recorded as one"
    )


async def test_a_channel_that_stays_silent_is_let_go(discoveries):
    coordinator = _coordinator(
        picture=(TimeoutError(),),
        profiles=(TimeoutError(),),
        cloud=(TimeoutError(),),
    )
    await coordinator._async_probe_video_color()
    await coordinator._async_probe_profile_mode()
    await coordinator._async_probe_cloud_upgrade()
    at_setup = dict(coordinator.client.reads)

    for _ in range(10):
        await coordinator._async_retry_unanswered_capabilities()

    budget = coordinator_module.MAX_CAPABILITY_RETRIES
    assert {
        name: count - at_setup[name] for name, count in coordinator.client.reads.items()
    } == {"picture": budget, "profiles": budget, "cloud": budget}
    assert discoveries == []


# --- one judgment, in one place ----------------------------------------------

#: Every module of the integration at once, through the helper that exists so a
#: scan like this does not become a coupling to which file holds the code.
_SOURCE = source()


def test_the_picture_judgment_is_made_in_exactly_one_place():
    assert _SOURCE.count('_note_probe_refusal("video_color"') == 1
    assert _SOURCE.count("self.client.async_get_video_color()") == 2, (
        "once in the probe and once in the poll's fetch. A third call site "
        "means a second judgment"
    )


def test_the_profile_judgment_is_made_in_exactly_one_place():
    assert _SOURCE.count('_note_probe_refusal("profile_mode"') == 1


def test_the_cloud_judgment_is_made_in_exactly_one_place():
    assert _SOURCE.count('_note_probe_refusal("cloud_upgrade"') == 1
    assert (
        _SOURCE.count("async_get_cloud_upgrade_info()") == 2
    ), "once in the probe and once in the poll's periodic re-read"
