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
        # No brightness asked for means full. Full on the HASS scale is 255;
        # 100 here is a Dahua-scale value and came out as 39% instead.
        hass_brightness = 255
    return int((hass_brightness / 255) * 100)


# https://github.com/rroller/dahua/issues/166
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
    event_blocks = re.split(r'--myboundary\n', data)

    events = []

    for event_block in event_blocks:
        # Skip the first 3 lines... the first line looks like: Content-Type: text/plain
        s = event_block.split("\n", 3)
        if len(s) < 3:
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
            key, value = key_value.split('=', 1)
            event[key] = value

        # data is a json string, convert it to real json and add it back to the output dic
        if "data" in event:
            try:
                data = json.loads(event["data"])
                event["data"] = data
            except Exception:  # pylint: disable=broad-except
                pass
        events.append(event)

    return events


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

    # Clean and standardize plate: uppercase, remove non-alphanumeric, convert common Greek/Cyrillic homoglyphs to Latin
    homoglyphs = {
        'Α': 'A', 'Β': 'B', 'Ε': 'E', 'Ζ': 'Z', 'Η': 'H', 'Ι': 'I',
        'Κ': 'K', 'Μ': 'M', 'Ν': 'N', 'Ο': 'O', 'Ρ': 'P', 'Τ': 'T',
        'Υ': 'Y', 'Χ': 'X'
    }
    clean_plate = plate_text.upper()
    for gr, lat in homoglyphs.items():
        clean_plate = clean_plate.replace(gr, lat)
    clean_plate = re.sub(r'[^A-Z0-9]', '', clean_plate)

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

