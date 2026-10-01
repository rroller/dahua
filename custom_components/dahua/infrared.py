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

from . import dahua_utils, refusals
from .const import DOMAIN

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

# Which control this is, in refusals.py's store. Named rather than inferred so a
# recorder refusing its infrared does not silence its siren on the same channel.
CONTROL = "infrared"


def infrared_write_is_refused(coordinator) -> bool:
    """Whether this channel has already refused a lighting write outright."""
    return refusals.is_refused(coordinator, CONTROL)


def forget_refused_infrared_writes(coordinator=None) -> None:
    """Drop what was learnt, so the device is asked again."""
    refusals.forget(coordinator, None if coordinator is None else CONTROL)


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
            coordinator.get_infrared_profile(),
            coordinator.get_infrared_bank())
    except WRITE_FAILED as err:
        if refusals.refusal_is_outright(err):
            refusals.remember(coordinator, CONTROL,
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
