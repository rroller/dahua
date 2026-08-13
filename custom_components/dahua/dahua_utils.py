"""
Various utilities for Dahua cameras
"""
import json
import re


def dahua_brightness_to_hass_brightness(bri_str: str) -> int:
    """
    Converts a dahua brightness (which is 0 to 100 inclusive) and converts it to what HASS
    expects, which is 0 to 255 inclusive
    """
    bri = 100
    if bri_str:
        bri = int(bri_str)

    current = bri / 100
    return int(current * 255)


def hass_brightness_to_dahua_brightness(hass_brightness: int) -> int:
    """
    Converts a HASS brightness (which is 0 to 255 inclusive) to a Dahua brightness (which is 0 to 100 inclusive)
    """
    if hass_brightness is None:
        hass_brightness = 100
    return int((hass_brightness / 255) * 100)


# https://github.com/rroller/dahua/issues/166
def parse_event(data: str) -> list[dict[str, any]]:
    event_blocks = re.split(r'--myboundary\r?\n?', data)

    events = []

    for event_block in event_blocks:
        event_block = event_block.strip()
        if not event_block:
            continue

        # Look for Code= in the block regardless of how many header lines precede it
        code_pos = event_block.find("Code=")
        if code_pos == -1:
            continue
        event_content = event_block[code_pos:].strip()

        # Extract key/value pairs safely
        event = dict()
        for key_value in event_content.split(';'):
            key_value = key_value.strip()
            if not key_value or '=' not in key_value:
                continue
            key, value = key_value.split('=', 1)
            event[key] = value

        # data is a json string, convert it to real json and add it back to the output dict
        if "data" in event:
            try:
                data_json = json.loads(event["data"])
                event["data"] = data_json
            except Exception:  # pylint: disable=broad-except
                pass
        events.append(event)

    return events
