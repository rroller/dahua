"""Constants for Dahua."""

# Base component constants
NAME = "Dahua"
DOMAIN = "dahua"
DOMAIN_DATA = f"{DOMAIN}_data"
ATTRIBUTION = "Data provided by https://ronnieroller.com"
ISSUE_URL = "https://github.com/brianegge/dahua/issues"

# Icons - https://materialdesignicons.com/
ICON = "mdi:format-quote-close"

# Platforms
BINARY_SENSOR = "binary_sensor"
SWITCH = "switch"
LIGHT = "light"
CAMERA = "camera"
SELECT = "select"
PLATFORMS = [BINARY_SENSOR, SWITCH, LIGHT, CAMERA, SELECT]


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

# Defaults
DEFAULT_NAME = "Dahua"

STARTUP_MESSAGE = f"""
-------------------------------------------------------------------
{NAME}
This is a custom integration for Dahua cameras!
If you have any issues with this you need to open an issue here:
{ISSUE_URL}
-------------------------------------------------------------------
"""
