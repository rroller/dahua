"""DahuaBaseEntity class"""

from custom_components.dahua import (
    DahuaDataUpdateCoordinator,
    async_host_is_unreachable,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

"""
For a list of entity types, see https://developers.home-assistant.io/docs/core/entity/
"""


class DahuaBaseEntity(CoordinatorEntity):
    """
    DahuaBaseEntity is the base entity for all Dahua entities
    """

    # `has-entity-name` is a Bronze rule on Home Assistant's integration quality
    # scale, and every entity here inherits this class, so it is set once.
    #
    # Each entity used to compose its own display name as
    # `get_device_name() + " Motion Detection"`. Home Assistant composes
    # DEVICE + ENTITY itself when this is True, so the names it produces are
    # unchanged -- what changes is that the entity now says only its own half.
    # That is what makes the name translatable, keeps it correct when somebody
    # renames the device, and stops the device name being baked into the
    # entity_id of everything created from now on, which config_flow.py:152
    # records the consequence of.
    _attr_has_entity_name = True

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        super().__init__(coordinator)
        self.config_entry = config_entry
        self._coordinator = coordinator

    # https://developers.home-assistant.io/docs/entity_registry_index
    @property
    def unique_id(self):
        """Return a unique ID to use for this entity."""
        return self._coordinator.get_serial_number()

    # https://developers.home-assistant.io/docs/device_registry_index
    @property
    def device_info(self):
        # A channel of a recorder is a camera, and until now its device page
        # described the recorder: the recorder's model, the recorder's firmware.
        # The recorder knows better and says so in RemoteDevice, which is the
        # only place that can answer -- a PoE camera is on the recorder's own
        # network and nothing else on the LAN can reach it to ask.
        #
        # Each one falls back to what the device itself reports, which is right
        # for a standalone camera, for a slot the recorder said nothing about,
        # and for a channel whose DeviceType is a class of thing rather than a
        # model ("IP Camera").
        coordinator = self._coordinator

        # One channel of a recorder has no device of its own. `get_serial_number`
        # appends the channel for every channel but the lowest, so that one's
        # identifier is the bare serial, which is the recorder's -- and the
        # recorder's own entities are filed against it: four disk sensors, the
        # storage warnings, the processor and the memory. Describing that page
        # as the camera in the slot would leave the recorder's disks under a
        # camera's model, serial, firmware and MAC, with the firmware and serial
        # sensors on the same page still reporting the recorder's.
        #
        # Measured on a DHI-NVR5464-16P-EI: slot 0 reports serial
        # 4L07AD6PAG5F0F2 and firmware 2.622.0000000.31.R while the recorder is
        # BC0A198PAJ779DF on 4.005.0000000.3.R.
        #
        # So that page stays the recorder's -- unless the recorder now has a
        # device of its own, which is what `uses_separate_host_device` answers.
        # Once the disks and the load sensors have somewhere else to live, this
        # page is only the camera in the lowest slot, and it says so again.
        shares_the_host_device = coordinator.stands_in_for_the_host()

        def channel_or_host(channel_value, host_value):
            if shares_the_host_device:
                return host_value()
            return channel_value() or host_value()

        info = {
            "identifiers": {(DOMAIN, coordinator.get_serial_number())},
            "name": coordinator.get_device_name(),
            "model": channel_or_host(
                coordinator.get_channel_model, coordinator.get_model
            ),
            "manufacturer": "Dahua",
            "configuration_url": coordinator.get_configuration_url(),
            "sw_version": channel_or_host(
                coordinator.get_channel_firmware_version,
                coordinator.get_firmware_version,
            ),
            "serial_number": channel_or_host(
                coordinator.get_channel_serial_number,
                coordinator.get_device_serial_number,
            ),
        }

        # The MAC goes in `connections` because that is the only field Home
        # Assistant renders it in. It also lets Home Assistant recognise the
        # same hardware reached another way, which is usually what someone
        # wants and is worth knowing about rather than discovering: a device
        # another integration has already claimed with this MAC would be the
        # same device, not a second one.
        #
        # Not on the page the recorder shares: that MAC belongs to the camera in
        # the slot, and `connections` is how Home Assistant decides two devices
        # are the same hardware, so it would invite a merge with whatever else
        # has claimed that camera.
        mac = None if shares_the_host_device else coordinator.get_channel_mac()
        if mac:
            info["connections"] = {(dr.CONNECTION_NETWORK_MAC, mac)}
        # Where the area chosen while adding this device is applied. Home
        # Assistant honours suggested_area only when it *creates* the device, so
        # this files a new one and never argues with a device the user has since
        # moved. Changing the area of an existing device is the options flow's
        # job, which moves it through the device registry instead.
        #
        # Omitted rather than passed as None: every key here is handed to
        # async_get_or_create as given.
        area = self._coordinator.configured_area_name()
        if area:
            info["suggested_area"] = area
        return info

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "id": str(self.coordinator.data.get("id")),
            "integration": DOMAIN,
        }


class DahuaHostEntity(DahuaBaseEntity):
    """An entity that describes the box rather than one of its channels.

    The disks are the recorder's disks and there is one processor in the
    chassis, so these are created once per entry rather than once per channel.
    Until now they were filed against the lowest channel, because that channel
    holds the bare serial and so is the only thing resembling a device for the
    box -- which left a recorder's disks on a page named after a camera.

    The fallback matters as much as the main path: a camera with an SD card
    gets a disk sensor too, and a camera is already its own device. There is
    nothing to separate, so it keeps the page it has.
    """

    @property
    def device_info(self):
        coordinator = self._coordinator
        if not coordinator.uses_separate_host_device():
            return super().device_info

        # No `connections`: the only MAC read here is the camera's, out of the
        # recorder's slot table, and claiming it for the recorder would tell
        # Home Assistant the two are the same hardware.
        #
        # No `suggested_area` either. The area is a per channel option and
        # means "where this camera points"; the recorder is somewhere else.
        return {
            "identifiers": {(DOMAIN, coordinator.host_device_identifier())},
            "name": self.config_entry.title or coordinator.get_model(),
            "model": coordinator.get_model(),
            "manufacturer": "Dahua",
            "configuration_url": coordinator.get_configuration_url(),
            "sw_version": coordinator.get_firmware_version(),
            "serial_number": coordinator.get_device_serial_number(),
        }


class DahuaEventDrivenEntity(DahuaBaseEntity):
    """An entity whose state is written when the device sends an event.

    These do not read the poll at all. They are pushed to, from the event
    stream, and they never subscribe to the coordinator -- their
    `async_added_to_hass` registers an event listener instead of calling
    `CoordinatorEntity`'s.

    They did still inherit `available` from it, which reports the result of the
    last poll. Home Assistant writes `unavailable` in place of whatever state an
    entity reports whenever that property is False, so a config read that timed
    out could turn the doorbell press that arrived a moment later into
    `unavailable` rather than `on`. The press was not missed -- it reached the
    bus and the listener ran -- but the state that automations trigger on never
    said so.

    The two transports are independent: the event stream can be connected and
    delivering while a `configManager.cgi` read is slow enough to time out. So
    judge these on whether the device has stopped answering altogether.
    """

    @property
    def available(self) -> bool:
        return not async_host_is_unreachable(self._coordinator.get_address())
