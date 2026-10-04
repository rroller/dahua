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
# Kept as the v1 name so nothing that imported it has to change.
CONTROL = refusals.INFRARED_V1


def infrared_transports(coordinator) -> list:
    """The write paths available for this channel, in the order to try them.

    v1 first, always: it is the table every working device uses today. v2 is
    appended only when the device serves a row there, and is reached only once v1
    has refused -- see `infrared_transport` in coordinator.py for why the rule is
    "what the device accepts" and not "what the device reports it has".
    """
    paths = [refusals.INFRARED_V1]
    if coordinator.get_infrared_v2_row() is not None:
        paths.append(refusals.INFRARED_V2)
    return paths


def infrared_write_is_refused(coordinator) -> bool:
    """Whether every path available for this channel has been refused.

    Not just v1. A channel with a v2 row still has somewhere to go after v1 is
    refused, so reporting it as refused then would take the control away while a
    working path remained.
    """
    return all(
        refusals.is_refused(coordinator, path)
        for path in infrared_transports(coordinator)
    )


def forget_refused_infrared_writes(coordinator=None) -> None:
    """Drop what was learnt, so the device is asked again.

    Both paths, because a reload should re-ask the question from the top rather
    than leave a channel pinned to the fallback it learnt last time.
    """
    if coordinator is None:
        refusals.forget()
        return
    for path in (refusals.INFRARED_V1, refusals.INFRARED_V2):
        refusals.forget(coordinator, path)


async def async_write_infrared_mode(coordinator, mode: str, brightness: int) -> None:
    """Write one infrared mode and brightness, and raise if it did not take.

    `mode` is the device's own spelling: `Auto`, `Manual` or `Off`.

    Refreshes before checking, because the check is against what the device
    reports rather than what it was asked, and reads the *mode* rather than
    `is_on`: an ignored turn-off leaves a channel on `Auto`, which is not
    `Manual` either, so `is_on` would agree with the write that failed.
    """
    device = coordinator.get_device_name()
    channel = coordinator.get_channel()

    untried = [
        path
        for path in infrared_transports(coordinator)
        if not refusals.is_refused(coordinator, path)
    ]
    if not untried:
        # Nothing is sent. Every path this channel has was refused, and a second
        # identical request is a round trip to hear the same answer.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="infrared_write_already_refused",
            translation_placeholders={"device": device},
        )

    last = None
    for path in untried:
        try:
            if path == refusals.INFRARED_V2:
                profile, index, bank = coordinator.get_infrared_v2_row()
                await coordinator.client.async_set_lighting_v2_mode(
                    channel, mode, brightness, profile, index, bank
                )
            else:
                await coordinator.client.async_set_lighting_v1_mode(
                    channel,
                    mode,
                    brightness,
                    coordinator.get_infrared_profile(),
                    coordinator.get_infrared_bank(),
                )
        except WRITE_FAILED as err:
            last = err
            if refusals.refusal_is_outright(err):
                # Learnt, and the next path is tried in the same press rather
                # than making the user click again to discover the fallback.
                refusals.remember(
                    coordinator, path, dahua_utils.describe_write_refusal(err)
                )
                continue
            # Not a refusal -- a timeout or a dropped connection says nothing
            # about whether this path works, so do not burn the fallback on it.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="infrared_write_refused",
                translation_placeholders={
                    "device": device,
                    "reason": dahua_utils.describe_write_refusal(err),
                },
            ) from err
        break
    else:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="infrared_write_refused",
            translation_placeholders={
                "device": device,
                "reason": dahua_utils.describe_write_refusal(last),
            },
        ) from last

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
            device,
            mode,
            coordinator.get_channel(),
            reported,
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="infrared_write_ignored",
            translation_placeholders={
                "device": device,
                "wanted": mode,
                "reported": reported,
            },
        )
