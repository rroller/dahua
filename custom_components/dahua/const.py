"""Constants for Dahua."""

# Base component constants
NAME = "Dahua"
DOMAIN = "dahua"
DOMAIN_DATA = f"{DOMAIN}_data"
ATTRIBUTION = "Data provided by https://ronnieroller.com"
ISSUE_URL = "https://github.com/rroller/dahua/issues"

# Icons - https://materialdesignicons.com/

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
CONF_EVENTS = "events"
CONF_NAME = "name"
CONF_CHANNEL = "channel"
CONF_AUTO_DETECT_CHANNEL = "auto_detect_channel"
# Which other channels of a recorder to add alongside the one being set up.
CONF_EXTRA_CHANNELS = "extra_channels"
# Take every channel discovery found, without ticking sixteen boxes.
CONF_ALL_CHANNELS = "all_channels"
# Which Home Assistant area this device belongs in, as an area_id. Chosen while
# adding a recorder, so ten channels do not arrive unfiled.
CONF_AREA = "area"
CONF_USE_HTTPS = "use_https"
CONF_SCAN_INTERVAL = "scan_interval"
# Prototype: route config reads over RPC2's session instead of a fresh digest
# handshake per call. Off by default -- see #636.
CONF_USE_RPC2 = "use_rpc2"
CONF_NVR_ACTIVE_DETERRENCE = "nvr_active_deterrence"
CONF_MANUAL_SIREN = "manual_siren"
CONF_MANUAL_SECURITY_LIGHT = "manual_security_light"
# Ask go2rtc not to open the RTSP talk channel. It otherwise holds it for as
# long as HA streams, which puts doorbells in a call state -- see #595.
CONF_DISABLE_BACKCHANNEL = "disable_backchannel"
CONF_AUTHORIZED_PLATES = "authorized_plates"
CONF_AUTHORIZED_HOLD_TIME = "authorized_hold_time"

# Settings that belong to one channel rather than to the host.
#
# `channel_option` reads a channel's own answer before the entry's, and a merged
# recorder's channel keeps its answer in its subentry's data. So this is the set of
# keys the #827 migration has to carry across: a key that belongs here and is left
# behind reverts to whatever was stored when the camera was added, and a key that
# does not belong here would be frozen per channel instead of following the host.
#
# test_channel_options_survive_the_merge.py scans the package for every key read
# through channel_option, _channel_first or events_for_channel and fails if one is
# missing from here, so a new per-channel option cannot be added without it.
CHANNEL_OPTION_KEYS = frozenset(
    {
        CONF_AREA,
        CONF_AUTHORIZED_HOLD_TIME,
        CONF_AUTHORIZED_PLATES,
        CONF_AUTO_DETECT_CHANNEL,
        CONF_DISABLE_BACKCHANNEL,
        CONF_EVENTS,
        CONF_MANUAL_SECURITY_LIGHT,
        CONF_MANUAL_SIREN,
        CONF_NVR_ACTIVE_DETERRENCE,
    }
)

# Events
EVENT_DAHUA_ANPR_RECOGNIZED = "dahua_anpr_recognized"

# Defaults
DEFAULT_NAME = "Dahua"
# What an entry subscribes to when nothing has ever said otherwise. Lives here
# rather than in config_flow because __init__ needs it too, and config_flow
# imports __init__ back.
DEFAULT_EVENTS = [
    "VideoMotion",
    "CrossLineDetection",
    "AlarmLocal",
    "VideoLoss",
    "VideoBlind",
    "AudioMutation",
    "CrossRegionDetection",
    "SmartMotionHuman",
    "SmartMotionVehicle",
]
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
