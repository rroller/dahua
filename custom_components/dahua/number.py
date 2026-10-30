"""Number entity platform for Dahua: picture adjustments, and a monitor's volumes.

Brightness, contrast, saturation, hue and gamma, the controls Reolink and Tapo
expose as sliders and this integration did not. Backed by the VideoColor config
table (measured 0-100 on a DHI-NVR5464 and a VTO), read on the poll and written
per change to the channel's general profile (index 0).
"""

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import entry_coordinators

from .const import DOMAIN
from .entity import DahuaBaseEntity
from .late import async_add_and_keep_watching

_LOGGER = logging.getLogger(__package__)

# Writes move the device, so one at a time, like the other acting platforms.
PARALLEL_UPDATES = 1

# (translation key, VideoColor field). The key is also the unique-id suffix;
# the field is what the device stores. The display name used to be a third
# element, set as _attr_name, which no language file can reach -- the thing
# CONTRIBUTING forbids and two tests check for, neither of which scanned this
# platform because it was missing from both their platform lists.
IMAGE_ADJUSTMENTS = (
    ("image_brightness", "Brightness"),
    ("image_contrast", "Contrast"),
    ("image_saturation", "Saturation"),
    ("image_hue", "Hue"),
    ("image_gamma", "Gamma"),
)


# (translation key, VideoTalkPhoneGeneral field) for an indoor monitor's
# volumes (#949). Same shape and the same reason as IMAGE_ADJUSTMENTS above:
# one class with four instances cannot declare a class attribute per name, and
# the two name scanners read the keys out of this literal. The device-side
# names are also in const.VTH_VOLUME_FIELD_NAMES, which the coordinator's probe
# reads; a test derives one from the other rather than trusting them to match.
MONITOR_VOLUMES = (
    ("vth_ring_volume", "RingVolume"),
    ("vth_indoor_ring_volume", "VthRingVolume"),
    ("vth_talk_volume", "TalkVolume"),
    ("vth_alarm_volume", "AlarmVolume"),
)


# (translation key, MotionDetect region field) for the motion detection region
# the device's own UI calls Region 1 (#1115). Same shape and the same reason as
# the two tables above.
#
# Both are 0 to 100, which is measured rather than assumed: the recorder's own
# web UI offers that range for each, and setting Channel 1's sensitivity to 100
# there put 100 into `MotionDetect[0].MotionDetectWindow[0].Sensitive`. The
# displayed scale and the stored field are the same number.
MOTION_REGION_FIELDS = (
    ("motion_region1_sensitivity", "Sensitive"),
    ("motion_region1_threshold", "Threshold"),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Set up the number platform."""

    async def build(coordinator):
        """Which sliders this channel warrants, as the device answers today.

        Called again when the VideoColor read finally answers on a later poll,
        so a channel whose probe timed out at setup gets its sliders then
        rather than at the next reload (#1057). Nothing in here talks to the
        device, so a rebuild costs no request.
        """
        # An indoor monitor without a camera has no picture to adjust.
        if coordinator.is_indoor_monitor_without_video():
            return []
        # One entity per adjustment the device actually reported for this
        # channel. These were created unconditionally, which gave sliders
        # to every device including the ones whose VideoColor table is refused
        # outright -- a camera account in the `user` group answers 403 to it
        # (#1006) -- and a slider that can only read unknown is worse than no
        # slider (the profile sensor's rule, #641). Per field, because a device
        # reporting only some adjustments should get only those entities.
        return [
            DahuaImageAdjustmentNumber(coordinator, entry, key, field)
            for key, field in IMAGE_ADJUSTMENTS
            if coordinator.supports_video_color(field)
        ]

    async def build_monitor_volumes(coordinator):
        """The volumes an indoor monitor actually reported (#949).

        Separate from the picture sliders because the gate above returns early
        for a monitor with no camera, which is exactly the device these belong
        to. One entity per field the device answered, so a monitor reporting
        four of the five call settings gets four rather than a fifth stuck on
        unknown.
        """
        return [
            DahuaMonitorVolumeNumber(coordinator, entry, key, field)
            for key, field in MONITOR_VOLUMES
            if coordinator.supports_vth_phone_setting(field)
        ]

    async def build_vto_ring_volumes(coordinator):
        """One ring volume per door station the monitor knows (#949).

        Separate from the monitor-wide volumes above because it IS separate on
        the device: measured on two VTH5221D with the owner listening, turning
        `VTOInfo.Vto00.RingVolume` down to 10 made the doorbell ring quieter
        while `VideoTalkPhoneGeneral.RingVolume` stayed at 70. Two settings,
        two sliders.

        ⚠ Built from the same read the camera-link select uses, so a monitor
        that would not answer `VTOInfo` gets none of these rather than a slider
        with nothing behind it, and a slot whose row carries no `RingVolume`
        is skipped rather than given one reading zero.
        """
        if not coordinator.is_indoor_monitor():
            return []
        vtos = (coordinator.get_vth_camera_links() or {}).get("vtos") or {}
        return [
            DahuaVtoRingVolumeNumber(coordinator, entry, vto)
            for vto, slot in vtos.items()
            if slot.get("ring_volume") is not None
        ]

    async def build_motion_region(coordinator):
        """The motion region sliders, where the device carries a region at all.

        Not every device does. A recorder channel has
        `MotionDetectWindow[0..3]`; a VTO2000A has none and only the
        whole-channel `Level` (#1115). Per field, like the two above, so a
        device reporting one of the two gets one.
        """
        return [
            DahuaMotionRegionNumber(coordinator, entry, key, field)
            for key, field in MOTION_REGION_FIELDS
            if coordinator.supports_motion_region(field)
        ]

    await async_add_and_keep_watching(
        hass, entry, build, async_add_devices, entry_coordinators
    )
    await async_add_and_keep_watching(
        hass, entry, build_monitor_volumes, async_add_devices, entry_coordinators
    )
    await async_add_and_keep_watching(
        hass, entry, build_vto_ring_volumes, async_add_devices, entry_coordinators
    )
    await async_add_and_keep_watching(
        hass, entry, build_motion_region, async_add_devices, entry_coordinators
    )


class DahuaImageAdjustmentNumber(DahuaBaseEntity, NumberEntity):
    """One picture adjustment (0-100) on a channel's general VideoColor profile."""

    # has_entity_name comes from DahuaBaseEntity, which sets it for every
    # entity here; repeating it invites the two from drifting apart.
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry, key, field):
        DahuaBaseEntity.__init__(self, coordinator, entry)
        NumberEntity.__init__(self)
        self._coordinator = coordinator
        self._key = key
        self._field = field
        # The key is the translation key and the unique-id suffix both, so the
        # strings are looked up under the ids people already have and nothing
        # moves.
        self._attr_translation_key = key
        self._attr_unique_id = "%s_%s" % (coordinator.get_serial_number(), key)

    @property
    def unique_id(self):
        # DahuaBaseEntity.unique_id returns the bare serial; each entity that is
        # not the one legacy VideoMotion sensor overrides it, as select/switch do.
        return self._attr_unique_id

    @property
    def available(self) -> bool:
        """Off until the poll has a value, so a device without VideoColor does not
        show sliders stuck at an invented default."""
        return (
            super().available
            and self._coordinator.get_video_color(self._field) is not None
        )

    @property
    def native_value(self):
        return self._coordinator.get_video_color(self._field)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.client.async_set_video_color(
            self._coordinator.get_channel(), self._field, int(value)
        )
        await self._coordinator.async_refresh()


class DahuaMonitorVolumeNumber(DahuaBaseEntity, NumberEntity):
    """One of an indoor monitor's volumes, 0-100 (#949).

    Backed by `VideoTalkPhoneGeneral`, measured on a VTH5221D
    (3.000.0012000.0.R) where RingVolume, VthRingVolume, TalkVolume and
    AlarmVolume all read 70.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry, key, field):
        DahuaBaseEntity.__init__(self, coordinator, entry)
        NumberEntity.__init__(self)
        self._coordinator = coordinator
        self._key = key
        self._field = field
        self._attr_translation_key = key
        self._attr_unique_id = "%s_%s" % (coordinator.get_serial_number(), key)

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def available(self) -> bool:
        """Off until the poll has a value, so a slider never shows an invented
        default for a monitor that stopped answering."""
        return (
            super().available
            and self._coordinator.get_vth_phone_number(self._field) is not None
        )

    @property
    def native_value(self):
        return self._coordinator.get_vth_phone_number(self._field)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.client.async_set_video_talk_phone_field(
            self._field, int(value)
        )
        await self._coordinator.async_refresh()


class DahuaVtoRingVolumeNumber(DahuaBaseEntity, NumberEntity):
    """How loudly a monitor rings for calls from ONE door station, 0-100 (#949).

    Asked for by @kmich as a Number backed by `VTOInfo.VtoNN.RingVolume`, and
    held back until it was clear it is not the monitor-wide
    `VideoTalkPhoneGeneral.RingVolume` under a second name. It is not.
    Measured on two VTH5221D (3.000.0012000.0.R) with the owner at the door:
    both slots turned down to 10 and the bell rang audibly quieter on both
    monitors, while the monitor-wide field still read 70 throughout. Writing
    the captured tables back restored each byte for byte.

    ⚠ Whether the monitor-wide one drives anything on this firmware is NOT
    settled -- the converse test was not run. So this adds a control rather
    than replacing one, and nothing here says the other slider is wrong.

    One per populated slot, named by the door station, because a villa with a
    gate and a front door wants them at different volumes and that is the whole
    point of the field being per slot.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_translation_key = "vth_vto_ring_volume"

    def __init__(self, coordinator, entry, vto: str):
        DahuaBaseEntity.__init__(self, coordinator, entry)
        NumberEntity.__init__(self)
        self._coordinator = coordinator
        self._vto = vto
        self._attr_translation_placeholders = {
            "vto": self._slot().get("name") or vto,
        }

    @property
    def unique_id(self):
        return "{0}_vto_ring_volume_{1}".format(
            self._coordinator.get_serial_number(), self._vto.lower()
        )

    def _slot(self) -> dict:
        links = self._coordinator.get_vth_camera_links() or {}
        return (links.get("vtos") or {}).get(self._vto) or {}

    @property
    def available(self) -> bool:
        """Off until the poll has a value, so the slider never shows an invented
        zero for a door station the monitor has stopped reporting."""
        return super().available and self._slot().get("ring_volume") is not None

    @property
    def native_value(self):
        return self._slot().get("ring_volume")

    async def async_set_native_value(self, value: float) -> None:
        landed = await self._coordinator.client.async_set_vth_vto_ring_volume(
            self._vto, int(value)
        )
        await self._coordinator.async_refresh()
        if not landed:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="vth_vto_ring_volume_ignored",
                translation_placeholders={
                    "device": self._coordinator.get_device_name(),
                    "vto": self._slot().get("name") or self._vto,
                },
            )


class DahuaMotionRegionNumber(DahuaBaseEntity, NumberEntity):
    """Motion region 1's sensitivity or threshold, 0-100 (#1115).

    Asked for by @clore-hub, whose doorbell's web UI offers no motion settings
    at all, so Home Assistant would be the only interface it has. His device
    turns out to need a different field from this one -- it has no regions and
    only the whole-channel `Level`, whose range nothing will state -- but the
    measurement that came out of chasing it is this one: a recorder's own UI
    offers both of these as 0 to 100 and writes the displayed number straight
    into the field.

    Region 1 of four, for the reason on `DahuaDataUpdateCoordinator.MOTION_REGION`.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry, key, field):
        DahuaBaseEntity.__init__(self, coordinator, entry)
        NumberEntity.__init__(self)
        self._coordinator = coordinator
        self._key = key
        self._field = field
        self._attr_translation_key = key
        self._attr_unique_id = "%s_%s" % (coordinator.get_serial_number(), key)

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def available(self) -> bool:
        """Off until the poll has a value, so a slider never shows an invented
        default for a channel whose MotionDetect read failed."""
        return (
            super().available
            and self._coordinator.get_motion_region_value(self._field) is not None
        )

    @property
    def native_value(self):
        return self._coordinator.get_motion_region_value(self._field)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.client.async_set_motion_region(
            self._coordinator.get_channel(),
            self._coordinator.MOTION_REGION,
            self._field,
            int(value),
        )
        await self._coordinator.async_refresh()
