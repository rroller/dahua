"""
Custom integration to integrate Dahua cameras with Home Assistant.
"""
import asyncio
from collections import deque
from typing import Any, Dict
import logging
import random
import re
import ssl
import time

from datetime import timedelta

from homeassistant.components.tag import async_scan_tag
import hashlib

from aiohttp import ClientError, ClientResponseError, ClientSession, TCPConnector
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from . import dahua_utils
from .client import (
    _HOST_RPC2_EVENT_POLL,
    _HOST_RPC2_EVENT_STATE,
    DahuaClient,
    clear_host_cache,
)
from .model_profiles import is_sdt4e425
from .ivs import ivs_rules_for_channel, ivs_rule_index

from .const import (
    CONF_EVENTS,
    CONF_PASSWORD,
    ISSUE_URL,
    CONF_PORT,
    CONF_USERNAME,
    CONF_ADDRESS,
    CONF_NAME,
    DOMAIN,
    PLATFORMS,
    CAMERA,
    LIGHT,
    SELECT,
    SWITCH,
    UPDATE,
    CONF_RTSP_PORT,
    STARTUP_MESSAGE,
    CONF_CHANNEL,
    CONF_AUTO_DETECT_CHANNEL,
    CONF_USE_HTTPS,
    CONF_SCAN_INTERVAL,
    CONF_USE_RPC2,
    CONF_NVR_ACTIVE_DETERRENCE,
    CONF_MANUAL_SIREN,
    CONF_MANUAL_SECURITY_LIGHT,
    CONF_AREA,
    CONF_AUTHORIZED_PLATES,
    DEFAULT_EVENTS,
    CONF_AUTHORIZED_HOLD_TIME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_AUTHORIZED_HOLD_TIME,
    MIN_SCAN_INTERVAL,
    EVENT_DAHUA_ANPR_RECOGNIZED,
)
from .dahua_utils import cloud_upgrade_version, parse_event
from .deterrence import (
    product_definition_supports_security_light,
    product_definition_supports_siren,
    siren_definition_failure_reason,
    security_light_definition_failure_reason,
)
from .illuminator_restore import IlluminatorRestoreStore
from .vto import DahuaVTOClient


# A stream that keeps heartbeating but has stopped reporting events looks
# healthy to a read timeout, so recycle it periodically as well.
EVENT_STREAM_MAX_LIFETIME_SECONDS = 3600

# An NVR gets one config entry per channel, and they all start within a moment
# of each other, so a fixed lifetime makes every channel drop and re-attach in
# the same second, once an hour. Spreading them means the device sees a trickle
# of reconnections instead of a burst.
EVENT_STREAM_JITTER = 0.1

# The same applies after a failure: a device that rejected every channel at once
# would otherwise be retried by every channel at once, sixty seconds later.
EVENT_STREAM_RETRY_SECONDS = 60

# A stream that lived this long was working, so reconnect at once. Anything
# shorter gets backed off, because the fast path used to have no delay at all:
# a device closing the socket at eleven seconds reconnected forever, silently.
EVENT_STREAM_HEALTHY_SECONDS = 60
EVENT_STREAM_SHORT_RETRY_SECONDS = 10

# A stream that dies on contact will keep dying on contact: the device is
# refusing us, not hiccuping. Asking again every sixty seconds forever is how a
# device that ran out of connections stays out of connections, because each
# attempt costs it another one. Back off instead, to this ceiling.
EVENT_STREAM_MAX_RETRY_SECONDS = 600


# A capability probe that times out has told us what an errored probe tells us:
# this device will not serve that call, so do not offer the entity. Timeouts are
# not aiohttp.ClientError -- asyncio.TimeoutError is the builtin -- so before
# this they escaped the probe, hit the outer handler, and failed the whole
# config entry with ConfigEntryNotReady. One slow capability check took the
# device down and Home Assistant retried it forever. See #594 and #631.
PROBE_FAILED = (ClientError, TimeoutError)

# The coaxial probe deliberately only treats an HTTP error response as "not
# supported"; a connection failure there should still fail setup. Timeouts join
# it for the reason above, without widening the rest.
PROBE_REFUSED = (ClientResponseError, TimeoutError)


# Camera uptime is host-wide. An NVR may have many config entries, one per
# channel, but they all refer to the same physical recorder uptime.
#
# Keep one shared sample per host so coordinator polling does not multiply
# uptime requests by channel count.
_HOST_UPTIME_STATE: dict[str, dict[str, Any]] = {}
_HOST_UPTIME_LOCKS: dict[str, asyncio.Lock] = {}

# NVR channel coordinators normally poll within a moment of each other.
# Reuse the first host uptime read for the others in that poll burst.
HOST_UPTIME_DEDUPE_SECONDS = 5.0


async def _async_get_host_uptime_generation(coordinator) -> int:
    """Poll one host-wide uptime value and return its reboot generation."""

    address = coordinator._address

    state = _HOST_UPTIME_STATE.setdefault(
        address,
        {
            "uptime": None,
            "generation": 0,
            "last_read": 0.0,
        },
    )

    lock = _HOST_UPTIME_LOCKS.setdefault(
        address,
        asyncio.Lock(),
    )

    async with lock:
        # Another channel may have completed the host read while this
        # coordinator was waiting for the lock.
        now = time.monotonic()

        if (
            now - state["last_read"] < HOST_UPTIME_DEDUPE_SECONDS
        ):
            return int(state["generation"])

        try:
            current = await coordinator.client.async_get_uptime_last()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Cache failed attempts too, otherwise every NVR channel may repeat
            # the same failed uptime request during the same poll burst.
            state["last_read"] = time.monotonic()

            # Uptime is an optional enhancement. A device/transport that does
            # not support it must not make the normal coordinator poll fail.
            _LOGGER.debug(
                "Could not read host uptime for %s",
                address,
                exc_info=True,
            )
            return int(state["generation"])

        previous = state["uptime"]

        if (
            previous is not None
            and current < previous
        ):
            state["generation"] += 1

            _LOGGER.info(
                "Dahua host %s reboot detected "
                "(uptime %s -> %s, generation=%s)",
                address,
                previous,
                current,
                state["generation"],
            )

        state["uptime"] = current
        state["last_read"] = time.monotonic()

        return int(state["generation"])


def stream_lifetime(lived_seconds: float, received_data: bool) -> float:
    """How long the stream really lasted, for the purpose of retrying it.

    A socket that stayed open an hour and delivered nothing -- not one event, not
    even the heartbeat the subscription asks for every
    EVENT_STREAM_HEARTBEAT_SECONDS -- did not last an hour in any sense that
    should earn an immediate reconnect. It never worked at all.

    This matters because aiohttp's `sock_read` timeout and the deliberate recycle
    raise the *same* exception: `ServerTimeoutError` is a `TimeoutError`. Duration
    alone therefore cannot tell a stream that worked for an hour from one that sat
    mute until the read timeout fired, and reading the second as the first is how
    a device that reports nothing looks healthy forever.

    Silence is safe to judge on because a stream is only ever started when some
    event is wanted, so there is no correctly-subscribed device with nothing to
    say.
    """
    return lived_seconds if received_data else 0.0


def event_stream_retry_delay(lived_seconds: float, consecutive_failures: int = 0,
                            received_data: bool = False) -> float:
    """How long to wait before re-attaching, given how long the stream lasted.

    `received_data` is whether the device sent anything at all on this attach --
    an event or a heartbeat. It separates the two things a short stream can mean.
    A device that refuses attach and one whose firmware hangs up after eight
    seconds of perfectly good events look identical by duration, and only the
    first should be backed off: the second is working, and backing it off to ten
    minutes is how a camera that still detects motion stops reporting any.
    """
    if lived_seconds < 10 and not received_data:
        # Double per successive instant death, so a device that is refusing
        # attach gets asked less often the longer it keeps refusing.
        doublings = max(0, consecutive_failures - 1)
        backoff = EVENT_STREAM_RETRY_SECONDS * (2 ** min(doublings, MAX_BACKOFF_DOUBLINGS))
        return jittered(min(backoff, EVENT_STREAM_MAX_RETRY_SECONDS))
    if lived_seconds < EVENT_STREAM_HEALTHY_SECONDS:
        return jittered(EVENT_STREAM_SHORT_RETRY_SECONDS)
    return 0.0


def vto_retry_state(lived_seconds: float, consecutive_failures: int,
                    received_data: bool) -> tuple:
    """How long to wait before re-attaching to a doorbell, and the new count.

    The doorbell's event connection had two fixed delays -- five seconds after a
    disconnect, thirty after a failure -- and no notion of a device that is
    simply refusing. A doorbell that has been unplugged was therefore contacted
    2,880 times a day, forever, where the same device as an IP camera would have
    been backed off to one attempt every ten minutes.

    The rule is the one the camera stream already uses, and it judges on whether
    the device spoke rather than on how long the socket lasted: `received_data`
    resets the count and makes the lifetime count for something, and its absence
    means this attach achieved nothing however long it sat there.
    """
    failures = 0 if received_data else consecutive_failures + 1
    delay = event_stream_retry_delay(
        stream_lifetime(lived_seconds, received_data), failures, received_data)
    return delay, failures


# A single missed poll is a blip -- a snapshot timing out, a device busy writing
# to disk -- and backing off on one would make the integration feel sluggish for
# no reason. Past that, the device is not answering and polling it on the
# configured cadence only adds to whatever is wrong.
FAILURES_BEFORE_BACKOFF = 2

# Guard on the exponent so the arithmetic stays sane for a device that has been
# failing for a week. The time ceilings below are what actually bind.
MAX_BACKOFF_DOUBLINGS = 6

# How many times a host may refuse these credentials before the integration
# stops offering them.
#
# Not one, because a single 401 is not proof of a wrong password: channels of
# one NVR share a digest challenge, and a nonce that races between them is
# refused exactly like a bad credential. Treating the first one as fatal is
# what #714 was.
#
# Not many either, because a 401 only reaches here after DigestAuth has
# already spent its own MAX_AUTH_ATTEMPTS on it, including two passes through
# the same-nonce refusal path. A 401 that survives all of that is much more
# likely to be real than a raw one, so the budget here can be small.
MAX_AUTH_REFUSALS = 3

# However long the poll interval is, never leave a failing device unpolled for
# longer than this, or a device that recovers stays missing for an afternoon.
POLL_BACKOFF_CAP = timedelta(minutes=15)


def failure_backoff(base: timedelta, consecutive: int) -> timedelta:
    """The interval to poll at, given this many consecutive failures.

    Returns the configured interval until the failures stop looking incidental,
    then doubles per failure up to a ceiling.
    """
    if consecutive <= FAILURES_BEFORE_BACKOFF:
        return base
    doublings = min(consecutive - FAILURES_BEFORE_BACKOFF, MAX_BACKOFF_DOUBLINGS)
    # Never shorter than the interval the user asked for: someone already
    # polling every half hour is not the problem this is here to solve, and
    # backing "off" to something faster would be worse than doing nothing.
    return min(base * (2 ** doublings), max(POLL_BACKOFF_CAP, base))


# Lighting_V2 lists a device's lights by index, and the index order is not the
# same on every model. The device names each one in LightType, so it does not
# have to be guessed.
WHITE_LIGHT = "WhiteLight"
MAX_LIGHTING_V2_LIGHTS = 4


# A light's brightness lives in one of several named banks, and which one is not
# the same for every light or every model. MiddleLight first, so a device that
# exposes more than one keeps the bank this integration has always used.
LIGHT_BRIGHTNESS_BANKS = ("MiddleLight", "NearLight", "FarLight")


def illuminator_brightness_bank(data: dict, channel: int, profile_mode, light_index: int) -> str:
    """Which brightness bank this light actually uses on this device.

    The bank was hardcoded to MiddleLight, which is right for the infrared
    emitter on most models and frequently wrong for the white one. Measured:
    an IPC-HFW2449T-AS-IL reports `[0] InfraredLight -> MiddleLight` but
    `[1] WhiteLight -> NearLight`, and a DHI-NVR5464-16P-EI channel reports a
    WhiteLight row with no brightness bank at all.

    Since #652 the illuminator writes to the row the device calls white, so a
    hardcoded MiddleLight now aims brightness at a bank that row may not have.

    Falls back to MiddleLight when the device names none, which is what every
    caller did before this existed.
    """
    for bank in LIGHT_BRIGHTNESS_BANKS:
        key = "table.Lighting_V2[{0}][{1}][{2}].{3}[0].Light".format(
            channel, profile_mode, light_index, bank)
        if key in data:
            return bank
    return LIGHT_BRIGHTNESS_BANKS[0]


SMART_MOTION_ROW = re.compile(r"^table\.SmartMotionDetect\[(\d+)\]")


def smart_motion_row_indices(table) -> tuple:
    """Which channel rows a device reports in its SmartMotionDetect table.

    The presence of this channel's row is what decides whether it gets a smart
    motion switch (#635), so when the answer surprises someone this is the fact
    they need. Nothing logged it: the integration never logs a response body, so
    #669 spent two rounds inferring the shape of a table that could simply have
    been printed.

    Returns a sorted tuple of the indices found, empty when the table is empty
    or not a dict -- never raising, because a diagnostic that can take setup
    down is worse than no diagnostic.
    """
    if not isinstance(table, dict):
        return ()
    found = set()
    for key in table:
        match = SMART_MOTION_ROW.match(str(key))
        if match:
            found.add(int(match.group(1)))
    return tuple(sorted(found))


def infrared_profile(data: dict, channel: int, profile_mode) -> str:
    """Which Lighting profile this channel's infrared light is really using.

    The v1 Lighting table is indexed [channel][profile], exactly as Lighting_V2
    is, and the profiles genuinely differ. Measured on a DHI-NVR5464-16P-EI,
    where five of fifteen channels report four profiles apiece and their modes
    disagree:

        table.Lighting[3][0].Mode=Auto
        table.Lighting[3][1].Mode=ZoomPrio
        table.Lighting[3][2].Mode=ZoomPrio
        table.Lighting[3][3].Mode=ZoomPrio

    The poll already fetches the *live* profile --
    async_get_config_lighting(channel, self._profile_mode) -- while the reader
    and the writer both hardcoded profile 0. On a camera running anything but
    day that means the data holds one profile and the entity reads another, so
    the light reports off whatever it is doing, and every write lands on a
    profile the camera is not rendering from.

    Falls back to profile 0 when the live one is not in what the device
    returned, which is the single-profile case and also what keeps a channel
    working if VideoInMode names a profile the Lighting table does not have.
    Unlike the row 0 fallbacks removed in #679 and #683, this one stays inside
    the same channel -- it can only ever return this camera's own row.
    """
    live = str(profile_mode)
    if "table.Lighting[{0}][{1}].Mode".format(channel, live) in data:
        return live
    return "0"


# VideoInOptions[channel].DayNightColor, as the device spells the Day/Night
# setting. The names are the ones the existing set_video_in_day_night_mode
# service already accepts, so the select and the service speak the same words.
DAY_NIGHT_NAMES = {"0": "Color", "1": "Auto", "2": "BlackWhite"}


def day_night_color_name(data: dict, channel: int):
    """This channel's Day/Night mode by name, or None if it did not report one.

    None rather than a default: a device that does not carry this setting must
    not be shown as though it were in Color, and an unrecognised value is a
    device telling us something this mapping does not cover.
    """
    value = data.get("table.VideoInOptions[{0}].DayNightColor".format(channel))
    if value is None:
        return None
    return DAY_NIGHT_NAMES.get(str(value).strip())


# DeviceType values that name a class of device rather than a model. Measured on
# a DHI-NVR5464-16P-EI, which answers "IP Camera" and "IPC" for most channels and
# a real model for one; the Lorex N843A8 on #669 answers a model for every
# populated channel. Treating these as a model would be worse than having none.
GENERIC_DEVICE_TYPES = {"", "ip camera", "ipc", "camera", "ip dome", "unknown"}


def remote_device_model(data: dict, channel: int):
    """The model of the camera on this NVR channel, or None if it did not say.

    Every channel of a recorder reports the *recorder's* model, because that is
    what magicBox.cgi getSystemInfo answers. The camera's own model is in
    RemoteDevice, indexed by channel:

        table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_6.DeviceType=B451AJ

    Both spellings are accepted: this uuid-keyed form, measured on a
    DHI-NVR5464-16P-EI and on the Lorex N843A8 of #669, and the plain bracket
    form in case firmware elsewhere uses it.

    Returns None rather than a guess when the value names a class of device
    instead of a model -- see GENERIC_DEVICE_TYPES. A caller that cannot tell
    "no answer" from "IP Camera" would confidently misidentify every channel of
    a recorder like mine.
    """
    if not isinstance(data, dict):
        return None
    for key in ("table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_{0}.DeviceType",
                "table.RemoteDevice[{0}].DeviceType"):
        value = data.get(key.format(channel))
        if value is None:
            continue
        value = str(value).strip()
        if value.lower() in GENERIC_DEVICE_TYPES:
            return None
        return value
    return None


def model_name(resolved, reported) -> str:
    """The model string to gate capabilities on, never None.

    getSystemInfo answers `deviceType` for cameras, but recorders answer a
    number (Lorex sends 31) or omit it entirely, with the real model in
    `updateSerial`. #59 was a DVR that omitted it, and setup died on
    `'NoneType' object has no attribute 'upper'`. That was fixed by falling
    back to `updateSerial`, and then to getDeviceType.

    Both fallbacks can still come back empty -- getDeviceType answering an
    empty body, or an error string with no "=" in it, leaves `.get("type")`
    None again -- so the crash is still reachable by a different road. Two
    things keep it shut:

    `reported` is the generic value the fallback chain set out to improve on.
    Preferring it to nothing means a device calling itself "IP Camera" stays
    "IP Camera" instead of becoming None the moment the more specific lookups
    come back empty.

    And the result is always a string, so a device that answers nothing useful
    ends up with "" -- which every capability check reads as a model matching
    no prefix, leaving its feature off. That is the right outcome for an
    unknown device, and it is what the attribute is initialised to.
    """
    return (resolved or reported or "").strip()


def remote_device_protocol(data: dict, channel: int):
    """How the recorder reaches the camera on this channel, lowercased.

    Sits beside the ProtocolType that remote_device_model reads DeviceType
    from, and accepts the same two spellings:

        table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_10.ProtocolType=Onvif

    Measured on a DHI-NVR5464-16P-EI, fifteen populated channels: fourteen
    report `Private` and one reports `Onvif`. That one is the reason this
    exists -- see is_onvif_channel.
    """
    if not isinstance(data, dict):
        return None
    for key in ("table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_{0}.ProtocolType",
                "table.RemoteDevice[{0}].ProtocolType"):
        value = data.get(key.format(channel))
        if value is None:
            continue
        value = str(value).strip().lower()
        return value or None
    return None


def is_onvif_channel(data: dict, channel: int) -> bool:
    """True when the recorder reaches this camera over ONVIF rather than Dahua.

    Such a channel is not served on the recorder's own Dahua paths. Measured on
    a DHI-NVR5464-16P-EI, same recorder, same request, same minute:

        index 10  ch=11  Onvif    snapshot.cgi -> 400 Bad Request, no image
        index  1  ch=2   Private  snapshot.cgi -> 200, 1,420,074 bytes
        index 11  ch=12  Private  snapshot.cgi -> 200,   175,172 bytes

    Its RTSP path times out as well. So the camera exists, streams, and is
    visible to the recorder -- and nothing this integration asks for reaches it.
    """
    return remote_device_protocol(data, channel) == "onvif"


# The codes whose Pulse carries a `Data.State` that decides the sensor. Every
# other Pulse is a moment -- something happened -- and reading a State out of one
# that has none produced 0, which was then treated as "not ringing" and wrote the
# sensor off. Thirteen of the forty-two selectable codes are Pulse shaped, so
# thirteen sensors could never turn on (#573, and part of #336 and #456).
#
# `DoorbellPressed` is what BackKeyLight and PhoneCallDetect are translated to.
# `AccessControl` belongs here too and is easy to miss: its Pulse carries a State
# where 1 is a granted card and 0 is not, so treating it as a bare moment would
# raise the sensor on a *refused* card. A pre-existing test caught that.
# `DoorStatus` is handled separately above, on Open and Close rather than State.
PULSE_STATE_CODES = frozenset({"DoorbellPressed", "AccessControl"})

# BackKeyLight State values that mean the doorbell is ringing. See
# myhomeiot/DahuaVTO, which documents the wider set: 4 voice message,
# 5 answered from the VTH, 6 not answered, 7 VTH calling the VTO, 8 unlock,
# 9 unlock failed, 11 rebooted. Only a ring should raise the button sensor.
DOORBELL_RINGING_STATES = frozenset({1, 2})

# How many recent events diagnostics keeps per device.
#
# Ten rather than fifty: an ANPR event measured about 5 KB, so ten is a diagnostics
# file somebody can still attach, and the questions this answers, which Code did
# the device send and which field carries the state, are answered by the last few
# rather than by a history.
RECENT_EVENT_COUNT = 10


# BackKeyLight State values that are not about ringing at all, and the event
# each one deserves. Measured on a VTO2000A: opening the door through the
# integration produces State 8 within a second, and no AccessControl event.
# 9 is documented by myhomeiot/DahuaVTO as the failed counterpart.
DOORBELL_STATE_EVENTS = {8: "DoorUnlocked", 9: "DoorUnlockFailed"}


def doorbell_state(event: dict):
    """The BackKeyLight State as an int, or None if it did not say.

    The payload is JSON over DHIP, so this is normally already an int, but
    nothing guarantees it and a string must not read as a different state.
    """
    value = event.get("Data", {}).get("State") if isinstance(event.get("Data"), dict) else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def door_index(event: dict) -> int:
    """Which door a VTO DoorStatus event is about.

    The door number arrives in the event's `Index`, 0-based. A VTO paired with
    an access control extension module has a second door and reports it as 1
    (#488).

    Anything missing, negative or unreadable is the first door. That is what a
    single-door VTO sends -- and `Index: -1` is what the same device puts on a
    BackKeyLight event, so a negative is "not a door number" rather than a door.
    """
    try:
        index = int(event.get("Index"))
    except (TypeError, ValueError):
        return 0
    return max(0, index)


WHITE_LIGHT_SCHEME = "WhiteMode"


def scheme_blocking_white_light(data: dict, channel: int, profile_mode):
    """The LightingScheme mode stopping the white light, or None if nothing is.

    Writing the right Lighting_V2 row is not always enough. On Smart Dual Light
    cameras a separate setting decides which emitter the camera is willing to
    use, and while it reads AIMode or InfraredMode the white light stays off
    whatever is written -- the value is stored, Home Assistant reports the light
    on, and nothing lights up. Measured on a DH-IPC-HFW3449E-S-IL in #647.

    Returns None when the device reports no scheme at all, which is every camera
    that predates this: absent is not blocking.
    """
    mode = data.get(
        "table.LightingScheme[{0}][{1}].LightingMode".format(channel, profile_mode)
    )
    if mode is None or mode == WHITE_LIGHT_SCHEME:
        return None
    return mode


def illuminator_light_index(data: dict, channel: int, profile_mode) -> int:
    """Which Lighting_V2 light index is the white illuminator on this device.

    This was hardcoded to 0, and on most cameras 0 is the white light. On some
    it is not: the HFW3449E-S-IL in #647 reports index 0 as `InfraredLight` and
    the white light at 1. Driving 0 there turns the *infrared* emitter up and
    down -- the write is accepted, the config changes, and the user sees nothing
    happen, because infrared is invisible. The white light is never touched.

    Only moves off 0 when the device positively says 0 is something other than
    the white light, so a device that reports no LightType keeps exactly the
    behaviour it has always had.
    """
    key = "table.Lighting_V2[{0}][{1}][{2}].LightType"
    declared = data.get(key.format(channel, profile_mode, 0))
    if declared is None or declared == WHITE_LIGHT:
        return 0
    for index in range(1, MAX_LIGHTING_V2_LIGHTS):
        if data.get(key.format(channel, profile_mode, index)) == WHITE_LIGHT:
            return index
    # It says 0 is not the white light and names no other. Changing the index on
    # that basis would be a guess, and the old behaviour is the better guess.
    return 0


def describe_update_failure(exception: BaseException) -> str:
    """A short phrase naming why a poll failed, for a log line and the UI.

    `str()` on the exceptions this actually raises is very often empty --
    `asyncio.TimeoutError` and most `aiohttp.ClientError` subclasses carry no
    message -- so formatting one straight into a log gives the reader a blank
    where the cause should be. Falling back to the class name is the difference
    between "TimeoutError" and nothing at all.
    """
    return str(exception).strip() or type(exception).__name__


def jittered(seconds: float, fraction: float = EVENT_STREAM_JITTER) -> float:
    """Spread a shared interval so simultaneous callers stop being simultaneous."""
    if seconds <= 0 or fraction <= 0:
        return seconds
    spread = seconds * fraction
    return max(1.0, seconds + random.uniform(-spread, spread))

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.set_ciphers("DEFAULT")
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

_LOGGER: logging.Logger = logging.getLogger(__package__)

def get_configured_events(entry: ConfigEntry) -> list:
    """Returns the events this entry subscribes to. Never None.

    Options win when present, so the subscription can be changed after setup,
    and an empty selection there is honoured because the user chose it. The
    setup-time value in `data` is honoured the same way, empty included.

    Neither present is a different thing, and it used to return None. That is
    reachable by an entry old enough to predate the setting, and None is not a
    list: `binary_sensor.async_setup_entry` iterates this without a guard, so
    the whole platform raised TypeError and the device got **no binary sensors
    at all** -- while `async_start_event_listener` quietly skipped the stream
    because it does guard, so no events either, and nothing in the log tying the
    two together. DEFAULT_EVENTS is what such an entry would be created with
    today.
    """
    if CONF_EVENTS in entry.options:
        return list(entry.options[CONF_EVENTS] or [])
    if CONF_EVENTS in entry.data:
        return list(entry.data[CONF_EVENTS] or [])
    return list(DEFAULT_EVENTS)


def get_configured_use_https(entry: ConfigEntry):
    """Whether this entry forces HTTPS.

    None means "decide from the port", which is what the client did before the
    option existed: HTTPS only on 443. An unticked box must keep that, so it
    reads as None rather than False.
    """
    return True if entry.data.get(CONF_USE_HTTPS) else None


def get_configured_scan_interval(entry: ConfigEntry) -> timedelta:
    """Returns how often this entry polls its device.

    Options win when present. Values below MIN_SCAN_INTERVAL are raised to it,
    so a hand-edited entry cannot hammer the device.
    """
    seconds = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = DEFAULT_SCAN_INTERVAL
    return timedelta(seconds=max(seconds, MIN_SCAN_INTERVAL))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(STARTUP_MESSAGE)

    # Before anything reads the entry's identity. Costs nothing unless the id is md5
    # shaped, and never raises: a camera that cannot be identified better keeps the
    # identity it has rather than failing to set up.
    try:
        await async_migrate_synthesised_unique_id(hass, entry)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("Could not re-identify %s from the network",
                      entry.data.get(CONF_ADDRESS), exc_info=True)

    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    address = entry.data.get(CONF_ADDRESS)
    port = int(entry.data.get(CONF_PORT))
    rtsp_port = int(entry.data.get(CONF_RTSP_PORT))
    events = get_configured_events(entry)
    name = entry.data.get(CONF_NAME)
    channel = entry.data.get(CONF_CHANNEL, 0)
    use_https = get_configured_use_https(entry)

    coordinator = DahuaDataUpdateCoordinator(hass, entry=entry, events=events, address=address, port=port,
                                             rtsp_port=rtsp_port, username=username, password=password, name=name,
                                             channel=channel, use_https=use_https)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # The coordinator opens a session and takes a reference on the host's
        # shared connection pool in its constructor, and only async_stop gives
        # them back. Nothing reaches async_stop unless the coordinator makes it
        # into hass.data, which a failed setup never does -- so without this,
        # every retry against a device that is not answering leaks one session
        # and one reference, forever, and Home Assistant retries forever.
        await coordinator.async_stop()
        raise

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # https://developers.home-assistant.io/docs/config_entries_index/
    # Forward every platform in one call. Home Assistant gathers them into
    # concurrent tasks, so one call sets all of them up at once; calling it once
    # per platform instead serialised them, and an entry's setup budget then had
    # to cover the sum of six platforms rather than the slowest one. A device
    # answering slowly could exhaust it and take the whole entry down with a
    # CancelledError -- see #513.
    coordinator.platforms.extend(p for p in PLATFORMS if entry.options.get(p, True))
    if coordinator.platforms:
        await hass.config_entries.async_forward_entry_setups(entry, coordinator.platforms)

    # Wrapped, because unloading does not clear an entry's update listeners.
    # A plain add_update_listener leaves one behind on every reload, and then a
    # single options change fires as many reloads as the entry has ever had --
    # against an NVR, exactly the burst that wedges it.
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_stop)
    )

    return True


# Every config entry for one NVR used to open its own connection pool, so the
# channels of a single device never reused a connection between them. Share one
# pool per address instead, reference counted so the last entry to unload closes
# it. Sessions stay per entry; only the connector underneath is shared.
_HOST_CONNECTORS: dict = {}


# How many consecutive failed refreshes before we call a host unreachable. At
# the default 30s interval that is about two and a half minutes, long enough to
# ride out a single dropped poll.
UNREACHABLE_AFTER_FAILURES = 5

# A TCP probe is cheap but not free, and a wedged host fails every single poll.
HTTPS_PROBE_MIN_INTERVAL = 600

ISSUE_UNREACHABLE = "unreachable_{0}"
ISSUE_HTTP_DEAD_HTTPS_AVAILABLE = "http_dead_https_available_{0}"
# Raised when an entry is removed and other entries for the same recorder are
# still configured. An NVR is one entry per channel, so "remove the recorder"
# is eleven deletions and nobody realises until they are eight in.
# Raised when a channel the user ticked on the channels step could not be added.
# That step spawns one import-sourced flow per channel, and an import flow renders
# no card at all, so its abort was invisible: somebody ticked sixteen channels, got
# twelve, and had nothing anywhere telling them which four or why.
ISSUE_CHANNEL_NOT_ADDED = "channel_not_added_{0}_{1}"

ISSUE_SIBLINGS_REMAIN = "siblings_remain_{0}"
# Raised when the removed entry was referenced by automations or scripts. Those
# break silently -- the entities simply stop existing and nothing fires.
ISSUE_REMOVAL_BROKE_THINGS = "removal_broke_things_{0}"

# address -> {"consecutive": int, "since": float, "entry_ids": set, "last_probe": float}
#
# Module level rather than on the coordinator, for two reasons. A failed setup
# never publishes its coordinator to hass.data, because
# async_config_entry_first_refresh raises first, so every retry would build and
# discard a fresh counter. And an NVR has one config entry per channel, so the
# count must be shared or eight channels of one box raise eight separate cards.
_HOST_FAILURES: dict = {}

# Whether a device numbers its channels from zero, decided once for the whole
# device rather than once per config entry.
#
# Every entry used to probe this for itself, and the probe is the same URL for
# all of them -- snapshot.cgi?channel=0. Six entries on one recorder therefore
# fired six identical requests at setup, through a semaphore two wide, with the
# timeout covering the wait for a slot as well as the request. On a busy XVR
# some won and some timed out, and a timeout was read as a definite no:
#
#     PROBE_FAILED = (ClientError, TimeoutError)
#
# so those entries kept channel + 1 and pointed at the next channel's video
# while their neighbours pointed at their own. That is #724: channels
# duplicating, cameras vanishing, and a different arrangement on every
# restart, because which entry won the race changed each time.
#
# Keyed by device rather than address because one address can answer for more
# than one device, the same reason the digest state is keyed that way.
# Statuses that mean "this device does not serve that here", as opposed to a device that
# cannot be reached. A 400 is what a recorder returns for a capability endpoint it does not
# implement on a channel: measured on a DHI-NVR5464, where `coaxialControlIO.cgi` has
# answered 400 on seven different channels, and it is the same shape as `LightingScheme`
# answering 400 on recorders. 404 and 501 are the CGI-absent pair already used for the
# event and config endpoints.
CAPABILITY_REFUSED = (400, 404, 501)

# Which (device, channel) pairs have already had a capability refusal reported, so a
# recorder that refuses on every poll produces one warning rather than one per poll. It
# refused 949 times in nine hours here, each one a WARNING, which buries everything else.
_CAPABILITY_REFUSALS_REPORTED: set = set()

_HOST_CHANNEL_BASE: dict = {}
_HOST_CHANNEL_BASE_LOCKS: dict = {}

# What the network probe said about a host, and the lock that stops a recorder's eleven
# entries all asking at once. `None` is cached as an answer: a device that did not reply
# will not reply for the next channel either, and re-probing eleven times would spend
# eleven timeouts on it.
_HOST_NETWORK_IDENTITY: dict = {}
_HOST_NETWORK_IDENTITY_LOCKS: dict = {}

# A synthesised identity is md5 hex, and a channel above zero carries its index. Matched
# rather than guessed at, because a real Dahua serial is shorter, upper case and not hex:
# BC0A198PAJ779DF against 4f3a9c8ecafe4f3a9c8ecafe4f3a9c8e.
_SYNTHESISED_UNIQUE_ID = re.compile(r"^[0-9a-f]{32}(?:_\d+)?$")


def normalize_address(address: str) -> str:
    """One device, one key.

    DahuaClient rstrips the address before keying its request limiter, but
    _acquire_connector did not, so a trailing slash could leave a single device
    holding two differently keyed pools. Everything host scoped goes through
    this.
    """
    return (address or "").strip().rstrip("/")


def _entries_for_address(hass: HomeAssistant, address: str) -> list:
    """Every config entry pointing at this host."""
    wanted = normalize_address(address)
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if normalize_address(entry.data.get(CONF_ADDRESS)) == wanted
    ]


async def _async_probe_tcp(address: str, port: int, timeout: float = 5.0) -> bool:
    """Can we open a TCP connection? No HTTP, no credentials, no retry."""
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port), timeout
        )
        return True
    except Exception:  # pylint: disable=broad-except
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pylint: disable=broad-except
                pass


async def _async_evaluate_host(hass: HomeAssistant, address: str) -> None:
    """Decide which card, if any, this host has earned.

    Some Dahua firmwares stop serving plain HTTP while HTTPS keeps working. If
    that is what happened we can say so and offer to switch. Otherwise all we
    can honestly report is that the device is not answering.
    """
    address = normalize_address(address)
    unreachable_id = ISSUE_UNREACHABLE.format(address)
    https_id = ISSUE_HTTP_DEAD_HTTPS_AVAILABLE.format(address)

    entries = _entries_for_address(hass, address)
    if not entries:
        return

    # Nothing to offer if this host is already reached over HTTPS.
    already_https = any(
        str(entry.data.get(CONF_PORT)) == "443" or entry.data.get(CONF_USE_HTTPS)
        for entry in entries
    )

    state = _HOST_FAILURES.get(address)
    https_is_open = False
    if not already_https and state is not None:
        state["last_probe"] = time.time()
        https_is_open = await _async_probe_tcp(address, 443)

    minutes = 1
    if state:
        minutes = max(1, int((time.time() - state.get("since", time.time())) / 60))

    placeholders = {
        "address": address,
        "entries": str(len(entries)),
        "minutes": str(minutes),
        "port": str(entries[0].data.get(CONF_PORT, "80")),
    }

    if https_is_open:
        ir.async_delete_issue(hass, DOMAIN, unreachable_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            https_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="http_dead_https_available",
            translation_placeholders=placeholders,
            data={"address": address},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, https_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            unreachable_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="device_unreachable",
            translation_placeholders=placeholders,
            learn_more_url="https://github.com/rroller/dahua#debugging",
        )


@callback
def async_record_host_failure(hass: HomeAssistant, address: str, entry_id: str) -> int:
    """Note a failed refresh, raising a card once it stops looking like a blip.

    Returns how many consecutive failures this host has now had, which is what
    the caller backs off on. Keyed by host rather than by entry: eleven channels
    of one NVR are eleven witnesses to a single outage, not eleven outages.
    """
    address = normalize_address(address)
    state = _HOST_FAILURES.setdefault(
        address,
        {"consecutive": 0, "since": time.time(), "entry_ids": set(), "last_probe": 0},
    )
    state["consecutive"] += 1
    state["entry_ids"].add(entry_id)

    if state["consecutive"] < UNREACHABLE_AFTER_FAILURES:
        return state["consecutive"]
    # Re-evaluate on the threshold, then only as often as the probe interval
    # allows, so a wedged host does not get probed on every poll.
    if state["consecutive"] == UNREACHABLE_AFTER_FAILURES or (
        time.time() - state.get("last_probe", 0) >= HTTPS_PROBE_MIN_INTERVAL
    ):
        hass.async_create_task(_async_evaluate_host(hass, address))
    return state["consecutive"]


@callback
def async_host_is_unreachable(address: str) -> bool:
    """Whether this host has failed enough polls to be called unreachable.

    The same count and threshold the unreachable repair card is raised on, so
    what an entity says about a device and what the card says about it cannot
    disagree. One dropped poll is not an answer to this question.
    """
    state = _HOST_FAILURES.get(normalize_address(address))
    return bool(state and state["consecutive"] >= UNREACHABLE_AFTER_FAILURES)


def is_synthesised_identity(value) -> bool:
    """Whether an identity is a hash of the credentials rather than a device serial.

    A real Dahua serial is shorter, upper case and not hex: `BC0A198PAJ779DF` against
    `4f3a9c8ecafe4f3a9c8ecafe4f3a9c8e`. The optional suffix is the channel index, which a
    unique_id carries and a bare serial does not.
    """
    return bool(value) and bool(_SYNTHESISED_UNIQUE_ID.match(str(value)))


async def async_network_identity(hass, address: str) -> dict:
    """What the device says about itself over DHDiscover, asked once per host.

    Credential free, which is the whole point: the devices that need this are the ones
    that will not answer `magicBox.cgi`, and the identity those get today is
    md5(address_rtspport_username_password). Change the password and the same physical
    camera becomes a different device (#805, and #320 from the user's side).

    UDP on the local subnet, so a camera behind a router answers nothing and keeps the
    hash. An empty answer is cached for exactly that reason: it will be empty for the next
    channel too, and re-probing would spend one timeout per entry on a recorder.
    """
    if address in _HOST_NETWORK_IDENTITY:
        return _HOST_NETWORK_IDENTITY[address]

    lock = _HOST_NETWORK_IDENTITY_LOCKS.get(address)
    if lock is None:
        lock = _HOST_NETWORK_IDENTITY_LOCKS[address] = asyncio.Lock()

    async with lock:
        # Another entry may have settled it while this one waited.
        if address in _HOST_NETWORK_IDENTITY:
            return _HOST_NETWORK_IDENTITY[address]
        # Imported here because discovery imports nothing from us but config_flow does.
        from .discovery import async_probe
        found = await async_probe(address)
        _HOST_NETWORK_IDENTITY[address] = found
        return found


async def async_migrate_synthesised_unique_id(hass, entry) -> None:
    """Swap a credentials-derived unique_id for the serial the network offers.

    Only ever touches an entry whose id is md5 shaped, so a device that answered
    `magicBox.cgi` costs nothing here, not even the probe.

    Deliberately does **not** merge or delete anything. If the serial is already held by
    another entry then this camera is configured twice, which is #320, and both entries
    have their own entities and history. Saying so and leaving them alone is the only safe
    thing an automatic migration can do.
    """
    unique_id = entry.unique_id
    if not is_synthesised_identity(unique_id):
        return

    address = entry.data.get(CONF_ADDRESS)
    if not address:
        return

    found = await async_network_identity(hass, address)
    serial = (found or {}).get("SerialNo")
    if not serial:
        _LOGGER.debug(
            "%s still has a synthesised id and the network probe offered no serial, so "
            "it keeps the one it has", address)
        return

    # Imported here because config_flow imports this module.
    from .config_flow import channel_unique_id
    wanted = channel_unique_id(serial, entry.data.get(CONF_CHANNEL, 0))
    if wanted == unique_id:
        return

    # No need to exclude this entry: its own id is md5 shaped and `wanted` is a device
    # serial, and the one case where they are equal already returned above.
    taken = [other for other in hass.config_entries.async_entries(DOMAIN)
             if other.unique_id == wanted]
    if taken:
        _LOGGER.warning(
            "%s reports serial %s over the network, but the entry %s already holds that "
            "identity, so this camera is configured twice. Leaving both alone: remove "
            "whichever one you do not want rather than have this pick for you",
            address, serial, taken[0].title)
        return

    _LOGGER.info(
        "%s was identified by a hash of its own credentials, which changes whenever the "
        "password does. The network probe reports serial %s, so this entry is being moved "
        "onto it and will survive a credential change from now on", address, serial)
    hass.config_entries.async_update_entry(entry, unique_id=wanted)


async def async_device_is_zero_indexed(client, device: str):
    """Whether this device numbers its channels from zero.

    Asked once per device and shared, so the entries do not race each other
    to the same answer and do not spend six requests arriving at it.

    Returns True, False, or None when the device did not say. None is the
    point of this function: a timeout or a dropped connection is not the
    device telling us it is one-indexed, it is the device not answering, and
    treating those alike is what renumbered a working channel.

    Only an HTTP status counts as a no. That is the device answering.
    """
    if device in _HOST_CHANNEL_BASE:
        return _HOST_CHANNEL_BASE[device]

    lock = _HOST_CHANNEL_BASE_LOCKS.get(device)
    if lock is None:
        lock = _HOST_CHANNEL_BASE_LOCKS[device] = asyncio.Lock()

    async with lock:
        # Another entry may have settled it while this one waited.
        if device in _HOST_CHANNEL_BASE:
            return _HOST_CHANNEL_BASE[device]
        try:
            await client.async_probe_snapshot(0)
            _HOST_CHANNEL_BASE[device] = True
        except ClientResponseError:
            # The device answered, and the answer was no.
            _HOST_CHANNEL_BASE[device] = False
        except (ClientError, TimeoutError, asyncio.TimeoutError):
            # It did not answer. Decide nothing and leave it for the next
            # entry or the next restart, rather than caching a guess that
            # every other channel would then inherit.
            _LOGGER.debug(
                "%s did not answer the channel numbering probe; leaving it undecided",
                device, exc_info=True)
            return None
    return _HOST_CHANNEL_BASE[device]


@callback
def async_record_host_auth_refusal(address: str) -> int:
    """Note that this host refused the credentials, and say how often it has.

    Keyed by host rather than by entry because the consequence is host-wide:
    a Dahua box locks the source IP after repeated failed logins, so ten
    channels of one NVR are ten entries renewing one lock. A per-entry count
    would let the first entry to notice give up while the other nine kept the
    lock alive, which is the bug one level up.

    Cleared by async_record_host_success, so this only ever counts refusals
    with nothing succeeding in between.
    """
    address = normalize_address(address)
    state = _HOST_FAILURES.setdefault(
        address,
        {"consecutive": 0, "since": time.time(), "entry_ids": set(), "last_probe": 0},
    )
    state["auth_refusals"] = state.get("auth_refusals", 0) + 1
    return state["auth_refusals"]


@callback
def async_record_host_success(hass: HomeAssistant, address: str) -> None:
    """The device answered, so withdraw anything we said about it.

    Keyed by host: if any channel of an NVR replies, the box is up.
    """
    address = normalize_address(address)
    if _HOST_FAILURES.pop(address, None) is None:
        return
    ir.async_delete_issue(hass, DOMAIN, ISSUE_UNREACHABLE.format(address))
    ir.async_delete_issue(hass, DOMAIN, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE.format(address))


def _acquire_connector(address: str) -> TCPConnector:
    """Returns the shared connector for this address, creating it if needed."""
    address = normalize_address(address)
    holder = _HOST_CONNECTORS.get(address)
    if holder is None or holder[0].closed:
        # enable_cleanup_closed is deliberately not set: aiohttp ignores it on
        # every Python that Home Assistant now runs on, and warns once per
        # connector in the user's log for the trouble.
        holder = [TCPConnector(ssl=SSL_CONTEXT), 0]
        _HOST_CONNECTORS[address] = holder
    holder[1] += 1
    return holder[0]


async def _release_connector(address: str) -> None:
    """Drops a reference, closing the connector once nothing is using it."""
    address = normalize_address(address)
    holder = _HOST_CONNECTORS.get(address)
    if holder is None:
        return
    holder[1] -= 1
    if holder[1] <= 0:
        _HOST_CONNECTORS.pop(address, None)
        clear_host_cache(address)
        await holder[0].close()


class DahuaHostEventStream:
    """One event stream for a host, shared by every channel configured on it.

    The device's event stream is not per channel: attaching to it returns every
    channel's events regardless of who asked. An NVR with eleven channels was
    therefore holding eleven identical streams and having ten of them throw each
    event away. This holds one, and hands each event to the channels that want
    it.
    """

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self._hass = hass
        self._address = address
        # channel index -> coordinators listening on that channel
        self._by_channel: Dict[int, list] = {}
        self._owner = None  # whose client the stream currently borrows
        self._events: frozenset = frozenset()
        self._task: asyncio.Task | None = None
        # Whether the last attach failed, so an outage is reported once.
        self._failing = False
        # How many times running the stream has died on contact, which is what
        # the retry delay backs off on.
        self._consecutive_failures = 0
        # Whether the device sent anything on the current attach. Reset per
        # attempt, so it describes this stream and not the one before it.
        self._received_data = False

    @property
    def coordinators(self) -> list:
        return [c for group in self._by_channel.values() for c in group]

    def _union(self) -> frozenset:
        """Every event any channel on this host asked for.

        Attaching with one channel's list would silently stop delivering the
        codes another channel selected.
        """
        union = set()
        for coordinator in self.coordinators:
            union.update(coordinator.events or [])
        return frozenset(union)

    def register(self, coordinator) -> None:
        self._by_channel.setdefault(coordinator.get_channel(), []).append(coordinator)
        if self._owner is None:
            self._owner = coordinator
        self._restart_if_needed()

    async def unregister(self, coordinator) -> bool:
        """Drop a channel. Returns True when nothing is left on this host."""
        group = self._by_channel.get(coordinator.get_channel(), [])
        if coordinator in group:
            group.remove(coordinator)
        if not group:
            self._by_channel.pop(coordinator.get_channel(), None)

        remaining = self.coordinators
        if not remaining:
            await self.async_stop()
            return True

        # The stream borrows the owner's client, and unloading an entry closes
        # its session, so hand the stream to someone still here.
        if coordinator is self._owner:
            self._owner = remaining[0]
            self._events = frozenset()  # force a restart on the new client
        self._restart_if_needed()
        return False

    def _restart_if_needed(self) -> None:
        wanted = self._union()
        if self._task is not None and not self._task.done() and wanted == self._events:
            return
        self._events = wanted
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if wanted and self._owner is not None:
            self._task = asyncio.create_task(self._async_run())

    async def async_stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._by_channel.clear()
        self._owner = None
        self._events = frozenset()

    async def _async_run(self) -> None:
        """Hold the stream open, recycling it the way a single channel used to."""
        while True:
            start_time = time.monotonic()
            self._received_data = False
            try:
                await asyncio.wait_for(
                    self._owner.client.stream_events(
                        self.on_receive, sorted(self._events), 0
                    ),
                    timeout=jittered(EVENT_STREAM_MAX_LIFETIME_SECONDS),
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # Two opposite things raise this. The wait_for above fires at
                # EVENT_STREAM_MAX_LIFETIME_SECONDS, which is the deliberate
                # recycle of a stream that has been working. aiohttp's sock_read
                # timeout fires when the socket has delivered nothing for
                # EVENT_STREAM_READ_TIMEOUT_SECONDS -- and ServerTimeoutError is
                # a TimeoutError, so it lands in the same place. Whether anything
                # arrived is what tells them apart.
                if self._received_data:
                    self._failing = False
                    self._consecutive_failures = 0
                    _LOGGER.debug("Recycling event stream for %s", self._address)
                else:
                    self._consecutive_failures += 1
                    if not self._failing:
                        self._failing = True
                        _LOGGER.warning(
                            "Event stream for %s attached but delivered nothing, not "
                            "even the heartbeat it asks for, so no events will arrive "
                            "from it. Some firmware stops matching anything when too "
                            "many event types are subscribed at once: selecting fewer "
                            "event types for this device is the first thing to try.",
                            self._address,
                        )
                    else:
                        _LOGGER.debug(
                            "Event stream for %s still silent", self._address
                        )
            except Exception as ex:  # pylint: disable=broad-except
                # Credentials the device is refusing must stop being offered
                # here too, not just on the poll. This stream backs off to at
                # most EVENT_STREAM_MAX_RETRY_SECONDS, which is well inside
                # the half hour a Dahua box locks a source IP for -- so on its
                # own it would keep renewing the lock that #729 is about, and
                # the reauth the coordinator asked for would still be refused.
                #
                # The count is shared with the polls and cleared by any
                # success on this host, so a stream cannot reach the budget
                # while anything here is still authenticating.
                if isinstance(ex, ClientResponseError) and ex.status == 401:
                    refusals = async_record_host_auth_refusal(self._address)
                    if refusals >= MAX_AUTH_REFUSALS:
                        _LOGGER.warning(
                            "Event stream for %s stopped: the device refused these credentials %d times. It will start again once the credentials are re-entered",
                            self._address, refusals,
                        )
                        return
                # Say it once per outage, not once per retry. Silence was the
                # old behaviour and it is why these failures went unreported;
                # a warning every sixty seconds forever is the other extreme.
                if self._received_data:
                    # It attached and it talked; the socket ending is not this
                    # device refusing contact, so the instant-death counter must
                    # not climb on it.
                    self._consecutive_failures = 0
                else:
                    self._consecutive_failures += 1
                if not self._failing:
                    self._failing = True
                    _LOGGER.warning(
                        "Event stream for %s ended unexpectedly: %s", self._address, ex
                    )
                else:
                    _LOGGER.debug(
                        "Event stream for %s still failing: %s", self._address, ex
                    )
            else:
                self._failing = False
                self._consecutive_failures = 0

            retry_in = event_stream_retry_delay(
                stream_lifetime(time.monotonic() - start_time, self._received_data),
                self._consecutive_failures,
                self._received_data,
            )
            if retry_in:
                _LOGGER.debug(
                    "Reconnecting to event stream for %s in %.0fs",
                    self._address,
                    retry_in,
                )
                await asyncio.sleep(retry_in)
            else:
                _LOGGER.debug("Reconnecting to event stream for %s", self._address)

    def on_receive(self, data_bytes: bytes, _channel: int) -> None:
        """Parse once, then hand each event only to the channels that want it."""
        # Before the parse, deliberately: a heartbeat carries no event but is
        # still the device talking, and that is what the retry delay needs to
        # know. A camera sitting quietly with nothing to report is not a camera
        # refusing to attach.
        self._received_data = True

        events = parse_event(data_bytes.decode("utf-8", errors="ignore"))
        if not events:
            return

        for event in events:
            index = 0
            if "index" in event:
                try:
                    index = int(event["index"])
                except ValueError:
                    index = 0

            # AlarmLocal numbers its `index` from the physical alarm input
            # terminal, not the video channel -- so on a host with a single
            # configured channel it can legitimately be nonzero (e.g. index=1)
            # while that channel is 0. There is no channel to disambiguate
            # when only one is configured, so the index there means something
            # else and must not be used to drop the event. See #231.
            #
            # Guarded on the number of *channels*, not the number of
            # coordinators: two config entries can share one channel (see
            # test_two_entries_on_one_channel_both_get_it), and both must
            # still get the event. Every other event code, and every host
            # with more than one channel configured, keeps the existing
            # per-index filtering below untouched: a channel nobody
            # configured must stay silent.
            if event.get("Code") == "AlarmLocal" and len(self._by_channel) == 1:
                for coordinator in next(iter(self._by_channel.values())):
                    try:
                        coordinator.handle_event(dict(event))
                    except Exception:  # pylint: disable=broad-except
                        # Same reach as the guard below (#706): this stream is
                        # shared by every channel on the host, and
                        # stream_events wraps its call to on_receive in
                        # try/finally with no handler, so an unguarded
                        # exception here would take every camera on the
                        # device down until the retry reconnects, not just
                        # drop this one event.
                        #
                        # The index is deliberately not logged as a channel:
                        # for AlarmLocal it is the alarm input, which is the
                        # whole reason this branch exists.
                        _LOGGER.warning(
                            "Unhandled error while handling a %s event from %s; "
                            "the event is dropped and the stream continues",
                            event.get("Code", "?"), self._address, exc_info=True,
                        )
                continue

            # A channel nobody has configured stays silent, exactly as it did
            # when every coordinator discarded it.
            for coordinator in self._by_channel.get(index, ()):
                try:
                    coordinator.handle_event(dict(event))
                except Exception:  # pylint: disable=broad-except
                    # This stream is shared by every channel on the host, and
                    # stream_events wraps its call to on_receive in try/finally
                    # with no handler -- so an exception here does not just lose
                    # this event, it leaves the read loop and takes events for
                    # every camera on the device down until the retry
                    # reconnects. One malformed payload did exactly that (#475).
                    #
                    # Per coordinator rather than per event, so a channel whose
                    # handler fails does not rob the other channels of an event
                    # they could have handled.
                    _LOGGER.warning(
                        "Unhandled error while handling a %s event from %s on channel %s; "
                        "the event is dropped and the stream continues",
                        event.get("Code", "?"), self._address, index, exc_info=True,
                    )


# address -> DahuaHostEventStream
_HOST_STREAMS: Dict[str, DahuaHostEventStream] = {}


def _host_stream(hass: HomeAssistant, address: str) -> DahuaHostEventStream:
    address = normalize_address(address)
    stream = _HOST_STREAMS.get(address)
    if stream is None:
        stream = _HOST_STREAMS[address] = DahuaHostEventStream(hass, address)
    return stream


async def _release_host_stream(coordinator) -> None:
    address = normalize_address(coordinator.get_address())
    stream = _HOST_STREAMS.get(address)
    if stream is None:
        return
    if await stream.unregister(coordinator):
        _HOST_STREAMS.pop(address, None)


class DahuaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, events: list, address: str, port: int, rtsp_port: int,
                 username: str, password: str, name: str, channel: int,
                 use_https: bool = None) -> None:
        """Initialize the coordinator."""
        # Self signed certs are used over HTTPS so we'll disable SSL verification.
        # connector_owner=False keeps the shared pool alive when this session closes.
        self._session = ClientSession(
            connector=_acquire_connector(address), connector_owner=False
        )

        # The client used to communicate with Dahua devices
        self.client: DahuaClient = DahuaClient(username, password, address, port, rtsp_port, self._session,
                                               use_https,
                                               use_rpc2=entry.options.get(CONF_USE_RPC2, False),
                                               illuminator_restore_store=IlluminatorRestoreStore(
                                                   hass, entry.entry_id
                                               ))

        # self.config_entry = entry
        self.platforms = []
        self.initialized = False
        self.model = ""
        self._firmware_version = ""
        # What the device calls itself, "" when it did not answer. See
        # async_get_device_class.
        self._device_class = ""
        self._siren_detection_sources = []
        self._security_light_detection_sources = []
        self._siren_detection_failures = []
        self._security_light_detection_failures = []
        self.connected = None
        self.events: list = events
        self._supports_coaxial_control = False
        # Doorbell call states already complained about, so the warning below is
        # one per state rather than one per ring.
        self._unknown_doorbell_states: set = set()
        self._supports_rpc2_siren = False
        self._supports_rpc2_security_light = False
        self._alarm_output_slots = 0
        self._nvr_active_deterrence = entry.options.get(CONF_NVR_ACTIVE_DETERRENCE, False)
        self._manual_siren = entry.options.get(CONF_MANUAL_SIREN, False)
        self._manual_security_light = entry.options.get(CONF_MANUAL_SECURITY_LIGHT, False)
        self._supports_disarming_linkage = False
        self._supports_event_notifications = False
        # What the device's own cloud OTA check last found, read from its
        # _DHCloudUpgrade_ config table. Reading it is local; see
        # _async_probe_cloud_upgrade.
        self._supports_cloud_upgrade = False
        self._cloud_firmware_version: str | None = None
        self._ivs_rules = []
        self._supports_smart_motion_detection = False
        self._supports_ptz_position = False
        self._supports_lighting = False
        self._supports_day_night_color = False
        # What the device said when a probe failed, keyed by probe name.
        self._probe_refusals: Dict[str, dict] = {}
        self._channel_model = None
        self._supports_privacy_mode = False
        self._supports_floodlightmode = False
        self._serial_number: str
        self._profile_mode = "0"
        self._preset_position = "0"
        self._supports_profile_mode = False
        self._channel = channel
        self._address = address
        self._max_streams = 3  # 1 main stream + 2 sub-streams by default

        self._supports_lighting_v2 = False
        self._supports_lighting_scheme_illuminator = False

        # Host-wide camera/NVR reboot generation.
        # Multiple NVR channel coordinators share the host uptime read.
        self._camera_reboot_generation = 0

        # channel_number is not the channel_index. channel_number is the index + 1.
        # So channel index 0 is channel number 1. Except for some older firmwares where channel
        # and channel number are the same! We check for this in _async_update_data and adjust the
        # channel number as needed.
        self._channel_number = channel + 1

        # This is the name for the device given by the user during setup
        self._name = name

        # This is the name as reported from the camera itself
        self.machine_name = ""

        # VTO connection credentials
        self._username = username
        self._password = password

        # The CGI event stream is not here: #615 moved it to one shared
        # DahuaHostEventStream per address, keyed in _HOST_STREAMS. The
        # _event_task that used to live here stayed behind as an attribute that
        # was always None, and diagnostics went on reporting it, so
        # stream_task_running read False for every device for three weeks.
        self._vto_task: asyncio.Task | None = None
        self._vto_client: DahuaVTOClient | None = None

        # A dictionary of event name (CrossLineDetection, VideoMotion, etc) to a listener for that event
        # The key will be formed from self.get_event_key(event_name) and includes the channel
        # A list, not one listener: two entities can want the same event, and
        # assignment meant the second silently replaced the first. Only the
        # binary sensor subscribed until now, one per code, so nothing had
        # collided yet -- but a doorbell press is wanted by a binary sensor and
        # an event entity at once (#715).
        self._dahua_event_listeners: Dict[str, list] = dict()

        # A dictionary of event name (CrossLineDetection, VideoMotion, etc) to the time the event fire or was cleared.
        # If cleared the time will be 0. The time unit is seconds epoch
        self._dahua_event_timestamp: Dict[str, int] = dict()

        self._floodlight_mode = 2

        self._last_plate_data: dict = {}
        self._last_plate_timestamp: int = 0
        self._plate_listeners: list = []

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=get_configured_scan_interval(entry),
        )

    async def async_start_event_listener(self):
        """ Starts the event listeners for IP cameras (this does not work for doorbells (VTO)) """
        if self.events is not None:
            # Join this host's stream rather than opening another one. The
            # device sends every channel's events down any stream, so one is
            # enough no matter how many channels are configured.
            _host_stream(self.hass, self._address).register(self)

    async def async_start_vto_event_listener(self):
        """ Starts the event listeners for doorbells (VTO). This will not work for IP cameras"""
        self._vto_task = asyncio.create_task(self._async_stream_vto_events())

    async def _async_stream_vto_events(self):
        """Continuously stream VTO events from a doorbell, reconnecting on failure."""
        consecutive_failures = 0
        while True:
            protocol = None
            started = time.monotonic()
            try:
                _LOGGER.debug("Connecting to VTO event stream at %s", self._address)
                loop = asyncio.get_event_loop()
                _, protocol = await loop.create_connection(
                    lambda: DahuaVTOClient(
                        self._address, self._username, self._password, False, self.on_receive_vto_event
                    ),
                    host=self._address,
                    port=5000,
                )
                self._vto_client = protocol
                await protocol.disconnected
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                delay, consecutive_failures = vto_retry_state(
                    time.monotonic() - started, consecutive_failures,
                    getattr(protocol, "received_data", False))
                _LOGGER.error("VTO connection to %s failed, retrying in %ss: %s",
                              self._address, round(delay), ex)
                await asyncio.sleep(delay)
                continue

            delay, consecutive_failures = vto_retry_state(
                time.monotonic() - started, consecutive_failures, protocol.received_data)
            if delay:
                _LOGGER.warning("Disconnected from VTO at %s, reconnecting in %ss",
                                self._address, round(delay))
                await asyncio.sleep(delay)
            else:
                # It was connected and talking, so the socket ending is not the
                # device refusing us. Go straight back.
                _LOGGER.warning("Disconnected from VTO at %s, reconnecting", self._address)

    async def async_stop(self, event: Any = None):
        """ Stop anything we need to stop """
        await _release_host_stream(self)
        if self._vto_task is not None:
            self._vto_task.cancel()
            self._vto_task = None
        await self._close_session()

    async def _close_session(self) -> None:
        _LOGGER.debug("Closing Session")
        try:
            await self.client.close()
        except Exception:
            _LOGGER.debug("Failed to close the client's RPC2 session", exc_info=True)
        if self._session is not None:
            try:
                await self._session.close()
                self._session = None
            except Exception as e:
                _LOGGER.exception("serverConnect - failed to close session")
            finally:
                await _release_connector(self._address)

    def _restore_poll_interval(self) -> None:
        """Put the configured interval back after a device starts answering."""
        # Read it back from the entry rather than remembering it: the user may
        # have changed the option while we were backed off, and their new value
        # should win over whatever we were doubling from.
        configured = get_configured_scan_interval(self.config_entry)
        if self.update_interval != configured:
            _LOGGER.debug(
                "%s is answering again, polling every %ss", self._address, configured.total_seconds()
            )
            self.update_interval = configured

    def _back_off_poll_interval(self, consecutive: int) -> None:
        """Poll a device that is not answering less often, not just as often.

        Every request costs the device a connection and a login it has to
        refuse. Keeping the configured cadence against a device that is already
        refusing is what turns a device that ran out of connections into one
        that stays out of them until it is power cycled.
        """
        interval = failure_backoff(get_configured_scan_interval(self.config_entry), consecutive)
        if self.update_interval != interval:
            _LOGGER.debug(
                "%s has failed %s times, backing off to %ss",
                self._address, consecutive, interval.total_seconds(),
            )
            self.update_interval = interval

    def _wanted_by(self, *platforms: str) -> bool:
        """Whether anything that reads this answer is actually loaded.

        The poll used to fetch purely on what the device reported supporting,
        so an entry with `select` switched off still paid for a PTZ position
        read on every cycle to feed an entity that was never created. Each of
        those costs the device a connection and a login it has to refuse.

        Read from the entry options rather than `coordinator.platforms`: the
        first refresh runs before the platforms are forwarded, so that list is
        still empty then and everything would be skipped on the first poll.
        """
        return any(self.config_entry.options.get(platform, True) for platform in platforms)

    def _note_probe_refusal(self, name: str, exception) -> None:
        """Remember what the device said when a capability probe failed.

        An HTTP status is the device answering, and a 400 for a config table
        is it saying it does not serve that table. That is a fact about the
        model and it generalises. A timeout or a dropped connection is the
        device not answering, which is a fact about that moment and
        generalises to nothing.

        Both used to end at `self._supports_x = False`, which records that
        the feature is off and throws away which of the two it was. Every
        capability is remembered and every refusal is discarded, and the
        refusal is the half that says what a model will not do. Not having
        that, per model, is what the model-name matching in #570, #676 and
        #690 exists to work around.
        """
        status = getattr(exception, "status", None)
        # Self-initialising, because a probe must never be the thing that
        # raises. Coordinators are built with object.__new__ in a dozen
        # tests, which skips __init__, and a capability probe is exactly
        # the wrong place to start depending on that having run.
        refusals = getattr(self, "_probe_refusals", None)
        if refusals is None:
            refusals = self._probe_refusals = {}
        refusals[name] = {
            "answered": status is not None,
            "status": status,
            "error": type(exception).__name__,
        }

    def _auth_refused(self, exception) -> Exception:
        """What to raise when the device refuses these credentials.

        Below the budget this is an ordinary failed poll, because one 401 is
        not proof of a wrong password (#714).

        At the budget it is ConfigEntryAuthFailed, rather than calling
        async_start_reauth by hand and raising UpdateFailed. Home Assistant
        does two things for that exception and only the first was happening:
        it opens the reauth flow, and it stops scheduling refreshes --

            if not auth_failed and self._listeners and not self.hass.is_stopping:
                self._schedule_refresh()

        Stopping the polls is the part that matters. A Dahua box locks the
        source IP for thirty minutes after repeated failed logins, so an entry
        that keeps polling keeps renewing the lock, and the correct password
        typed into the reauth dialog is refused along with everything else.
        That is the loop in #729: reauth asked for, reauth impossible.
        """
        refusals = async_record_host_auth_refusal(self._address)
        if refusals < MAX_AUTH_REFUSALS:
            _LOGGER.debug(
                "Authentication refused by %s (%d of %d). Not treating it as a wrong password yet",
                self._address, refusals, MAX_AUTH_REFUSALS,
            )
            self._back_off_poll_interval(
                async_record_host_failure(self.hass, self._address, self.config_entry.entry_id)
            )
            return UpdateFailed("Authentication refused by " + self._address)
        _LOGGER.warning(
            "%s has refused these credentials %d times, so Home Assistant will stop trying them and ask for new ones. Repeated failed logins can lock a Dahua device out for around thirty minutes, so polling stops until the credentials are re-entered",
            self._address, refusals,
        )
        return ConfigEntryAuthFailed("Authentication failed for " + self._address)

    async def _async_update_data(self):
        """Reload the camera information"""
        data = {}

        # Do the one time initialization (do this when Home Assistant starts)
        if not self.initialized:
            try:
                # Find the max number of streams. 1 main stream + n number of sub-streams
                self._max_streams = await self.client.get_max_extra_streams() + 1
                _LOGGER.debug("Using max streams %s", self._max_streams)

                machine_name = await self.client.async_get_machine_name()
                sys_info = await self.client.async_get_system_info()
                version = await self.client.get_software_version()
                data.update(machine_name)
                data.update(sys_info)
                data.update(version)

                device_type = data.get("deviceType", None)
                # Kept so the chain below can fall back to it: it is generic,
                # but it beats the None a failed lookup would otherwise leave.
                reported_type = device_type
                # Lorex NVRs return deviceType=31, but the model is in the updateSerial
                # /cgi-bin/magicBox.cgi?action=getSystemInfo"
                # deviceType=31
                # processor=ST7108
                # serialNumber=ND0219110NNNNN
                # updateSerial=DHI-NVR4108HS-8P-4KS2
                if device_type in ["IP Camera", "31"] or device_type is None:
                    # Some firmwares put the device type in the "updateSerial" field. Weird.
                    device_type = data.get("updateSerial", None)
                    if device_type is None:
                        # If it's still none, then call the device type API
                        dt = await self.client.get_device_type()
                        device_type = dt.get("type")
                device_type = model_name(device_type, reported_type)
                data["model"] = device_type
                self.model = device_type
                self.machine_name = data.get("table.General.MachineName")
                self._serial_number = data.get("serialNumber")
                self._firmware_version = data.get("version") or ""

                # Ask the device what it is, before anything asks the model name.
                # Cached here rather than read from is_doorbell(), which is called
                # on every poll and from ten other places.
                try:
                    self._device_class = await self.client.async_get_device_class()
                except PROBE_FAILED as probe_error:
                    self._note_probe_refusal("device_class", probe_error)
                    self._device_class = ""
                _LOGGER.debug("Device reports class=%s", self._device_class or "<no answer>")

                # Some Dahua firmwares index channels from 0, others from 1. The default
                # is to auto-detect: if a snapshot at index 0 succeeds, treat this camera as
                # 0-indexed and reset channel_number accordingly. Users on cameras where this
                # heuristic gets it wrong (HTTP snapshot at 0 succeeds but RTSP only streams
                # on channel=1) can disable it via the integration options.
                auto_detect = self.config_entry.options.get(CONF_AUTO_DETECT_CHANNEL, True)
                if auto_detect:
                    # Asked once for the device and shared, and a device that does
                    # not answer leaves this alone rather than renumbering the
                    # channel behind the user's back (#724).
                    zero_indexed = await async_device_is_zero_indexed(
                        self.client, self.client.device_key)
                    # A doorbell is excluded because channel 0 does not exist on a VTO.
                    if zero_indexed and not self.is_doorbell():
                        self._channel_number = self._channel
                _LOGGER.debug("Using channel number %s (auto_detect=%s)", self._channel_number, auto_detect)

                await self._async_probe_direct_deterrence()
                if self._wanted_by(LIGHT, SWITCH) and not self.uses_rpc2_deterrence():
                    try:
                        coaxial_channel = self._channel_number if self.uses_recorder_deterrence() else 1
                        await self.client.async_get_coaxial_control_io_status(coaxial_channel)
                        self._supports_coaxial_control = True
                    except PROBE_REFUSED as probe_error:
                        self._note_probe_refusal("coaxial_control", probe_error)
                        self._supports_coaxial_control = False
                _LOGGER.debug("Device supports Coaxial Control=%s", self._supports_coaxial_control)

                try:
                    alarm_output_data = await self.client.async_get_alarm_output_slots()
                    try:
                        self._alarm_output_slots = max(0, int(alarm_output_data.get("result", "0")))
                    except (ValueError, TypeError):
                        self._alarm_output_slots = 0
                except PROBE_FAILED as probe_error:
                    self._note_probe_refusal("alarm_output", probe_error)
                    self._alarm_output_slots = 0
                _LOGGER.debug("Device alarm output slots=%s", self._alarm_output_slots)
                if self._alarm_output_slots > 1:
                    _LOGGER.debug(
                        "Device reports %s alarm outputs; entities are not created because "
                        "the multi-output getOutState encoding is not yet verified",
                        self._alarm_output_slots,
                    )

                try:
                    await self.client.async_get_disarming_linkage()
                    self._supports_disarming_linkage = True
                except PROBE_FAILED as probe_error:
                    self._note_probe_refusal("disarming_linkage", probe_error)
                    self._supports_disarming_linkage = False
                _LOGGER.debug("Device supports disarming linkage=%s", self._supports_disarming_linkage)

                try:
                    await self.client.async_get_event_notifications()
                    self._supports_event_notifications = True
                except PROBE_FAILED as probe_error:
                    self._note_probe_refusal("event_notifications", probe_error)
                    self._supports_event_notifications = False
                _LOGGER.debug("Device supports event notifications=%s", self._supports_event_notifications)

                await self._async_probe_cloud_upgrade()
                _LOGGER.debug(
                    "Device supports cloud upgrade=%s", self._supports_cloud_upgrade
                )

                # PTZ position readback. The SDT4E425 PTZ sensor is controllable,
                # but firmware V3.200.0000027.6.R returns HTTP 400 for CGI getStatus.
                # Do not conflate PTZ/preset control with CGI position readback.
                if is_sdt4e425(self.model):
                    self._supports_ptz_position = False
                else:
                    try:
                        await self.client.async_get_ptz_position()
                        self._supports_ptz_position = True
                    except PROBE_FAILED as probe_error:
                        self._note_probe_refusal("ptz_position", probe_error)
                        self._supports_ptz_position = False
                _LOGGER.debug("Device supports PTZ position=%s", self._supports_ptz_position)

                # Smart motion detection is enabled/disabled/fetched differently on Dahua devices compared to Amcrest
                # The following lines are for Dahua devices
                smart_motion_rows = None
                try:
                    table = await self.client.async_get_smart_motion_detection()
                    self._supports_smart_motion_detection = True
                    smart_motion_rows = smart_motion_row_indices(table)
                except PROBE_FAILED as probe_error:
                    self._note_probe_refusal("smart_motion_detect", probe_error)
                    self._supports_smart_motion_detection = False
                _LOGGER.debug("Device supports smart motion detection=%s", self._supports_smart_motion_detection)

                try:
                    remote_ivs = self.is_nvr_channel()
                    ivs_table = (
                        await self.client.async_get_remote_ivs_rules(self._channel)
                        if remote_ivs else await self.client.async_get_ivs_rules()
                    )
                    name = "RemoteVideoAnalyseRule" if remote_ivs else "VideoAnalyseRule"
                    self._ivs_rules = ivs_rules_for_channel(ivs_table, self._channel, name)
                    if remote_ivs:
                        for rule in self._ivs_rules:
                            rule["remote"] = True
                    data.update(ivs_table)
                except PROBE_FAILED + (ConnectionError, ValueError):
                    self._ivs_rules = []
                _LOGGER.debug("Device IVS rules=%s", self._ivs_rules)

                # Day/Night mode. Judged by whether this channel's row came
                # back, not by whether the request raised: async_get_config
                # swallows a ClientResponseError and returns {}, and a device
                # can answer 200 with an empty body for a table it lacks.
                try:
                    options = await self.client.async_get_video_in_options()
                    self._supports_day_night_color = (
                        day_night_color_name(options, self._channel) is not None)
                except PROBE_FAILED as probe_error:
                    self._note_probe_refusal("day_night_color", probe_error)
                    self._supports_day_night_color = False
                _LOGGER.debug("Device supports day/night mode=%s", self._supports_day_night_color)

                # Which camera is actually on this channel. Every channel of a
                # recorder reports the recorder's model, so a doorbell behind an
                # NVR is invisible as one and every model-string capability
                # check sees the wrong device. Read once at setup: RemoteDevice
                # is large and never changes between reboots, and the shared read
                # cache answers it once for all of a recorder's channels.
                try:
                    remote = await self.client.async_get_config("RemoteDevice")
                    self._channel_model = remote_device_model(remote, self._channel)
                    if is_onvif_channel(remote, self._channel):
                        # Say it once, plainly, instead of leaving a camera
                        # entity that answers 400 for the life of the entry.
                        _LOGGER.warning(
                            "Channel %s of %s is attached to the recorder over ONVIF, "
                            "not Dahua's own protocol. A recorder does not serve such a "
                            "channel on its Dahua paths -- measured on a "
                            "DHI-NVR5464-16P-EI, snapshot.cgi answers 400 for the ONVIF "
                            "channel while every Dahua-protocol channel on the same "
                            "recorder returns an image -- so video for this camera will "
                            "not work here whatever channel number is used. Home "
                            "Assistant's own ONVIF integration, pointed at the recorder "
                            "rather than at the camera, does serve it (#646).",
                            self._channel, self._address)
                except PROBE_FAILED as probe_error:
                    self._note_probe_refusal("channel_model", probe_error)
                    self._channel_model = None
                if self._channel_model:
                    _LOGGER.debug(
                        "Channel %s carries a %s; the device itself reports %s",
                        self._channel, self._channel_model, self.model)
                if self._supports_smart_motion_detection:
                    # Which rows the device reports is the whole capability
                    # decision for this channel (#635), and nothing logged it.
                    # #669 spent two rounds of guessing for want of this line,
                    # because a response body is never logged at debug.
                    _LOGGER.debug(
                        "SmartMotionDetect rows reported: %s; this channel is %s, so its "
                        "switch is %s",
                        smart_motion_rows if smart_motion_rows else "none",
                        self._channel,
                        "created" if self._channel in (smart_motion_rows or ()) else "not created",
                    )

                is_doorbell = self.is_doorbell()
                _LOGGER.debug("Device is a doorbell=%s", is_doorbell)

                is_flood_light = self.is_flood_light()
                _LOGGER.debug("Device is a floodlight=%s", is_flood_light)

                self._supports_floodlightmode = self.supports_floodlightmode()

                self._supports_lighting = await self.async_detect_lighting_support()
                _LOGGER.debug("Device supports infrared lighting=%s", self.supports_infrared_light())

#Checking lighting_v2 support
                try:
                    await self.client.async_get_lighting_v2()
                    self._supports_lighting_v2 = True
                except PROBE_FAILED as probe_error:
                    self._note_probe_refusal("lighting_v2", probe_error)
                    self._supports_lighting_v2 = False
                    pass
                _LOGGER.debug("Device supports Lighting_V2=%s", self._supports_lighting_v2)

                # IPC-Color4M-TZ accepts ordinary Lighting_V2 writes but its
                # physical white emitter also requires LightingScheme. Probe
                # that second capability before exposing the entity.
                if self.model.upper().startswith("IPC-COLOR4M-TZ"):
                    try:
                        scheme = await self.client.async_get_lighting_scheme()
                        self._supports_lighting_scheme_illuminator = any(
                            key.endswith(".LightingMode") for key in scheme
                        )
                    except (ClientError, TimeoutError, ConnectionError,
                            ValueError, KeyError, TypeError):
                        self._supports_lighting_scheme_illuminator = False
                    _LOGGER.debug(
                        "Device supports LightingScheme illuminator=%s",
                        self._supports_lighting_scheme_illuminator,
                    )

                # Checking privacy mode (LeLensMask) support. This is RPC2 only and many models lack it.
                # Deliberately broader than PROBE_FAILED: a camera without LeLensMask answers with an
                # RPC2 result=false, which surfaces as ConnectionError, and a malformed table raises
                # ValueError. Neither is a ClientError, so narrowing this would fail the whole entry.
                try:
                    await self.client.async_get_privacy_mode()
                    self._supports_privacy_mode = True
                except Exception as exception:
                    self._supports_privacy_mode = False
                    _LOGGER.debug("Privacy mode not available", exc_info=exception)
                _LOGGER.debug("Device supports privacy mode=%s", self._supports_privacy_mode)


                if not is_doorbell:
                    # Start the event listeners for IP cameras
                    await self.async_start_event_listener()

                    try:
                        # Some cams don't support profile modes, check and see... use 2 to check
                        conf = await self.client.async_get_config("Lighting[0][2]")
                        # We'll get back an error like this if it doesn't work:
                        # Error: Error -1 getting param in name=Lighting[0][1]
                        # Otherwise we'll get multiple lines of config back
                        self._supports_profile_mode = len(conf) > 1
                    except PROBE_FAILED as probe_error:
                        self._note_probe_refusal("profile_mode", probe_error)
                        _LOGGER.debug("Cam does not support profile mode. Will use mode 0")
                        self._supports_profile_mode = False
                    _LOGGER.debug("Device supports profile mode=%s", self._supports_profile_mode)
                else:
                    # Start the event listeners for doorbells (VTO)
                    await self.async_start_vto_event_listener()

                self.initialized = True
            except ClientResponseError as exception:
                if exception.status == 401:
                    raise self._auth_refused(exception) from exception
                _LOGGER.warning("Failed to initialize device at %s: %s", self._address, exception)
                self._back_off_poll_interval(
                    async_record_host_failure(self.hass, self._address, self.config_entry.entry_id)
                )
                raise UpdateFailed("Dahua device at " + self._address + " isn't fully initialized yet")
            except Exception as exception:
                _LOGGER.warning("Failed to initialize device at %s: %s", self._address, exception)
                self._back_off_poll_interval(
                    async_record_host_failure(self.hass, self._address, self.config_entry.entry_id)
                )
                raise UpdateFailed("Dahua device at " + self._address + " isn't fully initialized yet")

        # This is the event loop code that's called every n seconds
        try:
            # We need the profile mode (0=day, 1=night, 2=scene)
            if self._supports_profile_mode and not self.is_doorbell():
                try:
                    mode_data = await self.client.async_get_video_in_mode()
                    data.update(mode_data)
                    self._profile_mode = self.read_profile_mode(mode_data)
                except Exception as exception:
                    # I believe this API is missing on some cameras so we'll just ignore it and move on
                    _LOGGER.debug("Could not get profile mode", exc_info=exception)
                    pass
            
            # The profile mode above has to be read first because the lighting
            # call below needs it. The PTZ position does not, so it joins the
            # fan-out rather than costing an extra round trip ahead of it.
            async def _ptz_position():
                try:
                    return await self.client.async_get_ptz_position()
                except Exception as exception:
                    # I believe this API is missing on some cameras so we'll just ignore it and move on
                    _LOGGER.debug("Could not get preset position", exc_info=exception)
                    return None

            # Figure out which APIs we need to call and then fan out and gather the results
            # Motion detection state is read by the camera entity as well as
            # the switch, so it survives either one being enabled.
            coros = []
            if self._wanted_by(CAMERA, SWITCH):
                coros.append(asyncio.ensure_future(self.client.async_get_config_motion_detection()))
            # Only the preset position select reads this, and it is one of the
            # two per-poll calls the config cache does not cover.
            if self._supports_day_night_color and self._wanted_by(SELECT):
                coros.append(asyncio.ensure_future(self.client.async_get_video_in_options()))
            if self._supports_ptz_position and self._wanted_by(SELECT):
                coros.append(asyncio.ensure_future(_ptz_position()))
            if self.supports_infrared_light() and self._wanted_by(LIGHT):
                coros.append(
                    asyncio.ensure_future(self.client.async_get_config_lighting(self._channel, self._profile_mode)))
            if self._supports_disarming_linkage and self._wanted_by(SWITCH):
                coros.append(asyncio.ensure_future(self.client.async_get_disarming_linkage()))
            if self._supports_event_notifications and self._wanted_by(SWITCH):
                coros.append(asyncio.ensure_future(self.client.async_get_event_notifications()))
            if self.supports_alarm_output() and self._wanted_by(SWITCH):
                coros.append(asyncio.ensure_future(self.client.async_get_alarm_output_state()))
            # The siren switch and the security light both read this one.
            if self.uses_rpc2_deterrence() and self._wanted_by(LIGHT, SWITCH):
                coros.append(asyncio.ensure_future(
                    self.client.async_get_coaxial_control_io_status_rpc2()
                ))
            elif self._supports_coaxial_control and self.reads_coaxial_status():
                coaxial_channel = self._channel_number if self.uses_recorder_deterrence() else 1
                # Wrapped, because a device that refuses this must not take the whole
                # entry offline. See _async_coaxial_status.
                coros.append(
                    asyncio.ensure_future(
                        self._async_coaxial_status(coaxial_channel)
                    )
                )
            if getattr(self, "_ivs_rules", []) and self._wanted_by(SWITCH):
                ivs_read = (
                    self.client.async_get_remote_ivs_rules(self._channel)
                    if self.is_nvr_channel() else self.client.async_get_ivs_rules()
                )
                coros.append(asyncio.ensure_future(ivs_read))
            if self._supports_smart_motion_detection and self._wanted_by(SWITCH):
                coros.append(asyncio.ensure_future(self.client.async_get_smart_motion_detection()))
            if self.supports_smart_motion_detection_amcrest() and self._wanted_by(SWITCH):
                coros.append(asyncio.ensure_future(self.client.async_get_video_analyse_rules_for_amcrest()))
            if self.is_amcrest_doorbell() and self._wanted_by(LIGHT):
                coros.append(asyncio.ensure_future(self.client.async_get_light_global_enabled()))
            # Lighting_V2 is the light platform's table -- except that the
            # Amcrest doorbell's "Security Light" is a *select*, and its
            # current_option reads table.Lighting_V2[0][0][1].Mode/.State. A
            # select is not a light, so gating this on LIGHT alone left that
            # entity reading an absent table and reporting "Off" forever for
            # anyone who turned the light platform off. The condition mirrors
            # the one select.py creates it under, so nothing else over-fetches.
            if self._supports_lighting_v2 and (
                    self._wanted_by(LIGHT)
                    or (self.is_amcrest_doorbell()
                        and self.supports_security_light()
                        and self._wanted_by(SELECT))):
                coros.append(asyncio.ensure_future(self.client.async_get_lighting_v2()))
            if (getattr(self, "_supports_lighting_scheme_illuminator", False)
                    and self._wanted_by(LIGHT)):
                coros.append(asyncio.ensure_future(self.client.async_get_lighting_scheme()))
            # Only the privacy mode switch reads this one.
            if self._supports_privacy_mode and self._wanted_by(SWITCH):
                coros.append(asyncio.ensure_future(self._async_fetch_privacy_mode()))


            # Gather results and update the data map
            results = await asyncio.gather(*coros)
            for result in results:
                if result is not None:
                    data.update(result)

            # The cloud OTA record is a local config read, but it is only of
            # use to the informational update entity, so it is not read at all
            # when that platform is switched off -- the same rule the coaxial
            # status followed in #817. A refusal here is not fatal: the last
            # known answer stands.
            wants_cloud_upgrade = getattr(self, "_supports_cloud_upgrade", False)
            if wants_cloud_upgrade and self._wanted_by(UPDATE):
                try:
                    info = await self.client.async_get_cloud_upgrade_info()
                    self._cloud_firmware_version = cloud_upgrade_version(info)
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.debug(
                        "Could not read the cloud upgrade record", exc_info=True
                    )

            if (getattr(self, "_supports_lighting_scheme_illuminator", False)
                    and self._wanted_by(LIGHT)):
                try:
                    await self.client.async_reconcile_lighting_scheme_restore_modes()
                except Exception:  # pylint: disable=broad-except
                    # A stale recovery record is harmless and the next poll
                    # retries it. Do not take the camera offline because HA
                    # could not clean up its own local storage.
                    _LOGGER.warning(
                        "Could not reconcile stale illuminator recovery state"
                    )
                    _LOGGER.debug(
                        "Could not reconcile stale illuminator recovery state",
                        exc_info=True,
                    )

            if self._supports_ptz_position:
                self._preset_position = data.get("status.PresetID", "0") or "0"

            # Only if it was not already fetched above: on a camera that both
            # supports the v2 API and reports a security light, this was being
            # requested twice on every poll.
            if (((self.supports_security_light() and not self.uses_rpc2_deterrence(1))
                    or self.is_flood_light())
                    and not self._supports_lighting_v2 and self._wanted_by(LIGHT)):
                light_v2 = await self.client.async_get_lighting_v2()
                if light_v2 is not None:
                    data.update(light_v2)

            # Uptime is host-wide, so the shared helper collapses simultaneous
            # NVR channel coordinator polls into one actual uptime request.
            # Only use RPC2 when the user has explicitly enabled it.
            if (
                self._supports_lighting_v2
                and self._wanted_by(LIGHT)
                and self.client.use_rpc2
            ):
                self._camera_reboot_generation = (
                    await _async_get_host_uptime_generation(self)
                )

            async_record_host_success(self.hass, self._address)
            self._restore_poll_interval()
            return data
        except Exception as exception:
            # A 401 out here never started a reauth at all: the entry just went
            # unavailable and kept polling, which is the other half of #729.
            if isinstance(exception, ClientResponseError) and exception.status == 401:
                raise self._auth_refused(exception) from exception
            detail = describe_update_failure(exception)
            _LOGGER.warning("Failed to sync device state for %s: %s. See README to enable debug logs to get full exception",
                            self._address, detail)
            _LOGGER.debug("Failed to sync device state for %s", self._address, exc_info=exception)
            consecutive = async_record_host_failure(self.hass, self._address, self.config_entry.entry_id)
            self._back_off_poll_interval(consecutive)
            # Carried into UpdateFailed so the coordinator's own "Error fetching
            # dahua data" line names the fault too. Raised bare, it prints that
            # sentence and then nothing, which is what sends people to the
            # debug-logging instructions for what is often a one-word answer.
            raise UpdateFailed(detail) from exception

    def _handle_anpr_plate(self, event: dict):
        """Extract license plate data from event, fire ANPR events, and notify plate listeners."""
        plate_info = dahua_utils.extract_plate_data(event)
        if plate_info and plate_info.get("plate"):
            plate_info["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self._last_plate_data = plate_info
            self._last_plate_timestamp = int(time.time())
            event["PlateNumber"] = plate_info["plate"]
            event["PlateData"] = plate_info
            _LOGGER.info(
                "Dahua ANPR Plate detected on %s: %s (event %s)",
                self.get_device_name(),
                plate_info["plate"],
                event.get("Code"),
            )
            # Dedicated event on Home Assistant event bus
            anpr_event_data = {
                "device_name": self.get_device_name(),
                "channel": self._channel,
                "plate": plate_info["plate"],
                "raw_plate": plate_info.get("raw_plate"),
                "confidence": plate_info.get("confidence"),
                "vehicle_type": plate_info.get("vehicle_type"),
                "vehicle_color": plate_info.get("vehicle_color"),
                "vehicle_brand": plate_info.get("vehicle_brand"),
                "vehicle_series": plate_info.get("vehicle_series"),
                "direction": plate_info.get("direction"),
                "is_authorized": self.is_plate_authorized(plate_info["plate"]),
                "raw_event_code": event.get("Code"),
                "timestamp": self._last_plate_timestamp,
            }
            self.hass.bus.fire(EVENT_DAHUA_ANPR_RECOGNIZED, anpr_event_data)

            for listener in self._plate_listeners:
                try:
                    listener()
                except Exception as ex:
                    _LOGGER.warning("Error calling plate listener: %s", ex)

    def on_receive_vto_event(self, event: dict):
        event["DeviceName"] = self.get_device_name()
        _LOGGER.debug(f"VTO Data received: {event}")
        self._handle_anpr_plate(event)
        self.hass.bus.fire("dahua_event_received", event)

        # Example events:
        # {
        #   "Code":"VideoMotion",
        #   "Action":"Start",
        #   "Data":{
        #     "LocaleTime":"2021-06-19 15:36:58",
        #     "UTC":1624088218.0
        # }
        #
        # {
        #   "Code":"DoorStatus",
        #   "Action":"Pulse",
        #   "Data":{
        #      "LocaleTime":"2021-04-11 21:34:52",
        #      "Status":"Close",
        #      "UTC":1618148092
        #    },
        #    "Index":0
        # }
        #
        # {
        #    "Code":"BackKeyLight",
        #    "Action":"Pulse",
        #    "Data":{
        #       "LocaleTime":"2021-06-20 13:52:20",
        #       "State":1,
        #       "UTC":1624168340.0
        #    },
        #    "Index":-1
        # }

        # DHIP capitalises it; the CGI stream does not.
        self._dispatch_event(event, event.get("Action", ""))

    def on_receive(self, data_bytes: bytes, channel: int):
        """
        Takes in bytes from the Dahua event stream, converts to a string, parses to a dict and fires an event with the data on the HA event bus
        Example input:

        b'Code=VideoMotion;action=Start;index=0;data={\n'
        b'   "Id" : [ 0 ],\n'
        b'   "RegionName" : [ "Region1" ]\n'
        b'}\n'
        b'\r\n'


        Example events that are fired on the HA event bus:
        {'name': 'Cam13', 'Code': 'VideoMotion', 'action': 'Start', 'index': '0', 'data': {'Id': [0], 'RegionName': ['Region1'], 'SmartMotionEnable': False}}
        {'name': 'Cam13', 'Code': 'VideoMotion', 'action': 'Stop', 'index': '0', 'data': {'Id': [0], 'RegionName': ['Region1'], 'SmartMotionEnable': False}}
        {
            'name': 'Cam8', 'Code': 'CrossLineDetection', 'action': 'Start', 'index': '0', 'data': {'Class': 'Normal', 'DetectLine': [[18, 4098], [8155, 5549]], 'Direction':      'RightToLeft', 'EventSeq': 40, 'FrameSequence': 549073, 'GroupID': 40, 'Mark': 0, 'Name': 'Rule1', 'Object': {'Action': 'Appear', 'BoundingBox': [4816, 4552, 5248, 5272], 'Center': [5032, 4912], 'Confidence': 0, 'FrameSequence': 0, 'ObjectID': 542, 'ObjectType': 'Unknown', 'RelativeID': 0, 'Source': 0.0, 'Speed': 0, 'SpeedTypeInternal': 0}, 'PTS': 42986015370.0, 'RuleId': 1, 'Source': 51190936.0, 'Track': None, 'UTC': 1620477656, 'UTCMS': 180}
        }
        """
        for event in parse_event(data_bytes.decode("utf-8", errors="ignore")):
            index = 0
            if "index" in event:
                try:
                    index = int(event["index"])
                except ValueError:
                    index = 0
            if index == self._channel:
                self.handle_event(event)

    def _dispatch_event(self, event: dict, action: str) -> None:
        """Apply one event to this channel's sensors, whichever stream it came from.

        The two streams spell the action differently -- DHIP sends "Action",
        the CGI wire format parses to "action" -- and everything after that is
        the same. It is shared because it did not used to be: the CGI path had
        no Pulse branch and no NFC tag scan, so on that transport every Pulse
        event reached the event bus and then updated nothing, and an
        AccessControl card was never handed to async_scan_tag. Both behaviours
        existed on the doorbell path the whole time.
        """
        for code in self.translate_event_code(event):
            event_key = self.get_event_key(code)

            if code == "AccessControl":
                card_id = event.get("Data", {}).get("CardNo", "")
                if card_id:
                    card_id_md5 = hashlib.md5(card_id.encode()).hexdigest()
                    self.hass.async_create_task(
                        async_scan_tag(self.hass, card_id_md5, self.get_device_name())
                    )

            listeners = self._dahua_event_listeners.get(event_key)
            if not listeners:
                continue

            if action == "Start":
                self._dahua_event_timestamp[event_key] = int(time.time())
            elif action == "Stop":
                self._dahua_event_timestamp[event_key] = 0
            elif action == "Pulse":
                if code == "DoorStatus":
                    # The door number is in Index, and it was being thrown
                    # away. A VTO with an access control extension module
                    # has a second door whose events carry Index 1 (#488),
                    # and every one of them landed on the single Door Status
                    # sensor -- so door 2 closing reported door 1 as closed
                    # while it stood open. One sensor exists, it is door 1's,
                    # and only door 1 may write to it.
                    if door_index(event) != 0:
                        continue
                    if event.get("Data", {}).get("Status", "") == "Open":
                        self._dahua_event_timestamp[event_key] = int(time.time())
                    else:
                        self._dahua_event_timestamp[event_key] = 0
                elif code not in PULSE_STATE_CODES and code not in DOORBELL_STATE_EVENTS.values():
                    # A Pulse that is not a door state and not a call state is a
                    # notification that something happened. There is no Stop
                    # coming, so raise it and let the sensor's hold clear it --
                    # the same mechanism #761 gave the doorbell press.
                    #
                    # Recorded as momentary from what the device actually sent,
                    # rather than from a list of codes here. The list would be a
                    # guess: only InterVideoAccess has ever been seen as a Pulse
                    # in a report (#329), and two more codes were added to the
                    # selectable set in the last week alone.
                    momentary = getattr(self, "_momentary_events", None)
                    if momentary is None:
                        momentary = self._momentary_events = set()
                    momentary.add(event_key)
                    self._dahua_event_timestamp[event_key] = int(time.time())
                else:
                    # BackKeyLight carries the VTO's call state, and more than
                    # one value means ringing. myhomeiot/DahuaVTO documents
                    # 1 and 2 as Call/Ring (4 voice message, 5 answered,
                    # 6 not answered, 8 unlock, 11 rebooted), and its reference
                    # automation treats `State | int in [1, 2]` as the ring.
                    # Only 1 was accepted here, so a device that reports 2
                    # never raised the sensor at all.
                    #
                    # That project also warns the values vary by model, so this
                    # widens what counts as a ring rather than claiming a
                    # complete mapping.
                    state = event.get("Data", {}).get("State", 0)
                    try:
                        numeric_state = int(state)
                    except (TypeError, ValueError):
                        numeric_state = None
                    pressed = numeric_state in DOORBELL_RINGING_STATES
                    if pressed:
                        self._dahua_event_timestamp[event_key] = int(time.time())
                    else:
                        self._dahua_event_timestamp[event_key] = 0
                        self._note_unknown_doorbell_state(numeric_state, state)
            else:
                continue

            for listener in listeners:
                listener()

    def _remember_event(self, event: dict) -> None:
        """Keep the last few events, so diagnostics can show what arrived.

        Deliberately does nothing but store. Everything that could go wrong,
        redaction, truncation, serialising, happens when diagnostics is asked
        for, which is a cold path with its own guards. This runs on the event
        stream, where an unguarded exception takes every camera on the host down
        until the stream reconnects (#705, #706), so it is written to be
        incapable of raising rather than wrapped in a handler: a getattr with a
        default, a dict copy, and an append to a bounded deque.

        Stored before the event is enriched with the device name, because what
        matters for diagnosis is what the device sent.
        """
        buffer = getattr(self, "_recent_events", None)
        if buffer is None:
            buffer = self._recent_events = deque(maxlen=RECENT_EVENT_COUNT)
        buffer.append({"seconds_ago_at_capture": int(time.time()),
                       "event": dict(event)})

    def handle_event(self, event: dict):
        """Handle one event the host stream has decided belongs to this channel."""
        self._remember_event(event)
        _LOGGER.debug(
            "Event received from %s on channel %s: %s",
            self.get_address(),
            self._channel,
            event,
        )

        # Check for license plate data in the event
        self._handle_anpr_plate(event)

        # Put the event on the HA event bus
        event["name"] = self.get_device_name()
        event["DeviceName"] = self.get_device_name()
        self.hass.bus.fire("dahua_event_received", event)

        # When there's an event start we'll update the a map x to the current timestamp in seconds for the event.
        # We'll reset it to 0 when the event stops.
        # We'll use these timestamps in binary_sensor to know how long to trigger the sensor

        # The wire format is "Code=VideoMotion;action=Start;index=0", so the
        # action arrives lowercased here and capitalised on the DHIP path.
        self._dispatch_event(event, event.get("action", ""))

    def translate_event_code(self, event: dict):
        """
        translate_event_code returns a list of event codes to dispatch.
        For CrossLine/CrossRegion events with a recognized ObjectType, returns both the
        original code AND the SmartMotion* code (if listeners exist), so both sensors fire.
        """
        code = event.get("Code", "")

        if code == "CrossLineDetection" or code == "CrossRegionDetection":
            data = event.get("data", event.get("Data", {}))
            # parse_event turns the payload into a dict, but only when it is
            # valid JSON. A device whose payload arrives truncated leaves the
            # raw string here, and .get() on a string raises AttributeError --
            # out of this call, out of handle_event, out of on_receive, and out
            # of the stream loop, which wraps it in try/finally with no handler.
            # One malformed CrossLine event therefore took the event stream for
            # every channel on the host down with it (#475). A payload we could
            # not read is a payload with no ObjectType, not a reason to stop
            # listening.
            if not isinstance(data, dict):
                data = {}
            object_type = data.get("Object", {}).get("ObjectType", "").lower()
            codes = []

            # Always include the original CrossLine/CrossRegion if a listener exists
            if self._dahua_event_listeners.get(self.get_event_key(code)):
                codes.append(code)

            # Also include SmartMotion translation if applicable
            if object_type == "human":
                if self._dahua_event_listeners.get(self.get_event_key("SmartMotionHuman")):
                    codes.append("SmartMotionHuman")
                elif not codes:
                    codes.append("SmartMotionHuman")
            elif object_type == "vehicle":
                if self._dahua_event_listeners.get(self.get_event_key("SmartMotionVehicle")):
                    codes.append("SmartMotionVehicle")
                elif not codes:
                    codes.append("SmartMotionVehicle")

            return codes if codes else [code]

        # Convert doorbell pressed related events to common event name, DoorbellPressed.
        # VTO devices will use the event BackKeyLight and the Amcrest devices seem to use PhoneCallDetect
        if code == "BackKeyLight" or code == "PhoneCallDetect":
            # BackKeyLight is the VTO's call state, and ringing is only part of
            # what it reports. Collapsing every one of them to DoorbellPressed
            # threw the rest away on arrival -- an unlock arrives as State 8
            # and was read only as "not a ring", so it silently cleared the
            # button sensor and nothing could ever see the unlock itself.
            #
            # Measured on a VTO2000A, pressing the integration's own Open Door
            # button: 0.7s later the device sent
            #   Code=BackKeyLight Action=Pulse Data={"State": 8}
            # and no AccessControl event at all, so this is the only signal a
            # door-lock entity could confirm an unlock from.
            extra = DOORBELL_STATE_EVENTS.get(doorbell_state(event))
            if extra:
                return ["DoorbellPressed", extra]
            return ["DoorbellPressed"]

        return [code]

    def _note_unknown_doorbell_state(self, numeric_state, raw_state) -> None:
        """Say so, once, when a doorbell reports a call state we do not know.

        Only 1 and 2 count as ringing, and the comment beside that set has
        always conceded the values vary by model. Everything else is treated as
        "not ringing" and, until now, silently: a doorbell that reports its ring
        as some other number produced no button press, no error, and nothing in
        the log to say why.

        That is the missing piece in a long row of issues, all of the shape "my
        button press stopped working" with no way to tell whether the device is
        quiet or is speaking a dialect we do not read (#175, #250, #329, #358,
        #417, #556, #564, #593, #690). Every one of them needed this number and
        could only get it by turning on debug logging and reading raw events.

        Logged once per state per device, because a doorbell reports its state
        on every call and a warning per ring would be worse than the bug.
        """
        if numeric_state in DOORBELL_STATE_EVENTS or numeric_state == 0:
            # 8 and 9 are the unlock results, handled separately; 0 is idle,
            # which is the normal way a call ends.
            return
        # getattr, like the other per-coordinator state: plenty of tests build a
        # coordinator with object.__new__ and set only what they are about, and
        # a diagnostic must never be the thing that breaks one.
        seen = getattr(self, "_unknown_doorbell_states", None)
        if seen is None:
            seen = self._unknown_doorbell_states = set()
        if numeric_state in seen:
            return
        seen.add(numeric_state)
        _LOGGER.warning(
            "%s reported doorbell call state %r, which this integration does "
            "not recognise, so no button press was raised. Known states are "
            "1 and 2 for ringing, 8 and 9 for unlock, 0 for idle. If the "
            "doorbell was ringing when this appeared, please report this state "
            "number at %s so it can be added",
            self.get_device_name(), raw_state, ISSUE_URL,
        )

    def event_is_momentary(self, event_name: str) -> bool:
        """Whether this event has ever arrived as a Pulse on this device.

        Asked by the binary sensor to decide whether it needs to clear itself.
        A Pulse has no closing event, so a sensor raised by one and left alone
        would stay on until Home Assistant restarted.

        Derived from what the device sent rather than from a list of codes,
        because the list would be a guess and would drift: the selectable set
        gained FaceRecognition and HumanTrait in the last week.
        """
        return self.get_event_key(event_name) in getattr(self, "_momentary_events", ())

    def get_event_timestamp(self, event_name: str) -> int:
        """
        Returns the event timestamp. If the event is firing then it will be the time of the firing. Otherwise returns 0.
        event_name: the event name, example: CrossLineDetection
        """
        event_key = self.get_event_key(event_name)
        return self._dahua_event_timestamp.get(event_key, 0)

    def add_dahua_event_listener(self, event_name: str, listener: CALLBACK_TYPE):
        """ Adds an event listener for the given event (CrossLineDetection, etc).
        This callback will be called when the event fire """
        event_key = self.get_event_key(event_name)
        self._dahua_event_listeners.setdefault(event_key, []).append(listener)

    def supports_disarming_linkage(self) -> bool:
        """Whether the device answered the disarming linkage read during setup."""
        return self._supports_disarming_linkage

    def supports_alarm_output(self) -> bool:
        """Whether a safely decodable single alarm output is available."""
        return self._alarm_output_slots == 1

    def is_alarm_output_on(self) -> bool:
        """Return the physical state reported by getOutState."""
        return self.data.get("status.AlarmOut[0]") == "1"

    def supports_profile_mode(self) -> bool:
        """Whether this device has selectable day/night/general profiles.

        Only set for non-doorbell devices that answered the Lighting profile
        probe; for doorbells and unsupported cameras this stays False, so the
        profile sensor exists only where the profile is ever updated.
        """
        return self._supports_profile_mode

    def supports_cloud_upgrade(self) -> bool:
        """Whether the device serves a cloud OTA record to read.

        Set once at setup from the ``_DHCloudUpgrade_`` config table; a
        firmware without it gets no firmware-update entity, the same gate the
        profile sensor uses.
        """
        return self._supports_cloud_upgrade

    def get_cloud_firmware_version(self) -> str | None:
        """The newest firmware the device's own cloud check found, or None.

        None means "nothing known", not "up to date": the device only fills
        this in after its own OTA check has run, so Home Assistant reports the
        update entity as unknown until then rather than inventing a result.
        """
        return self._cloud_firmware_version

    async def _async_probe_cloud_upgrade(self) -> None:
        """Read the device's cached cloud OTA record, once, at setup.

        The record is a local config table, so this costs the device one config
        read and nothing else: no Dahua server is asked and no separate cloud
        poll is added. A firmware that serves no ``_DHCloudUpgrade_`` table is
        simply not given an update entity later, the same way the profile
        sensor is only created where the profile is ever read.
        """
        try:
            info = await self.client.async_get_cloud_upgrade_info()
        except Exception as probe_error:  # pylint: disable=broad-except
            self._note_probe_refusal("cloud_upgrade", probe_error)
            self._supports_cloud_upgrade = False
            self._cloud_firmware_version = None
            return
        self._supports_cloud_upgrade = True
        self._cloud_firmware_version = cloud_upgrade_version(info)

    async def _async_probe_direct_deterrence(self) -> None:
        """Cache independent positive ProductDefinition and getCaps evidence."""
        self._supports_rpc2_siren = False
        self._supports_rpc2_security_light = False
        self._siren_detection_sources = []
        self._security_light_detection_sources = []
        self._siren_detection_failures = []
        self._security_light_detection_failures = []
        if self.uses_recorder_deterrence():
            reason = "Direct-camera probes skipped: recorder classification or legacy fallback"
            self._siren_detection_failures.append(reason)
            self._security_light_detection_failures.append(reason)
            return
        definitions = {}
        try:
            full_definition = await self.client.async_get_product_definition_rpc2()
            if isinstance(full_definition, dict):
                definitions = full_definition
        except Exception:
            _LOGGER.debug(
                "Full ProductDefinition probe failed; trying named blocks", exc_info=True
            )

        for names, attribute, parser in (
            (
                ("LightingControl", "LightingControlMulti"),
                "_supports_rpc2_security_light",
                product_definition_supports_security_light,
            ),
            (
                ("AudioFileManager",),
                "_supports_rpc2_siren",
                product_definition_supports_siren,
            ),
        ):
            failures = (
                self._siren_detection_failures
                if attribute == "_supports_rpc2_siren"
                else self._security_light_detection_failures
            )
            for name in names:
                path = (
                    f"LightingControlMulti[{self.get_channel()}]"
                    if name == "LightingControlMulti"
                    else name
                )
                definition = definitions.get(name)
                if not isinstance(definition, (dict, list)):
                    try:
                        definition = await self.client.async_get_product_definition_rpc2(
                            name
                        )
                    except Exception as error:
                        failures.append(
                            f"ProductDefinition: {path} query failed ({type(error).__name__})"
                        )
                        _LOGGER.debug(
                            "ProductDefinition %s probe failed", name, exc_info=True
                        )
                        continue
                if name == "LightingControlMulti":
                    channel = self.get_channel()
                    if isinstance(definition, list) and not (
                        0 <= channel < len(definition)
                    ):
                        failures.append(
                            f"ProductDefinition: {path} channel index out of range (entries={len(definition)})"
                        )
                    definition = (
                        definition[channel]
                        if isinstance(definition, list) and 0 <= channel < len(definition)
                        else None
                    )
                if parser(definition):
                    setattr(self, attribute, True)
                    if attribute == "_supports_rpc2_siren":
                        siren = definition.get("SirenFileManager")
                        if isinstance(siren, dict) and siren.get("Support") is True:
                            reason = (
                                "ProductDefinition: "
                                "AudioFileManager.SirenFileManager.Support=true"
                            )
                        else:
                            reason = (
                                "ProductDefinition: AudioFileManager, "
                                "SupportEventLinkList non-empty and "
                                "PlayFormat/PlayFormet contains wav/pcm/aac/mp3"
                            )
                        self._siren_detection_sources.append(reason)
                    else:
                        path = (
                            f"LightingControlMulti[{self.get_channel()}]"
                            if name == "LightingControlMulti"
                            else "LightingControl"
                        )
                        for key in ("FilckerLighting", "FlickerLighting"):
                            flicker = definition["LinkingDetail"].get(key)
                            if (
                                isinstance(flicker, dict)
                                and flicker.get("Support") is True
                                and isinstance(flicker.get("LightType"), list)
                                and bool(flicker["LightType"])
                            ):
                                self._security_light_detection_sources.append(
                                    f"ProductDefinition: {path}.LinkingDetail.{key}, "
                                    "Support=true and LightType non-empty"
                                )
                    break
                explain = (
                    siren_definition_failure_reason
                    if attribute == "_supports_rpc2_siren"
                    else security_light_definition_failure_reason
                )
                failures.append(f"ProductDefinition: {path}, {explain(definition)}")

        try:
            caps = await self.client.async_get_coaxial_control_io_caps_rpc2()
            if caps.get("SupportControlSpeaker") is True:
                self._supports_rpc2_siren = True
                self._siren_detection_sources.append("getCaps: SupportControlSpeaker=true")
            else:
                self._siren_detection_failures.append(
                    "getCaps: SupportControlSpeaker is missing or not true"
                )
            if caps.get("SupportControlLight") is True:
                self._supports_rpc2_security_light = True
                self._security_light_detection_sources.append(
                    "getCaps: SupportControlLight=true"
                )
            else:
                self._security_light_detection_failures.append(
                    "getCaps: SupportControlLight is missing or not true"
                )
        except Exception as error:
            reason = f"getCaps: query failed ({type(error).__name__})"
            self._siren_detection_failures.append(reason)
            self._security_light_detection_failures.append(reason)
            _LOGGER.debug(
                "Direct-camera getCaps probe failed; keeping other evidence", exc_info=True
            )

    def uses_rpc2_deterrence(self, dahua_type: int | None = None) -> bool:
        """Select RPC2 for detected or manually enabled direct-camera outputs."""
        speaker = getattr(self, "_supports_rpc2_siren", False)
        light = getattr(self, "_supports_rpc2_security_light", False)
        manual_siren = getattr(self, "_manual_siren", False)
        manual_light = getattr(self, "_manual_security_light", False)
        if not (speaker or light or manual_siren or manual_light):
            return False
        if self.uses_recorder_deterrence():
            return False
        if (manual_siren or manual_light) and not self.is_doorbell():
            speaker = speaker or manual_siren
            light = light or manual_light
        return {1: light, 2: speaker}.get(dahua_type, speaker or light)

    def is_recorder_host(self) -> bool:
        """Return whether the device class explicitly identifies a recorder."""
        device_class = getattr(self, "_device_class", "")
        return isinstance(device_class, str) and device_class.strip().upper() in {
            "NVR",
            "DVR",
            "XVR",
            "HCVR",
        }

    def uses_recorder_deterrence(self) -> bool:
        """Use reported host class, falling back to legacy routing if unavailable."""
        device_class = getattr(self, "_device_class", "")
        if isinstance(device_class, str) and device_class.strip():
            return self.is_recorder_host()
        return self.is_nvr_channel()

    def get_siren_detection_sources(self) -> list[str]:
        """Describe the evidence and failed checks used by supports_siren."""
        sources = list(getattr(self, "_siren_detection_sources", []))
        if getattr(self, "_manual_siren", False) and not self.is_doorbell():
            sources.append("Manual override: manual_siren=true")
        m = self.model.upper()
        if "AS-PV" in m:
            sources.append("Model fallback: contains AS-PV")
        if "L46N" in m:
            sources.append("Model fallback: contains L46N")
        if m.startswith("W452ASD"):
            sources.append("Model fallback: starts with W452ASD")
        if "TPC-BF1241" in m:
            sources.append("Model fallback: contains TPC-BF1241")
        if sources:
            return sources
        failures = list(getattr(self, "_siren_detection_failures", []))
        if not failures:
            failures.append("Automatic detection has not run or evidence is unavailable")
        failures.append("Model fallback: no matching siren model")
        failures.append(
            "Manual override: excluded for doorbell"
            if getattr(self, "_manual_siren", False) and self.is_doorbell()
            else "Manual override: disabled"
        )
        return failures

    def get_security_light_detection_sources(self) -> list[str]:
        """Describe the evidence and failed checks used by supports_security_light."""
        sources = list(getattr(self, "_security_light_detection_sources", []))
        if getattr(self, "_manual_security_light", False) and not self.is_doorbell():
            sources.append("Manual override: manual_security_light=true")
        m = self.model.upper()
        if "AS-PV" in m:
            sources.append("Model fallback: contains AS-PV")
        if m in {"AD410", "DB61I"}:
            sources.append(f"Model fallback: {m}")
        if m.startswith("IP8M-2796E"):
            sources.append("Model fallback: starts with IP8M-2796E")
        if m.startswith("IPC-COLOR4M-TZ"):
            sources.append("Model fallback: starts with IPC-COLOR4M-TZ")
        if sources:
            return sources
        failures = list(getattr(self, "_security_light_detection_failures", []))
        if not failures:
            failures.append("Automatic detection has not run or evidence is unavailable")
        failures.append("Model fallback: no matching security-light model")
        failures.append(
            "Manual override: excluded for doorbell"
            if getattr(self, "_manual_security_light", False) and self.is_doorbell()
            else "Manual override: disabled"
        )
        return failures

    def supports_siren(self) -> bool:
        """
        Returns true if this camera has a siren. For example, the IPC-HDW3849HP-AS-PV does
        https://dahuawiki.com/Template:NameConvention
        """
        m = self.model.upper()
        return (
            getattr(self, "_supports_rpc2_siren", False)
            or (getattr(self, "_manual_siren", False) and not self.is_doorbell())
            or self.uses_rpc2_deterrence(2)
            or "AS-PV" in m
            or "L46N" in m
            or m.startswith("W452ASD")
            or "TPC-BF1241" in m
        )

    def supports_nvr_active_deterrence(self) -> bool:
        """Return whether NVR active-deterrence entities were explicitly enabled."""
        return self._nvr_active_deterrence

    def supports_security_light(self) -> bool:
        """
        Returns true if this camera has the red/blue flashing security light feature.  For example, the
        IPC-HDW3849HP-AS-PV does https://dahuawiki.com/Template:NameConvention
        Addressed issue https://github.com/rroller/dahua/pull/405
        """
        m = self.model.upper()
        return (
            getattr(self, "_supports_rpc2_security_light", False)
            or (getattr(self, "_manual_security_light", False) and not self.is_doorbell())
            or self.uses_rpc2_deterrence(1)
            or "AS-PV" in m
            or m == "AD410"
            or m == "DB61I"
            or m.startswith("IP8M-2796E")
            # Verified on two IPC-Color4M-TZ cameras: Type=1 drives the
            # alternating red/blue active-deterrence LEDs, despite the CGI
            # status field calling the output WhiteLight.
            or m.startswith("IPC-COLOR4M-TZ")
        )

    def is_doorbell(self) -> bool:
        """ Returns true if this is a doorbell (VTO)

        The device's own answer first, then the model-name list. Measured on a
        VTO2000A, which reports `class=VTO`, and on a DHI-NVR5464-16P-EI, which
        reports `class=NVR`.

        Deliberately additive. A device that answers `VTO` is one, whatever its
        model string says, which is what the list of prefixes below keeps
        failing to cover for rebadges (#690). But a device that answers
        something else, or does not answer at all, still gets the list: no
        Amcrest or Imou doorbell has been measured here, and a wrong negative
        would take every doorbell entity away from people who have them today.
        """
        if getattr(self, "_device_class", "") == "VTO":
            return True
        m = self.model.upper()
        return (
            m.startswith(("VTO", "DH-VTO", "DHI-VTO", "DH_VTO", "DHI_VTO"))
            or "-VTO" in m
            or "_VTO" in m
            or self.is_amcrest_doorbell()
            or self.is_empiretech_doorbell()
            or self.is_avaloidgoliath_doorbell()
        )

    def is_amcrest_doorbell(self) -> bool:
        """ Returns true if this is an Amcrest doorbell - IMOU DB61i is identical """
        return self.model.upper().startswith("AD") or self.model.upper().startswith("DB6")

    def is_empiretech_doorbell(self) -> bool:
        """ Returns true if this is an EmpireTech doorbell """
        return self.model.upper().startswith("DB2X")

    def is_avaloidgoliath_doorbell(self) -> bool:
        """ Returns true if this is an Avaloid Goliath doorbell """
        return self.model.upper().startswith("AV-V")

    def is_flood_light(self) -> bool:
        """ Returns true if this camera is an floodlight camera (eg.ASH26-W) """
        m = self.model.upper()
        return m.startswith("ASH26") or "L26N" in m or "L46N" in m or m.startswith("V261LC") or m.startswith("W452ASD")

    def supports_infrared_light(self) -> bool:
        """
        Returns true if this camera has an infrared light.  For example, the IPC-HDW3849HP-AS-PV does not, but most
        others do. I don't know of a better way to detect this
        """
        if not self._supports_lighting:
            return False
        return "-AS-PV" not in self.model and "-AS-NI" not in self.model and "LED-S2" not in self.model     #IPC-HFW2439SP-SA-LED-S2 also has no infrared light

    def supports_floodlightmode(self) -> bool:
        """ Returns true if this camera supports floodlight mode """
        return "W452ASD" in self.model.upper() or "L46N" in self.model.upper()

    def supports_illuminator(self) -> bool:
        """
        Returns true if this camera has an illuminator (white light for color cameras).  For example, the
        IPC-HDW3849HP-AS-PV does
        """
        if self.model.upper().startswith("IPC-COLOR4M-TZ"):
            return self._supports_lighting_scheme_illuminator and any(
                self.data.get(
                    "table.Lighting_V2[{0}][{1}][{2}].LightType".format(
                        self._channel, profile, index
                    )
                ) == WHITE_LIGHT
                for profile in range(9)
                for index in range(MAX_LIGHTING_V2_LIGHTS)
            )
        if self.is_amcrest_doorbell() or self.is_flood_light():
            return False
        if "table.Lighting_V2[{0}][0][0].Mode".format(
                self._channel) not in self.data:
            return False
        return self._declares_a_white_light()

    def _declares_a_white_light(self) -> bool:
        """Whether this channel reports a white emitter, if it says at all.

        Having a Lighting_V2 row only says the camera has *a* light. #540 is
        an IPC-HDW2831T-AS-S2, which has no white light at all: its single
        emitter is infrared, so the Illuminator entity drove the night vision
        LED and the camera's own UI calls that control Illuminator too, which
        is how it went unnoticed.

        The same check already existed for one model, behind a name prefix.
        This is it applied to whatever the device says, which is the fourth
        time a capability here turns out to be gated on a model string when
        the device was willing to answer the question (#570, #676, #690).

        A device that names no LightType keeps exactly the behaviour it has
        always had. Older firmware does not report them, and withdrawing an
        entity on silence would take the light away from everyone who has one
        working. Only a device that lists its emitters and names no white one
        loses it, which is the only case where we know it was wrong.
        """
        key = "table.Lighting_V2[{0}][0][{1}].LightType"
        declared = [
            self.data.get(key.format(self._channel, index))
            for index in range(MAX_LIGHTING_V2_LIGHTS)
        ]
        named = [light for light in declared if light is not None]
        if not named:
            return True
        return WHITE_LIGHT in named

    def uses_lighting_scheme_illuminator(self) -> bool:
        """Whether this device needs the two-table white-light contract."""
        return getattr(self, "_supports_lighting_scheme_illuminator", False)

    def is_motion_detection_enabled(self) -> bool:
        """ Returns true if motion detection is enabled for the camera """
        return self.data.get("table.MotionDetect[{0}].Enable".format(self._channel), "").lower() == "true"

    def is_disarming_linkage_enabled(self) -> bool:
        """ Returns true if disarming linkage is enable """
        return self.data.get("table.DisableLinkage.Enable", "").lower() == "true"

    def is_event_notifications_enabled(self) -> bool:
        """ Returns true if event notifications is enable """
        return self.data.get("table.DisableEventNotify.Enable", "").lower() == "false"

    def _smart_motion_row(self):
        """This channel's row in the SmartMotionDetect table, or None.

        SmartMotionDetect is a host-wide read that returns a row per channel,
        and the rows are sparse: a device only reports the channels that can
        actually do it. The row is therefore the per-channel capability signal
        as well as the state -- measured on an NVR, disabling it leaves the row
        in place reading false, and writing a row that does not exist is
        accepted with 200 and silently discarded.

        Both the capability check and the state read go through here so they
        cannot disagree about which row belongs to this channel.

        There is no fallback to row 0. A single camera sits on channel 0, so the
        lookup below already reads row 0 for it -- a fallback can only ever fire
        on a channel that is not row 0's owner, and handing it that row reports
        another camera's state and creates a switch whose writes the device
        accepts and discards.
        """
        return self.data.get("table.SmartMotionDetect[{0}].Enable".format(self._channel))

    def get_ivs_rules(self) -> list[dict]:
        """Return the normal rules discovered during setup."""
        return list(getattr(self, "_ivs_rules", []))

    def is_ivs_rule_enabled(self, channel: int, rule_id: str) -> bool | None:
        """Read the rule's current position; absent rules have unknown state."""
        table = self.data or {}
        name = "RemoteVideoAnalyseRule" if self.is_nvr_channel() else "VideoAnalyseRule"
        index = ivs_rule_index(table, channel, rule_id, name)
        if index is None:
            return None
        return table[f"table.{name}[{channel}][{index}].Enable"] == "true"

    def is_smart_motion_detection_enabled(self) -> bool:
        """ Returns true if smart motion detection is enabled """
        if self.supports_smart_motion_detection_amcrest():
            return self.data.get("table.VideoAnalyseRule[0][0].Enable", "").lower() == "true"
        return (self._smart_motion_row() or "").lower() == "true"

    def creates_siren_entity(self) -> bool:
        """Whether switch.py creates the siren for this entry.

        One source of truth, because two places need the same answer: the platform
        deciding whether to create the entity, and the poll deciding whether anything
        will read what it fetches. Two copies of this rule drifting apart would either
        spend a request per poll on nothing or leave a switch reading a value nobody
        fetched, and the second is much worse than the first.
        """
        if self.uses_recorder_deterrence():
            return self.supports_nvr_active_deterrence()
        return self.supports_siren()

    def creates_security_light_entity(self) -> bool:
        """Whether light.py creates the security light for this entry.

        An Amcrest doorbell is excluded because its Security Light is a *select* built in
        select.py, and that reads `Lighting_V2` rather than the coaxial status.
        """
        if self.is_amcrest_doorbell():
            return False
        if self.uses_recorder_deterrence():
            return self.supports_nvr_active_deterrence()
        return self.supports_security_light()

    def reads_coaxial_status(self) -> bool:
        """Whether any entity this entry creates reads `coaxialControlIO` status.

        Three do, and the third is the one to miss: a flood light reads `WhiteLight` out
        of this same status when the camera reports floodlightmode, and reads
        `Lighting_V2` when it does not. See `is_flood_light_on`.

        Measured on a DHI-NVR5464 with eleven entries: it answered this endpoint on every
        poll while having no siren and no security light entity anywhere, which is on the
        order of 7,900 requests a day for a value nothing displays. The platform gates
        were always there; the poll simply asked a broader question than the entities did.

        The RPC2 branch deliberately keeps its own condition. `uses_rpc2_deterrence`
        already returns False unless a speaker or light is detected or manually enabled,
        so it cannot fetch for nothing.
        """
        if self.creates_siren_entity() and self._wanted_by(SWITCH):
            return True
        if self._wanted_by(LIGHT):
            if self.creates_security_light_entity():
                return True
            # The flood light only reads the coaxial status on firmware that reports
            # floodlightmode. Without it the same entity reads Lighting_V2 instead, and
            # fetching this would be pointless for it too.
            if self.is_flood_light() and self._supports_floodlightmode:
                return True
        return False

    def is_siren_on(self) -> bool:
        """ Returns true if the camera siren is on """
        return self.get_status_value("Speaker").lower() == "on"

    def get_device_name(self) -> str:
        """ returns the device name, e.g. Cam 2 """
        if self._name is not None:
            return self._name
        # Earlier releases of this integration didn't allow for setting the camera name, it always used the machine name
        # Now we fall back to the machine name if that wasn't supplied at config time.
        return self.machine_name

    def get_model(self) -> str:
        """ returns the device model, e.g. IPC-HDW3849HP-AS-PV """
        return self.model

    def get_channel_model(self):
        """The model of the camera on this channel, or None if unknown.

        Deliberately separate from get_model(), which still answers what the
        device itself reports. Nothing is gated on this yet -- see #690.
        """
        return self._channel_model

    def get_firmware_version(self) -> str:
        """The firmware the device reported, e.g. 2.800.0000016.0.R.

        Kept on the coordinator rather than read back out of ``data``.
        The poll rebuilds that dict from scratch every cycle and only the
        one-time initialization ever puts a version in it, so anything
        reading it there got an answer on the first refresh and nothing
        at all from the second one onwards.
        """
        return self._firmware_version

    def get_build_date(self) -> str:
        """Return the firmware build date, e.g. 2020-06-05, if known.

        The CGI endpoint returns strings like
        ``2.800.0000016.0.R,build:2020-06-05``; peel the date off.
        """
        version = self._firmware_version
        if "build:" in version:
            return version.rsplit("build:", 1)[-1].strip()
        return ""

    def get_device_serial_number(self) -> str:
        """The serial the device reports, without the channel suffix.

        get_serial_number below appends the channel so that every entry on an
        NVR gets its own entity keys. That composite is not a serial number and
        should not be shown to anyone as one.
        """
        return self._serial_number

    def get_serial_number(self) -> str:
        """ returns the device serial number. This is unique per device """
        if self._channel > 0:
            # We need a unique identifier. For NVRs we get back the same serial, so add the channel to the end of the sn
            return "{0}_{1}".format(self._serial_number, self._channel)
        return self._serial_number

    def get_last_plate(self) -> str:
        """Return the last recognized license plate string, or 'unknown'."""
        if self._last_plate_data:
            return self._last_plate_data.get("plate", "unknown")
        return "unknown"

    def get_last_plate_data(self) -> dict:
        """Return the full metadata dict for the last recognized plate."""
        return self._last_plate_data or {}

    def get_last_plate_timestamp(self) -> int:
        """Return the unix epoch timestamp when the last plate was recognized."""
        return self._last_plate_timestamp

    def add_plate_listener(self, listener):
        """Add a callback listener invoked when a new license plate event is parsed."""
        self._plate_listeners.append(listener)

    def get_configured_area(self):
        """The area_id this entry was given, or None.

        Options win over data, like every other setting that can be changed
        after setup. Chosen while adding a recorder so that ten channels do not
        all arrive unfiled.
        """
        return self.config_entry.options.get(
            CONF_AREA, self.config_entry.data.get(CONF_AREA)) or None

    def configured_area_name(self):
        """That area's *name*, which is what device_info has to be given.

        The picker in the config flow returns an area_id; `suggested_area` in
        device_info is matched on the name. So this is the one conversion in the
        middle, and it has to tolerate an area the user has since deleted --
        passing a stale id through would have Home Assistant create a new area
        named after it.
        """
        area_id = self.get_configured_area()
        if not area_id:
            return None
        area = ar.async_get(self.hass).async_get_area(area_id)
        return area.name if area else None

    def get_authorized_plates(self) -> list[str]:
        """Return the list of configured authorized license plates (uppercase & normalized)."""
        raw = self.config_entry.options.get(
            CONF_AUTHORIZED_PLATES,
            self.config_entry.data.get(CONF_AUTHORIZED_PLATES, ""),
        )
        return dahua_utils.parse_authorized_plates(raw)

    def get_authorized_hold_time(self) -> int:
        """Return the duration in seconds an authorized vehicle binary sensor stays active."""
        try:
            return int(self.config_entry.options.get(
                CONF_AUTHORIZED_HOLD_TIME,
                self.config_entry.data.get(
                    CONF_AUTHORIZED_HOLD_TIME, DEFAULT_AUTHORIZED_HOLD_TIME
                ),
            ))
        except (ValueError, TypeError):
            return DEFAULT_AUTHORIZED_HOLD_TIME

    def is_plate_authorized(self, plate: str | None) -> bool:
        """Return True if the given plate matches any configured authorized plate."""
        if not plate or plate == "unknown":
            return False
        norm = dahua_utils.normalize_plate(plate)
        auth_plates = self.get_authorized_plates()
        if norm in auth_plates:
            return True
        # Equate 0 and O OCR confusions as fallback
        norm_fuzzy = norm.replace("0", "O")
        return any(norm_fuzzy == p.replace("0", "O") for p in auth_plates)

    def get_event_list(self) -> list:
        """
        Returns the list of events selected when configuring the camera in Home Assistant. For example:
        [VideoMotion, VideoLoss, CrossLineDetection]
        """
        return self.events

    def get_infrared_profile(self) -> str:
        """The Lighting profile this channel's infrared light is really using."""
        return infrared_profile(self.data, self._channel, self.get_profile_mode())


    def supports_day_night_color(self) -> bool:
        """True if this channel reported a Day/Night mode we understand."""
        return self._supports_day_night_color

    def get_day_night_color(self):
        """This channel's Day/Night mode by name, or None."""
        return day_night_color_name(self.data, self._channel)

    def is_infrared_light_on(self) -> bool:
        """ returns true if the infrared light is on """
        return self.data.get(
            "table.Lighting[{0}][{1}].Mode".format(
                self._channel, self.get_infrared_profile()), "") == "Manual"

    def get_infrared_brightness(self) -> int:
        """Return the brightness of this light, as reported by the camera itself, between 0..255 inclusive"""

        bri = self.data.get(
            "table.Lighting[{0}][{1}].MiddleLight[0].Light".format(
                self._channel, self.get_infrared_profile()))
        return dahua_utils.dahua_brightness_to_hass_brightness(bri)

    def get_illuminator_index(self) -> int:
        """The Lighting_V2 light index this device puts its white light on."""
        return illuminator_light_index(self.data, self._channel, self.get_profile_mode())

    def get_illuminator_bank(self) -> str:
        """The brightness bank this device's white light actually uses."""
        return illuminator_brightness_bank(
            self.data, self._channel, self.get_profile_mode(), self.get_illuminator_index())

    def is_illuminator_on(self) -> bool:
        """Return true if the illuminator light is on"""
        # profile_mode 0=day, 1=night, 2=scene
        profile_mode = self.get_profile_mode()
        index = self.get_illuminator_index()
        manually_on = self.data.get(
            "table.Lighting_V2[{0}][{1}][{2}].Mode".format(self._channel, profile_mode, index), ""
        ) == "Manual"
        if self.uses_lighting_scheme_illuminator():
            scheme_mode = self.data.get(
                "table.LightingScheme[{0}][{1}].LightingMode".format(
                    self._channel, profile_mode
                ), ""
            )
            return scheme_mode == "WhiteMode" and manually_on
        return manually_on

    def is_flood_light_on(self) -> bool:

        if self._supports_floodlightmode:
          # 'coaxialControlIO.cgi?action=getStatus&channel=1'
            return self.get_status_value("WhiteLight").lower() == "on"
        else:
            """Return true if the amcrest flood light light is on"""
            # profile_mode 0=day, 1=night, 2=scene
            profile_mode = self.get_profile_mode()
            return self.data.get(f'table.Lighting_V2[{self._channel}][{profile_mode}][1].Mode') == "Manual"

    def is_ring_light_on(self) -> bool:
        """Return true if ring light is on for an Amcrest Doorbell"""
        return self.data.get("table.LightGlobal[0].Enable") == "true"

    def get_illuminator_brightness_field(self) -> str:
        """Return the brightness field used by this WhiteLight."""
        profile_mode = self.get_profile_mode()
        index = self.get_illuminator_index()

        base = (
            f"table.Lighting_V2[{self._channel}]"
            f"[{profile_mode}][{index}]"
        )

        if f"{base}.NearLight[0].Light" in self.data:
            return "NearLight"

        if f"{base}.MiddleLight[0].Light" in self.data:
            return "MiddleLight"

        return "MiddleLight"

    def get_illuminator_brightness(self) -> int:
        """Return the brightness of the illuminator light, as reported by the camera itself, between 0..255 inclusive"""

        if self.uses_lighting_scheme_illuminator():
            bri = self.data.get(
                "table.Lighting_V2[{0}][{1}][{2}].PercentOfMaxBrightness".format(
                    self._channel, self.get_profile_mode(), self.get_illuminator_index()
                )
            )
            return dahua_utils.dahua_brightness_to_hass_brightness(bri)
        # The profile was hardcoded to 0 here while is_illuminator_on reads the
        # live one, so on a camera running Night this reported the Day
        # brightness. The bank was hardcoded too; see illuminator_brightness_bank.
        bri = self.data.get(
            "table.Lighting_V2[{0}][{1}][{2}].{3}[0].Light".format(
                self._channel, self.get_profile_mode(),
                self.get_illuminator_index(), self.get_illuminator_bank()
            )
        )
        return dahua_utils.dahua_brightness_to_hass_brightness(bri)

    def is_security_light_on(self) -> bool:
        """Return true if the security light is on. This is the red/blue flashing light"""
        return self.get_status_value("WhiteLight").lower() == "on"

    def read_profile_mode(self, mode_data: dict) -> str:
        """Picks this channel's day/night profile out of the VideoInMode table.

        The profile chooses which Lighting[channel][profile] the light is read
        from and written to, so getting it wrong means commands are accepted and
        nothing lights up.

        Three device behaviours have to coexist here, and two of them disagree
        about which field is authoritative:

        - **General profile management** (`Config[0]` = 2). One profile covers
          all conditions and it is profile 2. `ConfigEx` is still present and
          still echoes day/night, but it selects nothing -- preferring it sent
          every write to the day profile while the camera rendered from 2 (#605).
        - **IL series dual smart light.** The profile is chosen by the `ConfigEx`
          string; `Config[0]` stays a static 0 whichever profile is live, so
          reading it left the illuminator permanently tracking day (#582).
        - **Everything else.** `Config[0]` is the profile.

        The read is host-wide -- getConfig&name=VideoInMode returns a row per
        channel -- so an NVR channel takes its own row and no other. There is no
        fallback to row 0: a single camera sits on channel 0, so the lookup
        below already reads row 0 for it, and a fallback could therefore only
        ever fire on a channel that is not row 0's owner. On a recorder that row
        is camera 1, and adopting its profile decides which
        Lighting_V2[channel][profile] every light command for this camera is
        written to. Camera 1 on day and this one on night sends every write to a
        profile the camera is not using, where it is accepted and ignored.

        Worse, the fallback was per field, so Config[0] could come from this
        channel while ConfigEx came from another -- one answer assembled from
        two cameras.
        """
        def field(name):
            return mode_data.get(
                "table.VideoInMode[{0}].{1}".format(self._channel, name))

        config = field("Config[0]")
        config_ex = field("ConfigEx")

        if config == "2":
            return "2"
        if config_ex is not None:
            # Only act on a value we recognise. Treating anything else as day
            # would override a Config[0] that is very likely right, for a
            # string we do not understand.
            named = str(config_ex).strip().lower()
            if named == "night":
                return "1"
            if named == "day":
                return "0"
        return config or "0"

    async def async_detect_lighting_support(self) -> bool:
        """Does this channel have an infrared light?

        Judged by what comes back, not by an exception. async_get_config
        catches aiohttp.ClientResponseError and returns {}, so an
        exception-only probe could never fail: every device was marked as
        having an IR light and then fetched Lighting[channel][mode] on every
        poll, forever. The profile mode probe reads its result the same way.
        """
        try:
            conf = await self.client.async_get_config_lighting(self._channel, self._profile_mode)
        except PROBE_FAILED as probe_error:
            self._note_probe_refusal("lighting", probe_error)
            return False
        return len(conf) > 0

    def get_camera_reboot_generation(self) -> int:
        """Return the host reboot generation observed by this coordinator."""
        return self._camera_reboot_generation

    def get_profile_mode(self) -> str:
        # profile_mode 0=day, 1=night, 2=scene
        return self._profile_mode

    def get_channel(self) -> int:
        """returns the channel index of this camera. 0 based. Channel index 0 is channel number 1"""
        return self._channel

    def is_nvr_channel(self) -> bool:
        """Return whether this entry represents a camera channel on an NVR.

        Channel 0 is the awkward one. It is both the only channel a standalone
        camera has and the first channel of every recorder, so the model string
        is all that separates them -- and plenty of recorders do not say "NVR"
        in theirs. A Lorex N843A8 does not, nor do most OEM rebrands.

        The consequence was silent and lopsided: a user who switched on NVR
        active deterrence got the entity on channels 1 upwards and nothing at
        all on channel 0, because that channel took the standalone-camera branch
        and was tested against a model whitelist the recorder can never match.

        So the option counts as an answer. It is offered for recorders, it
        defaults off, and a user who turns it on has said what this entry is
        more directly than any model string does.

        This decides the control path as well as whether the entity exists --
        an NVR channel drives deterrence through coaxialControlIO on its own
        channel number, a camera through its channel index -- so the two have to
        be decided by the same question or the entity would appear and then
        write to the wrong place.
        """
        return (self._channel > 0
                or "NVR" in self.model.upper()
                or self._nvr_active_deterrence)

    def get_channel_number(self) -> int:
        """returns the channel number of this camera"""
        return self._channel_number

    def get_event_key(self, event_name: str) -> str:
        """returns the event key we use for listeners. It uses the channel index to support multiple channels"""
        return "{0}-{1}".format(event_name, self._channel)

    def get_address(self) -> str:
        """returns the IP address of this camera"""
        return self._address

    def get_configuration_url(self) -> str:
        """Where the device's own web interface actually is.

        The same scheme and port the integration itself uses, because anything else
        is a guess: the device page's link was built as "http://" + address, with no
        port and always plain HTTP, so it was broken for every device on a port other
        than 80 and every device that only serves HTTPS -- on the first page somebody
        opens after adding a camera.
        """
        return self.client.base_url()

    def get_max_streams(self) -> int:
        """Returns the max number of streams supported by the device. All streams might not be enabled though"""
        return self._max_streams

    def supports_smart_motion_detection(self) -> bool:
        """True if *this channel* can do smart motion detection.

        The probe behind _supports_smart_motion_detection fetches the whole
        host-wide table with no channel argument, so it succeeds for every
        channel of an NVR whether or not that channel has the feature. On a
        sixteen channel recorder measured for this, ten channels had cameras
        and two had rows -- the other eight carried a switch that was
        permanently off and whose writes the device accepted and ignored.

        The row is the real signal, and it is already in data we fetch.
        """
        if not self._supports_smart_motion_detection:
            return False
        return self._smart_motion_row() is not None

    def supports_smart_motion_detection_amcrest(self) -> bool:
        """ True if smart motion detection is supported for an amcrest device

        Matched the way is_amcrest_doorbell matches, which is the point: these
        two questions are about the same devices and disagreed. That one folds
        case and takes a prefix; this one compared the raw string exactly, so a
        doorbell reporting `DB61I` rather than `DB61i` was a doorbell to one
        check and not to the other.

        A device that falls through here is not merely missing its switch. The
        smart motion state and the write both take the non-Amcrest branch, which
        reads SmartMotionDetect -- a table an Amcrest doorbell does not have --
        so it reports nothing and its IVS rule is never touched.
        """
        model = self.model.upper()
        return model.startswith("AD410") or model.startswith("DB61")

    def supports_privacy_mode(self) -> bool:
        """ True if the camera exposes the lens privacy mask over RPC2 """
        return self._supports_privacy_mode

    def is_privacy_mode_enabled(self) -> bool:
        """ True if the lens privacy mask is currently enabled """
        return self.data.get("privacy_mode_enabled", False)

    async def _async_coaxial_status(self, channel: int):
        """Read the siren and white light state, or give up on this poll only.

        This is one optional capability among a dozen gathered together, and the gather
        is what the coordinator's result depends on, so any exception here used to take
        the **entire entry** offline for that cycle: every entity stale, a host failure
        recorded against the shared count that all of a recorder's channels use, and the
        poll interval backed off.

        Measured on a DHI-NVR5464: `coaxialControlIO.cgi?action=getStatus` answers 400 on
        seven of its channels at various times, once for nine hours straight at about 109
        an hour. A 400 is the device understanding the request and refusing it, which is
        not a reason to report the camera as broken.

        Deliberately does **not** stop asking. The refusals here recovered on their own,
        so a capability the user may rely on must not be switched off for the life of the
        process by one bad answer. What changes is that a refusal costs a stale reading
        rather than a failed poll, and says so once rather than every two minutes.
        """
        try:
            return await self.client.async_get_coaxial_control_io_status(channel)
        except ClientResponseError as error:
            if error.status not in CAPABILITY_REFUSED:
                raise
            target = (self.client.device_key, channel)
            if target not in _CAPABILITY_REFUSALS_REPORTED:
                _CAPABILITY_REFUSALS_REPORTED.add(target)
                _LOGGER.warning(
                    "%s refused the siren and white light state for channel %s with "
                    "HTTP %s. That is this device saying it does not serve that here, "
                    "not a fault, so those entities will hold their last value and the "
                    "rest of this camera is unaffected. Reported once per channel",
                    self._address, channel, error.status)
            else:
                _LOGGER.debug(
                    "%s still refuses coaxial status for channel %s (HTTP %s)",
                    self._address, channel, error.status)
            return None

    async def _async_fetch_privacy_mode(self) -> dict:
        """ Poll the privacy mode state, keeping the last known value on failure """
        try:
            return {"privacy_mode_enabled": await self.client.async_get_privacy_mode()}
        except Exception as exception:
            _LOGGER.debug("Failed to fetch privacy mode state", exc_info=exception)
            previous = self.data.get("privacy_mode_enabled", False) if self.data else False
            return {"privacy_mode_enabled": previous}

    def get_vto_client(self) -> DahuaVTOClient | None:
        """The doorbell's client, or None when there is not a live one.

        Returns an instance of the connected VTO client if this is a VTO device.
        We need this because there's different ways to call a VTO device and the
        VTO client will handle that. For example, to hang up a call.

        `_vto_client` is only ever assigned, never cleared, so after a drop it
        goes on naming a protocol whose socket has gone -- and a doorbell
        reconnects often enough for that to be reachable. `disconnected` is the
        future the protocol resolves when its connection ends, so a client
        holding a finished one is a client there is no point handing out.
        """
        client = self._vto_client
        if client is None or client.disconnected.done():
            return None
        return client

    def get_status_value(self, key):
        v = self.data.get(f"status.status.{key}")
        if v is None:
           v = self.data.get(f"status.{key}", "")
        return v

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        # Setup may have failed before the coordinator was registered, or a
        # previous unload may already have removed it. Treat that as unloaded
        # so an options-triggered reload can continue cleanly.
        return True

    await coordinator.async_stop()
    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in PLATFORMS
                if platform in coordinator.platforms
            ]
        )
    )
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)

    # The host-scoped cleanup that used to live here has moved to
    # async_remove_entry. It could never run from this function: Home Assistant
    # unloads an entry *before* it deletes it, so _entries_for_address still
    # counted the entry being unloaded and the "last one for this host" test was
    # never true. It also should not run on a reload, which is the other reason
    # this function is called -- dropping the failure count there would reset the
    # poll backoff every time somebody saved an option.
    return unloaded


@callback
def _async_forget_host(hass: HomeAssistant, address: str) -> None:
    """Drop what is remembered about a host, and withdraw what we said about it.

    Only correct once the last entry for the address has gone. The failure count
    drives the poll backoff and the two issues are per host, so a host that still
    has entries must keep all of it.
    """
    _HOST_FAILURES.pop(address, None)
    _HOST_UPTIME_STATE.pop(address, None)
    _HOST_UPTIME_LOCKS.pop(address, None)
    # The RPC2 poller's own state. Safe only here: the active set deliberately outlives
    # a reload, because a Start whose Stop was lost is still owed one, and clearing it
    # while the host still had entries would leave those sensors stuck on. With the last
    # entry gone there is nobody left to owe.
    _HOST_RPC2_EVENT_POLL.pop(address, None)
    _HOST_RPC2_EVENT_STATE.pop(address, None)
    # Which channels have already been reported as refusing a capability. Keyed by
    # device_key, which is "address:port", so this matches on the address part.
    for target in [t for t in _CAPABILITY_REFUSALS_REPORTED
                   if str(t[0]).split(":")[0] == address]:
        _CAPABILITY_REFUSALS_REPORTED.discard(target)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_UNREACHABLE.format(address))
    ir.async_delete_issue(
        hass, DOMAIN, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE.format(address)
    )


@callback
def _async_dependents(hass: HomeAssistant, entry_id: str) -> dict:
    """What referenced this entry, by kind, so a removal can say what it broke.

    Home Assistant computes this; it is the same call the frontend's "related"
    panel makes. Only reachable from async_remove_entry: it reads the entity and
    device registry rows for the entry, and Home Assistant clears those
    immediately afterwards.

    Deliberately forgiving. This is a courtesy on a teardown path and nothing
    here is worth failing a removal over, so an unavailable search component or
    a changed signature degrades to "nothing found" rather than raising into a
    hook whose exceptions are only logged anyway.
    """
    try:
        # Imported here because the search component is an after_dependency:
        # available in practice, but not something to fail setup over.
        from homeassistant.components.search import (  # pylint: disable=import-outside-toplevel
            ItemType, Searcher)
        from homeassistant.helpers.entity import (  # pylint: disable=import-outside-toplevel
            entity_sources)

        found = Searcher(hass, entity_sources(hass)).async_search(
            ItemType.CONFIG_ENTRY, entry_id)
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Could not work out what referenced %s: %s", entry_id, err)
        return {}

    # Only the kinds that break *silently*. A dashboard card that points at a
    # missing entity says so on screen; an automation just stops firing. Scenes
    # and groups are listed for the same reason.
    return {
        kind: sorted(found.get(kind) or ())
        for kind in ("automation", "script", "scene", "group")
        if found.get(kind)
    }


def _describe_dependents(dependents: dict) -> str:
    """"3 automations and 1 script", or "" when nothing referenced it."""
    words = {"automation": "automation", "script": "script",
             "scene": "scene", "group": "group"}
    parts = [
        "%d %s%s" % (len(items), words[kind], "" if len(items) == 1 else "s")
        for kind, items in dependents.items()
    ]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> bool:
    """May the user delete this device from the device page?

    Defining this at all is what puts a Delete button on a device card. Without it
    `entry.supports_remove_device` is False and there is no button, so removing one
    camera meant finding its config entry instead -- which on a recorder means
    finding the right one of sixteen.

    One entry is one channel is one device, and the identifier is derived from the
    serial the device reports (`serial`, or `serial_N` above channel 0). So the device
    this entry *currently* creates must not be removable: Home Assistant would delete
    the row and the next reload would put it straight back, which looks like the
    button did nothing.

    A device whose identifier is not the one this entry now produces is stale, and
    that really happens. A camera that answered with a synthesised identity and later
    reported its real serial leaves the old record behind -- #583 has two device rows
    for one camera, the live one carrying the fallback identity and the stale one
    carrying the real model name. Those are exactly what the button is for.

    Refusing when the coordinator is missing is deliberate. Setup failed or the entry
    is unloaded, so nothing can be said about which device is current, and deleting
    the live one on a guess is worse than leaving a stale row alone for now.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        return False

    current = coordinator.get_serial_number()
    return not any(
        domain == DOMAIN and value == current for domain, value in device.identifiers
    )


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Withdraw anything said about a host once its last entry is removed.

    This hook exists because unloading and removing are different things and
    only this one means removal. Home Assistant deletes the entry from its own
    registry *before* calling it, so unlike in async_unload_entry the count
    below correctly excludes the entry that has just gone.

    Without it a device that was unreachable, or that was offered the HTTPS
    switch, left its Repairs card on the Settings page after being deleted --
    pointing at an address with no entries, offering to reconfigure nothing, and
    with no way for the user to dismiss it. The remembered failure count stayed
    for the life of the process too, so re-adding the device inherited a backoff
    it had not earned.

    Deliberately takes no interest in whether setup ever succeeded. The card is
    most likely to be there precisely when it did not.
    """
    address = normalize_address(entry.data.get(CONF_ADDRESS))
    # Read before anything else: Home Assistant clears the registry rows this
    # depends on as soon as this hook returns.
    dependents = _async_dependents(hass, entry.entry_id)
    siblings = _entries_for_address(hass, address) if address else []

    if address and not siblings:
        _async_forget_host(hass, address)
        _LOGGER.debug(
            "Last entry for %s removed; forgot its host state and withdrew its repairs",
            address,
        )

    _async_report_removal(hass, entry, address, siblings, dependents)


@callback
def _async_report_removal(hass: HomeAssistant, entry: ConfigEntry, address: str,
                          siblings: list, dependents: dict) -> None:
    """Say what the removal left behind, and offer to finish it.

    At most one card, because two would be noise on a single deletion:

    - other entries for the same recorder -> offer to remove them too, and
      mention anything that referenced the one just removed
    - nothing left for the host but things referenced it -> say what they were

    Nothing to say means nothing raised. Removing a standalone camera that no
    automation touched is silent, which is the common case.

    Persistent on purpose: a removal is usually followed by a restart, and a
    card that vanishes over one is no use to somebody who wanted to read it
    afterwards.
    """
    described = _describe_dependents(dependents)

    if siblings:
        titles = ", ".join(sorted(e.title or "untitled" for e in siblings))
        ir.async_create_issue(
            hass, DOMAIN, ISSUE_SIBLINGS_REMAIN.format(address),
            is_fixable=True, is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="siblings_remain",
            # The title's placeholders, and only those. The fix flow fills its
            # own form separately, from `data` below -- a placeholder named here
            # but not there renders literally as {removed} on screen.
            translation_placeholders={
                "address": address,
                "count": str(len(siblings)),
            },
            data={
                "address": address,
                "removed": entry.title or "untitled",
                # A whole sentence or nothing, so the form reads correctly
                # either way. A bare count would leave a dangling clause.
                "dependents_note": (
                    "%s also referenced the entry you removed, and are not "
                    "repaired by this." % described.capitalize()
                ) if described else "",
            },
        )
        return

    if described:
        ir.async_create_issue(
            hass, DOMAIN, ISSUE_REMOVAL_BROKE_THINGS.format(entry.entry_id),
            is_fixable=False, is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="removal_broke_things",
            translation_placeholders={
                "removed": entry.title or "untitled",
                "dependents": described,
                "names": ", ".join(
                    name for items in dependents.values() for name in items),
            },
        )


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)

