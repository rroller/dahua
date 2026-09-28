"""Switch platform for dahua."""
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from custom_components.dahua import DahuaDataUpdateCoordinator, entry_coordinators

from .const import DOMAIN, DISARMING_ICON, MOTION_DETECTION_ICON, SIREN_ICON, BELL_ICON, PRIVACY_MODE_ICON
from .entity import DahuaBaseEntity
from .client import SIREN_TYPE


# One at a time, because every toggle is a write to the device and these devices are measurably intolerant of
# concurrent requests: MAX_CONCURRENT_REQUESTS_PER_HOST is 2 for the same reason,
# and the login storms behind #577 and #603 are what happens without it. A
# coordinator does not help here, since it only centralises inbound reads and
# leaves outbound actions uncontrolled.
PARALLEL_UPDATES = 1

async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup sensor platform."""
    for coordinator in entry_coordinators(entry).values():
        # I think most cameras have a motion sensor so we'll blindly add a switch for it
        devices = [
            DahuaMotionDetectionBinarySwitch(coordinator, entry),
        ]

        # But only some cams have a siren, very few do actually. The rule lives on the
        # coordinator because the poll needs the same answer to decide whether to fetch the
        # status this entity reads.
        if coordinator.creates_siren_entity():
            devices.append(
                DahuaSirenBinarySwitch(
                    coordinator,
                    entry,
                    name="Alarm" if coordinator.uses_recorder_deterrence() else "Siren",
                )
            )
        if coordinator.supports_smart_motion_detection() or coordinator.supports_smart_motion_detection_amcrest():
            devices.append(DahuaSmartMotionDetectionBinarySwitch(coordinator, entry))
        if coordinator.supports_privacy_mode():
            devices.append(DahuaPrivacyModeBinarySwitch(coordinator, entry))
        if coordinator.supports_alarm_output():
            devices.append(DahuaAlarmOutputSwitch(coordinator, entry, output=0))

        # The coordinator already asked the device this during setup and kept the
        # answer. Asking again here put a network round trip inside platform setup,
        # where a device that is slow to answer eats the entry's setup budget.
        if coordinator.supports_disarming_linkage():
            devices.append(DahuaDisarmingLinkageBinarySwitch(coordinator, entry))
            devices.append(DahuaDisarmingEventNotificationsLinkageBinarySwitch(coordinator, entry))

        devices.extend(
            DahuaIVSRuleSwitch(coordinator, entry, rule)
            for rule in coordinator.get_ivs_rules()
        )
        async_add_devices(devices)


class DahuaMotionDetectionBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """dahua motion detection switch class. Used to enable or disable motion detection"""

    # Configuration, not a control: this changes how the camera behaves rather
    # than doing something now, so it belongs in the device page's configuration
    # section and out of auto-generated dashboards. The siren is deliberately
    # left alone -- that one is an action someone wants on a dashboard.
    _attr_entity_category = EntityCategory.CONFIG


    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on/enable motion detection."""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.enable_motion_detection(channel, True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off/disable motion detection."""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.enable_motion_detection(channel, False)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self._coordinator.get_device_name() + " " + "Motion Detection"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_motion_detection"

    @property
    def icon(self):
        """Return the icon of this switch."""
        return MOTION_DETECTION_ICON

    @property
    def is_on(self):
        """
        Return true if the switch is on.
        Value is fetched from api.get_motion_detection_config
        """
        return self._coordinator.is_motion_detection_enabled()


class DahuaDisarmingLinkageBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """will set the camera's disarming linkage (Event -> Disarming in the UI)"""

    _attr_entity_category = EntityCategory.CONFIG


    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on/enable linkage"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_disarming_linkage(channel, True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off/disable linkage"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_disarming_linkage(channel, False)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self._coordinator.get_device_name() + " " + "Disarming"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be
        configurable by the user or be changeable see
        https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_disarming"

    @property
    def icon(self):
        """Return the icon of this switch."""
        return DISARMING_ICON

    @property
    def is_on(self):
        """
        Return true if the switch is on.
        Value is fetched from client.async_get_linkage
        """
        return self._coordinator.is_disarming_linkage_enabled()

class DahuaDisarmingEventNotificationsLinkageBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """will set the camera's event notifications when device is disarmed (Event -> Disarming -> Event Notifications in the UI)"""

    _attr_entity_category = EntityCategory.CONFIG


    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on/enable event notifications"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_event_notifications(channel, True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off/disable event notifications"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_event_notifications(channel, False)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self._coordinator.get_device_name() + " " + "Event Notifications"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be
        configurable by the user or be changeable see
        https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_event_notifications"

    @property
    def icon(self):
        """Return the icon of this switch."""
        return BELL_ICON

    @property
    def is_on(self):
        """
        Return true if the switch is on.
        """
        return self._coordinator.is_event_notifications_enabled()

class DahuaSmartMotionDetectionBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """Enables or disables the Smart Motion Detection option in the camera"""

    _attr_entity_category = EntityCategory.CONFIG


    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on SmartMotionDetect"""
        if self._coordinator.supports_smart_motion_detection_amcrest():
            await self._coordinator.client.async_set_ivs_rule(0, 0, True)
        else:
            await self._coordinator.client.async_enabled_smart_motion_detection(
                self._coordinator.get_channel(), True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off SmartMotionDetect"""
        if self._coordinator.supports_smart_motion_detection_amcrest():
            await self._coordinator.client.async_set_ivs_rule(0, 0, False)
        else:
            await self._coordinator.client.async_enabled_smart_motion_detection(
                self._coordinator.get_channel(), False)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self._coordinator.get_device_name() + " " + "Smart Motion Detection"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be
        configurable by the user or be changeable see
        https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_smart_motion_detection"

    @property
    def icon(self):
        """Return the icon of this switch."""
        return MOTION_DETECTION_ICON

    @property
    def is_on(self):
        """ Return true if the switch is on. """
        return self._coordinator.is_smart_motion_detection_enabled()


class DahuaIVSRuleSwitch(DahuaBaseEntity, SwitchEntity):
    """Enable or disable one normal IVS rule by its stable Dahua ID."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, rule: dict):
        super().__init__(coordinator, entry)
        self._channel = rule["channel"]
        self._rule_id = rule["id"]
        self._rule_name = rule["name"]
        self._remote = rule.get("remote", False)

    async def _async_set_enabled(self, enabled):
        try:
            setter = (
                self._coordinator.client.async_set_remote_ivs_rule_by_id
                if self._remote else self._coordinator.client.async_set_ivs_rule_by_id
            )
            await setter(
                self._channel, self._rule_id, enabled
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self._coordinator.async_refresh()

    async def async_turn_on(self, **kwargs):
        """Enable this rule at its current position."""
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs):
        """Disable this rule at its current position."""
        await self._async_set_enabled(False)

    @property
    def name(self):
        """Return the camera and rule names."""
        return self._coordinator.get_device_name() + " " + self._rule_name

    @property
    def unique_id(self):
        """Keep identity when a rule is renamed or its array position changes."""
        return f"{self._coordinator.get_serial_number()}_ivs_rule_{self._channel}_{self._rule_id}"

    @property
    def is_on(self):
        """Resolve state from the current table."""
        return self._coordinator.is_ivs_rule_enabled(self._channel, self._rule_id)

    @property
    def available(self):
        """A removed or ambiguous rule cannot be controlled."""
        return super().available and self.is_on is not None


class DahuaSirenBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """dahua siren switch class. Used to enable or disable camera built in sirens"""

    _name = "Siren"

    def __init__(self, coordinator, entry, name="Siren"):
        super().__init__(coordinator, entry)
        self._name = name

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on/enable the camera's siren"""
        channel = self._coordinator.get_channel()
        if self._coordinator.uses_recorder_deterrence():
            await self._coordinator.client.async_set_nvr_coaxial_control_state(
                self._coordinator.get_channel_number(), SIREN_TYPE, True
            )
        elif self._coordinator.uses_rpc2_deterrence(SIREN_TYPE):
            await self._coordinator.client.async_set_coaxial_control_state_rpc2(SIREN_TYPE, True)
        else:
            await self._coordinator.client.async_set_coaxial_control_state(channel, SIREN_TYPE, True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off/disable camera siren"""
        channel = self._coordinator.get_channel()
        if self._coordinator.uses_recorder_deterrence():
            await self._coordinator.client.async_set_nvr_coaxial_control_state(
                self._coordinator.get_channel_number(), SIREN_TYPE, False
            )
        elif self._coordinator.uses_rpc2_deterrence(SIREN_TYPE):
            await self._coordinator.client.async_set_coaxial_control_state_rpc2(SIREN_TYPE, False)
        else:
            await self._coordinator.client.async_set_coaxial_control_state(channel, SIREN_TYPE, False)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self._coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_siren"

    @property
    def icon(self):
        """Return the icon of this switch."""
        return SIREN_ICON

    @property
    def is_on(self):
        """
        Return true if the siren is on.
        Value is fetched from api.get_motion_detection_config
        """
        return self._coordinator.is_siren_on()


class DahuaAlarmOutputSwitch(DahuaBaseEntity, SwitchEntity):
    """Switch for a physical alarm/relay output."""

    _attr_icon = "mdi:alarm-light"

    def __init__(self, coordinator, entry, output: int):
        super().__init__(coordinator, entry)
        self._output = output

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Force the alarm output on with AlarmOut.Mode=1."""
        await self._coordinator.client.async_set_alarm_output_state(self._output, True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Force the alarm output off with AlarmOut.Mode=2."""
        await self._coordinator.client.async_set_alarm_output_state(self._output, False)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self._coordinator.get_device_name() + " Alarm Output"

    @property
    def unique_id(self):
        """Return a stable unique ID for this alarm output."""
        return self._coordinator.get_serial_number() + "_alarm_output_" + str(self._output)

    @property
    def is_on(self):
        """Return the physical state from alarm.cgi?action=getOutState."""
        return self._coordinator.is_alarm_output_on()


class DahuaPrivacyModeBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """dahua privacy mode switch class. Used to enable or disable the lens privacy mask"""

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on/enable privacy mode"""
        await self._coordinator.client.async_set_privacy_mode(True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off/disable privacy mode"""
        await self._coordinator.client.async_set_privacy_mode(False)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self._coordinator.get_device_name() + " Privacy Mode"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_privacy_mode"

    @property
    def icon(self):
        """Return the icon of this switch."""
        return PRIVACY_MODE_ICON

    @property
    def is_on(self):
        """
        Return true if privacy mode is on.
        Value is fetched from client.async_get_privacy_mode
        """
        return self._coordinator.is_privacy_mode_enabled()

