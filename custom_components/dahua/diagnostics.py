"""Diagnostics support for Dahua.

Most bug reports against this integration arrive as a screenshot of one log line.
The dump below is meant to answer, without a round trip, the questions that are
actually asked: what did the device report it can do, what is the coordinator's
last error, and - on an NVR - how many other config entries are competing for the
same host.

Nothing here talks to the device. It only reports state the integration already
holds, so it is safe to pull when the device is unreachable, which is exactly
when someone will pull it.
"""
import time
from hashlib import sha256
from typing import Any, Callable, Mapping

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    CONF_ADDRESS,
    CONF_AUTO_DETECT_CHANNEL,
    CONF_CHANNEL,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)

# Redacted by key, everywhere they appear.
#
# The serial number is in here deliberately. When a device does not report one,
# the integration derives it as md5("{address}_{rtsp_port}_{username}_{password}")
# - see the fallback branches in client.py. Publishing that hash next to the
# address and RTSP port, which are not secret, would make an offline dictionary
# attack on the password straightforward. There is no reliable way to tell from
# outside which devices took the fallback, so the serial is always withheld and
# `serial_fingerprint` is offered instead.
TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    "unique_id",
    "serialNumber",
    "serial_number",
}


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    """Call an accessor, returning `default` if it raises.

    Several accessors dereference `coordinator.data`, which is None until the
    first successful refresh, and `get_serial_number()` reads an attribute that
    is only assigned during capability detection. A diagnostics handler that
    raises returns HTTP 500 with no explanation, so nothing here may propagate.
    """
    try:
        return fn()
    except Exception:  # pylint: disable=broad-except
        return default


def _serial_fingerprint(coordinator) -> str | None:
    """A stable, non-reversible stand-in for the serial number.

    Keeps the one property that matters for support - which entries belong to
    the same physical device - without publishing the serial itself.
    """
    serial = _safe(coordinator.get_serial_number)
    if not serial:
        return None
    return sha256(str(serial).encode("utf-8")).hexdigest()[:12]


def _entry_block(config_entry: ConfigEntry) -> dict[str, Any]:
    # Imported here rather than at module scope: __init__ imports this module's
    # siblings, and a top-level import would be circular.
    from . import (
        get_configured_events,
        get_configured_scan_interval,
        get_configured_use_https,
    )

    return {
        "entry_id": config_entry.entry_id,
        "version": config_entry.version,
        "source": config_entry.source,
        "state": str(config_entry.state),
        "disabled_by": str(config_entry.disabled_by),
        "data": async_redact_data(dict(config_entry.data), TO_REDACT),
        "options": dict(config_entry.options),
        # The *effective* values, after options override entry data. Users
        # routinely report the value they set rather than the one in force.
        "resolved_events": get_configured_events(config_entry),
        "resolved_use_https": get_configured_use_https(config_entry),
        "resolved_scan_interval_seconds": _safe(
            lambda: get_configured_scan_interval(config_entry).total_seconds()
        ),
    }


def _coordinator_block(coordinator) -> dict[str, Any]:
    last_success = getattr(coordinator, "last_update_success_time", None)
    exception = getattr(coordinator, "last_exception", None)
    data = coordinator.data or {}

    return {
        "initialized": coordinator.initialized,
        "last_update_success": coordinator.last_update_success,
        "last_update_success_time": last_success.isoformat() if last_success else None,
        "last_exception_type": type(exception).__name__ if exception else None,
        "last_exception": str(exception) if exception else None,
        "update_interval_seconds": _safe(
            lambda: coordinator.update_interval.total_seconds()
        ),
        "platforms": list(coordinator.platforms),
        "data_key_count": len(data),
        # Keys only, never values. Which keys the device returned is what decides
        # whether an entity exists; the values are camera configuration noise.
        "data_keys": sorted(data),
    }


def _device_block(coordinator, config_entry: ConfigEntry) -> dict[str, Any]:
    return {
        "model": _safe(coordinator.get_model),
        "machine_name": getattr(coordinator, "machine_name", None),
        "name": _safe(coordinator.get_device_name),
        "firmware": _safe(coordinator.get_firmware_version),
        "serial_fingerprint": _serial_fingerprint(coordinator),
        "serial_is_derived_from_credentials": getattr(
            coordinator.client, "identity_derived_from_credentials", None
        ),
        "channel_index": _safe(coordinator.get_channel),
        "channel_number": _safe(coordinator.get_channel_number),
        "auto_detect_channel": config_entry.options.get(CONF_AUTO_DETECT_CHANNEL, True),
        "max_streams": _safe(coordinator.get_max_streams),
        "profile_mode": _safe(coordinator.get_profile_mode),
    }


def _capabilities_block(coordinator) -> dict[str, Any]:
    """What the device was probed to support, and what its model name implies.

    Together these decide which entities exist, so "why do I have no siren
    entity" is answerable from this block alone.
    """
    probed = {
        name.lstrip("_").removeprefix("supports_"): getattr(coordinator, name)
        for name in vars(coordinator)
        if name.startswith("_supports_")
    }
    derived = {
        "is_doorbell": _safe(coordinator.is_doorbell),
        "is_amcrest_doorbell": _safe(coordinator.is_amcrest_doorbell),
        "is_flood_light": _safe(coordinator.is_flood_light),
        "supports_siren": _safe(coordinator.supports_siren),
        "supports_security_light": _safe(coordinator.supports_security_light),
        "supports_infrared_light": _safe(coordinator.supports_infrared_light),
        "supports_illuminator": _safe(coordinator.supports_illuminator),
        "supports_smart_motion_amcrest": _safe(
            coordinator.supports_smart_motion_detection_amcrest
        ),
    }
    return {"probed": probed, "derived_from_model": derived}


def _client_block(coordinator, config_entry: ConfigEntry) -> dict[str, Any]:
    client = coordinator.client
    return {
        # _base carries no credentials; the RTSP URL does, so it is described
        # rather than emitted. async_redact_data works by key and would not
        # touch a password interpolated into a URL value.
        "base_url": getattr(client, "_base", None),
        "address_as_client_sees_it": getattr(client, "_address", None),
        "address_as_entry_stores_it": config_entry.data.get(CONF_ADDRESS),
        "port": getattr(client, "_port", None),
        "rtsp_port": getattr(client, "_rtsp_port", None),
        "use_https": getattr(client, "_use_https", None),
        # Boolean only. The digest state holds the challenge nonce and the
        # response, which is derived from the password.
        "digest_challenge_cached": bool(getattr(client, "_digest_state", None)),
        "rpc2_session_active": getattr(client, "_rpc2_session_instance", None)
        is not None,
        "rtsp_url_shape": (
            "rtsp://REDACTED:REDACTED@{address}:{rtsp_port}"
            "/cam/realmonitor?channel={channel_number}&subtype=0"
        ),
    }


def _events_block(coordinator) -> dict[str, Any]:
    now = int(time.time())
    timestamps = getattr(coordinator, "_dahua_event_timestamp", {}) or {}
    event_task = getattr(coordinator, "_event_task", None)
    vto_task = getattr(coordinator, "_vto_task", None)

    return {
        "configured": _safe(coordinator.get_event_list, []),
        "stream_task_running": bool(event_task) and not event_task.done(),
        "vto_task_running": bool(vto_task) and not vto_task.done(),
        "vto_client_connected": getattr(coordinator, "_vto_client", None) is not None,
        # Listener keys are "<EventName>-<channel>". On an NVR, listeners for
        # channel 3 while events arrive with index 0 is the channel-offset bug.
        "listener_keys": sorted(getattr(coordinator, "_dahua_event_listeners", {})),
        # Ages, not epochs. A motion event 21600 seconds old is a binary sensor
        # that has been on for six hours because no Stop action ever arrived.
        "timestamp_age_seconds": {
            key: (now - value) if value else None for key, value in timestamps.items()
        },
        "active_count": sum(1 for value in timestamps.values() if value),
    }


def _host_block(hass: HomeAssistant, coordinator, config_entry: ConfigEntry) -> dict[str, Any]:
    """Per-host contention, which is invisible from a single entry today.

    An NVR gets one config entry per channel, so a report titled "my camera
    keeps dropping out" is often the sixth of eleven entries against one host.
    """
    from . import _HOST_CONNECTORS
    from .client import _HOST_LIMITS, MAX_CONCURRENT_REQUESTS_PER_HOST

    address = config_entry.data.get(CONF_ADDRESS)
    client_address = getattr(coordinator.client, "_address", address)
    holder = _HOST_CONNECTORS.get(address)
    limiter = getattr(coordinator.client, "_host_limit", None)

    siblings = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_ADDRESS) == address
    ]

    return {
        "address": address,
        "connector_refcount": holder[1] if holder else None,
        "connector_closed": holder[0].closed if holder else None,
        # If these two registries disagree - for example one key with a trailing
        # slash and one without - a single device is holding two pools.
        "connector_registry_keys": sorted(_HOST_CONNECTORS),
        "limiter_registry_keys": sorted(_HOST_LIMITS),
        "client_key_matches_connector_key": client_address == address,
        "max_concurrent_requests_per_host": MAX_CONCURRENT_REQUESTS_PER_HOST,
        "limiter_free_slots": getattr(limiter, "_value", None),
        "limiter_locked": _safe(limiter.locked) if limiter else None,
        "sibling_entry_count": len(siblings),
        "sibling_entries": [
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "channel": entry.data.get(CONF_CHANNEL, 0),
                "state": str(entry.state),
                "is_this_entry": entry.entry_id == config_entry.entry_id,
            }
            for entry in siblings
        ],
        "total_dahua_entries": len(hass.config_entries.async_entries(DOMAIN)),
    }


def _active_issues(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Our own repair issues.

    Home Assistant appends its own `issues` block, but for non-persistent issues
    it emits only the id, so it does not say what the issue was.
    """
    registry = ir.async_get(hass)
    return [
        {
            "issue_id": issue.issue_id,
            "translation_key": issue.translation_key,
            "severity": str(issue.severity),
            "is_fixable": issue.is_fixable,
            "active": issue.active,
            "created": issue.created.isoformat() if issue.created else None,
            "translation_placeholders": issue.translation_placeholders,
        }
        for (domain, _), issue in registry.issues.items()
        if domain == DOMAIN
    ]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> Mapping[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]

    return {
        "entry": _entry_block(config_entry),
        "coordinator": _coordinator_block(coordinator),
        "device": _device_block(coordinator, config_entry),
        "capabilities": _capabilities_block(coordinator),
        "client": _client_block(coordinator, config_entry),
        "events": _events_block(coordinator),
        "host": _host_block(hass, coordinator, config_entry),
        "active_issues": _active_issues(hass),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry, device: DeviceEntry
) -> Mapping[str, Any]:
    """Return diagnostics for a device.

    One config entry is one device here, so the content is the same. This exists
    so the download button also appears on the device page, which is where
    support threads send people.

    `device.identifiers` is deliberately not emitted: it contains the serial
    number.
    """
    diagnostics = dict(await async_get_config_entry_diagnostics(hass, config_entry))
    diagnostics["device_registry"] = {
        "name": device.name,
        "name_by_user": device.name_by_user,
        "model": device.model,
        "manufacturer": device.manufacturer,
        "sw_version": device.sw_version,
        "area_id": device.area_id,
        "disabled_by": str(device.disabled_by),
        "entry_type": str(device.entry_type),
    }
    return diagnostics
