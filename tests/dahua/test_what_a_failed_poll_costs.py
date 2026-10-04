"""What the poll does when a read fails, and the one failure it must treat differently.

`_async_update_data` is the biggest function in the integration and the least covered:
94 of its statements were unexecuted, including the whole of the handler below. That
handler is the other half of #729.

The rule it implements: a 401 is routed through `_auth_refused`, and everything else
becomes `UpdateFailed`. That distinction is the entire fix, because Home Assistant does
two things for `ConfigEntryAuthFailed` and only one of them for `UpdateFailed`. It opens
the reauth flow, and it stops scheduling refreshes:

    if not auth_failed and self._listeners and not self.hass.is_stopping:
        self._schedule_refresh()

Stopping the polls is the part that matters. A Dahua box locks the source IP for about
half an hour after repeated failed logins, so a coordinator that keeps polling keeps
renewing the lock, and the correct password typed into the reauth dialog is refused along
with everything else. Reauth asked for, reauth impossible.

The counterpart is #714: one 401 is not proof of a wrong password, so below the budget a
refusal is an ordinary failed poll and polling continues. Getting *that* backwards throws
a working camera into a reauth dialog over a single dropped request.

The harness builds the coordinator with `object.__new__` and turns every capability off,
so exactly one call is made and the test chooses what it does. The `gather` in the middle
of the poll has no `return_exceptions`, which is what carries a failed read out to the
handler.
"""

import pytest
from aiohttp import ClientResponseError
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from types import SimpleNamespace

from custom_components import dahua as dahua_module
from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import MAX_AUTH_REFUSALS

ADDRESS = "10.0.0.5"

# Every client call the poll can make. All are off in the harness except the motion
# read, which is the only one gated on nothing but `_wanted_by`.
CLIENT_METHODS = (
    "async_get_alarm_output_state",
    "async_get_config_lighting",
    "async_get_config_motion_detection",
    "async_get_disarming_linkage",
    "async_get_event_notifications",
    "async_get_ivs_rules",
    "async_get_light_global_enabled",
    "async_get_lighting_scheme",
    "async_get_lighting_v2",
    "async_get_ptz_position",
    "async_get_remote_ivs_rules",
    "async_get_smart_motion_detection",
    "async_get_video_analyse_rules_for_amcrest",
    "async_get_video_in_mode",
    "async_get_video_in_options",
    "async_reconcile_lighting_scheme_restore_modes",
)

# Everything the poll asks the coordinator about itself. Off, so the fan-out is
# one call and a test is about that call rather than about which flags it set.
CAPABILITY_FLAGS = (
    "_supports_coaxial_control",
    "_supports_day_night_color",
    "_supports_disarming_linkage",
    "_supports_event_notifications",
    "_supports_lighting_v2",
    "_supports_privacy_mode",
    "_supports_profile_mode",
    "_supports_ptz_position",
    "_supports_smart_motion_detection",
)
CAPABILITY_METHODS = (
    "is_doorbell",
    "is_amcrest_doorbell",
    "is_flood_light",
    "is_nvr_channel",
    "reads_coaxial_status",
    "supports_alarm_output",
    "supports_infrared_light",
    "supports_security_light",
    "supports_smart_motion_detection_amcrest",
    "uses_rpc2_deterrence",
)


@pytest.fixture(autouse=True)
def _clean():
    """Module level and host keyed, and the auth refusals live inside the same
    per host record, so one clear covers both.
    """
    dahua_module._HOST_FAILURES.clear()
    yield
    dahua_module._HOST_FAILURES.clear()


def _coordinator(hass, motion=None):
    """A coordinator whose poll makes exactly one call: the motion read."""

    async def _motion():
        if isinstance(motion, BaseException):
            raise motion
        return motion if motion is not None else {}

    client = SimpleNamespace(use_rpc2=False)
    for name in CLIENT_METHODS:

        async def _nothing(*args, **kwargs):
            return {}

        setattr(client, name, _nothing)
    client.async_get_config_motion_detection = _motion

    c = object.__new__(DahuaDataUpdateCoordinator)
    c.hass = hass
    c.client = client
    c._address = ADDRESS
    c._channel = 0
    c._profile_mode = "0"
    c._preset_position = "0"
    c._camera_reboot_generation = 0
    c.config_entry = SimpleNamespace(entry_id="e1")
    c.initialized = True
    for flag in CAPABILITY_FLAGS:
        setattr(c, flag, False)
    for name in CAPABILITY_METHODS:
        setattr(c, name, lambda *a, **k: False)
    c._wanted_by = lambda *a, **k: True
    c.read_profile_mode = lambda data: "0"
    c.get_coaxial_status_channel = lambda: 0
    c.get_rpc2_coaxial_status_channel = lambda: 1
    c.backed_off = []
    c.restored = 0
    c._back_off_poll_interval = lambda n: c.backed_off.append(n)
    c._restore_poll_interval = lambda: setattr(c, "restored", c.restored + 1)
    return c


def _response_error(status):
    """An aiohttp error with enough of a request on it to be printable.

    `describe_update_failure` is `str(exception)`, and aiohttp formats
    `request_info.real_url` into that, so a `request_info` of None raises while
    being described. The 401 path never reaches the describer, which is why only
    the other status noticed.
    """
    return ClientResponseError(
        request_info=SimpleNamespace(real_url="http://%s/cgi-bin/x.cgi" % ADDRESS),
        history=(),
        status=status,
    )


def _401():
    return _response_error(401)


# --- a poll that worked -----------------------------------------------------


async def test_a_good_poll_returns_what_it_read(hass):
    coordinator = _coordinator(hass, motion={"table.MotionDetect[0].Enable": "true"})

    data = await coordinator._async_update_data()

    assert data["table.MotionDetect[0].Enable"] == "true"


async def test_a_good_poll_clears_the_hosts_failures(hass):
    """The count drives both the backoff and the unreachable repair card, so a
    poll that worked has to say so. Left uncleared, one bad afternoon would keep a
    camera backed off for the life of the process."""
    coordinator = _coordinator(hass)
    dahua_module.async_record_host_failure(hass, ADDRESS, "e1")
    assert (
        ADDRESS in dahua_module._HOST_FAILURES
    ), "the failure was not recorded, so clearing it proves nothing"

    await coordinator._async_update_data()

    assert ADDRESS not in dahua_module._HOST_FAILURES


async def test_a_good_poll_restores_the_interval(hass):
    """The other half of backing off. A camera that recovers has to go back to
    polling normally rather than staying on the slow interval it earned."""
    coordinator = _coordinator(hass)

    await coordinator._async_update_data()

    assert coordinator.restored == 1


# --- a poll that failed for an ordinary reason ------------------------------


async def test_a_failed_read_becomes_UpdateFailed(hass):
    coordinator = _coordinator(hass, motion=TimeoutError())

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_the_failure_names_itself(hass):
    """Raised bare, the coordinator's own "Error fetching dahua data" line prints
    that sentence and then nothing, which is what sends people to the debug logging
    instructions for what is often a one word answer. `TimeoutError` carries no
    message of its own, so the class name is the whole of the answer here."""
    coordinator = _coordinator(hass, motion=TimeoutError())

    with pytest.raises(UpdateFailed) as caught:
        await coordinator._async_update_data()

    assert "TimeoutError" in str(caught.value)


async def test_a_failed_poll_backs_off(hass):
    """And is told how many times in a row this host has failed, since that is
    what the delay is computed from."""
    coordinator = _coordinator(hass, motion=TimeoutError())

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.backed_off == [1]
    assert coordinator.restored == 0


async def test_consecutive_failures_climb(hass):
    coordinator = _coordinator(hass, motion=TimeoutError())

    for _ in range(3):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert coordinator.backed_off == [1, 2, 3]


# --- and the one that has to be treated differently -------------------------


async def test_one_401_is_not_a_wrong_password(hass):
    """#714. A single refusal is an ordinary failed poll: polling continues and no
    reauth dialog is raised. Treating it as a wrong password throws a working
    camera at the user over one dropped request."""
    coordinator = _coordinator(hass, motion=_401())

    with pytest.raises(UpdateFailed) as caught:
        await coordinator._async_update_data()

    assert not isinstance(caught.value, ConfigEntryAuthFailed)


async def test_a_401_past_the_budget_stops_the_polling(hass):
    """#729, and the reason the distinction exists. Only ConfigEntryAuthFailed
    makes Home Assistant stop scheduling refreshes. UpdateFailed keeps polling,
    which keeps renewing the lockout, so the right password typed into the reauth
    dialog is refused along with everything else.
    """
    coordinator = _coordinator(hass, motion=_401())

    for _ in range(MAX_AUTH_REFUSALS - 1):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_the_refusal_count_is_per_host_not_per_entry(hass):
    """Ten channels of one recorder are ten entries renewing one lock, so the
    budget is shared. Counting per entry would let the first channel give up while
    the other nine kept the lock alive."""
    first = _coordinator(hass, motion=_401())
    second = _coordinator(hass, motion=_401())

    for _ in range(MAX_AUTH_REFUSALS - 1):
        with pytest.raises(UpdateFailed):
            await first._async_update_data()

    with pytest.raises(ConfigEntryAuthFailed):
        await second._async_update_data()


async def test_a_non_401_response_is_not_an_auth_failure(hass):
    """The check is on the status, not on the exception type, and a 500 from a
    device having a bad day must not empty the user's credentials dialog."""
    coordinator = _coordinator(hass, motion=_response_error(500))

    for _ in range(MAX_AUTH_REFUSALS + 1):
        with pytest.raises(UpdateFailed) as caught:
            await coordinator._async_update_data()
        assert not isinstance(caught.value, ConfigEntryAuthFailed)
