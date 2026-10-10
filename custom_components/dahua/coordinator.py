"""The per channel coordinator, and the pure functions that read a device.

Where Home Assistant expects an integration's coordinator to live, which is the
`common-modules` quality scale rule. It was in __init__.py together with the
config entry lifecycle and the host level machinery that is now host.py.

The helpers above the class are pure: given what a device said, what does it mean.
They are module level rather than methods because a test can then call them
without building a coordinator at all, which is what most of the suite does with
them.
"""

from collections import deque
from datetime import timedelta
from typing import Any, Dict
import asyncio
import hashlib
import logging
import re
import time

from aiohttp import ClientError, ClientResponseError, ClientSession

from homeassistant.components.tag import async_scan_tag
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import dahua_utils
from . import refusals
from .client import DahuaClient, parse_storage_disks, rpc2_refusal_is_a_stale_login
from .rpc2 import Rpc2MethodRefused
from .const import (
    CAMERA,
    CONF_AREA,
    CONF_AUTHORIZED_HOLD_TIME,
    CONF_AUTHORIZED_PLATES,
    CONF_AUTO_DETECT_CHANNEL,
    CONF_MANUAL_SECURITY_LIGHT,
    CONF_MANUAL_SIREN,
    CONF_NVR_ACTIVE_DETERRENCE,
    CONF_SCAN_INTERVAL,
    CONF_USE_RPC2,
    CONF_POLL_VIDEO_MOTION,
    DEFAULT_AUTHORIZED_HOLD_TIME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_DAHUA_ANPR_RECOGNIZED,
    FIRMWARE_UPGRADE_REFRESH_SECONDS,
    ISSUE_URL,
    LIGHT,
    MIN_SCAN_INTERVAL,
    NUMBER,
    SELECT,
    SWITCH,
    UPDATE,
)
from .dahua_utils import cloud_upgrade_version, parse_event
from .deterrence import (
    product_definition_supports_security_light,
    product_definition_supports_siren,
    security_light_definition_failure_reason,
    siren_definition_failure_reason,
)
from .host import (
    MAX_AUTH_REFUSALS,
    MAX_BACKOFF_DOUBLINGS,
    _CAPABILITY_REFUSALS_REPORTED,
    _acquire_connector,
    _async_get_host_uptime_generation,
    _host_stream,
    _release_connector,
    _release_host_stream,
    async_device_is_zero_indexed,
    async_record_host_auth_refusal,
    async_record_host_failure,
    async_record_host_success,
    event_stream_retry_delay,
    stream_lifetime,
)
from .illuminator_restore import IlluminatorRestoreStore, ChannelLightingSnapshotStore
from .ivs import ivs_discovery_diagnostics, ivs_rule_index, ivs_rules_for_channel
from .model_profiles import is_sdt4e425
from .vto import DahuaVTOClient

_LOGGER: logging.Logger = logging.getLogger(__package__)

# A capability probe that times out has told us what an errored probe tells us:
# this device will not serve that call, so do not offer the entity. Timeouts are
# not aiohttp.ClientError -- asyncio.TimeoutError is the builtin -- so before
# this they escaped the probe, hit the outer handler, and failed the whole
# config entry with ConfigEntryNotReady. One slow capability check took the
# device down and Home Assistant retried it forever. See #594 and #631.
PROBE_FAILED = (ClientError, TimeoutError)

# Where an indoor monitor's camera links live in the poll's data: see
# client.vth_camera_links for the shape.
VTH_CAMERA_LINKS = "vth.camera_links"

# The coaxial probe deliberately only treats an HTTP error response as "not
# supported"; a connection failure there should still fail setup. Timeouts join
# it for the reason above, without widening the rest.
PROBE_REFUSED = (ClientResponseError, TimeoutError)


def vto_retry_state(
    lived_seconds: float, consecutive_failures: int, received_data: bool
) -> tuple:
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
        stream_lifetime(lived_seconds, received_data), failures, received_data
    )
    return delay, failures


# A single missed poll is a blip -- a snapshot timing out, a device busy writing
# to disk -- and backing off on one would make the integration feel sluggish for
# no reason. Past that, the device is not answering and polling it on the
# configured cadence only adds to whatever is wrong.
FAILURES_BEFORE_BACKOFF = 2

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
    return min(base * (2**doublings), max(POLL_BACKOFF_CAP, base))


# Lighting_V2 lists a device's lights by index, and the index order is not the
# same on every model. The device names each one in LightType, so it does not
# have to be guessed.
WHITE_LIGHT = "WhiteLight"
INFRARED_LIGHT = "InfraredLight"

MAX_LIGHTING_V2_LIGHTS = 4

# A light's brightness lives in one of several named banks, and which one is not
# the same for every light or every model. MiddleLight first, so a device that
# exposes more than one keeps the bank this integration has always used.
LIGHT_BRIGHTNESS_BANKS = ("MiddleLight", "NearLight", "FarLight")


def illuminator_brightness_bank(
    data: dict, channel: int, profile_mode, light_index: int
) -> str:
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
            channel, profile_mode, light_index, bank
        )
        if key in data:
            return bank
    return LIGHT_BRIGHTNESS_BANKS[0]


def infrared_brightness_bank(data: dict, channel: int, profile) -> str:
    """Which brightness bank this channel's infrared emitter really uses.

    The v1 `Lighting` table has the same problem the v2 one had, and the fix for
    v2 (#652, `illuminator_brightness_bank`) never reached it: `MiddleLight[0].Light`
    was hardcoded in the write and in both readers.

    Measured on a DHI-NVR5464-16P-EI, channel 3:

        table.Lighting[3][0].FarLight[0].Light=50
        table.Lighting[3][0].NearLight[0].Light=50
        table.Lighting[3][0].Mode=Auto          <- and no MiddleLight at all

    while channel 6 of the same recorder has MiddleLight and no other bank. So
    naming the wrong one makes the device answer 200 with the body "Error", which
    read as success, and makes the level read as nothing at all.

    Returns None when the row has no brightness bank at all, which is a doorbell:
    a VTO2000A and a VTO2202F serve Lighting[0][0] as Mode only, with no
    MiddleLight/NearLight/FarLight field (measured; #963). Defaulting to
    MiddleLight there sent the write a `MiddleLight[0].Light` term for a field the
    row does not have, and the device threw on it. None means the caller writes the
    mode alone.
    """
    for bank in LIGHT_BRIGHTNESS_BANKS:
        key = "table.Lighting[{0}][{1}].{2}[0].Light".format(channel, profile, bank)
        if key in data:
            return bank
    return None


def infrared_v2_row(data: dict, channel: int, profile_mode):
    """(profile, index, bank) for this channel's infrared emitter in Lighting_V2.

    None when the device serves no such row, which is the signal to use the v1
    `Lighting` table instead.

    **Why this exists.** Measured on a DHI-NVR5464-16P-EI, with every write setting
    a field to the value it already held:

        Lighting_V2[11][0][0].Mode  (LightType=InfraredLight)   200 'OK'
        Lighting_V2[1][0][0].Mode   (LightType=InfraredLight)   200 'OK'
        Lighting[11][0].Mode                                    403 'Authority:check failure.'
        Lighting[1][0].Mode                                     403 'Authority:check failure.'

    and then with a real change, which is the part that settles it: v2
    `Mode` moved `ZoomPrio` -> `Manual` and read back `Manual`. So on that recorder
    the infrared emitter is writable through v2 and refused through v1, and the
    integration only ever wrote v1. Twelve of its fifteen channels have no v2 row
    and keep the old path, where the refusal is real.

    The index is found by what the device calls the row rather than assumed,
    exactly as `illuminator_light_index` does for the white light: channel 11
    reports index 0 as `InfraredLight` and 1 as `WhiteLight`. The bank comes from
    the row too -- channel 11's infrared carries `NearLight` and `FarLight` while
    channel 1's carries `MiddleLight`.

    The live profile is tried first and then 0, the same fallback
    `infrared_profile` uses, because a device can serve nine profiles (channel 11
    reports 0 through 8) and the poll only holds the one it reads.
    """
    for profile in dict.fromkeys([str(profile_mode), "0"]):
        for index in range(MAX_LIGHTING_V2_LIGHTS):
            base = "table.Lighting_V2[{0}][{1}][{2}].".format(channel, profile, index)
            if data.get(base + "LightType") != INFRARED_LIGHT:
                continue
            bank = next(
                (
                    candidate
                    for candidate in LIGHT_BRIGHTNESS_BANKS
                    if base + candidate + "[0].Light" in data
                ),
                LIGHT_BRIGHTNESS_BANKS[0],
            )
            return (profile, index, bank)
    return None


def infrared_transport(v2_row, v1_refused: bool) -> str:
    """Which lighting table to drive this channel's infrared through: "v1" or "v2".

    v1 unless the device has refused it outright for this channel *and* serves a
    v2 row to fall back to.

    **The order matters and the obvious rule is wrong.** The first version of this
    preferred v2 wherever a v2 row existed, which decides from what a device
    *reports it has*. Measured on a DHI-NVR5464-16P-EI that is right -- v1 is
    refused there and v2 works. But #647 carries a directly connected
    IPC-T5442TM-AS-6mm reporting three v2 InfraredLight rows while its v1 table is
    presumably accepted, and that issue's whole complaint is "the CGI values change
    and the physical LEDs do not". Moving every camera that merely reports a v2 row
    onto a table nobody has verified drives its emitter is that complaint, shipped.

    Deciding from what the device *accepts* keeps every working camera where it is
    and only moves the ones that have actually refused. It is also the shape this
    codebase has arrived at independently three times -- `_HOST_CGI_CONFIG_ABSENT`,
    `_RPC2_TABLE_UNAVAILABLE` and `refusals` all learn from a refusal rather than
    from a claim -- while the fifteen capabilities still gated on a model string are
    the recurring bug class (#570, #676, #690).
    """
    if v1_refused and v2_row is not None:
        return "v2"
    return "v1"


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


# The picture adjustments the number platform offers, as the device spells them
# in its VideoColor table.
VIDEO_COLOR_FIELDS = ("Brightness", "Contrast", "Saturation", "Hue")


def video_color_fields(data: dict, channel: int) -> frozenset:
    """Which picture adjustments this channel reports, as a set of field names.

    VideoColor is host-wide and indexed [channel][profile], so a 200 for the
    table says nothing about whether *this* channel is in it. Judged on the
    channel's own row for the same reason read_profile_mode and
    _smart_motion_row are: a recorder answers one request for every channel,
    and taking another channel's row as evidence is how a control comes to
    report a camera it is not attached to.

    Per field rather than a single yes, so a device reporting three of the four
    gets three sliders instead of a fourth that can only ever read unknown.

    Empty when the device named none, which covers both a table it does not
    have and the empty 200 some firmware answers for one -- the same thing
    async_detect_lighting_support judges on.
    """
    if not isinstance(data, dict):
        return frozenset()
    return frozenset(
        field
        for field in VIDEO_COLOR_FIELDS
        if data.get("table.VideoColor[{0}][0].{1}".format(channel, field)) is not None
    )


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
    for key in (
        "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_{0}.DeviceType",
        "table.RemoteDevice[{0}].DeviceType",
    ):
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
    for key in (
        "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_{0}.ProtocolType",
        "table.RemoteDevice[{0}].ProtocolType",
    ):
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

# BackKeyLight States that are known, documented above, and simply not a ring.
#
# These were warned about as though nobody had ever seen them, which is the
# opposite of what the warning is for: it exists to surface a doorbell reporting
# its ring as a number we cannot read, and a number we can already name is not
# that. #872 is a VTO2311R-WP reporting 5 during the call teardown -- after
# HungupPhone, Hangup and IgnoreInvite, immediately before idle -- on a press
# that had already raised the sensor from States 1 and 2. Nothing was missed and
# the reporter was asked for it anyway.
#
# Deliberately not merged into DOORBELL_STATE_EVENTS: these raise no event of
# their own. They are only reasons not to complain.
DOORBELL_KNOWN_QUIET_STATES = frozenset({4, 5, 6, 7, 11})


def event_payload(event: dict) -> dict:
    """An event's payload object, whichever transport delivered it.

    The two streams spell it differently. DHIP sends `Data`, and the CGI wire
    format is `Code=X;action=Y;index=Z;data={json}`, which `parse_event` splits
    on `=` -- so that path produces a lowercase `data`, as the example payloads
    in `on_receive`'s docstring show.

    `_dispatch_event` is shared by both, and several of its reads named only
    `Data`. On the CGI path those found nothing: a `DoorStatus` Pulse read
    closed while the door stood open, a `BackKeyLight` press never raised the
    button, and an `AccessControl` card was never handed to `async_scan_tag` --
    which is the behaviour sharing the function was meant to give that
    transport. Reachable by any doorbell that does not answer `getDeviceClass`
    and is not in `is_doorbell`'s model-prefix list, which is the #690 rebadge
    class.

    Returns {} for a payload that is not a dict. A truncated CGI event leaves
    the raw string there, and `.get` on a string raises AttributeError -- out
    of here, out of `handle_event`, out of `on_receive` and out of the stream
    loop, which wraps it in try/finally with no handler. One malformed event
    took every channel on the host down that way (#475), and the guards in
    `translate_event_code` and `_extract_event_details` are the same guard for
    the same reason.
    """
    data = event.get("data", event.get("Data", {}))
    return data if isinstance(data, dict) else {}


def doorbell_state(event: dict):
    """The BackKeyLight State as an int, or None if it did not say.

    The payload is JSON over DHIP, so this is normally already an int, but
    nothing guarantees it and a string must not read as a different state.
    """
    value = event_payload(event).get("State")
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

    Both casings, for the reason `event_payload` gives: DHIP sends `Index` and
    the CGI wire format parses to `index`. Reading only the first meant that on
    the CGI path every door looked like door 0, so a second door's Open and
    Close wrote to the first door's sensor -- which is exactly what #488 added
    this function to stop.
    """
    try:
        index = int(event.get("Index", event.get("index")))
    except (TypeError, ValueError):
        return 0
    return max(0, index)


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


class DahuaDataUpdateCoordinator(DataUpdateCoordinator):

    # Declared on the class, not only assigned in __init__, because a great many
    # tests build a coordinator with object.__new__ and set only the attributes
    # they are about. channel_option reads this, and those tests reach it through
    # configured_area_name and the authorized plate list, so without a default
    # they fail on the attribute rather than on anything they are testing.
    _channel_config: dict = {}

    # The recorder's disks (#745), on the class for the same reason: the poll and
    # the disk sensors read them, and the many object.__new__ tests do not set
    # them. Only ever reassigned, never mutated in place, so one shared default
    # is safe, like _channel_config above.
    _storage_disks: list = []
    _storage_last_refresh: float = 0.0

    # The recorder's configured camera slots (RemoteDevice), read once at setup
    # like the disks. A diagnostic read is a login the device logs, and the slots
    # change rarely, so it is not polled. Empty on anything that is not a recorder.
    _remote_devices: dict = {}

    # Which subentry of the entry this channel is, or None for a single camera.
    # Declared on the class for the same reason as the line above: a great many
    # tests build a coordinator with object.__new__, and the platforms read this
    # on every entity they add.
    subentry_id: str = None

    # Same reason, and None rather than {} because a dict here would be one dict
    # shared by every coordinator in the process: eleven channels of a recorder
    # would pool their counts and the field would name no channel in particular.
    _events_without_listener: dict = None

    # The flood light's pre-force mode, captured by turn_on before it forces the
    # camera to manual so turn_off can put it back. None means nothing was
    # captured this session, and turn_off must then not write a mode at all --
    # see FloodLight.async_turn_off.
    _floodlight_mode: int = None

    # Which picture adjustments this channel reported at setup. On the class for
    # the reason the three above are: the suite builds coordinators with
    # object.__new__ and sets only what each test is about, and both the poll
    # and the number platform read this. Only ever reassigned, never mutated, so
    # one shared empty default is safe.
    _video_color_fields: frozenset = frozenset()

    """Class to manage fetching data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        events: list,
        address: str,
        port: int,
        rtsp_port: int,
        username: str,
        password: str,
        name: str,
        channel: int,
        use_https: bool = None,
        channel_config: dict = None,
        subentry_id: str = None,
    ) -> None:
        """Initialize the coordinator.

        `channel_config` is this channel's own settings, which is a subentry's
        data on a merged recorder and None for a single camera (#827). It matters
        because several options were per entry, and an entry was a channel: a
        recorder where one camera has a siren and the rest do not would otherwise
        have had `manual_siren` applied to every channel at once the moment its
        entries were merged. See `channel_option`.
        """
        self._channel_config = dict(channel_config or {})
        self.subentry_id = subentry_id
        # Self signed certs are used over HTTPS so we'll disable SSL verification.
        # connector_owner=False keeps the shared pool alive when this session closes.
        self._session = ClientSession(
            connector=_acquire_connector(address), connector_owner=False
        )

        # The client used to communicate with Dahua devices
        self.client: DahuaClient = DahuaClient(
            username,
            password,
            address,
            port,
            rtsp_port,
            self._session,
            use_https,
            use_rpc2=entry.options.get(CONF_USE_RPC2, False),
            illuminator_restore_store=IlluminatorRestoreStore(hass, entry.entry_id),
            channel_snapshot_store=ChannelLightingSnapshotStore(hass, entry.entry_id),
        )

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
        # Host-wide: DahuaHostEventStream turns this into one request for the
        # recorder, regardless of how many channels are configured.
        self.poll_video_motion = entry.options.get(CONF_POLL_VIDEO_MOTION, False)
        self._supports_coaxial_control = False
        # Doorbell call states already complained about, so the warning below is
        # one per state rather than one per ring.
        self._unknown_doorbell_states: set = set()
        self._supports_rpc2_siren = False
        self._supports_rpc2_security_light = False
        self._alarm_output_slots = 0

        # Read off the local `entry` rather than through channel_option, because
        # that reads self.config_entry and DataUpdateCoordinator does not set it
        # until super().__init__ further down. The precedence is the same one
        # channel_option applies: this channel's own answer, then the entry's.
        def _channel_first(key, default=None):
            if key in self._channel_config:
                return self._channel_config[key]
            return entry.options.get(key, default)

        self._nvr_active_deterrence = _channel_first(CONF_NVR_ACTIVE_DETERRENCE, False)
        self._manual_siren = _channel_first(CONF_MANUAL_SIREN, False)
        self._manual_security_light = _channel_first(CONF_MANUAL_SECURITY_LIGHT, False)
        self._supports_disarming_linkage = False
        self._supports_event_notifications = False
        # What the device's own cloud OTA check last found, read from its
        # _DHCloudUpgrade_ config table. Reading it is local; see
        # _async_probe_cloud_upgrade.
        self._supports_cloud_upgrade = False
        self._cloud_firmware_version: str | None = None
        self._cloud_upgrade_checked_at: float | None = None
        self._ivs_rules = []
        self._supports_smart_motion_detection = False
        self._supports_ptz_position = False
        self._supports_lighting = False
        self._supports_day_night_color = False
        self._video_color_fields: frozenset = frozenset()
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
        self._supports_channel_scoped_illuminator = False

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

        # event_key -> the useful fields of the most recent event for that code
        # (rule name, direction, object type), for the sensor to expose as
        # attributes so an automation can tell which rule tripped (#373).
        self._dahua_event_details: Dict[str, dict] = dict()

        self._floodlight_mode = None

        # A recorder's disks (name, state, capacity, error), refreshed slowly
        # because each read costs a login the device logs. Empty on anything that
        # is not a recorder. See _async_refresh_storage (#745).
        self._storage_disks: list = []
        self._storage_last_refresh: float = 0.0

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
        """Starts the event listeners for IP cameras (this does not work for doorbells (VTO))"""
        if self.is_indoor_monitor_without_video():
            # None of the camera events exist on it: see get_event_list.
            _LOGGER.debug(
                "%s is an indoor monitor without a camera; no camera event stream",
                self._address,
            )
            return
        if self.events is not None:
            # Join this host's stream rather than opening another one. The
            # device sends every channel's events down any stream, so one is
            # enough no matter how many channels are configured.
            _host_stream(self.hass, self._address).register(self)

    async def async_start_vto_event_listener(self):
        """Starts the event listeners for doorbells (VTO). This will not work for IP cameras"""
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
                        self._address,
                        self._username,
                        self._password,
                        False,
                        self.on_receive_vto_event,
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
                    time.monotonic() - started,
                    consecutive_failures,
                    getattr(protocol, "received_data", False),
                )
                _LOGGER.error(
                    "VTO connection to %s failed, retrying in %ss: %s",
                    self._address,
                    round(delay),
                    ex,
                )
                await asyncio.sleep(delay)
                continue

            delay, consecutive_failures = vto_retry_state(
                time.monotonic() - started, consecutive_failures, protocol.received_data
            )
            if delay:
                _LOGGER.warning(
                    "Disconnected from VTO at %s, reconnecting in %ss",
                    self._address,
                    round(delay),
                )
                await asyncio.sleep(delay)
            else:
                # It was connected and talking, so the socket ending is not the
                # device refusing us. Go straight back.
                _LOGGER.warning(
                    "Disconnected from VTO at %s, reconnecting", self._address
                )

    async def async_stop(self, event: Any = None):
        """Stop anything we need to stop"""
        await _release_host_stream(self)
        if self._vto_task is not None:
            task = self._vto_task
            self._vto_task = None
            # Close the transport before cancelling: the task is parked on
            # `await protocol.disconnected`, and cancelling a task does not
            # close an asyncio transport. Leaving it open leaks the socket to
            # port 5000, its event subscription and its keep-alive for an entry
            # that no longer exists, once per reload.
            client = getattr(self, "_vto_client", None)
            if client is not None:
                client.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
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
                "%s is answering again, polling every %ss",
                self._address,
                configured.total_seconds(),
            )
            self.update_interval = configured

    def _back_off_poll_interval(self, consecutive: int) -> None:
        """Poll a device that is not answering less often, not just as often.

        Every request costs the device a connection and a login it has to
        refuse. Keeping the configured cadence against a device that is already
        refusing is what turns a device that ran out of connections into one
        that stays out of them until it is power cycled.
        """
        interval = failure_backoff(
            get_configured_scan_interval(self.config_entry), consecutive
        )
        if self.update_interval != interval:
            _LOGGER.debug(
                "%s has failed %s times, backing off to %ss",
                self._address,
                consecutive,
                interval.total_seconds(),
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
        return any(
            self.config_entry.options.get(platform, True) for platform in platforms
        )

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
        if status is None:
            # An RPC2 refusal carries a code rather than an HTTP status, and a
            # code is still the device answering -- see Rpc2MethodRefused.
            status = getattr(exception, "code", None)
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
        # Per entry, so a recorder's channels refusing in the same instant count
        # once rather than once each.
        refusals = async_record_host_auth_refusal(
            self._address, self.config_entry.entry_id
        )
        if refusals < MAX_AUTH_REFUSALS:
            _LOGGER.debug(
                "Authentication refused by %s (%d of %d). Not treating it as a wrong password yet",
                self._address,
                refusals,
                MAX_AUTH_REFUSALS,
            )
            self._back_off_poll_interval(
                async_record_host_failure(
                    self.hass, self._address, self.config_entry.entry_id
                )
            )
            return UpdateFailed("Authentication refused by " + self._address)
        _LOGGER.warning(
            "%s has refused these credentials %d times, so Home Assistant will stop trying them and ask for new ones. Repeated failed logins can lock a Dahua device out for around thirty minutes, so polling stops until the credentials are re-entered",
            self._address,
            refusals,
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
                _LOGGER.debug(
                    "Device reports class=%s", self._device_class or "<no answer>"
                )

                # A recorder has disks worth a health sensor; nothing else does.
                # Read them once here so the disk sensors can be enumerated, then
                # refresh slowly in the poll below (#745).
                if self.is_recorder_host():
                    await self._async_refresh_storage()
                    await self._async_refresh_remote_devices()

                # Some Dahua firmwares index channels from 0, others from 1. The default
                # is to auto-detect: if a snapshot at index 0 succeeds, treat this camera as
                # 0-indexed and reset channel_number accordingly. Users on cameras where this
                # heuristic gets it wrong (HTTP snapshot at 0 succeeds but RTSP only streams
                # on channel=1) can disable it via the integration options.
                auto_detect = self.channel_option(CONF_AUTO_DETECT_CHANNEL, True)
                if auto_detect:
                    # Asked once for the device and shared, and a device that does
                    # not answer leaves this alone rather than renumbering the
                    # channel behind the user's back (#724).
                    zero_indexed = await async_device_is_zero_indexed(
                        self.client, self.client.device_key
                    )
                    # A doorbell is excluded because channel 0 does not exist on a VTO.
                    if zero_indexed and not self.is_doorbell():
                        self._channel_number = self._channel
                _LOGGER.debug(
                    "Using channel number %s (auto_detect=%s)",
                    self._channel_number,
                    auto_detect,
                )

                await self._async_probe_direct_deterrence()
                if self._wanted_by(LIGHT, SWITCH) and not self.uses_rpc2_deterrence():
                    try:
                        coaxial_channel = self.get_coaxial_status_channel()
                        await self.client.async_get_coaxial_control_io_status(
                            coaxial_channel
                        )
                        self._supports_coaxial_control = True
                    except PROBE_REFUSED as probe_error:
                        self._note_probe_refusal("coaxial_control", probe_error)
                        self._supports_coaxial_control = False
                _LOGGER.debug(
                    "Device supports Coaxial Control=%s", self._supports_coaxial_control
                )

                try:
                    alarm_output_data = await self.client.async_get_alarm_output_slots()
                    try:
                        self._alarm_output_slots = max(
                            0, int(alarm_output_data.get("result", "0"))
                        )
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
                _LOGGER.debug(
                    "Device supports disarming linkage=%s",
                    self._supports_disarming_linkage,
                )

                try:
                    await self.client.async_get_event_notifications()
                    self._supports_event_notifications = True
                except PROBE_FAILED as probe_error:
                    self._note_probe_refusal("event_notifications", probe_error)
                    self._supports_event_notifications = False
                _LOGGER.debug(
                    "Device supports event notifications=%s",
                    self._supports_event_notifications,
                )

                if self._wanted_by(UPDATE):
                    await self._async_probe_cloud_upgrade()
                    _LOGGER.debug(
                        "Device supports cloud upgrade=%s",
                        self._supports_cloud_upgrade,
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
                _LOGGER.debug(
                    "Device supports PTZ position=%s", self._supports_ptz_position
                )

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
                _LOGGER.debug(
                    "Device supports smart motion detection=%s",
                    self._supports_smart_motion_detection,
                )

                try:
                    remote_ivs = self.is_nvr_channel()
                    ivs_table = (
                        await self.client.async_get_remote_ivs_rules(self._channel)
                        if remote_ivs
                        else await self.client.async_get_ivs_rules()
                    )
                    name = (
                        "RemoteVideoAnalyseRule" if remote_ivs else "VideoAnalyseRule"
                    )
                    self._ivs_rules = ivs_rules_for_channel(
                        ivs_table, self._channel, name
                    )
                    self._ivs_discovery_diagnostics = ivs_discovery_diagnostics(
                        ivs_table, self._channel, name
                    )
                    _LOGGER.debug("IVS discovery: %s", self._ivs_discovery_diagnostics)
                    if remote_ivs:
                        for rule in self._ivs_rules:
                            rule["remote"] = True
                    data.update(ivs_table)
                except PROBE_FAILED + (ConnectionError, ValueError) as err:
                    self._ivs_rules = []
                    self._ivs_discovery_diagnostics = {
                        "channel": self._channel,
                        "source": (
                            "RemoteVideoAnalyseRule"
                            if remote_ivs
                            else "VideoAnalyseRule"
                        ),
                        "discovered_count": 0,
                        "read_error": type(err).__name__,
                    }
                _LOGGER.debug("Device IVS rules=%s", self._ivs_rules)

                # Day/Night mode. Judged by whether this channel's row came
                # back, not by whether the request raised: async_get_config
                # swallows a ClientResponseError and returns {}, and a device
                # can answer 200 with an empty body for a table it lacks.
                try:
                    options = await self.client.async_get_video_in_options()
                    self._supports_day_night_color = (
                        day_night_color_name(options, self._channel) is not None
                    )
                except PROBE_FAILED as probe_error:
                    self._note_probe_refusal("day_night_color", probe_error)
                    self._supports_day_night_color = False
                _LOGGER.debug(
                    "Device supports day/night mode=%s", self._supports_day_night_color
                )

                # The picture adjustments, probed rather than read blind on
                # every poll. #1006 is a camera account in the device's `user`
                # group, which answers 403 to this table: the read sat in the
                # poll's gather with no capability check and no handler, so one
                # refusal failed the whole refresh, and on the first one that is
                # ConfigEntryNotReady -- every entity of a working camera
                # unavailable for four sliders it was never going to serve.
                #
                # Judged on this channel's own row, not on the request
                # succeeding; see video_color_fields. A device that names none
                # gets no number entities, which is the rule the profile sensor
                # established (#641): an entity that can only read unknown is
                # worse than no entity.
                #
                # Costs a recorder one request rather than one per channel. The
                # URL carries no channel, so the shared read cache answers it
                # once for every channel setting up and holds it for
                # CONFIG_CACHE_TTL_SECONDS.
                if self._wanted_by(NUMBER):
                    try:
                        self._video_color_fields = video_color_fields(
                            await self.client.async_get_video_color(), self._channel
                        )
                    except PROBE_FAILED as probe_error:
                        # A timeout is cached as "no" here, like every other
                        # probe in this block. That is deliberate rather than
                        # overlooked: it costs the sliders until the entry is
                        # reloaded, and the alternative -- retrying a capability
                        # question on a poll path -- is what this fix is
                        # removing. _note_probe_refusal records whether the
                        # device answered, so the two are told apart in
                        # diagnostics.
                        self._note_probe_refusal("video_color", probe_error)
                        self._video_color_fields = frozenset()
                    # Inside the gate, like the cloud upgrade record's: a device
                    # whose number platform is switched off was never asked, and
                    # saying it reports none would read as the device refusing.
                    _LOGGER.debug(
                        "Channel %s reports picture adjustments %s",
                        self._channel,
                        sorted(self._video_color_fields) or "none",
                    )

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
                            self._channel,
                            self._address,
                        )
                except PROBE_FAILED as probe_error:
                    self._note_probe_refusal("channel_model", probe_error)
                    self._channel_model = None
                if self._channel_model:
                    _LOGGER.debug(
                        "Channel %s carries a %s; the device itself reports %s",
                        self._channel,
                        self._channel_model,
                        self.model,
                    )
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
                        (
                            "created"
                            if self._channel in (smart_motion_rows or ())
                            else "not created"
                        ),
                    )

                is_doorbell = self.is_doorbell()
                _LOGGER.debug("Device is a doorbell=%s", is_doorbell)

                is_flood_light = self.is_flood_light()
                _LOGGER.debug("Device is a floodlight=%s", is_flood_light)

                self._supports_floodlightmode = self.supports_floodlightmode()

                self._supports_lighting = await self.async_detect_lighting_support()
                _LOGGER.debug(
                    "Device supports infrared lighting=%s",
                    self.supports_infrared_light(),
                )

                # Checking lighting_v2 support
                try:
                    await self.client.async_get_lighting_v2()
                    self._supports_lighting_v2 = True
                except PROBE_FAILED as probe_error:
                    self._note_probe_refusal("lighting_v2", probe_error)
                    self._supports_lighting_v2 = False
                    pass
                _LOGGER.debug(
                    "Device supports Lighting_V2=%s", self._supports_lighting_v2
                )

                # IPC-Color4M-TZ accepts ordinary Lighting_V2 writes but its
                # physical white emitter also requires LightingScheme. Probe
                # that second capability before exposing the entity.
                if self.model.upper().startswith("IPC-COLOR4M-TZ"):
                    try:
                        scheme = await self.client.async_get_lighting_scheme()
                        self._supports_lighting_scheme_illuminator = any(
                            key.endswith(".LightingMode") for key in scheme
                        )
                    except (
                        ClientError,
                        TimeoutError,
                        ConnectionError,
                        ValueError,
                        KeyError,
                        TypeError,
                    ):
                        self._supports_lighting_scheme_illuminator = False
                    _LOGGER.debug(
                        "Device supports LightingScheme illuminator=%s",
                        self._supports_lighting_scheme_illuminator,
                    )

                # Cameras whose LightingScheme answers per channel -- a recorder
                # channel such as a VSIPP on an NVR -- need the same two-table
                # white-light contract but in the channel-scoped shape the
                # whole-table path above cannot consume. Probed by capability,
                # not model name (#959), and kept a separate flag so routing
                # never sends these to the whole-table method. Mutually
                # exclusive with the Color4M path, which is checked first.
                # getattr, not a bare read: both flags are set in __init__, but
                # the suite builds coordinators with object.__new__ and sets only
                # some attributes, and uses_lighting_scheme_illuminator already
                # reads this one the same way.
                if getattr(self, "_supports_lighting_v2", False) and not getattr(
                    self, "_supports_lighting_scheme_illuminator", False
                ):
                    # The probe is read-only and self-guards, but keep the call
                    # itself from ever failing setup: a capability question must
                    # not be the thing that stops a device initialising.
                    try:
                        self._supports_channel_scoped_illuminator = await self.client.async_channel_scoped_white_light_supported(
                            self._channel
                        )
                    except Exception:  # pylint: disable=broad-except
                        self._supports_channel_scoped_illuminator = False
                    _LOGGER.debug(
                        "Device supports channel-scoped illuminator=%s",
                        self._supports_channel_scoped_illuminator,
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
                _LOGGER.debug(
                    "Device supports privacy mode=%s", self._supports_privacy_mode
                )

                if not is_doorbell:
                    # Start the event listeners for IP cameras
                    await self.async_start_event_listener()

                    try:
                        # Some cams don't support profile modes, check and see... use 2 to check
                        #
                        # This channel's row, not channel 0's. The name was
                        # hardcoded to `Lighting[0][2]`, so on a merged recorder
                        # every channel's answer came from camera 1's table. The
                        # consequence is the one `infrared_profile` and
                        # `read_profile_mode` each have a paragraph about: with
                        # this False the poll never reads VideoInMode, so
                        # `_profile_mode` stays "0", and a camera running Night
                        # has its lighting read from and written to the Day row,
                        # where the device accepts the write and renders from
                        # somewhere else.
                        conf = await self.client.async_get_config_lighting(
                            self._channel, 2
                        )
                        # We'll get back an error like this if it doesn't work:
                        # Error: Error -1 getting param in name=Lighting[0][1]
                        # Otherwise we'll get multiple lines of config back
                        self._supports_profile_mode = len(conf) > 1
                    except PROBE_FAILED as probe_error:
                        self._note_probe_refusal("profile_mode", probe_error)
                        _LOGGER.debug(
                            "Cam does not support profile mode. Will use mode 0"
                        )
                        self._supports_profile_mode = False
                    _LOGGER.debug(
                        "Device supports profile mode=%s", self._supports_profile_mode
                    )
                else:
                    # Start the event listeners for doorbells (VTO)
                    await self.async_start_vto_event_listener()

                self.initialized = True
            except ClientResponseError as exception:
                if exception.status == 401:
                    raise self._auth_refused(exception) from exception
                _LOGGER.warning(
                    "Failed to initialize device at %s: %s", self._address, exception
                )
                self._back_off_poll_interval(
                    async_record_host_failure(
                        self.hass, self._address, self.config_entry.entry_id
                    )
                )
                raise UpdateFailed(
                    "Dahua device at " + self._address + " isn't fully initialized yet"
                )
            except Exception as exception:
                _LOGGER.warning(
                    "Failed to initialize device at %s: %s", self._address, exception
                )
                self._back_off_poll_interval(
                    async_record_host_failure(
                        self.hass, self._address, self.config_entry.entry_id
                    )
                )
                raise UpdateFailed(
                    "Dahua device at " + self._address + " isn't fully initialized yet"
                )

        # This is the event loop code that's called every n seconds
        try:
            # A recorder's disks, refreshed at most hourly because each read is a
            # login the device logs. Gated on disks having been found at setup, so
            # anything that is not a recorder never pays for it (#745).
            if self._storage_disks and time.time() - self._storage_last_refresh >= 3600:
                await self._async_refresh_storage()

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
                    # The preset select reads this key. Carrying the last value
                    # stops a refusal from resetting the select to "0".
                    previous = (getattr(self, "data", None) or {}).get(
                        "status.PresetID"
                    )
                    if previous is None:
                        return None
                    return {"status.PresetID": previous}

            # Figure out which APIs we need to call and then fan out and gather the results
            # Motion detection state is read by the camera entity as well as
            # the switch, so it survives either one being enabled.
            coros = []
            if self._wanted_by(CAMERA, SWITCH):
                coros.append(
                    asyncio.ensure_future(
                        self.client.async_get_config_motion_detection()
                    )
                )
            # Only the preset position select reads this, and it is one of the
            # two per-poll calls the config cache does not cover.
            if self.supports_day_night_color() and self._wanted_by(SELECT):
                coros.append(
                    asyncio.ensure_future(self.client.async_get_video_in_options())
                )
            if self._supports_ptz_position and self._wanted_by(SELECT):
                coros.append(asyncio.ensure_future(_ptz_position()))
            if self.supports_infrared_light() and self._wanted_by(LIGHT):
                coros.append(
                    asyncio.ensure_future(
                        self.client.async_get_config_lighting(
                            self._channel, self._profile_mode
                        )
                    )
                )
            if self._supports_disarming_linkage and self._wanted_by(SWITCH):
                coros.append(
                    asyncio.ensure_future(self.client.async_get_disarming_linkage())
                )
            if self._supports_event_notifications and self._wanted_by(SWITCH):
                coros.append(
                    asyncio.ensure_future(self.client.async_get_event_notifications())
                )
            if self.supports_alarm_output() and self._wanted_by(SWITCH):
                coros.append(
                    asyncio.ensure_future(self.client.async_get_alarm_output_state())
                )
            # The siren switch and the security light both read this one, over
            # RPC2 on a direct camera and over CGI otherwise. Both go through
            # _async_coaxial_status, because a device that refuses this must not
            # take the whole entry offline, and the RPC2 branch used to call the
            # client directly and did exactly that (#848).
            #
            # The two conditions stay as they were: reads_coaxial_status explains
            # why the RPC2 branch keeps its own.
            if self.uses_rpc2_deterrence() and self._wanted_by(LIGHT, SWITCH):
                coros.append(
                    asyncio.ensure_future(
                        self._async_coaxial_status(
                            self.get_rpc2_coaxial_status_channel()
                        )
                    )
                )
            elif self._supports_coaxial_control and self.reads_coaxial_status():
                coaxial_channel = self.get_coaxial_status_channel()
                coros.append(
                    asyncio.ensure_future(self._async_coaxial_status(coaxial_channel))
                )
            if getattr(self, "_ivs_rules", []) and self._wanted_by(SWITCH):
                ivs_read = (
                    self.client.async_get_remote_ivs_rules(self._channel)
                    if self.is_nvr_channel()
                    else self.client.async_get_ivs_rules()
                )
                coros.append(asyncio.ensure_future(ivs_read))
            if self._supports_smart_motion_detection and self._wanted_by(
                SWITCH, SELECT
            ):
                coros.append(
                    asyncio.ensure_future(
                        self.client.async_get_smart_motion_detection()
                    )
                )
            if self.supports_smart_motion_detection_amcrest() and self._wanted_by(
                SWITCH
            ):
                coros.append(
                    asyncio.ensure_future(
                        self.client.async_get_video_analyse_rules_for_amcrest()
                    )
                )
            if self.is_amcrest_doorbell() and self._wanted_by(LIGHT):
                coros.append(
                    asyncio.ensure_future(self.client.async_get_light_global_enabled())
                )
            # Picture adjustments for the number platform (brightness, contrast,
            # saturation, hue): one read per poll, and only where the device
            # actually reported them at setup. Gated on the probe rather than on
            # the platform alone, so a device that refuses the table is never
            # asked again, and wrapped, so a permissions change made after setup
            # cannot take the entry down the way #1006 did.
            if self._video_color_fields and self._wanted_by(NUMBER):
                coros.append(asyncio.ensure_future(self._async_fetch_video_color()))
            # Lighting_V2 is the light platform's table -- except that the
            # Amcrest doorbell's "Security Light" is a *select*, and its
            # current_option reads table.Lighting_V2[0][0][1].Mode/.State. A
            # select is not a light, so gating this on LIGHT alone left that
            # entity reading an absent table and reporting "Off" forever for
            # anyone who turned the light platform off. The condition mirrors
            # the one select.py creates it under, so nothing else over-fetches.
            if self._supports_lighting_v2 and (
                self._wanted_by(LIGHT)
                or (
                    self.is_amcrest_doorbell()
                    and self.supports_security_light()
                    and self._wanted_by(SELECT)
                )
            ):
                coros.append(asyncio.ensure_future(self._async_fetch_lighting_v2()))
            if getattr(
                self, "_supports_lighting_scheme_illuminator", False
            ) and self._wanted_by(LIGHT):
                coros.append(
                    asyncio.ensure_future(self.client.async_get_lighting_scheme())
                )
            # Only the privacy mode switch reads this one.
            if self._supports_privacy_mode and self._wanted_by(SWITCH):
                coros.append(asyncio.ensure_future(self._async_fetch_privacy_mode()))
            # Only the camera-link selects of an indoor monitor read this one.
            if self.is_indoor_monitor() and self._wanted_by(SELECT):
                coros.append(
                    asyncio.ensure_future(self._async_fetch_vth_camera_links())
                )

            # Gather results and update the data map
            results = await asyncio.gather(*coros)
            for result in results:
                if result is not None:
                    data.update(result)

            # The cloud OTA record is a local config read, but it is only of
            # use to the informational update entity, so it is not read at all
            # when that platform is switched off -- the same rule the coaxial
            # status followed in #817. It is not read every poll either: the
            # device rewrites the record only after its own OTA check, so the
            # answer is reused for hours. A refusal here is not fatal; the last
            # known answer stands.
            if (
                getattr(self, "_supports_cloud_upgrade", False)
                and self._wanted_by(UPDATE)
                and self._cloud_upgrade_read_is_due()
            ):
                try:
                    info = await self.client.async_get_cloud_upgrade_info()
                    version = cloud_upgrade_version(info)
                    if version is not None:
                        self._cloud_firmware_version = version
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.debug(
                        "Could not read the cloud upgrade record", exc_info=True
                    )
                finally:
                    # Stamped whether or not the read worked: a device that
                    # starts refusing stays on the last known answer for the
                    # refresh interval instead of being retried every poll.
                    self._cloud_upgrade_checked_at = time.monotonic()

            if getattr(
                self, "_supports_lighting_scheme_illuminator", False
            ) and self._wanted_by(LIGHT):
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
            if (
                (
                    (
                        self.supports_security_light()
                        and not self.uses_rpc2_deterrence(1)
                    )
                    or self.is_flood_light()
                )
                and not self._supports_lighting_v2
                and self._wanted_by(LIGHT)
            ):
                # Wrapped, like the fan-out's own read. This line is reached
                # only when the setup probe failed, so the device that needs it
                # most is the one whose table is refused outright -- and an
                # unwrapped read there failed every poll for ever. See
                # _async_fetch_lighting_v2.
                light_v2 = await self._async_fetch_lighting_v2()
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
            _LOGGER.warning(
                "Failed to sync device state for %s: %s. See README to enable debug logs to get full exception",
                self._address,
                detail,
            )
            _LOGGER.debug(
                "Failed to sync device state for %s", self._address, exc_info=exception
            )
            consecutive = async_record_host_failure(
                self.hass, self._address, self.config_entry.entry_id
            )
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
        # First, like the camera path's. _remember_event stores what the device
        # sent rather than what we enriched it with, and the next line adds the
        # device name, so the order is the contract.
        #
        # This call was missing, so diagnostics' recent-events buffer was empty
        # for every doorbell -- the one device class whose BackKeyLight state
        # numbers the buffer exists to capture. #573 and #872 are both "which
        # number did it send", and the answer was a dump field that always read
        # as the device having sent nothing.
        self._remember_event(event)
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
        details = self._extract_event_details(event)
        codes = self.translate_event_code(event)
        raw_code = event.get("Code")
        data = event_payload(event)
        if data.get("Class") == "Normal":
            rule_id = data.get("RuleID")
            if rule_id is None:
                rule_id = data.get("RuleId")
            if rule_id is not None and any(
                rule["id"] == str(rule_id) for rule in self.get_ivs_rules()
            ):
                codes.append(f"IVSRule_{rule_id}")
                if action == "Start" and raw_code:
                    # Remember which rules this code actually lit, so the Stop
                    # below clears exactly those rather than trusting that the
                    # configured Type is spelled like the event Code.
                    active = getattr(self, "_ivs_active_rules", None)
                    if active is None:
                        active = self._ivs_active_rules = {}
                    active.setdefault(raw_code, set()).add(str(rule_id))
            else:
                # Configuration IDs and event IDs are not proven equivalent on
                # every NVR. Record the mismatch; never guess by name or index.
                reason = "missing_rule_id" if rule_id is None else "unknown_rule_id"
                counts = getattr(self, "_ivs_unmatched_counts", None)
                if counts is None:
                    counts = self._ivs_unmatched_counts = {}
                first = reason not in counts
                counts[reason] = counts.get(reason, 0) + 1
                self._ivs_last_unmatched = {
                    "channel": self._channel,
                    "reason": reason,
                    "rule_id": (
                        str(rule_id)[:80] if isinstance(rule_id, (str, int)) else None
                    ),
                    "code": str(raw_code or "")[:80],
                }
                if first:
                    _LOGGER.debug(
                        "Normal IVS event did not match a discovered rule: %s",
                        self._ivs_last_unmatched,
                    )

        # Dahua sends one Start per rule but a single Stop for the whole event
        # code, and that Stop names only one rule (the first Start's EventID):
        #   Start RuleID=9 EventID=10161
        #   Start RuleID=7 EventID=10163
        #   Start RuleID=8 EventID=10165
        #   Stop  RuleID=9 EventID=10161
        # So a Stop means "this code is now inactive", not "this rule ended".
        # Clear every rule that code lit, even when the Stop has no usable data.
        #
        # Those rules are only being cleared. The Stop's data describes the one
        # rule it names, so the others are kept out of the details write below
        # and keep the name, direction and object type of their own last event.
        clear_only = set()
        if action == "Stop" and raw_code:
            active = getattr(self, "_ivs_active_rules", None) or {}
            for rule_id in sorted(active.pop(raw_code, ())):
                rule_code = f"IVSRule_{rule_id}"
                if rule_code not in codes:
                    codes.append(rule_code)
                    clear_only.add(rule_code)
            # Also match by configured Type, and add those to the tracked rules
            # rather than using it only when nothing was tracked: a reload loses
            # the tracking, so a rule lit before it is known only by its Type
            # even when another rule of the same code was lit after it.
            for rule in self.get_ivs_rules():
                if rule.get("type") != raw_code:
                    continue
                rule_code = f"IVSRule_{rule['id']}"
                if rule_code not in codes:
                    codes.append(rule_code)
                    clear_only.add(rule_code)

        for code in codes:
            event_key = self.get_event_key(code)

            if code == "AccessControl":
                card_id = event_payload(event).get("CardNo", "")
                if card_id:
                    card_id_md5 = hashlib.md5(card_id.encode()).hexdigest()
                    self.hass.async_create_task(
                        async_scan_tag(self.hass, card_id_md5, self.get_device_name())
                    )

            listeners = self._dahua_event_listeners.get(event_key)
            if not listeners:
                # The event arrived, was decided to belong to this channel, and
                # updates nothing. Worth counting rather than dropping in silence:
                # the timestamp a binary sensor reads is written below this line, so
                # a sensor that exists while its key is absent here is a sensor that
                # can never move, and from the outside that is indistinguishable
                # from the device having stopped sending. It is the reading #825 has
                # been unable to get: the event reaches the bus either way.
                counts = self._events_without_listener
                if counts is None:
                    counts = self._events_without_listener = {}
                counts[event_key] = counts.get(event_key, 0) + 1
                continue

            # Keep the triggering event's rule name / direction / object type, so
            # the sensor can report which rule tripped (#373). Only when the event
            # carried any -- a plain VideoMotion leaves whatever was last there
            # rather than blanking it, matching how the timestamp persists.
            if details and code not in clear_only:
                self._dahua_event_details[event_key] = details

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
                    if event_payload(event).get("Status", "") == "Open":
                        self._dahua_event_timestamp[event_key] = int(time.time())
                    else:
                        self._dahua_event_timestamp[event_key] = 0
                elif code not in PULSE_STATE_CODES:
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
                    # DoorUnlocked and DoorUnlockFailed used to be excluded from
                    # the branch above and land here, where the ring check reads
                    # State 8 as "not 1 or 2" and writes the timestamp to 0. So
                    # they could never raise anything, whatever listened for
                    # them. They are moments -- the door unlocked -- rather than
                    # call states, so the momentary branch is theirs, and only
                    # DoorbellPressed, which really does carry a state, belongs
                    # here. PULSE_STATE_CODES is now the whole test.
                    #
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
                    state = event_payload(event).get("State", 0)
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
        buffer.append(
            {"seconds_ago_at_capture": int(time.time()), "event": dict(event)}
        )

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

    def _extract_event_details(self, event: dict) -> dict:
        """The fields of an IVS/smart event an automation wants, or {} if none.

        Which rule tripped (`rule_name`/`rule_id`), which way a line was crossed
        (`direction`), and what was seen (`object_type`). All optional: a plain
        VideoMotion, or a payload that arrived truncated, carries none and yields
        an empty dict, so the sensor exposes nothing extra.

        Read as defensively as translate_event_code, and for the same reason: a
        truncated payload leaves a string rather than a dict here, and a device
        sends `"Object": null`, so a careless `.get` chain would raise out of the
        stream loop and take every channel's events down with it (#475). RuleId is
        read in both casings because this NVR sends `RuleId` on a CrossLine event
        and `RuleID` on a HumanTrait one, measured on a DHI-NVR5464. "Unknown" is
        dropped rather than surfaced, since it is the device saying it did not
        classify the object, not a useful value for an automation.
        """
        data = event_payload(event)
        details = {}
        name = data.get("Name")
        if name:
            details["rule_name"] = name
        rule_id = data.get("RuleId", data.get("RuleID"))
        if rule_id is not None:
            details["rule_id"] = rule_id
        direction = data.get("Direction")
        if direction:
            details["direction"] = direction
        object_type = (data.get("Object") or {}).get("ObjectType")
        if object_type and object_type.lower() != "unknown":
            details["object_type"] = object_type
        return details

    def translate_event_code(self, event: dict):
        """
        translate_event_code returns a list of event codes to dispatch.
        For CrossLine/CrossRegion events with a recognized ObjectType, returns both the
        original code AND the SmartMotion* code (if listeners exist), so both sensors fire.
        """
        code = event.get("Code", "")

        if code == "CrossLineDetection" or code == "CrossRegionDetection":
            # `event_payload` is where the "not a dict" guard lives now. It
            # matters here: parse_event turns the payload into a dict only when
            # it is valid JSON, a truncated event leaves the raw string, and
            # .get() on a string raises AttributeError -- out of this call, out
            # of handle_event, out of on_receive, and out of the stream loop,
            # which wraps it in try/finally with no handler. One malformed
            # CrossLine event took the event stream for every channel on the
            # host down that way (#475). A payload we could not read is a
            # payload with no ObjectType, not a reason to stop listening.
            data = event_payload(event)
            # `or {}` rather than a default, because the key being present with a
            # null is not the same as the key being absent: `.get("Object", {})`
            # returns None for `"Object": null` and the next `.get` raises. The
            # device does send nulls -- `"Track": None` is in this module's own
            # example payload -- and a CrossLine event that detected no object is
            # exactly when it would. Same reasoning as the dict guard above,
            # which #475 added for the other half of this.
            object_type = (data.get("Object") or {}).get("ObjectType", "")
            object_type = (object_type or "").lower()
            codes = []

            # Always include the original CrossLine/CrossRegion if a listener exists
            if self._dahua_event_listeners.get(self.get_event_key(code)):
                codes.append(code)

            # Also include SmartMotion translation if applicable
            if object_type == "human":
                if self._dahua_event_listeners.get(
                    self.get_event_key("SmartMotionHuman")
                ):
                    codes.append("SmartMotionHuman")
                elif not codes:
                    codes.append("SmartMotionHuman")
            elif object_type == "vehicle":
                if self._dahua_event_listeners.get(
                    self.get_event_key("SmartMotionVehicle")
                ):
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
        if (
            numeric_state in DOORBELL_STATE_EVENTS
            or numeric_state in DOORBELL_KNOWN_QUIET_STATES
            or numeric_state == 0
        ):
            # 8 and 9 are the unlock results, handled separately; 0 is idle,
            # which is the normal way a call ends; and the quiet set is the
            # documented states that are not rings, which there is nothing to
            # report about.
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
            "1 and 2 for ringing, 8 and 9 for unlock, 0 for idle, and "
            "4, 5, 6, 7 and 11 for call handling that is not a ring. If the "
            "doorbell was ringing when this appeared, please report this state "
            "number at %s so it can be added",
            self.get_device_name(),
            raw_state,
            ISSUE_URL,
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

    def get_event_details(self, event_name: str) -> dict:
        """The rule name, direction and object type of the most recent event for
        this code, for the sensor to expose as attributes (#373). Empty until an
        event that carried any has arrived.
        """
        event_key = self.get_event_key(event_name)
        return self._dahua_event_details.get(event_key, {})

    def add_dahua_event_listener(
        self, event_name: str, listener: CALLBACK_TYPE
    ) -> CALLBACK_TYPE:
        """Listen for one event on this channel, and return how to stop.

        The return value is what `Entity.async_on_remove` wants, and it used to
        return nothing, so an entity that was removed left its callback here for
        the life of the coordinator. That is not only untidy. Whether a key has
        any listeners is read in two places that decide behaviour:
        `_dispatch_event` skips a code nothing is listening for, and
        `translate_event_code` decides whether to report the original
        CrossLineDetection alongside the SmartMotion translation. A listener
        belonging to an entity that no longer exists answers yes to both.

        The key goes when its last listener does, because both of those checks
        read the key rather than the list, and an empty list is still a key.
        """
        event_key = self.get_event_key(event_name)
        self._dahua_event_listeners.setdefault(event_key, []).append(listener)

        def remove() -> None:
            listeners = self._dahua_event_listeners.get(event_key)
            if not listeners:
                return
            if listener in listeners:
                listeners.remove(listener)
            if not listeners:
                del self._dahua_event_listeners[event_key]

        return remove

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

        None means "nothing known", not "up to date". A created entity does not
        normally see it: setup only offers the entity where the record named a
        version, so a device whose record is still empty gets no entity rather
        than one that can only read unknown.
        """
        return self._cloud_firmware_version

    async def _async_probe_cloud_upgrade(self) -> None:
        """Read the device's cached cloud OTA record, once, at setup.

        The record is a local config table, so this costs the device one config
        read and nothing else: no Dahua server is asked and no separate cloud
        poll is added. The entity is only offered where the record actually
        names a version; a table that exists but has not been filled in yet
        could only ever read unknown, which is the empty entity the profile
        sensor's gate exists to avoid.
        """
        try:
            info = await self.client.async_get_cloud_upgrade_info()
        except Exception as probe_error:  # pylint: disable=broad-except
            self._note_probe_refusal("cloud_upgrade", probe_error)
            self._supports_cloud_upgrade = False
            self._cloud_firmware_version = None
            return
        version = cloud_upgrade_version(info)
        self._supports_cloud_upgrade = version is not None
        self._cloud_firmware_version = version
        self._cloud_upgrade_checked_at = time.monotonic()

    def _cloud_upgrade_read_is_due(self) -> bool:
        """Whether the reused cloud upgrade record is old enough to re-read."""
        checked_at = getattr(self, "_cloud_upgrade_checked_at", None)
        if checked_at is None:
            return True
        return time.monotonic() - checked_at >= FIRMWARE_UPGRADE_REFRESH_SECONDS

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
                "Full ProductDefinition probe failed; trying named blocks",
                exc_info=True,
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
                        definition = (
                            await self.client.async_get_product_definition_rpc2(name)
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
                        if isinstance(definition, list)
                        and 0 <= channel < len(definition)
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
                self._siren_detection_sources.append(
                    "getCaps: SupportControlSpeaker=true"
                )
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
                "Direct-camera getCaps probe failed; keeping other evidence",
                exc_info=True,
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

    async def _async_refresh_storage(self) -> None:
        """Read the recorder's disks, slowly.

        Each read is a login the device writes to its own log, which users
        already complain about, so this is called once at setup and then at most
        hourly rather than on every poll (#745). A failure leaves the last known
        disks in place rather than blanking the sensors.
        """
        try:
            raw = await self.client.async_get_storage_device_info()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug(
                "Storage device probe failed on %s", self._address, exc_info=True
            )
            return
        self._storage_disks = parse_storage_disks(raw)
        self._storage_last_refresh = time.time()

    def get_storage_disks(self) -> list:
        """The recorder's disks, for the diagnostic disk sensors to read (#745)."""
        return list(self._storage_disks)

    async def _async_refresh_remote_devices(self) -> None:
        """Read which channels the recorder has a camera configured on, once.

        The same once-at-setup discipline as the disks: a getConfig is a login
        the device logs, and the camera slots do not change often enough to
        poll. A failure leaves the table empty, so the sensor simply does not
        appear.
        """
        try:
            raw = await self.client.async_get_remote_devices()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug(
                "RemoteDevice probe failed on %s", self._address, exc_info=True
            )
            return
        self._remote_devices = dahua_utils.parse_remote_devices(raw)

    def get_configured_channel_count(self):
        """How many channels the recorder reports a camera configured on, or None.

        None when nothing was read -- not a recorder, or the read failed -- so
        the sensor is created only where there is a real answer. Counts enabled
        slots: a slot the recorder lists but has switched off is not a camera.
        """
        if not self._remote_devices:
            return None
        return sum(1 for slot in self._remote_devices.values() if slot.get("enabled"))

    def supports_video_color(self, field: str) -> bool:
        """Whether this channel reported this picture adjustment at setup.

        What the number platform creates its entities from. One source of truth,
        because the poll reads the same answer to decide whether to fetch the
        table at all: two copies of this rule drifting apart would either spend
        a request per poll on nothing or leave a slider reading a value nobody
        fetched.
        """
        return field in getattr(self, "_video_color_fields", frozenset())

    def get_video_color(self, field: str):
        """A picture adjustment for this channel's general profile, or None.

        Read from the poll's VideoColor table by the image-adjustment number
        entities (brightness, contrast, saturation, hue).
        """
        raw = self.data.get(
            "table.VideoColor[{0}][0].{1}".format(self.get_channel(), field)
        )
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def reported_device_class(self) -> str:
        """The class the device itself answered, folded, or "" if it did not answer.

        Only its own answer: unlike is_doorbell, no model-name list stands in for
        it, so callers that need certainty about what the device is can tell
        "it said so" from "its model looks like it".
        """
        device_class = getattr(self, "_device_class", "")
        return device_class.strip().upper() if isinstance(device_class, str) else ""

    def is_indoor_monitor(self) -> bool:
        """Whether the device says it is an indoor monitor (VTH).

        Only its own answer counts. No VTH has ever been recognised by model name
        here, and the class is only "VTH" when the RPC2 identity said so.
        """
        device_class = getattr(self, "_device_class", "")
        return isinstance(device_class, str) and device_class.strip().upper() == "VTH"

    def is_indoor_monitor_without_video(self) -> bool:
        """Whether this is an indoor monitor (VTH) that says it has no camera.

        Such a device got the same set of camera entities as any other, for a
        camera it does not have: a VTH2421F-P has none, and says so in its own
        RemoteDevice entry (SupportVideo false). Some indoor monitors do have one, so the
        class alone does not decide this; the device's own answer does, and no
        answer leaves the cameras where they were.

        Both halves come from the RPC2 identity a VTH is given when it has no
        magicBox CGI, so nothing else can reach this.
        """
        device_class = getattr(self, "_device_class", "")
        if not isinstance(device_class, str) or device_class.strip().upper() != "VTH":
            return False
        own_video = getattr(self.client, "vth_own_video", None)
        return own_video is not None and own_video() is False

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
            failures.append(
                "Automatic detection has not run or evidence is unavailable"
            )
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
        if m.startswith("PTZ3E10X-T180"):
            sources.append("Model fallback: starts with PTZ3E10X-T180")
        if sources:
            return sources
        failures = list(getattr(self, "_security_light_detection_failures", []))
        if not failures:
            failures.append(
                "Automatic detection has not run or evidence is unavailable"
            )
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
            or (
                getattr(self, "_manual_security_light", False)
                and not self.is_doorbell()
            )
            or self.uses_rpc2_deterrence(1)
            or "AS-PV" in m
            or m == "AD410"
            or m == "DB61I"
            or m.startswith("IP8M-2796E")
            # Verified on two IPC-Color4M-TZ cameras: Type=1 drives the
            # alternating red/blue active-deterrence LEDs, despite the CGI
            # status field calling the output WhiteLight.
            or m.startswith("IPC-COLOR4M-TZ")
            # Both sensor channels alias one shared warning-light circuit.
            # Keep a CGI fallback when the RPC2 capability probe is unavailable.
            or m.startswith("PTZ3E10X-T180")
        )

    def is_doorbell(self) -> bool:
        """Returns true if this is a doorbell (VTO)

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
        """Returns true if this is an Amcrest doorbell - IMOU DB61i is identical"""
        return self.model.upper().startswith("AD") or self.model.upper().startswith(
            "DB6"
        )

    def is_empiretech_doorbell(self) -> bool:
        """Returns true if this is an EmpireTech doorbell"""
        return self.model.upper().startswith("DB2X")

    def is_avaloidgoliath_doorbell(self) -> bool:
        """Returns true if this is an Avaloid Goliath doorbell"""
        return self.model.upper().startswith("AV-V")

    def is_flood_light(self) -> bool:
        """Returns true if this camera is an floodlight camera (eg.ASH26-W)"""
        m = self.model.upper()
        return (
            m.startswith("ASH26")
            or "L26N" in m
            or "L46N" in m
            or m.startswith("V261LC")
            or m.startswith("W452ASD")
        )

    def supports_infrared_light(self) -> bool:
        """
        Returns true if this camera has an infrared light.  For example, the IPC-HDW3849HP-AS-PV does not, but most
        others do. I don't know of a better way to detect this
        """
        if not self._supports_lighting:
            return False
        return (
            "-AS-PV" not in self.model
            and "-AS-NI" not in self.model
            and "LED-S2" not in self.model
        )  # IPC-HFW2439SP-SA-LED-S2 also has no infrared light

    def supports_floodlightmode(self) -> bool:
        """Returns true if this camera supports floodlight mode"""
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
                )
                == WHITE_LIGHT
                for profile in range(9)
                for index in range(MAX_LIGHTING_V2_LIGHTS)
            )
        if self.is_amcrest_doorbell() or self.is_flood_light():
            return False
        if "table.Lighting_V2[{0}][0][0].Mode".format(self._channel) not in self.data:
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

    def uses_channel_scoped_illuminator(self) -> bool:
        """Whether this channel needs the channel-scoped two-table white light.

        The recorder-channel variant of the above (#959). Mutually exclusive
        with it: the detection only sets this when the whole-table path was not.
        """
        return getattr(self, "_supports_channel_scoped_illuminator", False)

    def is_motion_detection_enabled(self) -> bool:
        """Returns true if motion detection is enabled for the camera"""
        return (
            self.data.get(
                "table.MotionDetect[{0}].Enable".format(self._channel), ""
            ).lower()
            == "true"
        )

    def is_disarming_linkage_enabled(self) -> bool:
        """Returns true if disarming linkage is enable"""
        return self.data.get("table.DisableLinkage.Enable", "").lower() == "true"

    def is_event_notifications_enabled(self) -> bool:
        """Returns true if event notifications is enable"""
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
        return self.data.get(
            "table.SmartMotionDetect[{0}].Enable".format(self._channel)
        )

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
        """Returns true if smart motion detection is enabled"""
        if self.supports_smart_motion_detection_amcrest():
            return (
                self.data.get("table.VideoAnalyseRule[0][0].Enable", "").lower()
                == "true"
            )
        return (self._smart_motion_row() or "").lower() == "true"

    def get_smart_motion_sensitivity(self):
        """This channel's smart motion sensitivity word, or None.

        Read from the same per-channel SmartMotionDetect row the enable switch
        uses. None when the poll has not landed or the device reports a value
        the select does not offer, which shows as unknown rather than as a
        sensitivity the camera is not in.
        """
        return self.data.get(
            "table.SmartMotionDetect[{0}].Sensitivity".format(self._channel)
        )

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
        # Both sensor channels on this camera address one chassis-wide warning
        # circuit. Keep one entity on the primary channel rather than exposing
        # two controls that race the same output.
        if (
            str(getattr(self, "model", "")).upper().startswith("PTZ3E10X-T180")
            and getattr(self, "_channel", 0) != 0
        ):
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
        """Returns true if the camera siren is on"""
        return self.get_status_value("Speaker").lower() == "on"

    def get_device_name(self) -> str:
        """returns the device name, e.g. Cam 2"""
        if self._name is not None:
            return self._name
        # Earlier releases of this integration didn't allow for setting the camera name, it always used the machine name
        # Now we fall back to the machine name if that wasn't supplied at config time.
        return self.machine_name

    def get_model(self) -> str:
        """returns the device model, e.g. IPC-HDW3849HP-AS-PV"""
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

    def channel_option(self, key: str, default=None):
        """One channel's setting, preferring its own over the entry's.

        A single camera has no channel config, so this is exactly
        `entry.options.get(key, default)` and nothing changes for it.

        On a merged recorder the channel's subentry wins. That distinction is the
        whole reason this exists: `manual_siren`, `manual_security_light` and
        `nvr_active_deterrence` are per channel by nature -- they exist because
        one camera on a recorder has the hardware and the others do not -- and they
        were read from the entry's options back when an entry *was* a channel.
        Merging the entries without this would have quietly applied one channel's
        answer to all 64.
        """
        if key in self._channel_config:
            return self._channel_config[key]
        return self.config_entry.options.get(key, default)

    def get_serial_number(self) -> str:
        """returns the device serial number. This is unique per device"""
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

    def add_plate_listener(self, listener) -> CALLBACK_TYPE:
        """Listen for a parsed plate, and return how to stop.

        Same reason as add_dahua_event_listener: this returned nothing, so the
        authorized vehicle sensor could not let go of its callback and a removed
        one kept being called.
        """
        self._plate_listeners.append(listener)

        def remove() -> None:
            if listener in self._plate_listeners:
                self._plate_listeners.remove(listener)

        return remove

    def get_configured_area(self):
        """The area_id this channel was given, or None.

        Options win over data, like every other setting that can be changed
        after setup. Chosen while adding a recorder so that ten channels do not
        all arrive unfiled.

        `entry.data` is only this channel's data when the entry has no subentry
        for it, which is a single camera. On a merged recorder `entry.data` is the
        *primary* channel's config, so using it as the fallback filed every
        channel the user left blank into the primary's area. That is exactly what
        `_channel_subentries` pops the key to prevent, and the two cancelled out:
        measured, a channel whose area had been popped still reported the
        primary's.

        The entry's *options* are left as a fallback on purpose. That area is an
        entry-wide answer the user gave in the options flow, so applying it to a
        channel that has not chosen one is coherent. `entry.data`'s area is the
        primary channel's own answer during the add flow, which is nobody else's.
        """
        own_data_is_this_channels = not self._channel_config
        fallback = (
            self.config_entry.data.get(CONF_AREA) if own_data_is_this_channels else None
        )
        return self.channel_option(CONF_AREA, fallback) or None

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
        raw = self.channel_option(
            CONF_AUTHORIZED_PLATES,
            self.config_entry.data.get(CONF_AUTHORIZED_PLATES, ""),
        )
        return dahua_utils.parse_authorized_plates(raw)

    def get_authorized_hold_time(self) -> int:
        """Return the duration in seconds an authorized vehicle binary sensor stays active."""
        try:
            return int(
                self.channel_option(
                    CONF_AUTHORIZED_HOLD_TIME,
                    self.config_entry.data.get(
                        CONF_AUTHORIZED_HOLD_TIME, DEFAULT_AUTHORIZED_HOLD_TIME
                    ),
                )
            )
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

        Empty for an indoor monitor without a camera. The add-device form offers
        the camera events to every device, and a VTH2421F-P answered none of the
        nine it was given over RPC2 (VideoMotion, CrossLineDetection, AlarmLocal,
        VideoLoss, VideoBlind, AudioMutation, CrossRegionDetection,
        SmartMotionHuman, SmartMotionVehicle): its event stream ended with
        "reports none of the selected event types" and was retried for ever,
        and each of those events got a sensor that could never change.
        """
        if self.is_indoor_monitor_without_video():
            return []
        return self.events

    def get_infrared_profile(self) -> str:
        """The Lighting profile this channel's infrared light is really using."""
        return infrared_profile(self.data, self._channel, self.get_profile_mode())

    def supports_day_night_color(self) -> bool:
        """True if this channel reported a Day/Night mode we understand.

        Not on an indoor monitor without a camera. A VTH2421F-P has no camera
        and still answers a VideoInOptions table with DayNightColor in it, so
        the probe passed and it got a Day/Night select for an image it does
        not have.
        """
        return (
            self._supports_day_night_color
            and not self.is_indoor_monitor_without_video()
        )

    def get_day_night_color(self):
        """This channel's Day/Night mode by name, or None."""
        return day_night_color_name(self.data, self._channel)

    def get_infrared_mode(self) -> str:
        """This channel's infrared lighting mode, exactly as the device reports it.

        `Manual`, `Auto` and `Off` are the three the integration writes. A device
        may report others it chose itself -- a DHI-NVR5464-16P-EI answers
        `ZoomPrio` on two of fifteen channels -- and those are passed through
        rather than flattened, because a caller checking whether a write landed
        has to be able to see that it did not.

        Read from Lighting_V2 when this channel has a row there, because that is
        the table the write goes to and the two can disagree -- a channel can
        report `ZoomPrio` in v2 and something else in v1. Reading one and writing
        the other is how a control reports a state it is not setting.

        Empty when this channel reports no lighting at all.
        """
        if self.infrared_uses_lighting_v2():
            profile, index, _bank = self.get_infrared_v2_row()
            return self.data.get(
                "table.Lighting_V2[{0}][{1}][{2}].Mode".format(
                    self._channel, profile, index
                ),
                "",
            )
        return self.data.get(
            "table.Lighting[{0}][{1}].Mode".format(
                self._channel, self.get_infrared_profile()
            ),
            "",
        )

    def is_infrared_light_on(self) -> bool:
        """returns true if the infrared light is on"""
        return self.get_infrared_mode() == "Manual"

    def get_infrared_bank(self) -> str:
        """The brightness bank this channel's infrared emitter uses."""
        if self.infrared_uses_lighting_v2():
            return self.get_infrared_v2_row()[2]
        return infrared_brightness_bank(
            self.data, self._channel, self.get_infrared_profile()
        )

    def get_infrared_v2_row(self):
        """(profile, index, bank) for infrared in Lighting_V2, or None.

        None means this channel has no v2 row and the v1 `Lighting` table is all
        there is. On the recorder this was measured on, the channels that do have
        one are writable through it while v1 is refused outright.
        """
        return infrared_v2_row(self.data, self._channel, self.get_profile_mode())

    def infrared_uses_lighting_v2(self) -> bool:
        """Whether this channel's infrared is driven through Lighting_V2.

        Only once the v1 table has been refused for this channel, and only when the
        device serves a v2 row to fall back to. See `infrared_transport` for why it
        is not simply "v2 wherever a v2 row exists".
        """
        row = self.get_infrared_v2_row()
        if row is None:
            # No fallback exists, so the answer is v1 whatever the store says.
            # Short-circuited rather than asked: this is the common case -- thirteen
            # of the measured recorder's fifteen channels and every camera serving
            # only the v1 table -- and asking would make a read of the mode depend
            # on the refusal store, and through it on the device address. That is
            # not hypothetical: it broke five tests in test_infrared_profile.py,
            # which build the real coordinator with object.__new__ and never set
            # _address, and it would equally affect any caller holding a
            # half-constructed coordinator.
            return False
        return (
            infrared_transport(row, refusals.is_refused(self, refusals.INFRARED_V1))
            == "v2"
        )

    def get_infrared_level(self):
        """The infrared level on the device's own 0..100 scale, or None.

        Home Assistant publishes a light's `brightness` only while that light is
        on, and this light is on only when the mode is `Manual`. A camera on
        `Auto` is illuminating at this level and reads off, so the level has to be
        readable separately or it cannot be shown at all.

        None rather than a number when the channel reports none, so the attribute
        says "not known" instead of claiming the emitter is at zero.
        """
        if self.infrared_uses_lighting_v2():
            profile, index, bank = self.get_infrared_v2_row()
            level = self.data.get(
                "table.Lighting_V2[{0}][{1}][{2}].{3}[0].Light".format(
                    self._channel, profile, index, bank
                )
            )
        else:
            bank = self.get_infrared_bank()
            if bank is None:
                # A doorbell's infrared has no brightness bank, so there is no
                # level to read. Returning None here says "not known" rather than
                # reading a "Lighting[c][p].None[0].Light" key that cannot exist.
                return None
            level = self.data.get(
                "table.Lighting[{0}][{1}].{2}[0].Light".format(
                    self._channel, self.get_infrared_profile(), bank
                )
            )
        if level is None or level == "":
            return None
        try:
            return int(level)
        except (TypeError, ValueError):
            return None

    def get_infrared_brightness(self) -> int:
        """Return the brightness of this light, as reported by the camera itself, between 0..255 inclusive"""

        level = self.get_infrared_level()
        return dahua_utils.dahua_brightness_to_hass_brightness(
            None if level is None else str(level)
        )

    def get_illuminator_index(self) -> int:
        """The Lighting_V2 light index this device puts its white light on."""
        return illuminator_light_index(
            self.data, self._channel, self.get_profile_mode()
        )

    def get_illuminator_bank(self) -> str:
        """The brightness bank this device's white light actually uses."""
        return illuminator_brightness_bank(
            self.data,
            self._channel,
            self.get_profile_mode(),
            self.get_illuminator_index(),
        )

    def is_illuminator_on(self) -> bool:
        """Return true if the illuminator light is on"""
        # profile_mode 0=day, 1=night, 2=scene
        profile_mode = self.get_profile_mode()
        index = self.get_illuminator_index()
        manually_on = (
            self.data.get(
                "table.Lighting_V2[{0}][{1}][{2}].Mode".format(
                    self._channel, profile_mode, index
                ),
                "",
            )
            == "Manual"
        )
        if self.uses_lighting_scheme_illuminator():
            scheme_mode = self.data.get(
                "table.LightingScheme[{0}][{1}].LightingMode".format(
                    self._channel, profile_mode
                ),
                "",
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
            return (
                self.data.get(
                    f"table.Lighting_V2[{self._channel}][{profile_mode}][1].Mode"
                )
                == "Manual"
            )

    def is_ring_light_on(self) -> bool:
        """Return true if ring light is on for an Amcrest Doorbell"""
        return self.data.get("table.LightGlobal[0].Enable") == "true"

    def get_illuminator_brightness_field(self) -> str:
        """Return the brightness field used by this WhiteLight."""
        profile_mode = self.get_profile_mode()
        index = self.get_illuminator_index()

        base = f"table.Lighting_V2[{self._channel}]" f"[{profile_mode}][{index}]"

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
                self._channel,
                self.get_profile_mode(),
                self.get_illuminator_index(),
                self.get_illuminator_bank(),
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
                "table.VideoInMode[{0}].{1}".format(self._channel, name)
            )

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

    def describe_video_profile_shape(self) -> str:
        """Which of the three VideoInMode shapes this channel is in, by name.

        `read_profile_mode` already tells these apart to decide which profile is live.
        This says the same thing in a form a log line can use, because the shape also
        decides whether `set_video_profile_mode` can work at all: that service writes
        `VideoInMode[ch].Config[0]`, which only selects the profile in the ordinary
        shape.

        Measured on one DHI-NVR5464, all three shapes at once on different channels of
        the same recorder, which is why this is read per channel and not per model:

            ch0       Config[0]=0  ConfigEx=None   ordinary
            ch1       Config[0]=0  ConfigEx=Day    the IL shape
            ch9       Config[0]=2  ConfigEx=None   general profile management

        Returns "ordinary", "general", "configex", or "unknown" when nothing has been
        polled yet.
        """
        data = self.data or {}

        def field(name):
            return data.get("table.VideoInMode[{0}].{1}".format(self._channel, name))

        config = field("Config[0]")
        config_ex = field("ConfigEx")
        if config is None and config_ex is None:
            return "unknown"
        if config == "2":
            return "general"
        if config_ex is not None and str(config_ex).strip().lower() in ("day", "night"):
            return "configex"
        return "ordinary"

    def video_profile_mode_is_writable(self) -> bool:
        """Whether writing Config[0] actually selects the profile on this channel.

        True for the ordinary shape and for a channel nothing has been read from yet,
        because refusing on an unknown is worse than trying: the write is what the
        service has always done and some devices this has never been measured on may
        well answer it.
        """
        return self.describe_video_profile_shape() in ("ordinary", "unknown")

    async def async_detect_lighting_support(self) -> bool:
        """Does this channel have an infrared light?

        Judged by what comes back, not by an exception. async_get_config
        catches aiohttp.ClientResponseError and returns {}, so an
        exception-only probe could never fail: every device was marked as
        having an IR light and then fetched Lighting[channel][mode] on every
        poll, forever. The profile mode probe reads its result the same way.
        """
        try:
            conf = await self.client.async_get_config_lighting(
                self._channel, self._profile_mode
            )
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

    def is_ptz3e10x_t180(self) -> bool:
        """Return whether this is the verified dual-sensor T180 family."""
        return self.model.upper().startswith("PTZ3E10X-T180")

    def get_coaxial_status_channel(self) -> int:
        """Return the CGI channel that reports coaxial deterrence state."""
        if self.uses_recorder_deterrence():
            return self._channel_number
        if self.is_ptz3e10x_t180():
            return 2
        return 1

    def get_rpc2_coaxial_status_channel(self) -> int:
        """Return the RPC2 channel that reports direct deterrence state."""
        if self.is_ptz3e10x_t180():
            return 1
        return 0

    def get_security_light_control_channel(self) -> int:
        """Return the CGI channel that controls the security light."""
        if self.uses_recorder_deterrence():
            return self._channel_number
        if self.is_ptz3e10x_t180():
            return 1
        return self._channel

    def get_security_light_off_io(self) -> int:
        """Return the device-specific IO value that disables the light."""
        if not self.uses_recorder_deterrence() and self.is_ptz3e10x_t180():
            return 0
        return 2

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

        And the device's own answer counts for more than either. `getDeviceClass`
        returns NVR, DVR, XVR or HCVR on a recorder that says nothing of the kind
        in its model name, which is exactly the N843A8 case above, so asking it
        removes the guess rather than adding another one. `uses_recorder_deterrence`
        already preferred it for the deterrence entity; the remaining callers did
        not, and on channel 0 of such a recorder that meant the IVS rules were read
        from `VideoAnalyseRule` instead of `RemoteVideoAnalyseRule` and the
        floodlight drove the camera coaxial path instead of the recorder one.

        The model match stays as the fallback for a device that reports no class
        at all, which is what every pre-RPC2 identity path leaves behind.

        This decides the control path as well as whether the entity exists --
        an NVR channel drives deterrence through coaxialControlIO on its own
        channel number, a camera through its channel index -- so the two have to
        be decided by the same question or the entity would appear and then
        write to the wrong place.
        """
        return (
            self._channel > 0
            or self.is_recorder_host()
            or "NVR" in self.model.upper()
            or self._nvr_active_deterrence
        )

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
        """True if smart motion detection is supported for an amcrest device

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
        """True if the camera exposes the lens privacy mask over RPC2"""
        return self._supports_privacy_mode

    def is_privacy_mode_enabled(self) -> bool:
        """True if the lens privacy mask is currently enabled"""
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

        Covers the RPC2 transport as well as CGI, and did not (#848). An RPC2
        refusal raises Rpc2MethodRefused rather than ClientResponseError, so it
        missed the clause below, escaped the gather and became UpdateFailed on
        the first refresh: an AD410 answering CoaxialControlIO.getStatus with
        "Method not found!" never finished setup at all. Two reporters, two
        different codes, and a third device answering "Authority:check failure",
        which is measured to mean no such method rather than a permission.

        A stale login is re-raised rather than swallowed. That one is worth
        recovering from, and treating it as "this device has no siren" would
        silently drop a capability the device does have.
        """
        try:
            if self.uses_rpc2_deterrence():
                return await self.client.async_get_coaxial_control_io_status_rpc2(
                    channel
                )
            return await self.client.async_get_coaxial_control_io_status(channel)
        except Rpc2MethodRefused as refused:
            if rpc2_refusal_is_a_stale_login(refused):
                # The login, not the capability. Let it out so the session is
                # rebuilt rather than swallowing it as "no such method".
                raise
            target = (self.client.device_key, "rpc2")
            if target not in _CAPABILITY_REFUSALS_REPORTED:
                _CAPABILITY_REFUSALS_REPORTED.add(target)
                _LOGGER.warning(
                    "%s refused the siren and white light state over RPC2 (%s). "
                    "That is this device saying it does not serve that call, not a "
                    "fault, so those entities will hold their last value and the "
                    "rest of this camera is unaffected. Reported once per device",
                    self._address,
                    refused,
                )
            else:
                _LOGGER.debug(
                    "%s still refuses coaxial status over RPC2 (%s)",
                    self._address,
                    refused,
                )
            return self._previous_coaxial_status()
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
                    self._address,
                    channel,
                    error.status,
                )
            else:
                _LOGGER.debug(
                    "%s still refuses coaxial status for channel %s (HTTP %s)",
                    self._address,
                    channel,
                    error.status,
                )
            return self._previous_coaxial_status()

    def _previous_coaxial_status(self) -> dict | None:
        """The last siren and white light reading, so a refusal holds it.

        The entities reading these say they keep their last value on a refusal,
        but the poll builds its data from scratch and the gather drops a None,
        so without this the siren switch read "off" for that cycle while the
        device was on. Only this read's own keys are carried, never every
        status key, or a stale value would overwrite a fresh one from another
        coroutine in the same gather. None when there is nothing to hold, which
        is what the gather already skips.
        """
        previous = getattr(self, "data", None) or {}
        keys = (
            "status.status.Speaker",
            "status.status.WhiteLight",
            "status.Speaker",
            "status.WhiteLight",
        )
        carried = {key: previous[key] for key in keys if key in previous}
        return carried or None

    async def _async_fetch_vth_camera_links(self) -> dict | None:
        """Poll which camera each VTO's calls open on, keeping the last answer on failure.

        Carried rather than dropped so one refused read does not empty the
        select's options under the user, the same as the preset position.
        """
        try:
            return {VTH_CAMERA_LINKS: await self.client.async_get_vth_camera_links()}
        except Exception as exception:  # pylint: disable=broad-except
            _LOGGER.debug("Failed to fetch the VTH camera links", exc_info=exception)
            previous = (getattr(self, "data", None) or {}).get(VTH_CAMERA_LINKS)
            return None if previous is None else {VTH_CAMERA_LINKS: previous}

    def get_vth_camera_links(self) -> dict | None:
        """The links and cameras vth_camera_links describes, or None before a read."""
        return (getattr(self, "data", None) or {}).get(VTH_CAMERA_LINKS)

    async def _async_fetch_video_color(self) -> dict | None:
        """Poll the picture adjustments, keeping the last answer on a refusal.

        The probe at setup means this is only asked of a device that served the
        table, so a refusal here is a change of mind: an account whose rights
        were narrowed, or a device that has started answering 403 under load.
        Neither is a reason to fail the refresh and take every entity on the
        channel with it, which is what #1006 was.

        Carries only this table's own keys, never the whole of the last poll, so
        a stale value cannot overwrite a fresh one from another coroutine in the
        same gather -- the same rule _previous_coaxial_status follows. None when
        there is nothing to carry, which the gather already skips.
        """
        try:
            return await self.client.async_get_video_color()
        except Exception as exception:  # pylint: disable=broad-except
            _LOGGER.debug(
                "Could not read the picture adjustments for channel %s",
                self._channel,
                exc_info=exception,
            )
            previous = getattr(self, "data", None) or {}
            carried = {
                key: value
                for key, value in previous.items()
                if key.startswith("table.VideoColor[")
            }
            return carried or None

    async def _async_fetch_privacy_mode(self) -> dict:
        """Poll the privacy mode state, keeping the last known value on failure"""
        try:
            return {"privacy_mode_enabled": await self.client.async_get_privacy_mode()}
        except Exception as exception:
            _LOGGER.debug("Failed to fetch privacy mode state", exc_info=exception)
            previous = (
                self.data.get("privacy_mode_enabled", False) if self.data else False
            )
            return {"privacy_mode_enabled": previous}

    async def _async_fetch_lighting_v2(self) -> dict | None:
        """Poll the Lighting_V2 table, keeping the last answer on a refusal.

        This is the light platform's table, and it is read on two paths that
        both used to let a refusal out: the fan-out below, and the fallback
        further down for a security light or flood light on a device whose
        setup probe failed. The fan-out has no `return_exceptions`, so either
        one failed the whole refresh -- every entity on the channel
        unavailable, a host failure recorded against the count all of a
        recorder's channels share, and the poll backed off.

        The fallback path is the worse of the two, because it is reached
        *only* when `_supports_lighting_v2` is False, and that flag is set by a
        probe catching `(ClientError, TimeoutError)`. So two quite different
        devices arrive there. One whose table is genuinely refused -- 400 on a
        recorder channel, or an account without rights to it -- fails every
        poll for the life of the entry. One whose probe merely timed out does
        serve the table, and this read is the only thing that recovers its
        light, which is why the read stays rather than being deleted.

        Deliberately does not stop asking, for the reason `_async_coaxial_status`
        gives: the refusals measured on the recorder here recovered on their
        own, so a capability somebody may rely on must not be switched off for
        the process by one bad answer. What changes is that a refusal costs a
        stale reading rather than a failed poll.

        Carries only this table's own keys, never the whole of the last poll, so
        a stale value cannot overwrite a fresh one from another coroutine in the
        same gather -- the rule `_previous_coaxial_status` states. None when
        there is nothing to carry, which the gather already skips.
        """
        try:
            return await self.client.async_get_lighting_v2()
        except Exception as exception:  # pylint: disable=broad-except
            _LOGGER.debug(
                "Could not read Lighting_V2 for channel %s",
                self._channel,
                exc_info=exception,
            )
            previous = getattr(self, "data", None) or {}
            carried = {
                key: value
                for key, value in previous.items()
                if key.startswith("table.Lighting_V2[")
            }
            return carried or None

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
