"""Constants for Dahua."""
# Base component constants
NAME = "Dahua"
DOMAIN = "dahua"
DOMAIN_DATA = f"{DOMAIN}_data"
VERSION = "0.5.0"
ATTRIBUTION = "Data provided by https://ronnieroller.com"
ISSUE_URL = "https://github.com/rroller/dahua/issues"

# Icons - https://materialdesignicons.com/
ICON = "mdi:format-quote-close"
MOTION_DETECTION_ICON = "mdi:motion-sensor"
SECURITY_LIGHT_ICON = "mdi:alarm-light-outline"
SIREN_ICON = "mdi:bullhorn"
INFRARED_ICON = "mdi:weather-night"
DISARMING_ICON = "mdi:alarm-check"

# Device classes - https://www.home-assistant.io/integrations/binary_sensor/#device-class
MOTION_SENSOR_DEVICE_CLASS = "motion"
SAFETY_DEVICE_CLASS = "safety"
CONNECTIVITY_DEVICE_CLASS = "connectivity"

# Platforms
BINARY_SENSOR = "binary_sensor"
SWITCH = "switch"
LIGHT = "light"
CAMERA = "camera"
PLATFORMS = [BINARY_SENSOR, SWITCH, LIGHT, CAMERA]


# Configuration and options
CONF_ENABLED = "enabled"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_ADDRESS = "address"
CONF_PORT = "port"
CONF_RTSP_PORT = "rtsp_port"
CONF_STREAMS = "streams"
CONF_EVENTS = "events"

# Defaults
DEFAULT_NAME = "Dahua"

STARTUP_MESSAGE = f"""
-------------------------------------------------------------------
{NAME}
Version: {VERSION}
This is a custom integration for Dahua cameras!
If you have any issues with this you need to open an issue here:
{ISSUE_URL}
-------------------------------------------------------------------
"""

# Stream Names
STREAM_MAIN = "Main"
STREAM_SUB = "Sub"
STREAM_BOTH = "Both"
