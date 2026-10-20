"""Switch platform for dahua."""

import asyncio

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from custom_components.dahua import entry_coordinators

from . import dahua_utils, refusals
from .const import DOMAIN
from .entity import DahuaBaseEntity, DahuaHostEntity
from .const import VTH_SILENCE_FIELD
from .late import async_add_and_keep_watching
from .client import SIREN_TYPE

# What a device answers a siren write with when it cannot be reached, as opposed
# to a bug here. Rpc2MethodRefused subclasses ConnectionError, so it is covered.
SIREN_WRITE_FAILED = (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError)

# Which control this is in refusals.py's store, so a device refusing its siren
# does not silence its infrared on the same channel.
SIREN_CONTROL = refusals.SIREN


# One at a time, because every toggle is a write to the device and these devices are measurably intolerant of
# concurrent requests: MAX_CONCURRENT_REQUESTS_PER_HOST is 2 for the same reason,
# and the login storms behind #577 and #603 are what happens without it. A
# coordinator does not help here, since it only centralises inbound reads and
# leaves outbound actions uncontrolled.
PARALLEL_UPDATES = 1


def _cloud_reporter(coordinators: list):
    """The one coordinator that should carry the device's cloud switch.

    The cloud connection is the box's, not a channel's: VSP_PaaS is served
    host wide and carries one Enable for the whole device. A recorder is one
    entry with a coordinator per channel (#827), so without this every channel
    would get its own copy of the same switch, and sixteen switches would
    fight over one flag. The lowest channel that reported the key carries it,
    which is what the processor and memory sensors already do.

    Returns None when no channel reported the table.
    """
    answering = [c for c in coordinators if c.supports_cloud_connection()]
    if not answering:
        return None
    return min(answering, key=lambda c: c.get_channel())


def _alarm_output_reporter(coordinators: list):
    """The one coordinator that should carry the box's relay switches.

    `AlarmOut` and `alarm.cgi` take no channel: the relays are screwed to the
    back of the box. Same problem `_cloud_reporter` solves and the same
    answer, and it matters more here than it looks -- lifting the
    single-output restriction without this would give a twelve channel
    recorder with six relays seventy two switches, all of them writing to the
    same six rows.

    Returns None when no channel reported any relay, which is most cameras.
    """
    answering = [
        c
        for c in coordinators
        if getattr(c, "supports_alarm_output", None) and c.supports_alarm_output()
    ]
    if not answering:
        return None
    return min(answering, key=lambda c: c.get_channel())


def _filed_under(entity, coordinator):
    """Which subentry this entity belongs to, as `late.py` asks.

    The cloud connection is the box's rather than one channel's, so its switch
    belongs to the device the recorder has of its own. A camera has no separate
    host device, so its switch stays on its own channel. Same rule as the
    processor and memory sensors in sensor.py, and the same helper shape.
    """
    if isinstance(entity, DahuaHostEntity) and coordinator.uses_separate_host_device():
        return None
    return coordinator.subentry_id


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup sensor platform."""

    # Keep the selected reporter for this setup's lifetime: its serial is part
    # of the switch's unique id, so a lower channel answering later must not
    # create a second one. Reloading the entry makes a fresh selection. The
    # same rule the load sensors follow since #1076.
    cloud_reporter = _cloud_reporter(list(entry_coordinators(entry).values()))
    alarm_output_reporter = _alarm_output_reporter(
        list(entry_coordinators(entry).values())
    )

    async def build(coordinator):
        """Which switches this channel warrants, as the device answers today.

        Called again when a capability turns up on a later poll, so a channel
        whose SmartMotionDetect read timed out at setup gets its switch when
        the device finally answers rather than at the next reload (#1057).
        Nothing in here talks to the device, so a rebuild costs no request.
        """
        # I think most cameras have a motion sensor so we'll blindly add a switch for it.
        # Not an indoor monitor without a camera: there is no picture to detect motion in.
        nonlocal cloud_reporter, alarm_output_reporter
        if alarm_output_reporter is None and coordinator.supports_alarm_output():
            # A relay that turned up on a later poll, the same way the cloud
            # switch below is allowed to.
            alarm_output_reporter = coordinator

        devices = []

        # The cloud connection is the box's, so one channel carries the switch
        # and `_cloud_reporter` picks which. Built here rather than once at
        # setup so that a device whose VSP_PaaS read timed out gets the switch
        # when it finally answers, instead of at the next reload (#1057): the
        # retry announces the capability and this is what the announcement
        # rebuilds. `_filed_under` keeps it on the box's own device page.
        if cloud_reporter is None and coordinator.supports_cloud_connection():
            cloud_reporter = coordinator
        if cloud_reporter is coordinator:
            devices.append(DahuaCloudConnectionSwitch(coordinator, entry))

        if not coordinator.is_indoor_monitor_without_video():
            devices.append(DahuaMotionDetectionBinarySwitch(coordinator, entry))

        # But only some cams have a siren, very few do actually. The rule lives on the
        # coordinator because the poll needs the same answer to decide whether to fetch the
        # status this entity reads.
        if coordinator.creates_siren_entity():
            devices.append(
                DahuaSirenBinarySwitch(
                    coordinator,
                    entry,
                    translation_key=(
                        "alarm" if coordinator.uses_recorder_deterrence() else "siren"
                    ),
                )
            )
        if (
            coordinator.supports_smart_motion_detection()
            or coordinator.supports_smart_motion_detection_amcrest()
        ):
            devices.append(DahuaSmartMotionDetectionBinarySwitch(coordinator, entry))
        if coordinator.supports_privacy_mode():
            devices.append(DahuaPrivacyModeBinarySwitch(coordinator, entry))
        # The relays belong to the box, so one channel carries them all. A
        # camera is its own box and nothing moves; a recorder gets them on its
        # own device instead of a copy per channel.
        if alarm_output_reporter is coordinator:
            slots = coordinator.get_alarm_output_slots()
            build_output = (
                DahuaAlarmOutputSwitch if slots == 1 else DahuaNamedAlarmOutputSwitch
            )
            devices.extend(
                build_output(coordinator, entry, output) for output in range(slots)
            )

        # Per channel, and on purpose: IntelliTour is indexed by channel, so a
        # dual sensor camera gets the switch on its PTZ channel only and a
        # recorder with two PTZ cameras gets two switches rather than one that
        # writes to the wrong row (#1051).
        if coordinator.supports_intelli_tour():
            devices.append(DahuaIntelliTourSwitch(coordinator, entry))

        # The coordinator already asked the device this during setup and kept the
        # answer. Asking again here put a network round trip inside platform setup,
        # where a device that is slow to answer eats the entry's setup budget.
        # One switch per table, rather than both from the linkage answer. They
        # are two tables and a device can serve one without the other, and the
        # poll already decides separately whether to read each of them.
        if coordinator.supports_disarming_linkage():
            devices.append(DahuaDisarmingLinkageBinarySwitch(coordinator, entry))
        if coordinator.supports_event_notifications():
            devices.append(
                DahuaDisarmingEventNotificationsLinkageBinarySwitch(coordinator, entry)
            )

        devices.extend(
            DahuaIVSRuleSwitch(coordinator, entry, rule)
            for rule in coordinator.get_ivs_rules()
        )
        # An indoor monitor's do not disturb, where the monitor reported it
        # (#998). Gated on the field rather than on the device class: the key
        # is present on a VTH5221D's 3.000 firmware and on @kmich's 4.800, but
        # nothing says every monitor has it.
        if coordinator.supports_vth_phone_setting(VTH_SILENCE_FIELD):
            devices.append(DahuaMonitorSilenceSwitch(coordinator, entry))
        return devices

    await async_add_and_keep_watching(
        hass,
        entry,
        build,
        async_add_devices,
        entry_coordinators,
        subentry_for=_filed_under,
    )


class DahuaMotionDetectionBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """dahua motion detection switch class. Used to enable or disable motion detection"""

    _attr_translation_key = "motion_detection"

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
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_motion_detection"

    @property
    def is_on(self):
        """
        Return true if the switch is on.
        Value is fetched from api.get_motion_detection_config
        """
        return self._coordinator.is_motion_detection_enabled()


class DahuaCloudConnectionSwitch(DahuaHostEntity, SwitchEntity):
    """The device's connection to Dahua's cloud, which DMSS calls arrive over.

    Asked for in #591, where @ScratMan was driving `VSP_PaaS.Enable` from a
    `shell_command` curl to stop a doorbell ringing the DMSS app while someone
    was home.

    Worth knowing before using it: this is the whole cloud connection, not the
    ringing. Turning it off stops remote viewing through DMSS as well, which
    @ScratMan confirmed on a VTO2311R-WP. For events rather than calls, the
    Event Notifications switch is the lighter tool.

    A host entity rather than a channel one: there is one cloud connection per
    box. See `_cloud_reporter`.
    """

    _attr_translation_key = "cloud_connection"

    _attr_entity_category = EntityCategory.CONFIG

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Connect the device to the cloud again."""
        await self._coordinator.client.async_set_cloud_connection(True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Disconnect the device from the cloud."""
        await self._coordinator.client.async_set_cloud_connection(False)
        await self._coordinator.async_refresh()

    @property
    def unique_id(self):
        """Unique within the platform, and per device rather than per channel."""
        return self._coordinator.get_serial_number() + "_cloud_connection"

    @property
    def is_on(self):
        """Whether the device says its cloud connection is on."""
        return self._coordinator.is_cloud_connection_enabled()


class DahuaIntelliTourSwitch(DahuaBaseEntity, SwitchEntity):
    """Start and stop a PTZ camera's AI tour plan.

    Asked for in #1051 by @MassakerGU, who was driving `IntelliTour[1].Enable`
    from outside Home Assistant on a DH-SDT6E432-8P-MB-A-PV1 and wanted the
    tour to stop while someone was in the garden.

    What it does and does not do. It toggles the plan the camera already has;
    it does not create, edit or schedule one, and it touches nothing else in
    the row. The plan, its presets, its dwell times and the camera's PTZ
    behaviour are left exactly as the camera's own interface set them, which
    is what @MassakerGU asked for.

    A channel entity rather than a host one. `IntelliTour` is indexed by
    channel: on the measured camera, row [0] is null for the panorama sensor
    and row [1] carries the tour for the PTZ sensor, and Home Assistant shows
    those as two channels in that order.
    """

    _attr_translation_key = "intelli_tour"

    # Configuration rather than a control: this changes how the camera behaves
    # from now on rather than doing one thing now. Same reasoning as the motion
    # detection switch above.
    _attr_entity_category = EntityCategory.CONFIG

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Start this channel's tour plan."""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_intelli_tour(channel, True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Stop this channel's tour plan."""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_intelli_tour(channel, False)
        await self._coordinator.async_refresh()

    @property
    def unique_id(self):
        """Unique within the platform. The serial already carries the channel."""
        return self._coordinator.get_serial_number() + "_intelli_tour"

    @property
    def is_on(self):
        """Whether the camera says this channel's tour is running."""
        return self._coordinator.is_intelli_tour_enabled()


class DahuaDisarmingLinkageBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """will set the camera's disarming linkage (Event -> Disarming in the UI)"""

    _attr_translation_key = "disarming"

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
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be
        configurable by the user or be changeable see
        https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_disarming"

    @property
    def is_on(self):
        """
        Return true if the switch is on.
        Value is fetched from client.async_get_linkage
        """
        return self._coordinator.is_disarming_linkage_enabled()


class DahuaDisarmingEventNotificationsLinkageBinarySwitch(
    DahuaBaseEntity, SwitchEntity
):
    """will set the camera's event notifications when device is disarmed (Event -> Disarming -> Event Notifications in the UI)"""

    _attr_translation_key = "event_notifications"

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
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be
        configurable by the user or be changeable see
        https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_event_notifications"

    @property
    def is_on(self):
        """
        Return true if the switch is on.
        """
        return self._coordinator.is_event_notifications_enabled()


class DahuaSmartMotionDetectionBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """Enables or disables the Smart Motion Detection option in the camera"""

    _attr_translation_key = "smart_motion_detection"

    _attr_entity_category = EntityCategory.CONFIG

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on SmartMotionDetect"""
        if self._coordinator.supports_smart_motion_detection_amcrest():
            await self._coordinator.client.async_set_ivs_rule(0, 0, True)
        else:
            await self._coordinator.client.async_enabled_smart_motion_detection(
                self._coordinator.get_channel(), True
            )
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off SmartMotionDetect"""
        if self._coordinator.supports_smart_motion_detection_amcrest():
            await self._coordinator.client.async_set_ivs_rule(0, 0, False)
        else:
            await self._coordinator.client.async_enabled_smart_motion_detection(
                self._coordinator.get_channel(), False
            )
        await self._coordinator.async_refresh()

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be
        configurable by the user or be changeable see
        https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_smart_motion_detection"

    @property
    def is_on(self):
        """Return true if the switch is on."""
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
                if self._remote
                else self._coordinator.client.async_set_ivs_rule_by_id
            )
            await setter(self._channel, self._rule_id, enabled)
        except ValueError as err:
            # The reason is built in client.py, which resolves the rule just
            # before writing, so it is carried through as a placeholder rather
            # than replaced by a guess at what went wrong. It is still English
            # until those messages get keys of their own.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ivs_rule_write_failed",
                translation_placeholders={"reason": str(err)},
            ) from err
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
        return self._rule_name

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

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator, entry, *, translation_key):
        """`translation_key` rather than a name, and keyword only.

        Called "Alarm" on a recorder and "Siren" otherwise, which the
        platform decides.

        Keyword only so a call site still passing the old display
        name positionally fails with a TypeError instead of setting a
        translation key that is not a slug and never matching.
        """
        super().__init__(coordinator, entry)
        self._attr_translation_key = translation_key

    @property
    def available(self) -> bool:
        """Unavailable once the device has refused to operate the siren.

        An AD410 advertises a siren from two independent sources and then answers
        `CoaxialControlIO.control` with `268894210, "Method not found!"` (#942).
        The hardware is there -- it sounds from the Amcrest app -- but this call
        is not how that device drives it, so the switch can only ever throw.
        """
        return super().available and not refusals.is_refused(
            self._coordinator, SIREN_CONTROL
        )

    async def _async_set(self, enabled: bool) -> None:
        """Sound or silence the siren, and say so when the device will not.

        The refusal used to escape as a raw `Rpc2MethodRefused`, so the frontend
        showed the method name and an error number, and every later press asked
        again and was refused again.
        """
        if refusals.is_refused(self._coordinator, SIREN_CONTROL):
            # Nothing is sent. The device has already said it cannot do this.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="siren_already_refused",
                translation_placeholders={
                    "device": self._coordinator.get_device_name()
                },
            )

        channel = self._coordinator.get_channel()
        try:
            if self._coordinator.uses_recorder_deterrence():
                await self._coordinator.client.async_set_nvr_coaxial_control_state(
                    self._coordinator.get_channel_number(), SIREN_TYPE, enabled
                )
            elif self._coordinator.uses_rpc2_deterrence(SIREN_TYPE):
                await self._coordinator.client.async_set_coaxial_control_state_rpc2(
                    SIREN_TYPE, enabled
                )
            else:
                await self._coordinator.client.async_set_coaxial_control_state(
                    channel, SIREN_TYPE, enabled
                )
        except SIREN_WRITE_FAILED as err:
            if refusals.refusal_is_outright(err):
                refusals.remember(
                    self._coordinator,
                    SIREN_CONTROL,
                    dahua_utils.describe_write_refusal(err),
                )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="siren_refused",
                translation_placeholders={
                    "device": self._coordinator.get_device_name(),
                    "reason": dahua_utils.describe_write_refusal(err),
                },
            ) from err
        await self._coordinator.async_refresh()

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on/enable the camera's siren"""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off/disable camera siren"""
        await self._async_set(False)

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_siren"

    @property
    def is_on(self):
        """
        Return true if the siren is on.
        Value is fetched from api.get_motion_detection_config
        """
        return self._coordinator.is_siren_on()


class DahuaAlarmOutputSwitch(DahuaHostEntity, SwitchEntity):
    """Switch for a physical alarm/relay output.

    `AlarmOut` takes no channel: the relays are screwed to the back of the
    box, like the alarm inputs beside them. On a recorder this is a host
    entity, filed against the recorder rather than a camera. On a camera
    there is nothing to separate and it keeps the page it has.

    **State is read back rather than assumed.** These devices will accept a
    write, answer `OK` and drive nothing, so `is_on` comes from
    `alarm.cgi?action=getOutState` and never from what was last written.
    Measured on a DHI-NVR5464-16P-EI: forcing `AlarmOut[5].Mode = 1` makes
    that read answer 32, which is 1<<5, and the relay really closes.
    """

    _attr_device_class = SwitchDeviceClass.SWITCH
    # The name a single-relay device has always had, in every language it has
    # always had it in. A box with several relays cannot use this, because one
    # string cannot tell six entities apart -- that is
    # DahuaNamedAlarmOutputSwitch below, and the two are separate classes
    # because an entity that sets `_attr_name` never reads its translation.
    _attr_translation_key = "alarm_output"

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
    def unique_id(self):
        """Return a stable unique ID for this alarm output.

        The device's serial rather than `get_serial_number()`, which appends
        the channel. A relay belongs to the box, so its identity must not
        depend on which channel's coordinator happens to be carrying the
        switch -- otherwise removing channel 0 from a recorder would rename
        every one. The same rule, and the same wording, as the disk sensors.

        On a camera and on channel 0 the two are the same string, so the ids
        that exist today and survive this are byte for byte what they were.
        """
        return (
            self._coordinator.get_device_serial_number()
            + "_alarm_output_"
            + str(self._output)
        )

    @property
    def extra_state_attributes(self):
        return {
            **(super().extra_state_attributes or {}),
            # Both numbers, as for the alarm inputs: the device indexes from
            # zero and prints from one, and which a reader means depends on
            # whether they are looking at the chassis or at the config.
            "alarm_output_index": self._output,
            "alarm_output_number": self._output + 1,
        }

    @property
    def is_on(self):
        """Return the physical state from alarm.cgi?action=getOutState."""
        return self._coordinator.is_alarm_output_on(self._output)


class DahuaNamedAlarmOutputSwitch(DahuaAlarmOutputSwitch):
    """One relay of several, named rather than translated.

    A box with six relays cannot name them all "Alarm Output". The name has
    to come from the device or from the relay's number, and neither is
    something a translation file can hold -- so this names itself, the way
    the disk sensors and the per-rule IVS sensors do.

    Separate from its parent rather than a branch inside it because setting
    `_attr_name` makes Home Assistant ignore the translation key entirely:
    a class doing both silently keeps its English name in every language, and
    there is a test asserting none of them does.
    """

    _attr_translation_key = None

    def __init__(self, coordinator, entry, output: int):
        super().__init__(coordinator, entry, output)
        self._attr_name = coordinator.get_alarm_output_name(output)


class DahuaPrivacyModeBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """dahua privacy mode switch class. Used to enable or disable the lens privacy mask"""

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_translation_key = "privacy_mode"

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on/enable privacy mode"""
        await self._coordinator.client.async_set_privacy_mode(True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off/disable privacy mode"""
        await self._coordinator.client.async_set_privacy_mode(False)
        await self._coordinator.async_refresh()

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_privacy_mode"

    @property
    def is_on(self):
        """
        Return true if privacy mode is on.
        Value is fetched from client.async_get_privacy_mode
        """
        return self._coordinator.is_privacy_mode_enabled()


class DahuaMonitorSilenceSwitch(DahuaBaseEntity, SwitchEntity):
    """Do not disturb on an indoor monitor (#998).

    `VideoTalkPhoneGeneral.SilenceEnable`, measured as a real boolean on a
    VTH5221D (3.000.0012000.0.R), where it read False.

    The monitor also has `DisableRingStartTime` and `DisableRingEndTime`, a
    scheduled quiet period. That is a different thing from this switch, it is
    set on the device itself, and this does not touch it.
    """

    _attr_translation_key = "vth_silence"

    # Configuration rather than a control: it changes how the monitor behaves
    # from now on rather than doing something once.
    _attr_entity_category = EntityCategory.CONFIG

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Silence this monitor."""
        await self._set(True)

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Let this monitor ring again."""
        await self._set(False)

    async def _set(self, silent: bool) -> None:
        await self._coordinator.client.async_set_video_talk_phone_field(
            VTH_SILENCE_FIELD, silent
        )
        await self._coordinator.async_refresh()

    @property
    def unique_id(self):
        """Unique within the platform. The serial already carries the channel."""
        return self._coordinator.get_serial_number() + "_vth_silence"

    @property
    def available(self) -> bool:
        """Off until the poll has the flag, so it never shows a guessed state."""
        return (
            super().available
            and self._coordinator.get_vth_phone_flag(VTH_SILENCE_FIELD) is not None
        )

    @property
    def is_on(self):
        """Whether the monitor says it is silenced."""
        return self._coordinator.get_vth_phone_flag(VTH_SILENCE_FIELD)
