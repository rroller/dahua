"""Diagnostic sensors for Dahua devices.

Both of these report values the coordinator already holds from setup, so the
platform adds no requests at all. Anything that would need its own API call --
storage use is the obvious one -- deliberately does not live here, because a
diagnostic is not worth another login on a device that logs every one.
"""

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.dahua import DahuaDataUpdateCoordinator, entry_coordinators

from .entity import DahuaBaseEntity, DahuaEventDrivenEntity, DahuaHostEntity
from .late import async_add_and_keep_watching

# Maps the camera's lighting profile id to a readable label.
PROFILE_NAMES = {
    "0": "Day",
    "1": "Night",
    "2": "Scene",
}


# A coordinator centralises the inbound reads, and nothing here sends a command,
# so there is nothing to serialise: read only: every state comes from the coordinator.
PARALLEL_UPDATES = 0


def _load_reporter(coordinators: list):
    """The one coordinator that should carry the device's load sensors.

    The processor and memory describe the box, not a channel, and a recorder is
    one entry with a coordinator per channel (#827). So the lowest channel
    carries them and the rest carry none, which is what the disk sensors do too
    since #745's per-channel duplication was fixed.

    Returns None when nothing answered the probe.
    """
    answering = [c for c in coordinators if c.supports_system_load()]
    if not answering:
        return None
    return min(answering, key=lambda c: c.get_channel())


def _filed_under(entity, coordinator):
    """Which subentry this entity belongs to, as `late.py` asks.

    The box's own device belongs to the entry, not to one channel's subentry:
    the processor is not channel 0's processor. A camera reporting its SD card
    has no separate host device, so its sensors stay where they are, on its
    own channel.
    """
    if isinstance(entity, DahuaHostEntity) and coordinator.uses_separate_host_device():
        return None
    return coordinator.subentry_id


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup the sensor platform."""
    # Keep the selected reporter for this setup's lifetime: its serial is
    # part of the sensors' unique IDs. A lower channel answering later must
    # not create another pair. Reloading the entry makes a fresh selection.
    load_reporter = _load_reporter(list(entry_coordinators(entry).values()))

    async def build(coordinator):
        """Which sensors this channel warrants, as the device answers today.

        Called again when a capability turns up on a later poll, so a channel
        whose Lighting profile read timed out at setup gets its profile sensor
        then rather than at the next reload (#1057). Nothing in here talks to
        the device, so a rebuild costs no request.

        The processor and memory describe the box rather than a channel, so
        they are not created for whichever coordinator asks: `_load_reporter`
        picks one channel at setup and only that one carries them. If none
        answered then, the first channel rebuilt with an answer carries them.
        They are built here so a device whose processor read timed out at setup
        gets them when it finally answers (#1057), and `_filed_under` keeps
        them on the box's own device page rather than that channel's.
        """
        nonlocal load_reporter

        sensors = [
            DahuaFirmwareVersionSensor(coordinator, entry),
            DahuaSerialNumberSensor(coordinator, entry),
        ]

        if load_reporter is None and coordinator.supports_system_load():
            load_reporter = coordinator
        if load_reporter is coordinator:
            sensors.append(DahuaProcessorUsageSensor(coordinator, entry))
            sensors.append(DahuaMemoryUsedSensor(coordinator, entry))
        # A plate is read from a picture, which an indoor monitor without a
        # camera does not have.
        if not coordinator.is_indoor_monitor_without_video():
            sensors.append(DahuaLicensePlateSensor(coordinator, entry))

        # The profile is only ever read for devices that answered the Lighting
        # probe. Adding the sensor unconditionally would show "Day" forever on a
        # doorbell or a camera without selectable profiles, which is worse than
        # no sensor at all (see #641 review).
        if coordinator.supports_profile_mode():
            sensors.append(DahuaProfileSensor(coordinator, entry))

        # Only on a recorder that answered the RemoteDevice read at setup; None
        # everywhere else, so a camera does not get a meaningless channel count.
        if coordinator.get_configured_channel_count() is not None:
            sensors.append(DahuaConfiguredChannelsSensor(coordinator, entry))

        return sensors

    await async_add_and_keep_watching(
        hass,
        entry,
        build,
        async_add_devices,
        entry_coordinators,
        subentry_for=_filed_under,
    )


class DahuaFirmwareVersionSensor(DahuaBaseEntity, SensorEntity):
    """The firmware the device reports.

    It is already in the device registry, but only as a label. As a sensor it
    can be templated and compared, which is what makes "tell me when a camera
    is behind" possible.
    """

    _attr_translation_key = "firmware_version"

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_firmware_version"

    @property
    def native_value(self):
        """The firmware of whatever this page describes.

        A recorder channel is a camera, and since #1044 its device page says so.
        This sensor went on reporting the recorder's firmware, so one page
        showed 2.460.AC01.0.R at the top and 4.000.00AC000.0.R underneath it
        (#1043). The page was right.
        """
        coordinator = self._coordinator
        if coordinator.stands_in_for_the_host():
            return coordinator.get_firmware_version()
        return (
            coordinator.get_channel_firmware_version()
            or coordinator.get_firmware_version()
        )

    @property
    def extra_state_attributes(self):
        """Expose the build date when the camera reports one.

        Only alongside the host's own firmware. `get_build_date` is the box's,
        and the camera's version from the recorder's slot table has its date
        stripped (#1044), so pairing them would put the recorder's build date
        next to a camera's version. That is the same disagreement this sensor
        was just fixed for, one level quieter.
        """
        attrs = super().extra_state_attributes
        if self.native_value != self._coordinator.get_firmware_version():
            return attrs
        build_date = self._coordinator.get_build_date()
        if build_date:
            attrs["build_date"] = build_date
        return attrs


class DahuaSerialNumberSensor(DahuaBaseEntity, SensorEntity):
    """The serial the device reports."""

    _attr_translation_key = "serial_number"

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_serial_number"

    @property
    def native_value(self):
        """The serial of whatever this page describes.

        Never the channel-suffixed entity key, which is this integration's
        bookkeeping and not a serial number.

        It used to be the box's serial on every channel, on the reasoning that
        every channel of one NVR is the same physical box. The page disagrees
        with that now: since #1044 a channel's device page describes the camera
        in that slot, and since #1054 the box has a page of its own to be
        described on. A sensor saying the recorder's serial underneath a page
        saying the camera's is the inconsistency reported in #1043.
        """
        coordinator = self._coordinator
        if coordinator.stands_in_for_the_host():
            return coordinator.get_device_serial_number()
        return (
            coordinator.get_channel_serial_number()
            or coordinator.get_device_serial_number()
        )


class DahuaProfileSensor(DahuaBaseEntity, SensorEntity):
    """Sensor for the day/night lighting profile the camera is using right now."""

    _attr_translation_key = "profile"

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_profile"

    @property
    def native_value(self) -> str:
        mode = str(self._coordinator.get_profile_mode())
        return PROFILE_NAMES.get(mode, str(mode))

    @property
    def extra_state_attributes(self):
        """Expose the raw profile number (0=day, 1=night, 2=scene)."""
        attrs = super().extra_state_attributes
        attrs["profile_number"] = self._coordinator.get_profile_mode()
        return attrs


class DahuaConfiguredChannelsSensor(DahuaBaseEntity, SensorEntity):
    """How many channels a recorder has a camera configured on.

    Read once from the recorder's RemoteDevice table at setup, like the disk
    sensors, so it adds nothing to the poll. It counts configured (enabled)
    slots, not live connectivity: the table says a camera is set up on a
    channel, not that it is reachable right now.
    """

    _attr_translation_key = "configured_channels"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Off by default, like the disk sensors (#745). A recorder is represented by
    # one entry per channel and every one of them answers is_recorder_host, so
    # without this the same host-wide count appears once per channel. Enabling a
    # single one is enough.
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_configured_channels"

    @property
    def native_value(self):
        return self._coordinator.get_configured_channel_count()


class DahuaLicensePlateSensor(DahuaEventDrivenEntity, SensorEntity):
    """The last recognized license plate reported by the camera."""

    _attr_translation_key = "license_plate"

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry):
        super().__init__(coordinator, entry)
        self._unique_id = f"{coordinator.get_serial_number()}_license_plate"

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def native_value(self):
        val = self._coordinator.get_last_plate()
        return val if val != "unknown" else None

    @property
    def extra_state_attributes(self):
        """The plate's own fields, on top of what every entity here reports.

        Returned alone until now, so `id` and `integration` were missing from this
        sensor and only this one. Merged rather than replaced, and `or {}` because
        get_last_plate_data returns None before the first plate.
        """
        return {
            **(super().extra_state_attributes or {}),
            **(self._coordinator.get_last_plate_data() or {}),
        }

    async def async_added_to_hass(self):
        """Listen for a plate, and stop listening when removed.

        The remover has to be kept. add_plate_listener returns it for exactly this
        reason, and the authorized vehicle sensor next door uses it; this caller was
        missed. Without it the callback outlives the entity, so every reload leaves
        another dead one in the list and each ANPR plate then logs "Error calling plate
        listener" once per reload the entry has ever had.
        """
        self.async_on_remove(
            self._coordinator.add_plate_listener(self.schedule_update_ha_state)
        )

    @property
    def should_poll(self) -> bool:
        return False


class DahuaProcessorUsageSensor(DahuaHostEntity, SensorEntity):
    """How busy the device's processor is, as a percentage.

    `magicBox.getCPUUsage`, which is RPC2 only -- there is no CGI form -- so
    this exists only where RPC2 is enabled and the device answered the probe.
    Measured on a DHI-NVR5464-16P-EI, where it agreed with the same recorder's
    ONVIF reading to within a point.
    """

    _attr_translation_key = "processor_usage"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_processor_usage"

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("system.cpu_usage")


class DahuaMemoryUsedSensor(DahuaHostEntity, SensorEntity):
    """How much of the device's memory is in use, as a percentage.

    `magicBox.getMemoryInfo` reports free and total in bytes and no percentage,
    so the percentage is worked out from them. Both byte figures are attributes
    rather than their own sensors: three entities to say one thing is how an
    entity list becomes unreadable.
    """

    _attr_translation_key = "memory_used"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_memory_used"

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("system.memory_used_percent")

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {
            **(super().extra_state_attributes or {}),
            "total_bytes": data.get("system.memory_total"),
            "free_bytes": data.get("system.memory_free"),
        }
