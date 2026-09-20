"""
Various utilities for Dahua cameras
"""
import json
import logging
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
        # No brightness asked for means full. Full on the HASS scale is 255;
        # 100 here is a Dahua-scale value and came out as 39% instead.
        hass_brightness = 255
    return int((hass_brightness / 255) * 100)


# https://github.com/rroller/dahua/issues/166
_LOGGER = logging.getLogger(__name__)


def parse_event(data: str) -> list[dict[str, any]]:
    # This will turn the event stream data into a list of events, where each item in the list is a dictionary and where
    # the key of the dictionary is the key is for example "Code" and the value is "VideoMotion", etc
    # That's a little hard to explain... so look at this example...
    # Code=VideoMotion;action=Start;index=0;data={
    #    "Id" : [ 0 ],
    #    "RegionName" : [ "Region1" ],
    #    "SmartMotionEnable" : true
    # }
    # will be turned into
    # [{
    #   "Code":"VideoMotion",
    #   "action":"Start",
    #   "index":"0",
    #   ...
    # }]

    # We will split on "--myboundary" and then skip the first 3 lines so we end up with a string that starts with Code=
    event_blocks = re.split(r'--myboundary\r?\n', data)

    events = []

    for event_block in event_blocks:
        # Skip the first 3 lines... the first line looks like: Content-Type: text/plain
        s = event_block.split("\n", 3)
        # Four parts are needed to have a fourth, and a chunk can end
        # anywhere: stream_events hands on whatever iter_chunks gives it, so a
        # block that stops after its headers is ordinary, not exceptional. The
        # guard read "< 3" and then indexed [3], so such a block raised
        # IndexError out of on_receive and took the whole stream down with it.
        if len(s) < 4:
            continue
        event_block = s[3].strip()
        if not event_block.startswith("Code="):
            continue

        # At this point we'll have something that looks like this...
        # Code=VideoMotion;action=Start;index=0;data={
        #    "Id" : [ 0 ],
        #    "RegionName" : [ "Region1" ],
        #    "SmartMotionEnable" : true
        # }
        # And we want to put each key/value pair into a dictionary...
        event = dict()
        for key_value in event_block.split(';'):
            if '=' not in key_value:
                # Not a key=value pair. Either the device cut the block short,
                # or the JSON payload carries a semicolon of its own -- a rule
                # or region the user named "Drive; Gate" is enough. Unpacking
                # it raised ValueError, which lost every event in the batch and
                # ended the stream; skipping it loses only this fragment.
                continue
            key, value = key_value.split('=', 1)
            event[key] = value

        # data is a json string, convert it to real json and add it back to the output dic
        if "data" in event:
            try:
                data = json.loads(event["data"])
                event["data"] = data
            except Exception:  # pylint: disable=broad-except
                # Left as the raw string on purpose: it is what the device sent
                # and throwing it away helps nobody. But say so, because a
                # silent pass here is indistinguishable from a device that
                # sends no payload, and the usual cause is a truncated one.
                _LOGGER.debug(
                    "Could not parse the JSON payload of a %s event; leaving it as text",
                    event.get("Code", "?"), exc_info=True,
                )
        events.append(event)

    return events


HOMOGLYPHS = {
    'Α': 'A', 'Β': 'B', 'Ε': 'E', 'Ζ': 'Z', 'Η': 'H', 'Ι': 'I',
    'Κ': 'K', 'Μ': 'M', 'Ν': 'N', 'Ο': 'O', 'Ρ': 'P', 'Τ': 'T',
    'Υ': 'Y', 'Χ': 'X'
}


def normalize_plate(plate_text: str | None) -> str:
    """Clean and standardize plate: uppercase, remove non-alphanumeric, convert common Greek/Cyrillic homoglyphs to Latin."""
    if not plate_text:
        return ""
    clean = str(plate_text).upper()
    for gr, lat in HOMOGLYPHS.items():
        clean = clean.replace(gr, lat)
    clean = re.sub(r'[^A-Z0-9]', '', clean)
    # Standard 7-character plate format: 3 letters + 4 digits (e.g. ABO1234, XYZ5670)
    # Correct common OCR confusions between letter O and digit 0 based on position:
    if len(clean) == 7:
        letters_part = clean[:3].replace('0', 'O')
        digits_part = clean[3:].replace('O', '0')
        if letters_part.isalpha() and digits_part.isdigit():
            clean = letters_part + digits_part
    return clean


def _extract_plate_from_raw_string(text: str, event: dict) -> dict | None:
    """Extract plate and metadata from a raw (possibly truncated) JSON string using regex."""
    plate_text = None
    confidence = None

    # Check for PlateNumber in TrafficCar
    m_tc = re.search(r'"(?:PlateNumber|plateNumber)"\s*:\s*"([A-Za-z0-9]+)"', text)
    if m_tc:
        plate_text = m_tc.group(1)

    if not plate_text:
        # Collect all "Text" candidates
        candidates = re.findall(r'"Text"\s*:\s*"([A-Za-z0-9]+)"', text)
        best_plate = None
        for cand in candidates:
            norm_c = normalize_plate(cand)
            # 1. Exact Greek plate format: 3 letters + 4 digits
            if len(norm_c) == 7 and norm_c[:3].isalpha() and norm_c[3:].isdigit():
                best_plate = norm_c
                plate_text = cand
                break
            # 2. Check if Dahua flipped RTL: 4 digits + 3 letters
            if len(norm_c) == 7 and norm_c[:4].isdigit() and norm_c[4:].isalpha():
                flipped = norm_c[4:] + norm_c[:4]
                if flipped[:3].isalpha() and flipped[3:].isdigit():
                    best_plate = flipped
                    plate_text = flipped
                    break
            if best_plate is None and any(c.isdigit() for c in norm_c) and any(c.isalpha() for c in norm_c):
                best_plate = norm_c
                plate_text = cand

    if not plate_text:
        return None

    clean_plate = normalize_plate(plate_text)
    if not clean_plate:
        return None

    m_conf = re.search(r'"Confidence"\s*:\s*([0-9]+)', text)
    if m_conf:
        try:
            confidence = int(m_conf.group(1))
        except ValueError:
            pass

    m_brand = re.search(r'"(?:VehicleSign|Brand|VehicleLogo)"\s*:\s*"([^"]+)"', text)
    vehicle_brand = m_brand.group(1) if m_brand else None

    m_color = re.search(r'"VehicleColor"\s*:\s*"([^"]+)"', text)
    vehicle_color = m_color.group(1) if m_color else None

    return {
        "plate": clean_plate,
        "raw_plate": plate_text,
        "confidence": confidence,
        "vehicle_type": None,
        "vehicle_color": vehicle_color,
        "vehicle_brand": vehicle_brand,
        "vehicle_series": None,
        "direction": None,
        "event_code": event.get("Code"),
        "timestamp": None,
    }


def parse_authorized_plates(raw_str: str | list | None) -> list[str]:
    """Parse a comma-separated string or list of authorized plates into a normalized, deduplicated list."""
    if not raw_str:
        return []
    if isinstance(raw_str, list):
        items = raw_str
    elif isinstance(raw_str, str):
        items = raw_str.split(",")
    else:
        return []

    plates = []
    for item in items:
        norm = normalize_plate(item.strip())
        if norm and norm not in plates:
            plates.append(norm)
    return plates


def extract_plate_data(event: dict) -> dict | None:
    """Extract license plate and vehicle information from a Dahua ANPR/Traffic event dict.

    Supports multiple Dahua event schemas:
    - Standard WizMind/ITC Object schema: data.Object: { ObjectType: "Plate", Text: "..." }
    - Direct Plate object/string: data.Plate: { Text: "...", Confidence: ... } or data.Plate: "..."
    - TrafficCar schema: data.TrafficCar: { PlateNumber: "...", VehicleType: "...", VehicleColor: "...", Brand: "..." }
    - Direct plate number attributes: data.PlateNumber, data.PlateText, data.PlateNo

    Also extracts vehicle metadata attributes (type, color, brand/logo, model/series, direction)
    when provided by the camera AI.
    """
    if not isinstance(event, dict):
        return None

    data = event.get("data", event.get("Data", {}))
    if not isinstance(data, dict):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return _extract_plate_from_raw_string(data, event)
        else:
            return None

    plate_text = None
    confidence = None
    vehicle_type = None
    vehicle_color = None

    ignored_plates = {"unlicensed", "unknown", "none", "null", "--", ""}

    # 1. Check Object (WizMind / ITC standard: Object: { ObjectType: "Plate", Text: "..." })
    obj = data.get("Object") or data.get("object")
    if isinstance(obj, dict):
        obj_type = str(obj.get("ObjectType", "")).strip().lower()
        if obj_type in ("plate", "") or "plate" in obj:
            txt = str(obj.get("Text", "")).strip()
            if txt and txt.lower() not in ignored_plates:
                plate_text = txt
                confidence = obj.get("Confidence")

    # 2. Check Plate dict/string directly
    if not plate_text:
        plate_obj = data.get("Plate") or data.get("plate")
        if isinstance(plate_obj, dict):
            txt = str(plate_obj.get("Text", "")).strip()
            if txt and txt.lower() not in ignored_plates:
                plate_text = txt
                confidence = plate_obj.get("Confidence")
        elif isinstance(plate_obj, str):
            txt = plate_obj.strip()
            if txt and txt.lower() not in ignored_plates:
                plate_text = txt

    # 3. Check TrafficCar
    tc = data.get("TrafficCar") or data.get("trafficCar")
    if isinstance(tc, dict):
        if not plate_text:
            txt = str(tc.get("PlateNumber", "")).strip()
            if txt and txt.lower() not in ignored_plates:
                plate_text = txt
        vehicle_type = tc.get("VehicleType")
        vehicle_color = tc.get("VehicleColor")

    # 4. Check direct keys on data
    if not plate_text:
        for k in ["PlateNumber", "plateNumber", "PlateText", "plateText", "PlateNo", "plateNo"]:
            val = data.get(k)
            if isinstance(val, str):
                txt = val.strip()
                if txt and txt.lower() not in ignored_plates:
                    plate_text = txt
                    break

    if not plate_text:
        return None

    clean_plate = normalize_plate(plate_text)
    if not clean_plate:
        return None

    # Extract vehicle attributes
    vehicle_brand = None
    vehicle_series = None
    direction = None

    if isinstance(tc, dict):
        vehicle_brand = tc.get("Brand") or tc.get("VehicleSign") or tc.get("VehicleLogo") or tc.get("Logo")
        vehicle_series = tc.get("SubBrand") or tc.get("Series")
        direction = tc.get("Direction") or tc.get("DirectionName")

    veh = data.get("Vehicle") or data.get("vehicle")
    if isinstance(veh, dict):
        if not vehicle_type:
            vehicle_type = veh.get("Category") or veh.get("VehicleType")
        if not vehicle_color:
            c = veh.get("MainColor")
            if isinstance(c, str):
                vehicle_color = c
        if not vehicle_brand:
            vehicle_brand = veh.get("Brand") or veh.get("VehicleSign") or veh.get("VehicleLogo") or veh.get("Logo") or veh.get("Text")
        if not vehicle_series:
            vehicle_series = veh.get("SubBrand") or veh.get("Series")

    if not vehicle_type and isinstance(veh, dict):
        vehicle_type = veh.get("Text")

    if not vehicle_brand:
        vehicle_brand = data.get("Brand") or data.get("VehicleSign") or data.get("VehicleLogo") or data.get("Logo")
    if not direction:
        direction = data.get("Direction") or data.get("DirectionName")

    return {
        "plate": clean_plate,
        "raw_plate": plate_text,
        "confidence": confidence,
        "vehicle_type": vehicle_type,
        "vehicle_color": vehicle_color,
        "vehicle_brand": vehicle_brand,
        "vehicle_series": vehicle_series,
        "direction": direction,
        "event_code": event.get("Code"),
        "timestamp": event.get("UTC") or data.get("UTC") or data.get("LocaleTime"),
    }

