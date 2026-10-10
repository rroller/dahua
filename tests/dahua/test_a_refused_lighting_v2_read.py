"""A refused Lighting_V2 read must cost a stale reading, not the whole entry.

Lighting_V2 is the light platform's table and the poll reads it on two paths.
Neither was wrapped, and the fan-out they sit in has no `return_exceptions`, so
either one turned a refusal into `UpdateFailed`: every entity on the channel
unavailable, a host failure recorded against the count all of a recorder's
channels share, and the poll backed off. Same shape as #1006, which was the
picture adjustments.

**The fallback path is the worse of the two, and the reason it is not simply
deleted.** It runs only when `_supports_lighting_v2` is False, and that flag is
set by a probe catching `(ClientError, TimeoutError)`, so two quite different
devices reach it:

* one whose table is genuinely refused -- 400 on a recorder channel, or an
  account without rights to it -- which fails *every* poll for the life of the
  entry, because the read can never succeed
* one whose probe merely timed out, which does serve the table, and for which
  this read is the only thing that recovers its security light

Deleting the read would silently take the light from the second device. Wrapping
it fixes the first and keeps the second.

What this does **not** change is which reads happen. The RPC2-deterrence camera
of test_direct_rpc2_deterrence.py is excluded from the fallback by its condition
and stays excluded; that file already pins it, and `test_the_fallback_read_is_made`
below is the guard that the refusal tests here are not passing vacuously.

The mutations these are written against, none of which I can execute here (the
suite needs Home Assistant and this module imports it):

* calling `self.client.async_get_lighting_v2()` at either call site instead of
  the wrapper -- the matching does-not-fail test fails with `UpdateFailed`
* returning `{}` rather than None when there is nothing to carry -- harmless
* carrying the whole previous poll instead of this table's keys -- the
  carries-only-its-own-keys test fails
"""

from datetime import timedelta
from types import SimpleNamespace

from aiohttp import ClientResponseError

from custom_components.dahua import DahuaDataUpdateCoordinator

ADDRESS = "10.0.0.88"

LIGHTING_V2 = "async_get_lighting_v2"


def _refused(status=400):
    """A recorder channel refusing the table, which is what was measured here."""
    return ClientResponseError(
        request_info=SimpleNamespace(
            real_url="http://%s/cgi-bin/configManager.cgi" % ADDRESS
        ),
        history=(),
        status=status,
    )


class _Client:
    """Answers every read emptily, records what was asked, refuses one thing."""

    # A plain attribute, not routed through __getattr__ below: the uptime block
    # reads `client.use_rpc2` as a value, and a __getattr__ that handed back a
    # coroutine function would be truthy and pull the whole host uptime path
    # into a test that is not about it.
    use_rpc2 = False
    device_key = "%s:80" % ADDRESS

    def __init__(self, refusing=None, lighting=None):
        self.asked = []
        self.refusing = refusing
        self._lighting = lighting or {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args, **kwargs):
            self.asked.append(name)
            if name == self.refusing:
                raise _refused()
            return self._lighting if name == LIGHTING_V2 else {}

        return call


def _coordinator(*, supported, refusing=None, lighting=None, data=None, light=True):
    """A coordinator past its one-time init with only the light reads in play.

    `supported` is `_supports_lighting_v2`, which is what chooses between the
    two call sites: True puts the read in the fan-out, False routes it to the
    security-light fallback.
    """
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.hass = SimpleNamespace()
    c.client = _Client(refusing, lighting)
    c._address = ADDRESS
    c._channel = 0
    c._channel_number = 1
    c.initialized = True
    c.model = ""
    c._profile_mode = "0"
    c._preset_position = "0"
    c._camera_reboot_generation = 0
    c._supports_lighting_v2 = supported
    c._supports_profile_mode = False
    c._supports_ptz_position = False
    c._supports_coaxial_control = False
    c._supports_disarming_linkage = False
    c._supports_event_notifications = False
    c._supports_smart_motion_detection = False
    c._supports_privacy_mode = False
    c._supports_day_night_color = False
    c._supports_lighting = False
    c._supports_floodlightmode = False
    c._supports_cloud_upgrade = False
    c._supports_lighting_scheme_illuminator = False
    c._nvr_active_deterrence = False
    c._alarm_output_slots = 0
    c._ivs_rules = []
    c._video_color_fields = frozenset()
    c._serial_number = "SER1"
    c.machine_name = "cam"
    for name in (
        "is_doorbell",
        "is_amcrest_doorbell",
        "is_flood_light",
        "is_indoor_monitor",
        "is_nvr_channel",
        "reads_coaxial_status",
        "supports_alarm_output",
        "supports_infrared_light",
        "supports_smart_motion_detection_amcrest",
        "uses_rpc2_deterrence",
    ):
        setattr(c, name, lambda *a, **k: False)
    # The camera the fallback exists for: a security light driven over CGI. Set
    # as the leaf capability rather than overriding the condition, so the real
    # rule stays in play.
    c.supports_security_light = lambda: True
    c._wanted_by = lambda *platforms: light
    c.read_profile_mode = lambda mode_data: "0"
    c._back_off_poll_interval = lambda n: None
    c._restore_poll_interval = lambda: None
    c.config_entry = SimpleNamespace(data={}, options={}, entry_id="e1")
    c.update_interval = timedelta(seconds=30)
    if data is not None:
        c.data = data
    return c


# --- the fan-out read, for a device whose probe said yes ---------------------


async def test_the_fan_out_read_is_made():
    coordinator = _coordinator(supported=True)

    await coordinator._async_update_data()

    assert LIGHTING_V2 in coordinator.client.asked


async def test_the_fan_out_returns_what_it_read():
    coordinator = _coordinator(
        supported=True,
        lighting={"table.Lighting_V2[0][0][0].Mode": "Manual"},
    )

    data = await coordinator._async_update_data()

    assert data["table.Lighting_V2[0][0][0].Mode"] == "Manual"


async def test_a_refused_fan_out_read_does_not_fail_the_poll():
    """A device that served the table at setup and starts refusing it under load
    -- which the recorder here does for the coaxial status, 949 times in nine
    hours on one channel -- should cost a stale light, not the camera."""
    coordinator = _coordinator(supported=True, refusing=LIGHTING_V2)

    data = await coordinator._async_update_data()

    assert isinstance(data, dict), "the poll did not complete"


async def test_a_refused_fan_out_read_keeps_the_last_known_table():
    coordinator = _coordinator(
        supported=True,
        refusing=LIGHTING_V2,
        data={"table.Lighting_V2[0][0][0].Mode": "Manual"},
    )

    data = await coordinator._async_update_data()

    assert data.get("table.Lighting_V2[0][0][0].Mode") == "Manual"


async def test_a_refused_read_carries_only_its_own_table():
    """Carrying the whole previous poll would let a stale value overwrite a fresh
    one from another coroutine in the same gather."""
    coordinator = _coordinator(
        supported=True,
        refusing=LIGHTING_V2,
        data={
            "table.Lighting_V2[0][0][0].Mode": "Manual",
            "status.PresetID": "7",
        },
    )

    data = await coordinator._async_update_data()

    assert data.get("table.Lighting_V2[0][0][0].Mode") == "Manual"
    assert "status.PresetID" not in data


async def test_a_refused_read_with_nothing_remembered_says_nothing():
    coordinator = _coordinator(supported=True, refusing=LIGHTING_V2, data={})

    data = await coordinator._async_update_data()

    assert not [key for key in data if key.startswith("table.Lighting_V2[")]


# --- the fallback read, for a device whose probe failed ----------------------


async def test_the_fallback_read_is_made():
    """The guard. This path is what the tests below are about, and with the read
    never made at all every one of them would hold for the wrong reason."""
    coordinator = _coordinator(supported=False)

    await coordinator._async_update_data()

    assert LIGHTING_V2 in coordinator.client.asked


async def test_a_refused_fallback_read_does_not_fail_the_poll():
    """The live fault. This line is reached only when the probe already failed,
    so a device whose table is refused outright arrives here on every poll and
    the read can never succeed -- an entry unavailable for the life of the
    process over one optional table."""
    coordinator = _coordinator(supported=False, refusing=LIGHTING_V2)

    data = await coordinator._async_update_data()

    assert isinstance(data, dict), "the poll did not complete"


async def test_a_refused_fallback_read_keeps_the_last_known_table():
    coordinator = _coordinator(
        supported=False,
        refusing=LIGHTING_V2,
        data={"table.Lighting_V2[0][0][1].Mode": "Manual"},
    )

    data = await coordinator._async_update_data()

    assert data.get("table.Lighting_V2[0][0][1].Mode") == "Manual"


async def test_a_fallback_read_that_works_is_still_merged():
    """The device the read stays for: its probe timed out, so the flag is False,
    but it does serve the table and this is the only thing that fetches it."""
    coordinator = _coordinator(
        supported=False,
        lighting={"table.Lighting_V2[0][0][1].Mode": "Manual"},
    )

    data = await coordinator._async_update_data()

    assert data["table.Lighting_V2[0][0][1].Mode"] == "Manual"


async def test_neither_read_is_made_without_the_light_platform():
    """Both call sites are gated on the light platform, and the table feeds
    nothing else here."""
    coordinator = _coordinator(supported=True, light=False)

    await coordinator._async_update_data()

    assert LIGHTING_V2 not in coordinator.client.asked
