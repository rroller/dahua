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
from .dahua_utils import parse_event
from .deterrence import (
    product_definition_supports_security_light,
    product_definition_supports_siren,
    siren_definition_failure_reason,
    security_light_definition_failure_reason,
)
from .illuminator_restore import IlluminatorRestoreStore
from .vto import DahuaVTOClient

# Imported back rather than left behind. Every one of these was a public
# name of this package: platforms, entity.py, diagnostics and a good part of
# the test suite reach them as `custom_components.dahua.<name>`, and the
# state among them is shared by identity, not by value. Re-importing keeps
# all of that working, so the move is a move and nothing else.
from .host import (  # noqa: F401  pylint: disable=unused-import
    DERIVES_INTO,
    DahuaHostEventStream,
    EVENT_STREAM_HEALTHY_SECONDS,
    EVENT_STREAM_JITTER,
    EVENT_STREAM_MAX_LIFETIME_SECONDS,
    EVENT_STREAM_MAX_RETRY_SECONDS,
    EVENT_STREAM_RETRY_SECONDS,
    EVENT_STREAM_SHORT_RETRY_SECONDS,
    HOST_UPTIME_DEDUPE_SECONDS,
    HTTPS_PROBE_MIN_INTERVAL,
    ISSUE_HTTP_DEAD_HTTPS_AVAILABLE,
    ISSUE_UNREACHABLE,
    MAX_AUTH_REFUSALS,
    MAX_BACKOFF_DOUBLINGS,
    SSL_CONTEXT,
    UNREACHABLE_AFTER_FAILURES,
    _CAPABILITY_REFUSALS_REPORTED,
    _HOST_CHANNEL_BASE,
    _HOST_CHANNEL_BASE_LOCKS,
    _HOST_CONNECTORS,
    _HOST_FAILURES,
    _HOST_NETWORK_IDENTITY,
    _HOST_NETWORK_IDENTITY_LOCKS,
    _HOST_STREAMS,
    _HOST_UPTIME_LOCKS,
    _HOST_UPTIME_STATE,
    _SYNTHESISED_UNIQUE_ID,
    _acquire_connector,
    _async_evaluate_host,
    _async_get_host_uptime_generation,
    _async_probe_tcp,
    _entries_for_address,
    _host_stream,
    _release_connector,
    _release_host_stream,
    async_device_is_zero_indexed,
    async_host_is_unreachable,
    async_network_identity,
    async_record_host_auth_refusal,
    async_record_host_failure,
    async_record_host_success,
    event_stream_retry_delay,
    is_synthesised_identity,
    jittered,
    normalize_address,
    stream_lifetime,
)

# Imported back rather than left behind. Every one of these was a public
# name of this package: platforms, entity.py, diagnostics and a good part of
# the test suite reach them as `custom_components.dahua.<name>`, and the
# state among them is shared by identity, not by value. Re-importing keeps
# all of that working, so the move is a move and nothing else.
from .coordinator import (  # noqa: F401  pylint: disable=unused-import
    CAPABILITY_REFUSED,
    DAY_NIGHT_NAMES,
    DOORBELL_KNOWN_QUIET_STATES,
    DOORBELL_RINGING_STATES,
    DOORBELL_STATE_EVENTS,
    DahuaDataUpdateCoordinator,
    FAILURES_BEFORE_BACKOFF,
    GENERIC_DEVICE_TYPES,
    LIGHT_BRIGHTNESS_BANKS,
    MAX_LIGHTING_V2_LIGHTS,
    POLL_BACKOFF_CAP,
    PROBE_FAILED,
    PROBE_REFUSED,
    PULSE_STATE_CODES,
    RECENT_EVENT_COUNT,
    SMART_MOTION_ROW,
    VIDEO_COLOR_FIELDS,
    WHITE_LIGHT,
    day_night_color_name,
    describe_update_failure,
    door_index,
    doorbell_state,
    event_payload,
    failure_backoff,
    get_configured_scan_interval,
    illuminator_brightness_bank,
    illuminator_light_index,
    infrared_profile,
    is_onvif_channel,
    model_name,
    remote_device_model,
    remote_device_protocol,
    smart_motion_row_indices,
    video_color_fields,
    vto_retry_state,
)

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


_LOGGER: logging.Logger = logging.getLogger(__package__)

# The startup banner is logged once per Home Assistant run. It used to be guarded
# by whether hass.data[DOMAIN] existed, which is gone now that runtime state
# lives on the entry.
_STARTUP_LOGGED = False

# What an entry carries at runtime: the channels it owns, keyed by channel index.
#
# One member today, because a recorder is one config entry per channel. It is a
# mapping so that an entry can own several, which is what #827 needs and the
# reason removing a 64 channel NVR currently takes 64 deletions.
#
# Lazily evaluated, so naming DahuaDataUpdateCoordinator before it is defined is
# fine.
type DahuaConfigEntry = ConfigEntry[dict[int, "DahuaDataUpdateCoordinator"]]


def entry_coordinators(entry: DahuaConfigEntry) -> dict:
    """The channels this entry owns, keyed by channel index.

    `runtime_data` is Home Assistant's own place for this, and it deletes the
    attribute when an entry unloads, so an unloaded entry has none rather than an
    empty one. Callers that run during teardown, or against an entry whose setup
    failed, get an empty mapping instead of an AttributeError.
    """
    return getattr(entry, "runtime_data", None) or {}


def entry_coordinator(entry: DahuaConfigEntry) -> "DahuaDataUpdateCoordinator":
    """The single coordinator this entry owns.

    Raises when the entry is not set up. Platforms are only ever asked to set up
    an entry whose runtime data is already in place, so absence there is a bug
    worth hearing about rather than something to paper over with None.

    A named function rather than `next(iter(...))` spread across nine platforms,
    so that #827 has one place to come back to when an entry owns more than one.
    """
    channels = entry_coordinators(entry)
    if not channels:
        raise KeyError("Dahua entry %s has no coordinator" % entry.entry_id)
    return next(iter(channels.values()))


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


# The connection belongs to the device, not to the channel, and every writer
# after setup -- reauth, reconfigure, the HTTPS repair, the discovery heal --
# updates `entry.data`. A merged recorder's coordinators are built from subentry
# data, and each subentry kept its own copy from the add flow, so the entry's
# values are overlaid in channel_configs. Without that, the reload after a
# successful reauth rebuilt every coordinator from the stale copy: the new
# password was accepted, written to the entry, and then ignored, so reauth
# started again forever and the device accumulated failed logins it locks out for.
CONNECTION_KEYS = (
    CONF_ADDRESS,
    CONF_PORT,
    CONF_RTSP_PORT,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_USE_HTTPS,
)


def subentries_share_one_connection(configs: list) -> bool:
    """Whether these channel configs all describe the same device.

    A merged recorder's subentries all carry the connection they were added
    with. The old address-only migration could merge two devices that share an
    address on different ports into one entry, though, and that entry's
    subentries disagree about the connection. Only one can be enforced, and
    enforcing the entry's would point the other device's channels and entities
    at the wrong box, so an entry like that keeps each channel's own.
    """
    seen = {
        tuple((key, config.get(key)) for key in CONNECTION_KEYS) for config in configs
    }
    return len(seen) <= 1


def channel_configs(entry: DahuaConfigEntry) -> list:
    """(subentry_id, config) for every channel this entry owns.

    A merged recorder keeps one subentry per channel (#827). An entry with no
    subentries is a single camera, or a recorder channel that predates the merge,
    and its own `data` is that one channel -- so both shapes come out of here the
    same way and setup has one path rather than two.

    Sorted by channel so that runtime_data, and therefore every platform's
    entities, comes out in channel order rather than in whatever order the
    subentries happen to be stored.

    The channel comes back as an int even where it was stored as a string. The
    add flow wrote it as a string for extra channels and left it absent for the
    first, so both shapes are in the wild, and `runtime_data` is keyed on it:
    "3" and 3 are the same channel and two different keys.

    One channel is returned once. Two subentries claiming the same channel would
    otherwise each get a coordinator, only one of which ends up in
    `runtime_data` -- leaving the other polling the device with nothing owning it
    and nothing to stop it at unload.
    """
    if entry.subentries:
        subentries = [
            (subentry_id, dict(subentry.data))
            for subentry_id, subentry in entry.subentries.items()
        ]
        # The entry's connection is only authoritative when its channels agree
        # about what they are connected to. An entry the old address-only
        # migration built from two devices on one address disagrees with itself,
        # and one connection cannot describe both.
        if subentries_share_one_connection([config for _, config in subentries]):
            connection = {
                key: entry.data[key] for key in CONNECTION_KEYS if key in entry.data
            }
            pairs = [
                (subentry_id, {**config, **connection})
                for subentry_id, config in subentries
            ]
        else:
            pairs = subentries
    else:
        pairs = [(None, dict(entry.data))]

    def channel_of(config):
        try:
            return int(config.get(CONF_CHANNEL, 0) or 0)
        except (TypeError, ValueError):
            return 0

    channels: dict[int, tuple] = {}
    for subentry_id, config in pairs:
        channel = channel_of(config)
        if channel in channels:
            _LOGGER.warning(
                "Dahua entry %s has more than one channel %s; using the first "
                "and ignoring the rest",
                entry.entry_id,
                channel,
            )
            continue
        config[CONF_CHANNEL] = channel
        channels[channel] = (subentry_id, config)

    return [channels[channel] for channel in sorted(channels)]


def events_for_channel(entry: DahuaConfigEntry, config: dict) -> list:
    """The events one channel subscribes to.

    A single channel entry keeps them where it always did, so
    get_configured_events still decides for it. A channel of a merged recorder
    keeps its own list in its subentry, which is what stops one channel's
    selection becoming every channel's.
    """
    if not entry.subentries:
        return get_configured_events(entry)
    if CONF_EVENTS in config:
        return list(config[CONF_EVENTS] or [])
    return get_configured_events(entry)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Run once for the integration, before any config entry is set up.

    The only safe place for the channel merge (#827). Two reasons it cannot live
    in `async_migrate_entry`: Home Assistant sets entries up concurrently, so a
    per entry migration can run while another channel's entities are already
    loaded, and `async_update_entity_platform` refuses an entity that is loaded.
    Here, nothing is.

    Never fails setup. A recorder that could not be merged is still perfectly
    usable in the shape it is already in, so an exception here would take working
    cameras offline to fix a papercut.
    """
    try:
        from .migrate import async_merge_channel_entries

        await async_merge_channel_entries(hass)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception(
            "Could not merge the Dahua config entries. Every entry is left as it "
            "was and the integration will set up normally"
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DahuaConfigEntry):
    """Set up this integration using UI."""
    global _STARTUP_LOGGED
    if not _STARTUP_LOGGED:
        _STARTUP_LOGGED = True
        _LOGGER.info(STARTUP_MESSAGE)

    # Before anything reads the entry's identity. Costs nothing unless the id is md5
    # shaped, and never raises: a camera that cannot be identified better keeps the
    # identity it has rather than failing to set up.
    try:
        await async_migrate_synthesised_unique_id(hass, entry)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug(
            "Could not re-identify %s from the network",
            entry.data.get(CONF_ADDRESS),
            exc_info=True,
        )

    # One coordinator per channel. A single camera has one, a merged recorder has
    # one per subentry (#827), and channel_configs hands back the same shape for
    # both so there is a single path here.
    built = []
    coordinators = []
    failures = []
    try:
        for _subentry_id, config in channel_configs(entry):
            built.append(
                DahuaDataUpdateCoordinator(
                    hass,
                    entry=entry,
                    events=events_for_channel(entry, config),
                    address=config.get(CONF_ADDRESS),
                    port=int(config.get(CONF_PORT)),
                    rtsp_port=int(config.get(CONF_RTSP_PORT)),
                    username=config.get(CONF_USERNAME),
                    password=config.get(CONF_PASSWORD),
                    name=config.get(CONF_NAME),
                    channel=config.get(CONF_CHANNEL, 0),
                    use_https=True if config.get(CONF_USE_HTTPS) else None,
                    # This channel's own settings. Empty for a single camera, which
                    # is what keeps channel_option identical to entry.options there.
                    channel_config=config if _subentry_id else None,
                    # Which subentry this channel is, so the platforms can file its
                    # entities under it. None for a single camera, which is also what
                    # async_add_entities wants when an entry has no subentries.
                    subentry_id=_subentry_id,
                )
            )

        # Concurrently, because a 64 channel recorder doing these one at a time
        # would add minutes to startup. Not a thundering herd: every request still
        # passes the host's MAX_CONCURRENT_REQUESTS_PER_HOST semaphore, so this
        # changes how long the waiting takes rather than how hard the device is hit.
        results = await asyncio.gather(
            *[c.async_config_entry_first_refresh() for c in built],
            return_exceptions=True,
        )

        for coordinator, result in zip(built, results):
            if isinstance(result, BaseException):
                failures.append((coordinator.get_channel(), result))
            else:
                coordinators.append(coordinator)

        if not coordinators:
            # Nothing came up, so the host is the problem rather than one channel.
            # Raising is what gets Home Assistant to retry the whole entry.
            raise failures[0][1]
    finally:
        # Every coordinator holds an aiohttp session and a reference on the host's
        # shared connection pool from its constructor, and only async_stop gives
        # them back. Unload can only stop the ones that reached runtime_data, so
        # anything built and not adopted has to be given back here.
        #
        # In a finally rather than beside the failure branch, because the ways to
        # leave this block are more numerous than they look: a refusal from one
        # channel, a raise from int(port) on the next channel's config, or the
        # re-raise above. Home Assistant retries a failed setup forever, so a leak
        # here is not leaked once but once per retry for as long as the device is
        # down, which on a 64 channel recorder is 64 at a time.
        for coordinator in built:
            if coordinator not in coordinators:
                await coordinator.async_stop()

    if failures:
        # One bad channel must not take a recorder's other sixty three offline.
        # Said once, with the channels named, because the alternative is a user
        # wondering why one camera is missing and finding nothing in the log.
        _LOGGER.warning(
            "%s set up %d of %d channels. These did not answer and will be "
            "retried on the next poll: %s",
            entry.data.get(CONF_ADDRESS),
            len(coordinators),
            len(built),
            ", ".join(str(channel) for channel, _ in failures),
        )

    # Home Assistant's own place for per entry runtime state, and it clears the
    # attribute itself when the entry unloads, so there is nothing to pop.
    entry.runtime_data = {c.get_channel(): c for c in coordinators}

    # https://developers.home-assistant.io/docs/config_entries_index/
    # Forward every platform in one call. Home Assistant gathers them into
    # concurrent tasks, so one call sets all of them up at once; calling it once
    # per platform instead serialised them, and an entry's setup budget then had
    # to cover the sum of six platforms rather than the slowest one. A device
    # answering slowly could exhaust it and take the whole entry down with a
    # CancelledError -- see #513.
    #
    # The platform list is per entry, so it is the same for every channel and the
    # forward happens once. Each coordinator still carries its own copy because
    # unload reads it back off them.
    wanted = [p for p in PLATFORMS if entry.options.get(p, True)]
    for coordinator in coordinators:
        coordinator.platforms.extend(wanted)
    if wanted:
        await hass.config_entries.async_forward_entry_setups(entry, wanted)

    # Wrapped, because unloading does not clear an entry's update listeners.
    # A plain add_update_listener leaves one behind on every reload, and then a
    # single options change fires as many reloads as the entry has ever had --
    # against an NVR, exactly the burst that wedges it.
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # Every channel gets its own shutdown hook. A single one would have left the
    # other channels' sessions and host pool references open on a Home Assistant
    # stop, which is the leak async_stop exists to prevent.
    for coordinator in coordinators:
        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_stop)
        )

    return True


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
            "it keeps the one it has",
            address,
        )
        return

    # Imported here because config_flow imports this module.
    from .config_flow import channel_unique_id

    wanted = channel_unique_id(serial, entry.data.get(CONF_CHANNEL, 0))
    if wanted == unique_id:
        return

    # No need to exclude this entry: its own id is md5 shaped and `wanted` is a device
    # serial, and the one case where they are equal already returned above.
    taken = [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.unique_id == wanted
    ]
    if taken:
        _LOGGER.warning(
            "%s reports serial %s over the network, but the entry %s already holds that "
            "identity, so this camera is configured twice. Leaving both alone: remove "
            "whichever one you do not want rather than have this pick for you",
            address,
            serial,
            taken[0].title,
        )
        return

    _LOGGER.info(
        "%s was identified by a hash of its own credentials, which changes whenever the "
        "password does. The network probe reports serial %s, so this entry is being moved "
        "onto it and will survive a credential change from now on",
        address,
        serial,
    )
    hass.config_entries.async_update_entry(entry, unique_id=wanted)


async def async_unload_entry(hass: HomeAssistant, entry: DahuaConfigEntry) -> bool:
    """Handle removal of an entry."""
    channels = entry_coordinators(entry)
    if not channels:
        # Setup may have failed before the coordinator was registered, or a
        # previous unload may already have removed it. Treat that as unloaded
        # so an options-triggered reload can continue cleanly.
        return True

    # What the device refused is forgotten here rather than kept for the life of
    # the process, so that reloading an entry really does ask again -- which is
    # what the warning about a refused infrared write tells the user to do. Per
    # channel, so one recorder reloading does not cost every other host a refusal.
    from .refusals import forget as forget_refusals

    for coordinator in channels.values():
        forget_refusals(coordinator)

    # Every channel is stopped, and all of them are stopped even if one raises.
    # A coordinator that keeps its session and its host pool reference is the
    # leak async_stop exists to prevent, so one failure must not strand the rest.
    for result in await asyncio.gather(
        *[coordinator.async_stop() for coordinator in channels.values()],
        return_exceptions=True,
    ):
        if isinstance(result, BaseException):
            _LOGGER.debug(
                "Stopping a Dahua channel failed during unload", exc_info=result
            )

    # The union across channels: a platform is forwarded once per entry however
    # many channels asked for it, so unloading it once per channel would fail.
    wanted = {
        platform
        for coordinator in channels.values()
        for platform in coordinator.platforms
    }
    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in PLATFORMS
                if platform in wanted
            ]
        )
    )
    # Nothing to pop: Home Assistant deletes runtime_data itself when an entry
    # unloads. Which also means this function must not read it again below.

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

    Normalised here rather than trusted from the caller. Six stores and two
    repair ids are keyed below, every one of them written under
    `normalize_address`, so one raw address reaching this leaves all six behind
    for good and withdraws neither card -- and the single caller happens to
    normalise, which is what makes that a latent bug rather than a reported
    one. `normalize_address` says "everything host scoped goes through this",
    and this is one of the two places where that was a claim rather than a
    fact.
    """
    address = normalize_address(address)
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
    for target in [
        t for t in _CAPABILITY_REFUSALS_REPORTED if str(t[0]).split(":")[0] == address
    ]:
        _CAPABILITY_REFUSALS_REPORTED.discard(target)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_UNREACHABLE.format(address))
    ir.async_delete_issue(hass, DOMAIN, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE.format(address))


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
            ItemType,
            Searcher,
        )
        from homeassistant.helpers.entity import (  # pylint: disable=import-outside-toplevel
            entity_sources,
        )

        found = Searcher(hass, entity_sources(hass)).async_search(
            ItemType.CONFIG_ENTRY, entry_id
        )
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
    """ "3 automations and 1 script", or "" when nothing referenced it."""
    words = {
        "automation": "automation",
        "script": "script",
        "scene": "scene",
        "group": "group",
    }
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

    An entry owns one device per channel, and each identifier is derived from the
    serial that channel reports (`serial`, or `serial_N` above channel 0). So none of
    the devices this entry *currently* creates may be removable: Home Assistant would
    delete the row and the next reload would put it straight back, which looks like
    the button did nothing.

    Every channel, not the first one. This read
    `next(iter(channels.values())).get_serial_number()` while an entry was one
    channel, and #827 made an entry own all of them. On a ten channel recorder that
    offered a Delete button on nine live devices, and taking one deletes its entities
    from the registry along with whatever the user had set on them.

    A device whose identifier is not the one this entry now produces is stale, and
    that really happens. A camera that answered with a synthesised identity and later
    reported its real serial leaves the old record behind -- #583 has two device rows
    for one camera, the live one carrying the fallback identity and the stale one
    carrying the real model name. Those are exactly what the button is for.

    Refusing when the coordinator is missing is deliberate. Setup failed or the entry
    is unloaded, so nothing can be said about which device is current, and deleting
    the live one on a guess is worse than leaving a stale row alone for now.
    """
    channels = entry_coordinators(entry)
    if not channels:
        return False

    live = {coordinator.get_serial_number() for coordinator in channels.values()}
    return not any(
        domain == DOMAIN and value in live for domain, value in device.identifiers
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
    # Normally none: #827 merged each recorder onto one entry, so removing "the
    # recorder" is one deletion and there is nothing left to offer. This is kept
    # for the case that migration refused, where a host really does still have an
    # entry per channel and the offer is the only thing that makes removing it
    # bearable. Dormant rather than dead.
    siblings = _entries_for_address(hass, address) if address else []

    if address and not siblings:
        _async_forget_host(hass, address)
        _LOGGER.debug(
            "Last entry for %s removed; forgot its host state and withdrew its repairs",
            address,
        )

    _async_report_removal(hass, entry, address, siblings, dependents)


@callback
def _async_report_removal(
    hass: HomeAssistant,
    entry: ConfigEntry,
    address: str,
    siblings: list,
    dependents: dict,
) -> None:
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
            hass,
            DOMAIN,
            ISSUE_SIBLINGS_REMAIN.format(address),
            is_fixable=True,
            is_persistent=True,
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
                    (
                        "%s also referenced the entry you removed, and are not "
                        "repaired by this." % described.capitalize()
                    )
                    if described
                    else ""
                ),
            },
        )
        return

    if described:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_REMOVAL_BROKE_THINGS.format(entry.entry_id),
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="removal_broke_things",
            translation_placeholders={
                "removed": entry.title or "untitled",
                "dependents": described,
                "names": ", ".join(
                    name for items in dependents.values() for name in items
                ),
            },
        )


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
