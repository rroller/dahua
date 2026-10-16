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

# What is recorded against a path that answered OK and then changed nothing. It
# is shown as the reason in the refusals warning, so it is phrased as one.
ACCEPTED_AND_IGNORED = "it accepted the write and the light did not move"

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


def _readback_is_fresh(coordinator, samples_before: int) -> bool:
    """Whether the completed refresh published a real Lighting_V2 reading.

    The check after a write compares what was asked against what the device
    reports, and that is only evidence when the reading is newer than the write.
    Three things give a reading that is not, and none of them shows in the data:

    * `_async_fetch_lighting_v2` catches a read failure and carries the previous
      table forward, deliberately, so one bad answer costs a stale value rather
      than the whole poll;
    * a refresh that fails outright leaves `data` exactly as it was;
    * a poll that did not want the table never read it.

    The coordinator counts the readings that really came from the device, so a
    count that has not moved means none of them was this one. Counted rather
    than timestamped because the question is "did one arrive", and a clock
    invites a tolerance nobody can choose correctly.

    The overall refresh must also succeed. A lighting read can increment the
    count before another branch fails, in which case Home Assistant keeps the
    previous data. That unpublished sample cannot validate the mode read from
    the old data, even though the count moved.

    Only meaningful where the check reads Lighting_V2. A channel whose mode
    comes from the v1 table is judged exactly as it always was, so no device
    that works today changes behaviour because of this.
    """
    return (
        coordinator.last_update_success
        and coordinator.lighting_v2_samples() > samples_before
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

    The check runs per path rather than once at the end, because a device has two
    ways of saying no and only one of them raises. The second is to answer `OK`,
    store the value and leave the light alone, and a path that does that is
    remembered and stepped over exactly as an outright refusal is. The last
    remaining path is exempt: see the comment on that branch.
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
    reported = None
    for position, path in enumerate(untried):
        # What the warnings below may promise. None means this is the last path
        # there is, so a refusal here really does end the control.
        instead = untried[position + 1] if position + 1 < len(untried) else None
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
                    coordinator,
                    path,
                    dahua_utils.describe_write_refusal(err),
                    instead=instead,
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

        # Only where the check will read Lighting_V2, because that is the only
        # reading whose freshness this can establish, and asking a question
        # whose answer is never used is how a coordinator ends up required to
        # answer it.
        reads_v2 = coordinator.infrared_reads_lighting_v2()
        samples_before = coordinator.lighting_v2_samples() if reads_v2 else 0
        await coordinator.async_refresh()
        reported = coordinator.get_infrared_mode()
        if not reported or reported == mode:
            # Empty means no Lighting row for this channel at all, so there is
            # nothing to compare against, and raising would put an error on
            # every press for a device this entity should not exist on.
            return

        if reads_v2 and not _readback_is_fresh(coordinator, samples_before):
            # The reading that disagrees with the write may predate it, so it is
            # not evidence about this path. The poll carries the previous
            # Lighting_V2 table when its read fails, and leaves `data` alone
            # entirely when the whole refresh fails; neither is distinguishable
            # from a real answer in the data itself.
            #
            # Nothing is learnt, and nothing is raised. A check that could not
            # run has not found a fault, and "it still reports Off" would be
            # quoting a reading the device never gave. Learning here condemned a
            # working v1 because its verification read timed out, and a refused
            # v2 behind it then blocked the control until reload.
            #
            # The remaining paths are still tried, because the write may really
            # have been ignored and the user asked for the light.
            _LOGGER.debug(
                "%s could not confirm %s mode %s on channel %s against a fresh "
                "reading, so nothing is learnt about that path",
                device,
                path,
                mode,
                channel,
            )
            if instead is None:
                return
            continue

        if instead is None:
            # The last path there is. Nothing is learnt here: a write can be
            # ignored for a reason that does not last, and marking the only
            # remaining path refused would take the control away on the strength
            # of a single press. Say so instead.
            break

        # Accepted, and nothing moved. That is the device saying no in the other
        # voice it has, so it is remembered exactly as an outright refusal is and
        # the next path is tried in the same press. Measured on the
        # IPC-T5442TM-AS-6mm in #647, whose v1 table stores the mode, answers
        # `OK` and drives nothing, while its v2 row drives the emitter.
        _LOGGER.debug(
            "%s accepted %s mode %s on channel %s and still reports %s, trying %s",
            device,
            path,
            mode,
            channel,
            reported,
            instead,
        )
        refusals.remember(coordinator, path, ACCEPTED_AND_IGNORED, instead=instead)
    else:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="infrared_write_refused",
            translation_placeholders={
                "device": device,
                "reason": dahua_utils.describe_write_refusal(last),
            },
        ) from last

    _LOGGER.debug(
        "%s accepted Lighting mode %s on channel %s and still reports %s",
        device,
        mode,
        channel,
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
