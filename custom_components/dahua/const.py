"""Constants for Dahua."""
# Base component constants
NAME = "Dahua"
DOMAIN = "dahua"
DOMAIN_DATA = f"{DOMAIN}_data"
ATTRIBUTION = "Data provided by https://ronnieroller.com"
ISSUE_URL = "https://github.com/rroller/dahua/issues"

# Icons - https://materialdesignicons.com/
ICON = "mdi:format-quote-close"
MOTION_DETECTION_ICON = "mdi:motion-sensor"
SECURITY_LIGHT_ICON = "mdi:alarm-light-outline"
SIREN_ICON = "mdi:bullhorn"
INFRARED_ICON = "mdi:weather-night"
DISARMING_ICON = "mdi:alarm-check"
VOLUME_HIGH_ICON = "mdi:volume-high"
BELL_ICON = "mdi:bell-ring"
PRIVACY_MODE_ICON = "mdi:shield-lock"

# Device classes - https://www.home-assistant.io/integrations/binary_sensor/#device-class
MOTION_SENSOR_DEVICE_CLASS = "motion"
SAFETY_DEVICE_CLASS = "safety"
CONNECTIVITY_DEVICE_CLASS = "connectivity"
SOUND_DEVICE_CLASS = "sound"
DOOR_DEVICE_CLASS = "door"

# Platforms
BINARY_SENSOR = "binary_sensor"
SWITCH = "switch"
LIGHT = "light"
CAMERA = "camera"
SELECT = "select"
BUTTON = "button"
SENSOR = "sensor"
EVENT = "event"
PLATFORMS = [BINARY_SENSOR, SWITCH, LIGHT, CAMERA, SELECT, BUTTON, SENSOR, EVENT]


# Configuration and options
CONF_ENABLED = "enabled"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_ADDRESS = "address"
CONF_PORT = "port"
CONF_RTSP_PORT = "rtsp_port"
CONF_STREAMS = "streams"
CONF_EVENTS = "events"
CONF_NAME = "name"
CONF_CHANNEL = "channel"
CONF_AUTO_DETECT_CHANNEL = "auto_detect_channel"
# Which other channels of a recorder to add alongside the one being set up.
CONF_EXTRA_CHANNELS = "extra_channels"
CONF_USE_HTTPS = "use_https"
CONF_SCAN_INTERVAL = "scan_interval"
# Prototype: route config reads over RPC2's session instead of a fresh digest
# handshake per call. Off by default -- see #636.
CONF_USE_RPC2 = "use_rpc2"
CONF_NVR_ACTIVE_DETERRENCE = "nvr_active_deterrence"
# Ask go2rtc not to open the RTSP talk channel. It otherwise holds it for as
# long as HA streams, which puts doorbells in a call state -- see #595.
CONF_DISABLE_BACKCHANNEL = "disable_backchannel"
CONF_AUTHORIZED_PLATES = "authorized_plates"
CONF_AUTHORIZED_HOLD_TIME = "authorized_hold_time"

# Events
EVENT_DAHUA_ANPR_RECOGNIZED = "dahua_anpr_recognized"

# Defaults
DEFAULT_NAME = "Dahua"
DEFAULT_AUTHORIZED_HOLD_TIME = 60
# How often the coordinator polls each device for its settings. Events do not
# come from polling - they arrive on the event stream - so this only paces the
# configuration read-back.
DEFAULT_SCAN_INTERVAL = 30
# Below this the polling costs more than it tells you, especially on an NVR
# where every channel is a separate entry against the same host.
MIN_SCAN_INTERVAL = 10

STARTUP_MESSAGE = f"""
-------------------------------------------------------------------
{NAME}
This is a custom integration for Dahua cameras!
If you have any issues with this you need to open an issue here:
{ISSUE_URL}
-------------------------------------------------------------------
"""
