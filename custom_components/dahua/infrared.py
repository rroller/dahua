"""Writing the infrared light's mode, and checking the device actually took it.

Shared by the light entity, which can only say on or off, and the mode select,
which can also put a channel back to letting the camera decide. Both have to
handle the same device behaviours, and neither can be trusted to a status code
alone -- see the module docstring of
tests/dahua/test_the_infrared_write_is_not_silently_lost.py for the measurements.
"""

import asyncio
import logging

import aiohttp

from homeassistant.exceptions import HomeAssistantError

from . import dahua_utils
from .const import DOMAIN
from .rpc2 import Rpc2MethodRefused

_LOGGER = logging.getLogger(__name__)

# The three modes this integration writes. A device may report others it chose
# itself -- a DHI-NVR5464-16P-EI answers `ZoomPrio` on two of fifteen channels --
# and those are read back and shown, but never offered, because nothing here
# knows what they would mean to write.
INFRARED_MODES = ("Auto", "Manual", "Off")

# What Home Assistant asks for, and what the device calls it. Home Assistant
# looks a select option's label up under `entity.select.<key>.state.<option>`,
# and that lookup only works for a slug, so the options cannot be the device's
# own capitalised words.
MODE_BY_OPTION = {"auto": "Auto", "manual": "Manual", "off": "Off"}
OPTION_BY_MODE = {mode: option for option, mode in MODE_BY_OPTION.items()}

# The failures a device answers a lighting write with, as opposed to a bug here.
WRITE_FAILED = (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError)

# A device saying "not permitted", as opposed to one that could not be reached.
#
# 403 is what a DHI-NVR5464-16P-EI answers to every `Lighting` write, with
# `Authority:check failure.` in the body; 285278249 is the same answer over RPC2.
# 401 is deliberately not here: that is the credentials, which reauth exists for
# and which a later write may well succeed with.
WRITE_REFUSED_STATUS = frozenset({403})
WRITE_REFUSED_RPC2_CODES = frozenset({285278249})

# (device, channel) whose lighting write the device has already refused outright.
#
# Measured on a DHI-NVR5464-16P-EI: every one of its channels refuses, over CGI
# and RPC2 both, so the control there can only ever fail. Asking again on every
# press spends a request to be told the same thing, and leaves the user pressing
# a button that looks like it might work.
#
# Only an outright refusal counts. A timeout or a dropped connection says nothing
# about whether the write is possible, and writing the channel off for one would
# take a working control away from somebody whose camera was merely busy.
#
# Not remembered across a restart, on the same reasoning as
# `_HOST_CGI_CONFIG_ABSENT` in client.py: a permissions change on the device
# should cost one refusal to discover rather than being invisible until somebody
# thinks to reload the entry.
_WRITE_REFUSED: set = set()


def _key(coordinator) -> tuple:
    return (coordinator.get_serial_number(), coordinator.get_channel())


def infrared_write_is_refused(coordinator) -> bool:
    """Whether this channel has already refused a lighting write outright."""
    return _key(coordinator) in _WRITE_REFUSED


def forget_refused_infrared_writes(coordinator=None) -> None:
    """Drop what was learnt, so the device is asked again.

    Called for each channel as its entry unloads, which is what makes "reload the
    entry to try again" true -- and per channel rather than wholesale, so
    reloading one recorder does not make every other host pay a refusal again.
    """
    if coordinator is None:
        _WRITE_REFUSED.clear()
        return
    _WRITE_REFUSED.discard(_key(coordinator))


def refusal_is_outright(error) -> bool:
    """Whether the device answered and declined, rather than failing to answer.

    Judged on what it said, not on the class of the exception: an
    `aiohttp.ClientResponseError` is raised for a 403 and for a 500 alike, and
    only one of those means the write will never be accepted.
    """
    if isinstance(error, Rpc2MethodRefused):
        return getattr(error, "code", None) in WRITE_REFUSED_RPC2_CODES
    return getattr(error, "status", None) in WRITE_REFUSED_STATUS


async def async_write_infrared_mode(coordinator, mode: str, brightness: int) -> None:
    """Write one infrared mode and brightness, and raise if it did not take.

    `mode` is the device's own spelling: `Auto`, `Manual` or `Off`.

    Refreshes before checking, because the check is against what the device
    reports rather than what it was asked, and reads the *mode* rather than
    `is_on`: an ignored turn-off leaves a channel on `Auto`, which is not
    `Manual` either, so `is_on` would agree with the write that failed.
    """
    device = coordinator.get_device_name()

    if infrared_write_is_refused(coordinator):
        # Nothing is sent. The device has already said it will not do this, and a
        # second identical request is a round trip to hear it again.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="infrared_write_already_refused",
            translation_placeholders={"device": device},
        )

    try:
        await coordinator.client.async_set_lighting_v1_mode(
            coordinator.get_channel(), mode, brightness,
            coordinator.get_infrared_profile())
    except WRITE_FAILED as err:
        if refusal_is_outright(err):
            _WRITE_REFUSED.add(_key(coordinator))
            _LOGGER.warning(
                "%s will not accept an infrared write on channel %s (%s), so "
                "Home Assistant will stop asking. Its mode and level are still "
                "read and shown. Reload the entry to try again after changing "
                "anything on the device",
                device, coordinator.get_channel(),
                dahua_utils.describe_write_refusal(err))
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="infrared_write_refused",
            translation_placeholders={
                "device": device,
                "reason": dahua_utils.describe_write_refusal(err),
            },
        ) from err

    await coordinator.async_refresh()

    reported = coordinator.get_infrared_mode()
    if not reported:
        # No Lighting row for this channel at all, so there is nothing to
        # compare against. Raising here would put an error on every press for a
        # device this entity should not have been created on.
        return
    if reported != mode:
        _LOGGER.debug(
            "%s accepted Lighting mode %s on channel %s and still reports %s",
            device, mode, coordinator.get_channel(), reported)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="infrared_write_ignored",
            translation_placeholders={
                "device": device,
                "wanted": mode,
                "reported": reported,
            },
        )
