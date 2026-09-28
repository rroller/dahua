import time
import re

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from custom_components.dahua import DahuaDataUpdateCoordinator

from .const import (
    MOTION_SENSOR_DEVICE_CLASS,
    DOMAIN, SAFETY_DEVICE_CLASS, CONNECTIVITY_DEVICE_CLASS, SOUND_DEVICE_CLASS, DOOR_DEVICE_CLASS, VOLUME_HIGH_ICON,
)
from .entity import DahuaBaseEntity, DahuaEventDrivenEntity

# Override event names. Otherwise we'll generate the name from the event name for example SmartMotionHuman will
# become "Smart Motion Human"
NAME_OVERRIDES = {
    "VideoMotion": "Motion Alarm",
    "CrossLineDetection": "Cross Line Alarm",
    "DoorbellPressed": "Button Pressed",  # For VTO/Doorbell devices
}

# Events that are a moment rather than a state, and how long to show them for.
#
# Most events arrive as a pair: a Start raises the sensor and a Stop clears it.
# These do not. A doorbell press is a single notification that something
# happened, so nothing ever arrives to turn the sensor off again and it stays on
# until Home Assistant restarts (#375, #486).
#
# Deliberately a short list rather than a rule. VideoMotion and the IVS codes
# genuinely use Start and Stop and must keep doing so: clearing those on a timer
# would end motion detection early for everybody, which is far worse than the
# bug being fixed.
#
# Whichever comes first wins. A device that does send a closing event still
# clears the sensor immediately, so this only adds a floor for devices that
# never send one.
MOMENTARY_EVENT_HOLD_SECONDS = {
    "DoorbellPressed": 5,
    "CallNoAnswered": 5,
}

# How long to show any other event that arrives as a Pulse.
#
# A Pulse has no closing event either, so it needs the same treatment -- but
# which codes send one is not knowable from here. Thirteen of the forty-two
# selectable codes are Pulse shaped, only InterVideoAccess has ever been seen as
# one in a report (#329), and two more codes joined the list last week. So the
# coordinator records what the device actually sent and this is the hold applied
# when it says a code arrived that way.
DEFAULT_PULSE_HOLD_SECONDS = 5

# Override the device class for events
DEVICE_CLASS_OVERRIDES = {
    "VideoMotion": MOTION_SENSOR_DEVICE_CLASS,
    "CrossLineDetection": MOTION_SENSOR_DEVICE_CLASS,
    "AlarmLocal": SAFETY_DEVICE_CLASS,
    "VideoLoss": SAFETY_DEVICE_CLASS,
    "VideoBlind": SAFETY_DEVICE_CLASS,
    "StorageNotExist": CONNECTIVITY_DEVICE_CLASS,
    "StorageFailure": CONNECTIVITY_DEVICE_CLASS,
    "StorageLowSpace": SAFETY_DEVICE_CLASS,
    "FireWarning": SAFETY_DEVICE_CLASS,
    "DoorbellPressed": SOUND_DEVICE_CLASS,
    "DoorStatus": DOOR_DEVICE_CLASS,
    "AudioMutation": SOUND_DEVICE_CLASS,
}

ICON_OVERRIDES = {
    "AudioAnomaly": VOLUME_HIGH_ICON,
    "AudioMutation": VOLUME_HIGH_ICON,
}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup binary_sensor platform."""
    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    sensors: list[BinarySensorEntity] = []
    for event_name in coordinator.get_event_list():
        sensors.append(DahuaEventSensor(coordinator, entry, event_name))

    for rule in coordinator.get_ivs_rules():
        sensors.append(DahuaIVSRuleBinarySensor(coordinator, entry, rule))

    # For doorbells we'll just add these since most people will want them
    if coordinator.is_doorbell():
        sensors.append(DahuaEventSensor(coordinator, entry, "DoorbellPressed"))
        sensors.append(DahuaEventSensor(coordinator, entry, "Invite"))
        sensors.append(DahuaEventSensor(coordinator, entry, "DoorStatus"))
        sensors.append(DahuaEventSensor(coordinator, entry, "CallNoAnswered"))

    sensors.append(DahuaAuthorizedVehicleBinarySensor(coordinator, entry))

    if sensors:
        async_add_devices(sensors)


class DahuaEventSensor(DahuaEventDrivenEntity, BinarySensorEntity):
    """
    dahua binary_sensor class to record events. Many of these events are configured in the camera UI by going to:
    Setting -> Event -> IVS -> and adding a tripwire rule, etc. See the DahuaEventThread in thread.py on how we connect
    to the cammera to listen to events.
    """

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry, event_name: str):
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        BinarySensorEntity.__init__(self)

        # event_name is the event name, example: VideoMotion, CrossLineDetection, SmartMotionHuman, etc
        self._event_name = event_name

        # None for an ordinary event, which stays on until the device says stop.
        self._hold_seconds = MOMENTARY_EVENT_HOLD_SECONDS.get(event_name)
        self._unsub_timer = None

        self._coordinator = coordinator
        self._device_name = coordinator.get_device_name()
        self._device_class = DEVICE_CLASS_OVERRIDES.get(event_name, MOTION_SENSOR_DEVICE_CLASS)
        self._icon_override = ICON_OVERRIDES.get(event_name, None)

        # name is the friendly name, example: Cross Line Alarm. If the name is not found in the override it will be
        # generated from the event_name. For example SmartMotionHuman will become "Smart Motion Human"
        # https://stackoverflow.com/questions/25674532/pythonic-way-to-add-space-before-capital-letter-if-and-only-if-previous-letter-i/25674575
        default_name = re.sub(r"(?<![A-Z])(?<!^)([A-Z])", r" \1", event_name)
        self._name = NAME_OVERRIDES.get(event_name, default_name)

        # Build the unique ID. This will convert the name to lower underscores. For example, "Smart Motion Vehicle" will
        # become "smart_motion_vehicle" and will be added as a suffix to the device serial number
        self._unique_id = coordinator.get_serial_number() + "_" + self._name.lower().replace(" ", "_")
        if event_name == "VideoMotion":
            # We need this for backwards compatibility as the VideoMotion was created with a unique ID of just the
            # serial number and we don't want to break people who are upgrading
            self._unique_id = coordinator.get_serial_number()

    @property
    def unique_id(self):
        """Return the entity unique ID."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the binary_sensor. Example: Cam14 Motion Alarm"""
        return f"{self._device_name} {self._name}"

    @property
    def device_class(self):
        """Return the class of this binary_sensor, Example: motion"""
        return self._device_class

    @property
    def icon(self) -> str:
        return self._icon_override

    @property
    def is_on(self):
        """
        Return true if the event is activated.

        This is the magic part of this sensor along with the async_added_to_hass method below.
        The async_added_to_hass method adds a listener to the coordinator so when the event is started or stopped
        it calls the schedule_update_ha_state function. schedule_update_ha_state gets the current value from this is_on method.

        A momentary event also expires on its own, because no stop is coming.
        """
        started = self._coordinator.get_event_timestamp(self._event_name)
        if started <= 0:
            return False
        hold = self._hold()
        if hold is None:
            return True
        return (time.time() - started) < hold

    def _hold(self):
        """How long this sensor shows an event for, or None to wait for a Stop.

        The explicit list wins, because those two are known and documented. Past
        that, a code the device has sent as a Pulse gets the default hold: there
        is no Stop coming for it, so a sensor left alone would stay on until
        Home Assistant restarted.

        Resolved per read rather than fixed in __init__, because whether a code
        is momentary is learned from the first event rather than known at setup.
        """
        if self._hold_seconds is not None:
            return self._hold_seconds
        if self._coordinator.event_is_momentary(self._event_name):
            return DEFAULT_PULSE_HOLD_SECONDS
        return None

    async def async_added_to_hass(self):
        """Connect to dispatcher listening for entity data notifications."""
        self._coordinator.add_dahua_event_listener(self._event_name, self._async_event_fired)

    @callback
    def _async_event_fired(self):
        """Show the new state, and for a momentary event arrange to clear it.

        is_on going false on its own is not enough: nothing would ask it again,
        so the sensor would keep showing on until some other event happened to
        write to it. The timer is what makes Home Assistant look again.
        """
        self.schedule_update_ha_state()

        hold = self._hold()
        if hold is None:
            return

        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

        if self._coordinator.get_event_timestamp(self._event_name) > 0:
            self._unsub_timer = async_call_later(
                self.hass, hold, self._async_hold_expired)

    @callback
    def _async_hold_expired(self, _now=None):
        """The hold is over, so ask for the state again."""
        self._unsub_timer = None
        self.schedule_update_ha_state()

    async def async_will_remove_from_hass(self):
        """Drop the timer with the entity, so it cannot fire into nothing."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @property
    def should_poll(self) -> bool:
        """Return True if entity has to be polled for state.  False if entity pushes its state to HA"""
        return False


class DahuaIVSRuleBinarySensor(DahuaEventSensor):
    """Track one normal IVS rule by its stable Dahua rule ID."""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, rule: dict):
        super().__init__(coordinator, entry, f"IVSRule_{rule['id']}")
        self._name = rule["name"]
        self._unique_id = f"{coordinator.get_serial_number()}_ivs_rule_{rule['id']}"
        self._rule_id = rule["id"]
        self._rule_type = rule.get("type")

    @property
    def extra_state_attributes(self):
        return {"rule_id": self._rule_id, "rule_type": self._rule_type}


class DahuaAuthorizedVehicleBinarySensor(DahuaEventDrivenEntity, BinarySensorEntity):
    """Binary sensor that turns on when an authorized vehicle license plate is recognized."""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_device_class = BinarySensorDeviceClass.PRESENCE
        self._attr_icon = "mdi:car-check"
        self._unique_id = f"{coordinator.get_serial_number()}_authorized_vehicle"
        self._active_until: float = 0.0
        self._last_matched_plate: str | None = None
        self._last_matched_plate_data: dict = {}
        self._last_matched_time: int | None = None
        self._unsub_timer = None

    @property
    def name(self):
        """Return the name of the binary sensor."""
        return f"{self._coordinator.get_device_name()} Authorized Vehicle"

    @property
    def unique_id(self):
        """Return the entity unique ID."""
        return self._unique_id

    @property
    def is_on(self) -> bool:
        """Return True if an authorized vehicle was recognized within the hold time."""
        if time.time() < self._active_until:
            return True
        last_plate = self._coordinator.get_last_plate()
        last_time = self._coordinator.get_last_plate_timestamp()
        hold_time = self._coordinator.get_authorized_hold_time()
        if self._coordinator.is_plate_authorized(last_plate) and (time.time() - last_time < hold_time):
            return True
        return False

    @property
    def extra_state_attributes(self):
        """Return attributes including authorized plates, hold time, and last matched vehicle details."""
        plate_data = self._last_matched_plate_data or self._coordinator.get_last_plate_data() or {}
        return {
            "authorized_plates": self._coordinator.get_authorized_plates(),
            "hold_time_seconds": self._coordinator.get_authorized_hold_time(),
            "last_matched_plate": self._last_matched_plate,
            "last_matched_brand": plate_data.get("vehicle_brand"),
            "last_matched_color": plate_data.get("vehicle_color"),
            "last_matched_type": plate_data.get("vehicle_type"),
            "last_matched_time": self._last_matched_time,
            "direction": plate_data.get("direction"),
        }

    async def async_added_to_hass(self):
        """Connect to dispatcher listening for entity data notifications."""
        @callback
        def _on_plate_update():
            last_plate = self._coordinator.get_last_plate()
            if self._coordinator.is_plate_authorized(last_plate):
                hold_time = self._coordinator.get_authorized_hold_time()
                self._active_until = time.time() + hold_time
                self._last_matched_plate = last_plate
                self._last_matched_plate_data = dict(self._coordinator.get_last_plate_data() or {})
                self._last_matched_time = self._coordinator.get_last_plate_timestamp()

                if self._unsub_timer:
                    self._unsub_timer()
                    self._unsub_timer = None
                self._unsub_timer = async_call_later(
                    self.hass, hold_time, self._async_auto_off
                )
            self.schedule_update_ha_state()

        self._coordinator.add_plate_listener(_on_plate_update)

        # Recheck state on startup/reload in case plate was recognized right before reload
        last_plate = self._coordinator.get_last_plate()
        last_time = self._coordinator.get_last_plate_timestamp()
        hold_time = self._coordinator.get_authorized_hold_time()
        if last_time > 0 and self._coordinator.is_plate_authorized(last_plate):
            elapsed = time.time() - last_time
            if elapsed < hold_time:
                self._active_until = last_time + hold_time
                self._last_matched_plate = last_plate
                self._last_matched_plate_data = dict(self._coordinator.get_last_plate_data() or {})
                self._last_matched_time = last_time
                self._unsub_timer = async_call_later(
                    self.hass, hold_time - elapsed, self._async_auto_off
                )

    @callback
    def _async_auto_off(self, _now=None):
        """Callback to turn off entity after hold time expires."""
        self._unsub_timer = None
        self.schedule_update_ha_state()

    async def async_will_remove_from_hass(self):
        """Clean up timers when entity is removed."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    @property
    def should_poll(self) -> bool:
        """Return False as entity pushes state updates."""
        return False
