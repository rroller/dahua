"""A `Lighting` write that answers OK, is stored, and drives nothing (#647).

Measured on an EmpireTech IPC-T5442TM-AS-6mm, firmware V2.840.15OG008.0.R, by
the reporter, in one message so the two reads are of the same moment:

    setConfig&Lighting[0][0].Mode=Manual&Lighting[0][0].MiddleLight[0].Light=100
                                                                      200 'OK'

    getConfig&name=Lighting                 getConfig&name=Lighting_V2
    Lighting[0][0].Mode=Manual              Lighting_V2[0][0][0].LightType=InfraredLight
    Lighting[0][0].MiddleLight[0].Light=100 Lighting_V2[0][0][0].Mode=Off
    Lighting[0][1].Mode=Off                 Lighting_V2[0][0][0].MiddleLight[0].Light=86
    Lighting[0][2].Mode=Manual              Lighting_V2[0][1][0].Mode=Manual
    Lighting[0][3].Mode=Auto                Lighting_V2[0][2][0].Mode=Auto

So the write landed in v1 and the emitter, which follows v2, stayed dark. The
two tables are not linked on this firmware and do not even carry the same number
of profiles: four against three. Every v2 row is `InfraredLight` and there is no
`WhiteLight` row anywhere, which is also why this camera correctly has no
Illuminator entity.

The reporter's working commands, by profile, confirm which table drives the LED:

    day   (VideoInMode[0].Config[0]=0)   setConfig&Lighting_V2[0][0][0].Mode=Manual
    night (VideoInMode[0].Config[0]=1)   setConfig&Lighting_V2[0][1][0].Mode=Manual

**Why the integration could not see any of this.** Two things, and each one alone
would have hidden it:

1. It moves to v2 only once v1 has *refused*, and this camera never refuses.
2. Having written v1 it read v1 back to check, so it compared the value against
   the copy it had just stored and agreed with itself. The card said `Manual`
   with the LEDs off, and nothing appeared in the log as an error.

The fix keeps the rule that a device is moved by what it *does* and not by what
it reports it has. It just stops treating a raised refusal as the only way a
device says no: a path that answers OK and then does not move the light is
remembered and stepped over exactly as a 403 is.

Mutation table, each caught by the test named after it:

* reading the mode from whichever table was written, rather than from v2
* preferring v2 on presence, which moves cameras nobody has measured
* falling through without remembering, so every press pays for both writes
* remembering the last remaining path, which takes the control away
* writing the v2 fallback to profile 0 while the camera is on night
* choosing the brightness bank by the table being read instead of the one
  being written
* telling the user the control has stopped while the fallback is driving it
* learning from a reading that may predate the write, which condemns a working
  path whose verification read merely timed out (found by @rroller in review)
"""

import logging
from unittest.mock import AsyncMock

import aiohttp
import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import DahuaDataUpdateCoordinator, refusals
from custom_components.dahua.infrared import (
    async_write_infrared_mode,
    infrared_write_is_refused,
)

ADDRESS = "192.168.0.60"


def _response_error(status, message):
    """A refusal shaped the way the device's really arrives. `request_info` is
    not optional: aiohttp reads `real_url` while formatting one."""
    return aiohttp.ClientResponseError(
        aiohttp.RequestInfo(
            url="http://camera/cgi-bin/configManager.cgi",
            method="GET",
            headers=aiohttp.typedefs.CIMultiDict(),
            real_url="http://camera/cgi-bin/configManager.cgi",
        ),
        (),
        status=status,
        message=message,
    )


def _rows(
    v1_modes=("Off", "Off", "Manual", "Auto"),
    v2_modes=("Off", "Manual", "Auto"),
    v1_bank="MiddleLight",
    v2_bank="MiddleLight",
):
    """The reporter's two tables, keyed exactly as the device serves them.

    v1 carries four profiles and v2 three, which is the shape difference that
    makes "the same value in two places" impossible to assume.

    The banks are separate arguments because they are separate facts. A
    DHI-NVR5464-16P-EI serves `NearLight`/`FarLight` on one channel and
    `MiddleLight` on another, and naming the wrong one makes the device answer
    200 with the body `Error`, which every write method reads as success.
    """
    data = {}
    for profile, mode in enumerate(v1_modes):
        base = "table.Lighting[0][%d]." % profile
        data[base + "Mode"] = mode
        data[base + v1_bank + "[0].Light"] = "100"
        data[base + "Correction"] = "50"
    for profile, mode in enumerate(v2_modes):
        base = "table.Lighting_V2[0][%d][0]." % profile
        data[base + "LightType"] = "InfraredLight"
        data[base + "Mode"] = mode
        data[base + v2_bank + "[0].Light"] = "86"
    return data


class _Device:
    """A camera whose two lighting tables are, or are not, wired together.

    Holds the device's own state. The coordinator gets a **separate** snapshot,
    updated only by a refresh, because sharing one dict makes a write visible to
    the check before any read happens and no staleness can be expressed at all.
    """

    def __init__(
        self,
        data,
        linked=False,
        v2_ignores=False,
        v2_read_raises=None,
        v2_write_raises=None,
    ):
        self.data = data
        self.linked = linked
        self.v2_ignores = v2_ignores
        self.v2_read_raises = v2_read_raises
        self.v2_write_raises = v2_write_raises
        self.v1_writes = []
        self.v2_writes = []

    async def async_get_lighting_v2(self):
        """The poll's read of the v2 table, which is allowed to fail."""
        if self.v2_read_raises is not None:
            raise self.v2_read_raises
        return {
            key: value
            for key, value in self.data.items()
            if key.startswith("table.Lighting_V2[")
        }

    async def async_set_lighting_v1_mode(
        self, channel, mode, brightness, profile, bank
    ):
        self.v1_writes.append((channel, mode, brightness, profile, bank))
        # Accepted and stored, which is what makes the decoy a decoy.
        self.data["table.Lighting[%d][%s].Mode" % (channel, profile)] = mode
        if self.linked:
            mirror = "table.Lighting_V2[%d][%s][0].Mode" % (channel, profile)
            if mirror in self.data:
                self.data[mirror] = mode

    async def async_set_lighting_v2_mode(
        self, channel, mode, brightness, profile, index, bank
    ):
        self.v2_writes.append((channel, mode, brightness, profile, index, bank))
        if self.v2_write_raises is not None:
            raise self.v2_write_raises
        if not self.v2_ignores:
            self.data[
                "table.Lighting_V2[%d][%s][%d].Mode" % (channel, profile, index)
            ] = mode


def _coordinator(device, profile="0", channel=0, refresh_fails=False):
    """The real coordinator, so the real accessors decide which table is read.

    `async_refresh` runs the real `_async_fetch_lighting_v2`, which is the whole
    point: that is the method that carries the previous table forward when the
    read fails, and a double that returned the device's truth regardless would
    hide exactly the bug this guards.

    `refresh_fails` models a refresh that failed outright, where Home Assistant
    leaves `data` as it was and the v2 read never runs.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = channel
    coordinator._profile_mode = profile
    coordinator._address = ADDRESS
    coordinator.client = device
    coordinator.data = dict(device.data)
    coordinator.last_update_success = True
    coordinator.get_device_name = lambda: "Driveway"

    async def _refresh():
        if refresh_fails:
            coordinator.last_update_success = False
            return
        # The v1 table is not what is under test here, so it is copied straight
        # across. The v2 table goes through the real wrapper.
        coordinator.data.update(
            {
                key: value
                for key, value in device.data.items()
                if key.startswith("table.Lighting[")
            }
        )
        table = await DahuaDataUpdateCoordinator._async_fetch_lighting_v2(coordinator)
        if table is not None:
            coordinator.data.update(table)
        coordinator.last_update_success = True

    coordinator.async_refresh = _refresh
    return coordinator


def _v1_refused(coordinator):
    return refusals.is_refused(coordinator, refusals.INFRARED_V1)


def _v2_refused(coordinator):
    return refusals.is_refused(coordinator, refusals.INFRARED_V2)


# --- the read tells the truth -------------------------------------------------


def test_the_mode_is_read_from_the_table_the_emitter_follows():
    """The reporter's exact state after his v1 write: stored Manual, dark LED.

    Reading whichever table was written is what made the failure invisible, so
    this is the assertion the whole fix rests on.
    """
    device = _Device(_rows(v1_modes=("Manual", "Off", "Manual", "Auto")))
    coordinator = _coordinator(device)

    assert coordinator.data["table.Lighting[0][0].Mode"] == "Manual"
    assert coordinator.get_infrared_mode() == "Off"
    assert coordinator.is_infrared_light_on() is False


def test_the_level_is_read_from_the_same_table_as_the_mode():
    """86 is the v2 row's brightness and 100 the v1 row's. Reporting a level
    from a table whose mode is not shown describes no light that exists."""
    device = _Device(_rows())
    coordinator = _coordinator(device)

    assert coordinator.get_infrared_level() == 86


def test_the_night_profile_is_read_from_its_own_v2_row():
    """`Lighting_V2[0][1][0]` is Manual while `[0][0][0]` is Off. A reader that
    ignores the live profile reports the wrong half of the camera."""
    device = _Device(_rows())

    assert _coordinator(device, profile="1").get_infrared_mode() == "Manual"
    assert _coordinator(device, profile="0").get_infrared_mode() == "Off"


def test_a_channel_with_no_v2_row_still_reads_v1():
    """Thirteen of the measured recorder's fifteen channels, and every camera
    serving only the old table. Nothing here may move them."""
    device = _Device({"table.Lighting[0][0].Mode": "Auto"})

    assert _coordinator(device).get_infrared_mode() == "Auto"


def test_reading_the_mode_never_consults_the_refusal_store():
    """A read must not depend on the device address, which a half-constructed
    coordinator does not have. Kept from test_infrared_profile.py, which broke
    five ways the first time a read reached for it."""
    device = _Device(_rows())
    coordinator = _coordinator(device)

    def _explode():
        raise AssertionError("a read consulted the refusal store")

    coordinator.get_address = _explode

    assert coordinator.get_infrared_mode() == "Off"
    assert coordinator.get_infrared_level() == 86


# --- the write finds its way through ------------------------------------------


async def test_an_accepted_but_ignored_v1_write_falls_through_to_v2():
    """The reporter's press, end to end: v1 takes it, the light does not move,
    v2 is written in the same press and the light comes on."""
    device = _Device(_rows())
    coordinator = _coordinator(device)

    await async_write_infrared_mode(coordinator, "Manual", 100)

    assert device.v1_writes == [(0, "Manual", 100, "0", "MiddleLight")]
    assert device.v2_writes == [(0, "Manual", 100, "0", 0, "MiddleLight")]
    assert coordinator.get_infrared_mode() == "Manual"


async def test_v1_is_still_tried_first():
    """No camera is moved on the strength of a row it merely reports. The
    fallback is reached by measurement, in this press, and not by preference."""
    device = _Device(_rows())
    coordinator = _coordinator(device)

    await async_write_infrared_mode(coordinator, "Manual", 100)

    assert device.v1_writes, "v1 must be attempted before the fallback"


async def test_the_ignored_path_is_remembered_so_the_next_press_is_one_write():
    """Without this every press pays for a write the device is known to ignore,
    and the warning that explains it is never emitted."""
    device = _Device(_rows())
    coordinator = _coordinator(device)

    await async_write_infrared_mode(coordinator, "Manual", 100)
    assert _v1_refused(coordinator) is True
    assert _v2_refused(coordinator) is False

    device.v1_writes.clear()
    device.v2_writes.clear()
    await async_write_infrared_mode(coordinator, "Off", 100)

    assert device.v1_writes == []
    assert device.v2_writes == [(0, "Off", 100, "0", 0, "MiddleLight")]


async def test_each_write_carries_the_bank_its_own_table_names():
    """The banks differ per table on real hardware, and the bank is chosen by
    the *write* transport while the mode is read from v2 regardless. Tying the
    two together sends the v1 write a field its row does not have, which the
    device answers 200 `Error` to and every write method reads as success."""
    device = _Device(_rows(v1_bank="MiddleLight", v2_bank="NearLight"))
    coordinator = _coordinator(device)

    await async_write_infrared_mode(coordinator, "Manual", 100)

    assert device.v1_writes == [(0, "Manual", 100, "0", "MiddleLight")]
    assert device.v2_writes == [(0, "Manual", 100, "0", 0, "NearLight")]


async def test_the_fallback_writes_the_profile_the_camera_is_on():
    """Night. The reporter's working night command is `Lighting_V2[0][1][0]`,
    and a fallback hardcoded to profile 0 reproduces the original symptom."""
    device = _Device(_rows(v2_modes=("Off", "Off", "Auto")))
    coordinator = _coordinator(device, profile="1")

    await async_write_infrared_mode(coordinator, "Manual", 100)

    assert device.v2_writes == [(0, "Manual", 100, "1", 0, "MiddleLight")]


async def test_turning_off_falls_through_the_same_way():
    """On and off are not symmetric here: an ignored turn-off leaves the channel
    on a third value, so `is_on` would agree with the write that failed."""
    device = _Device(_rows(v1_modes=("Manual",) * 4, v2_modes=("Manual",) * 3))
    coordinator = _coordinator(device)

    await async_write_infrared_mode(coordinator, "Off", 0)

    assert device.v2_writes == [(0, "Off", 0, "0", 0, "MiddleLight")]
    assert coordinator.get_infrared_mode() == "Off"


# --- and does not go looking for trouble ---------------------------------------


async def test_a_camera_whose_tables_are_linked_never_reaches_the_fallback():
    """The case the old rule was protecting, and it is still protected: one
    write, nothing learnt, no second table touched."""
    device = _Device(_rows(), linked=True)
    coordinator = _coordinator(device)

    await async_write_infrared_mode(coordinator, "Manual", 100)

    assert device.v1_writes == [(0, "Manual", 100, "0", "MiddleLight")]
    assert device.v2_writes == []
    assert _v1_refused(coordinator) is False


async def test_the_last_remaining_path_is_reported_rather_than_remembered():
    """A write can be ignored for a reason that does not last. Marking the only
    path left refused would take the control away for the rest of the entry's
    life on the strength of one press."""
    device = _Device({"table.Lighting[0][0].Mode": "Auto"})
    coordinator = _coordinator(device)
    # The only path is v1, and this device stores the write without moving the
    # mode it reports.
    device.data["table.Lighting[0][0].Mode"] = "Auto"
    coordinator.client.async_set_lighting_v1_mode = AsyncMock()

    with pytest.raises(HomeAssistantError) as caught:
        await async_write_infrared_mode(coordinator, "Manual", 100)

    assert caught.value.translation_key == "infrared_write_ignored"
    assert caught.value.translation_placeholders["wanted"] == "Manual"
    assert caught.value.translation_placeholders["reported"] == "Auto"
    assert _v1_refused(coordinator) is False


async def test_a_fallback_that_is_also_ignored_is_reported_not_remembered():
    """Both paths gone through and the light still dark. The user gets the
    honest error, and the control is left askable."""
    device = _Device(_rows(), v2_ignores=True)
    coordinator = _coordinator(device)

    with pytest.raises(HomeAssistantError) as caught:
        await async_write_infrared_mode(coordinator, "Manual", 100)

    assert caught.value.translation_key == "infrared_write_ignored"
    assert caught.value.translation_placeholders["reported"] == "Off"
    assert device.v1_writes and device.v2_writes
    assert _v1_refused(coordinator) is True
    assert _v2_refused(coordinator) is False


async def test_the_warning_does_not_claim_a_working_control_has_stopped(caplog):
    """The refusals store is in memory, so this warning is emitted again on the
    first press after every restart. Saying "Home Assistant will stop asking,
    reload the entry" to a user whose light has just come on is false twice
    over, and it is the only thing about the fallback they will ever see."""
    device = _Device(_rows())
    coordinator = _coordinator(device)

    with caplog.at_level(logging.WARNING):
        await async_write_infrared_mode(coordinator, "Manual", 100)

    warned = " ".join(
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )
    assert "will use infrared_v2 instead" in warned
    assert "stop asking" not in warned
    assert "Reload the entry" not in warned


async def test_the_last_path_still_says_the_control_has_stopped(caplog):
    """The wording that was always right stays right where it applies: an
    outright refusal with nowhere left to go does end the control."""
    device = _Device({"table.Lighting[0][0].Mode": "Auto"})
    coordinator = _coordinator(device)
    coordinator.client.async_set_lighting_v1_mode = AsyncMock(
        side_effect=_response_error(403, "Forbidden")
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(HomeAssistantError):
            await async_write_infrared_mode(coordinator, "Manual", 100)

    assert "stop asking" in caplog.text


# --- nothing is learnt from a reading that may predate the write ---------------


async def test_a_timed_out_readback_does_not_condemn_a_working_path():
    """The regression @rroller found in review.

    `_async_fetch_lighting_v2` carries the previous table forward when its read
    fails, by design, so one bad answer costs a stale value instead of the whole
    poll. That stale value is indistinguishable from a real one. Here v1 works
    and the device really does reach Manual, but the verification read times out
    and the carried `Off` arrives instead. Learning from it marks a working path
    refused for ever.
    """
    device = _Device(_rows(), linked=True, v2_read_raises=TimeoutError())
    coordinator = _coordinator(device)

    await async_write_infrared_mode(coordinator, "Manual", 100)

    assert (
        device.data["table.Lighting_V2[0][0][0].Mode"] == "Manual"
    ), "the write reached the device; only the reading back of it failed"
    assert _v1_refused(coordinator) is False
    assert _v2_refused(coordinator) is False


async def test_an_unconfirmable_press_leaves_the_control_askable():
    """The consequence @rroller traced: v1 condemned on a stale reading and v2
    then refused outright left every later press raising
    `infrared_write_already_refused`, on a camera whose v1 was working."""
    device = _Device(
        _rows(),
        linked=True,
        v2_read_raises=TimeoutError(),
        v2_write_raises=_response_error(403, "Forbidden"),
    )
    coordinator = _coordinator(device)

    with pytest.raises(HomeAssistantError) as caught:
        await async_write_infrared_mode(coordinator, "Manual", 100)

    # v2 is genuinely refused and may be remembered. v1 must not be, so the
    # channel still has a path and the next press still reaches the device.
    assert _v1_refused(coordinator) is False
    assert infrared_write_is_refused(coordinator) is False
    assert caught.value.translation_key == "infrared_write_refused"

    device.v1_writes.clear()
    device.v2_read_raises = None
    await async_write_infrared_mode(coordinator, "Manual", 100)
    assert device.v1_writes, "the next press must still try v1"


async def test_a_refresh_that_failed_outright_teaches_nothing_either():
    """The other way a reading can predate the write: the refresh fails, Home
    Assistant keeps the last data, and the v2 read never runs at all. The data
    looks exactly the same as a successful poll's."""
    device = _Device(_rows(), linked=True)
    coordinator = _coordinator(device, refresh_fails=True)

    await async_write_infrared_mode(coordinator, "Manual", 100)

    assert _v1_refused(coordinator) is False
    assert _v2_refused(coordinator) is False


@pytest.mark.parametrize("fallback_refused", [False, True])
async def test_a_lighting_sample_discarded_by_a_failed_poll_is_not_evidence(
    hass, fallback_refused
):
    """Run the real HA refresh: the read succeeds but its data is not published.

    A sibling read failing after Lighting_V2 must not turn the old Off value
    into evidence that the working v1 write was ignored.
    """
    from homeassistant.helpers.update_coordinator import (
        DataUpdateCoordinator,
        UpdateFailed,
    )

    device = _Device(
        _rows(),
        linked=True,
        v2_write_raises=(
            _response_error(403, "Forbidden") if fallback_refused else None
        ),
    )
    coordinator = _coordinator(device)
    fail_poll = True

    async def poll():
        table = await coordinator._async_fetch_lighting_v2()
        if fail_poll:
            raise UpdateFailed("another poll read failed after Lighting_V2")
        return dict(device.data, **(table or {}))

    polling = DataUpdateCoordinator(
        hass,
        logging.getLogger(__name__),
        name="infrared readback",
        update_method=poll,
        config_entry=None,
    )
    polling.data = coordinator.data

    async def refresh():
        await polling.async_refresh()
        coordinator.data = polling.data
        coordinator.last_update_success = polling.last_update_success

    coordinator.async_refresh = refresh
    if fallback_refused:
        with pytest.raises(HomeAssistantError):
            await async_write_infrared_mode(coordinator, "Manual", 100)
    else:
        await async_write_infrared_mode(coordinator, "Manual", 100)

    assert coordinator.lighting_v2_samples() > 0
    assert polling.last_update_success is False
    assert coordinator.get_infrared_mode() == "Off"
    assert device.data["table.Lighting_V2[0][0][0].Mode"] == "Manual"
    assert _v1_refused(coordinator) is False
    assert infrared_write_is_refused(coordinator) is False

    # Once polling recovers, the next press still uses the working path.
    fail_poll = False
    device.v1_writes.clear()
    await async_write_infrared_mode(coordinator, "Manual", 100)
    assert device.v1_writes == [(0, "Manual", 100, "0", "MiddleLight")]
    assert coordinator.get_infrared_mode() == "Manual"


async def test_an_unconfirmable_press_still_tries_the_other_path():
    """Not knowing is not a reason to stop. The user asked for the light, the
    write may really have been ignored, so the press still does what it can.
    What it must not do is write that down as a fact."""
    device = _Device(_rows(), v2_read_raises=TimeoutError())
    coordinator = _coordinator(device)

    await async_write_infrared_mode(coordinator, "Manual", 100)

    assert device.v2_writes == [(0, "Manual", 100, "0", 0, "MiddleLight")]
    assert _v1_refused(coordinator) is False


async def test_a_fresh_reading_is_still_learnt_from():
    """The positive control. Without it every test above is satisfied by a fix
    that simply never learns anything, which would restore the original bug."""
    device = _Device(_rows())
    coordinator = _coordinator(device)

    await async_write_infrared_mode(coordinator, "Manual", 100)

    assert _v1_refused(coordinator) is True


def test_the_real_coordinator_answers_both_questions_the_write_path_asks():
    """Three test doubles now model `infrared_reads_lighting_v2` and
    `lighting_v2_samples`. If the real class ever loses one, only the doubles
    would still have it and the suite would stay green while production raised
    AttributeError on the first press."""
    for name in ("infrared_reads_lighting_v2", "lighting_v2_samples"):
        assert callable(getattr(DahuaDataUpdateCoordinator, name, None)), name


def test_the_sample_count_has_a_class_default():
    """Read before any poll has run, and on the many coordinators tests build
    with object.__new__. Without a default on the class this raises instead of
    answering, and it raises inside a write rather than at import."""
    coordinator = object.__new__(DahuaDataUpdateCoordinator)

    assert coordinator.lighting_v2_samples() == 0


async def test_a_channel_reporting_no_lighting_at_all_raises_nothing():
    """There is no state to compare against, and raising would put an error on
    every press for a device this entity should not have been created on."""
    device = _Device({})
    coordinator = _coordinator(device)

    await async_write_infrared_mode(coordinator, "Manual", 100)

    assert device.v1_writes == [(0, "Manual", 100, "0", None)]
