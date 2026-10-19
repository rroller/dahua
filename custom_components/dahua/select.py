"""Select entity platform for Dahua."""

import logging

from homeassistant.core import HomeAssistant
from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from custom_components.dahua import DahuaDataUpdateCoordinator

from . import dahua_utils, entry_coordinators
from .const import DOMAIN
from .entity import DahuaBaseEntity
from .late import async_add_and_keep_watching
from .infrared import (
    MODE_BY_OPTION,
    OPTION_BY_MODE,
    async_write_infrared_mode,
    infrared_write_is_refused,
)
from .model_profiles import is_sdt4e425

_LOGGER = logging.getLogger(__package__)


# One at a time, because selecting a preset moves the camera and these devices are measurably intolerant of
# concurrent requests: MAX_CONCURRENT_REQUESTS_PER_HOST is 2 for the same reason,
# and the login storms behind #577 and #603 are what happens without it. A
# coordinator does not help here, since it only centralises inbound reads and
# leaves outbound actions uncontrolled.
PARALLEL_UPDATES = 1


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup select platform."""

    async def build(coordinator):
        """Which selects this channel warrants, as the device answers today.

        Called again when a capability turns up on a later poll, so a camera
        whose infrared probe timed out at setup gets its Infrared Mode select
        when the device finally answers rather than at the next reload
        (#1057). That rebuild re-reads the preset list, which is why the
        signal names one channel rather than the whole entry.
        """
        devices = []

        if coordinator.is_amcrest_doorbell() and coordinator.supports_security_light():
            devices.append(DahuaDoorbellLightSelect(coordinator, entry))

        if is_sdt4e425(coordinator.get_model()):
            try:
                preset_ids = await coordinator.client.async_get_ptz_preset_ids(1)
            except Exception:
                _LOGGER.warning(
                    "Unable to enumerate SDT4E425 presets through RPC2", exc_info=True
                )
                preset_ids = []
            devices.append(
                DahuaCameraPresetPositionSelect(
                    coordinator, entry, preset_ids=preset_ids, rpc2_channel=1
                )
            )
        elif coordinator.is_indoor_monitor_without_video():
            # No camera, so no motor and no presets. Without this it reached
            # the branch below: a VTH answers 404 to ptz.cgi, which is None,
            # which keeps the ten-entry list for a device that has nothing to
            # move.
            _LOGGER.debug(
                "Indoor monitor without a camera, so no Preset Position control"
            )
        else:
            preset_ids = await _async_preset_ids(coordinator)
            if preset_ids == []:
                # The camera answered and holds no presets, so every option this
                # control could offer would be a GotoPreset the device refuses.
                # That is #525: four reporters, four cameras, one 400 from a
                # dropdown that was never going to work. Two of those cameras have
                # no PTZ motor at all.
                #
                # Not the same as None, which is the device declining to answer.
                # Saving a preset and reloading the entry brings the control back.
                _LOGGER.debug(
                    "Camera reports no presets, so no Preset Position control"
                )
            elif preset_ids is None and coordinator.reported_device_class() == "VTO":
                # A refusal keeps the ten-entry list, for cameras that refuse the
                # query but drive GotoPreset. A door station has no motor to
                # drive: a DHI-VTO2211G-WP-S2, which says class=VTO, refuses
                # both getPresets and the PTZ position probe (400) and was given
                # presets 1 to 10. Decided on its own answer, not on is_doorbell,
                # whose model-name list also matches devices that never said so.
                _LOGGER.debug(
                    "A VTO that will not list presets has none, so no Preset Position control"
                )
            else:
                devices.append(
                    DahuaCameraPresetPositionSelect(
                        coordinator, entry, preset_ids=preset_ids
                    )
                )

        if coordinator.supports_day_night_color():
            devices.append(DahuaDayNightModeSelect(coordinator, entry))

        if coordinator.supports_infrared_light():
            devices.append(DahuaInfraredModeSelect(coordinator, entry))

        # Only where the camera itself named its modes. Absent on most
        # channels of a recorder and present-but-false on some, so the probe
        # returns None rather than a list for both of those.
        if coordinator.supports_lighting_schemes():
            devices.append(DahuaLightingSchemeSelect(coordinator, entry))

        # The non-Amcrest SmartMotionDetect path only: it carries a Sensitivity
        # word, and supports_smart_motion_detection() is already the per-channel
        # "this channel has a row" signal the enable switch uses.
        if coordinator.supports_smart_motion_detection():
            devices.append(DahuaSmartMotionSensitivitySelect(coordinator, entry))

        # One per VTO the indoor monitor knows. Decided from the first poll's
        # read, so a monitor that would not answer it gets none until the entry
        # is reloaded, rather than a control with nothing to choose from.
        if coordinator.is_indoor_monitor():
            links = coordinator.get_vth_camera_links() or {}
            for vto in links.get("vtos") or {}:
                devices.append(DahuaVthCameraLinkSelect(coordinator, entry, vto))

        return devices

    await async_add_and_keep_watching(
        hass, entry, build, async_add_devices, entry_coordinators
    )


async def _async_preset_ids(coordinator):
    """Which presets this camera has: a list, or None when it would not say.

    Offering `1` to `10` to every camera means picking one the camera does not
    have, which it answers with a 400 that reads as the integration failing
    (#713). Asking costs one request at setup.

    **An empty list and None are different answers, and #762 conflated them.**
    That docstring claimed a camera with no presets and a camera that does not
    implement the query "cannot be told apart". Measurement says otherwise:

        no PTZ motor, HFW3449E-S-IL and HFW3449T-ZS-IL   200, zero bytes
        does not implement getPresets, Intelbras IM7      400, "Bad Request"

    Both measured by reporters on #525 and #713. So a device that answers at
    all has told us what it holds, and an empty answer means it holds nothing.
    Only an error is the device declining to say, and that stays unknown.

    Returns None on an error, deliberately, so the caller keeps the ten-entry
    list rather than taking a control away from somebody whose camera refuses
    the query but accepts GotoPreset. The SDT4E425 is exactly that shape: its
    CGI getStatus answers 400 while its PTZ works.
    """
    try:
        data = await coordinator.client.async_get_ptz_presets(
            coordinator.get_channel_number()
        )
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("Could not read the preset list", exc_info=True)
        return None
    presets = dahua_utils.parse_ptz_presets(data)
    _LOGGER.debug("Camera reports presets %s", presets)
    return presets


class DahuaDoorbellLightSelect(DahuaBaseEntity, SelectEntity):
    """Allow one to turn the doorbell light on/off/strobe."""

    _attr_translation_key = "security_light"

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        SelectEntity.__init__(self)
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.get_serial_number()}_security_light"
        self._attr_options = ["Off", "On", "Strobe"]

    @property
    def current_option(self) -> str:
        mode = self._coordinator.data.get("table.Lighting_V2[0][0][1].Mode", "")
        state = self._coordinator.data.get("table.Lighting_V2[0][0][1].State", "")
        if mode == "ForceOn" and state == "On":
            return "On"
        if mode == "ForceOn" and state == "Flicker":
            return "Strobe"
        return "Off"

    async def async_select_option(self, option: str) -> None:
        await self._coordinator.client.async_set_lighting_v2_for_amcrest_doorbells(
            option
        )
        await self._coordinator.async_refresh()

    @property
    def unique_id(self):
        return self._attr_unique_id


class DahuaInfraredModeSelect(DahuaBaseEntity, SelectEntity):
    """The infrared light's mode: automatic, manual, or off.

    The light entity can only say on or off, and what it writes is `Manual` or
    `Off`. `Auto` is what a camera ships on and what thirteen of fifteen channels
    on a DHI-NVR5464-16P-EI report, and from a light entity it is indistinguishable
    from `Off`: the emitter is illuminating every night and the entity reads off.
    Nothing in Home Assistant could see that, or hand a channel back to the
    camera's own judgement once something had written `Manual`.

    The options are slugs rather than the device's own capitalised words because
    Home Assistant looks a label up under
    `entity.select.infrared_mode.state.<option>`, and that lookup only works for a
    slug. The device's spelling is what goes on the wire.
    """

    _attr_translation_key = "infrared_mode"

    _attr_options = list(MODE_BY_OPTION)

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._coordinator = coordinator

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_infrared_mode"

    @property
    def available(self) -> bool:
        """Unavailable once the device has refused a write outright.

        This entity exists only to write. A recorder that answers
        `Authority:check failure` to every `Lighting` write -- measured on a
        DHI-NVR5464-16P-EI, on every channel and over both transports -- gives it
        nothing to do, and a dropdown that always throws is worse than no
        dropdown.

        The light entity stays available on purpose: its `mode` and
        `brightness_level` are read off the same table and are correct, so the
        reading half of this is still useful where the writing half is not.
        """
        return super().available and not infrared_write_is_refused(self._coordinator)

    @property
    def current_option(self):
        """The mode the device reports, or None when it is not one of the three.

        A device can report a mode of its own: this recorder answers `ZoomPrio`
        on two channels. Home Assistant rejects a `current_option` outside
        `options`, so that shows as unknown here rather than as one of the three
        the camera is not in. The light entity's `mode` attribute still names it.
        """
        return OPTION_BY_MODE.get(self._coordinator.get_infrared_mode())

    async def async_select_option(self, option: str) -> None:
        mode = MODE_BY_OPTION.get(option)
        if mode is None:
            return
        # The level the camera is already using, so selecting a mode does not
        # quietly change the brightness as well. Full only when it reports none.
        level = self._coordinator.get_infrared_level()
        await async_write_infrared_mode(
            self._coordinator, mode, 100 if level is None else level
        )


class DahuaSmartMotionSensitivitySelect(DahuaBaseEntity, SelectEntity):
    """How sensitive this channel's smart motion detection is.

    Reolink and Tapo expose this; the enable switch could turn smart motion on
    and off but nothing tuned it. The device stores a word (Low/Middle/High,
    measured Middle on a DHI-NVR5464), written to the same per-channel
    SmartMotionDetect row the switch writes, so it dodges the whole-table size
    ceiling that blocks the NVR IVS writes.
    """

    _attr_translation_key = "smart_motion_sensitivity"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["Low", "Middle", "High"]

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._coordinator = coordinator

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_smart_motion_sensitivity"

    @property
    def current_option(self):
        value = self._coordinator.get_smart_motion_sensitivity()
        return value if value in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            return
        await self._coordinator.client.async_set_smart_motion_sensitivity(
            self._coordinator.get_channel(), option
        )
        await self._coordinator.async_refresh()


class DahuaCameraPresetPositionSelect(DahuaBaseEntity, SelectEntity):
    """Select a camera preset position."""

    _attr_translation_key = "preset_position"

    def __init__(
        self,
        coordinator: DahuaDataUpdateCoordinator,
        config_entry,
        *,
        preset_ids: list[int] | None = None,
        rpc2_channel: int | None = None,
    ):
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        SelectEntity.__init__(self)
        self._coordinator = coordinator
        self._rpc2_channel = rpc2_channel
        suffix = "1_preset_position" if rpc2_channel == 1 else "preset_position"
        self._attr_unique_id = f"{coordinator.get_serial_number()}_{suffix}"
        if preset_ids is None:
            self._attr_options = [
                "Manual",
                "1",
                "2",
                "3",
                "4",
                "5",
                "6",
                "7",
                "8",
                "9",
                "10",
            ]
        else:
            self._attr_options = ["Manual", *[str(value) for value in preset_ids]]

    @property
    def current_option(self) -> str:
        if self._rpc2_channel is not None:
            # This firmware has no supported CGI position readback. Do not claim
            # a position we cannot verify.
            return "Manual"
        preset_id = self._coordinator.data.get("status.PresetID", "0")
        if preset_id == "0":
            return "Manual"
        return preset_id

    async def async_select_option(self, option: str) -> None:
        if option == "Manual":
            return
        if self._rpc2_channel is not None:
            await self._coordinator.client.async_goto_preset_rpc2(
                self._rpc2_channel, int(option)
            )
        else:
            channel = self._coordinator.get_channel_number()
            await self._coordinator.client.async_goto_preset_position(
                channel, int(option)
            )
        await self._coordinator.async_refresh()

    @property
    def unique_id(self):
        return self._attr_unique_id


class DahuaDayNightModeSelect(DahuaBaseEntity, SelectEntity):
    """The camera's Day/Night mode: colour, automatic, or black and white.

    #687 asked for this. The service to set it has existed for some time, but
    nothing read it back, so there was no way to notice a device that had
    changed mode by itself -- a VTO that reverts to Auto after a power cut, and
    then renders black and white at night, being the reported case.
    """

    _attr_translation_key = "day_night_mode"

    _attr_options = ["Color", "Auto", "BlackWhite"]

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._coordinator = coordinator

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_day_night_mode"

    @property
    def current_option(self):
        """The mode the device reports, or None when it has not reported one.

        None shows as unknown rather than as a mode the camera is not in, which
        is what a poll that has not landed yet, or a value outside the documented
        0/1/2, actually means.
        """
        return self._coordinator.get_day_night_color()

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            return
        await self._coordinator.client.async_set_video_in_day_night_mode(
            self._coordinator.get_channel(), "general", option
        )
        await self._coordinator.async_refresh()


# The option for "no camera": the VTH shows the VTO's own picture. A slug, so
# that Home Assistant can translate it; the cameras are named by the device and
# shown as they are.
NO_CAMERA = "none"


class DahuaVthCameraLinkSelect(DahuaBaseEntity, SelectEntity):
    """Which camera an indoor monitor (VTH) opens on when one VTO calls it.

    The VTH manual describes this per VTO ("select an IPC, and when this VTO
    calls, you will see the monitoring image from this IPC"), but on a
    VTH2421F-P on 4.800.0000000.1.R the screen does not offer it. The setting is
    in the VTH's VTOInfo table as LinkIPC, and setting it over RPC2 does what the
    manual says, on the main monitor and both extensions: see
    client.vth_camera_links.

    Configuration rather than state, so EntityCategory.CONFIG. It is written
    from automations as well as by hand: a second doorbell that is not wired to
    the VTO can point the monitors at its own camera, ring them with vto_call,
    and set this back.
    """

    _attr_translation_key = "vth_camera_link"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry, vto: str):
        super().__init__(coordinator, config_entry)
        self._coordinator = coordinator
        self._vto = vto
        self._attr_translation_placeholders = {
            "vto": self._vto_entry().get("name") or vto
        }

    @property
    def unique_id(self):
        return "{0}_camera_link_{1}".format(
            self._coordinator.get_serial_number(), self._vto.lower()
        )

    def _links(self) -> dict:
        return self._coordinator.get_vth_camera_links() or {}

    def _vto_entry(self) -> dict:
        return (self._links().get("vtos") or {}).get(self._vto) or {}

    def _choices(self) -> dict:
        """Option to camera slot key, with "none" for no camera.

        A camera named like another, or named "none", is told apart by its key,
        so every option maps to exactly one slot.
        """
        choices = {NO_CAMERA: ""}
        for key, name in (self._links().get("cameras") or {}).items():
            option = name
            if option in choices or option.lower() == NO_CAMERA:
                option = "{0} ({1})".format(name, key)
            choices[option] = key
        return choices

    @property
    def available(self) -> bool:
        return super().available and bool(self._vto_entry())

    @property
    def options(self) -> list:
        return list(self._choices())

    @property
    def current_option(self):
        """The camera this VTO's calls open on, or None for one not in the list.

        A LinkIPC naming a slot that is not a configured camera is not "none":
        the VTH has something set, and reporting it as nothing would hide that.
        """
        link = self._vto_entry().get("link", "")
        for option, key in self._choices().items():
            if key == link:
                return option
        return None

    async def async_select_option(self, option: str) -> None:
        camera = self._choices().get(option)
        if camera is None:
            return
        landed = await self._coordinator.client.async_set_vth_camera_link(
            self._vto, camera
        )
        await self._coordinator.async_refresh()
        if not landed:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="vth_camera_link_ignored",
                translation_placeholders={
                    "device": self._coordinator.get_device_name(),
                    "vto": self._vto_entry().get("name") or self._vto,
                    "option": option,
                },
            )


# The device's own spelling against the slug Home Assistant looks a label up
# by. Only these three have been seen; anything else the camera reports is
# offered by its own name rather than hidden, because a mode that exists and
# cannot be chosen is worse than an untranslated one.
_SCHEME_OPTIONS = {
    "InfraredMode": "infrared",
    "WhiteMode": "white",
    "AIMode": "smart_dual",
}
_SCHEME_BY_OPTION = {option: mode for mode, option in _SCHEME_OPTIONS.items()}


class DahuaLightingSchemeSelect(DahuaBaseEntity, SelectEntity):
    """Which emitter the camera is willing to use: infrared, white, or both.

    A dual-light camera decides its own illumination, and `LightingScheme` is
    the setting that says which half it may reach for. While it reads
    `AIMode` or `InfraredMode` the white light stays off whatever is written
    to `Lighting_V2` -- the value is stored, Home Assistant reports the light
    on, and nothing lights up (#647). That made the scheme invisible and
    unreachable from Home Assistant, and this is the control for it.

    `AIMode` is what the web interface calls Smart Dual Light: the camera runs
    infrared and switches to white when it recognises something. What it will
    switch for is its own setting, `AIMixLightObjFilterTypes`, and a camera
    filtering on `Human` alone never lights for a vehicle however this is set.

    Measured on a DHI-NVR5464-16P-EI driving a VSIPP-6DIRMD-I3 on channel 11:
    all three modes write to all nine scenes channel-scoped and read back, and
    the original restores cleanly.
    """

    _attr_translation_key = "lighting_scheme"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._coordinator = coordinator
        reported = coordinator.get_lighting_schemes() or []
        # The camera's list, in its order, mapped to slugs where one is known.
        self._modes = {_SCHEME_OPTIONS.get(mode, mode): mode for mode in reported}
        self._attr_options = list(self._modes)

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_lighting_scheme"

    @property
    def current_option(self):
        """The mode every scene agrees on, or None where they disagree."""
        mode = self._coordinator.get_lighting_scheme()
        if mode is None:
            return None
        return _SCHEME_OPTIONS.get(mode, mode)

    async def async_select_option(self, option: str) -> None:
        mode = self._modes.get(option)
        if mode is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="lighting_scheme_unknown",
                translation_placeholders={
                    "mode": option,
                    "known": ", ".join(self._modes),
                },
            )
        await self._coordinator.client.async_set_channel_scoped_lighting_scheme(
            self._coordinator.get_channel(), mode
        )
        await self._coordinator.async_request_refresh()
