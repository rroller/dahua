"""
Various utilities for Dahua cameras
"""

import json
import logging
import re


def describe_write_refusal(exception) -> str:
    """A short phrase naming why a device would not take a write.

    Written because the two ways a recorder declines a lighting write both
    reached the user as something unreadable. `str()` on an
    aiohttp.ClientResponseError is the whole request URL --
    "403, message='Forbidden', url='http://.../configManager.cgi?action=setConfig
    &Lighting%5B6%5D%5B0%5D.Mode=Manual'" -- which is a request, not a reason.
    And `str()` on a timeout is empty, so formatting one gives a blank where the
    cause should be.

    The status is the part that distinguishes them: measured on a
    DHI-NVR5464-16P-EI, 403 is a channel whose lighting the recorder will not
    write, and 400 is a table it does not serve at all. So name the status when
    there is one, and the class name when there is not.

    Duck-typed on `status` rather than checking the class, so this module keeps
    importing nothing but the standard library.
    """
    status = getattr(exception, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return (
            "HTTP %d %s" % (status, getattr(exception, "message", "") or "")
        ).strip()
    return str(exception).strip() or type(exception).__name__


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


# One slot of the recorder's camera table, in either spelling the firmwares use:
# a uuid-ish name carrying the index, or a plain bracket. Anchored on
# RemoteDevice so a nested field of some other table cannot match.
_REMOTE_DEVICE_FIELD = re.compile(
    r"RemoteDevice(?:\.uuid:\S*?INFO_|\[)(\d+)\]?\.(\w+)$"
)

# What the recorder knows about the camera in a slot, and what to call it here.
# Deliberately not UserName or Password: the slot holds the camera's own login
# in clear, and nothing in this integration needs it.
_CAMERA_FIELDS = {
    "DeviceType": "model",
    "SerialNo": "serial",
    "Mac": "mac",
    "Version": "firmware",
    "Vendor": "vendor",
}


# What a recorder puts in a slot's Mac field when it does not know one. Both
# are addresses no camera can have: ff:ff:ff:ff:ff:ff is the broadcast address
# and 00:00:00:00:00:00 is the unspecified one.
_PLACEHOLDER_MACS = frozenset({"ffffffffffff", "000000000000"})


def usable_mac(value) -> str | None:
    """The MAC as the device reported it, or None when it is not an address.

    #1078: a DH-NVR4232-EI reports `ff:ff:ff:ff:ff:ff` for a channel whose
    camera it cannot name. That went into the device page's `connections`,
    which is how Home Assistant decides two devices are the same hardware, so
    every channel the recorder could not name claimed to be the same device
    and Home Assistant refused the second one:

        Invalid device info ... connections: {('mac', 'ff:ff:ff:ff:ff:ff')}
        ... are already registered for device ... of the same config entry

    The camera was fine; its device simply could not be created. Judged on the
    value rather than on how many channels share it, because one channel with
    a broadcast MAC is already wrong and the second is what makes it visible.

    Anything that is not twelve hex digits is also refused. A malformed MAC in
    `connections` is worse than no MAC at all: it is the field that merges
    devices together.
    """
    if not value:
        return None
    digits = str(value).strip().lower()
    for separator in (":", "-", "."):
        digits = digits.replace(separator, "")
    if len(digits) != 12 or any(c not in "0123456789abcdef" for c in digits):
        return None
    if digits in _PLACEHOLDER_MACS:
        return None
    return str(value).strip()


def parse_remote_devices(data) -> dict:
    """Which channels a recorder says it has a camera on.

    RemoteDevice carries one block per slot, keyed by an index inside a
    uuid-ish name:

        table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_11.Enable=true
        table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_11.ProtocolType=Private

    and some firmware uses the plain bracket form instead:

        table.RemoteDevice[11].Enable=true
        table.RemoteDevice[11].ProtocolType=Private

    Returns {index: {"enabled": bool, "protocol": str, "model": str,
    "serial": str, "mac": str, "firmware": str, "vendor": str}}.

    Everything after "protocol" describes the *camera*, not the recorder, and
    the recorder is the only thing that can say it: a PoE camera sits on the
    recorder's own network (10.2.2.x here) and nothing else on the LAN can ask
    it anything. Measured on a DHI-NVR5464-16P-EI, where the slot for channel
    11 carries DeviceType H32_VSIPP-6DIRMD-I3, SerialNo BD01D6APAG74E89, Mac
    30:dd:aa:f6:24:9e and Version "3.140.101B000.0.R,2024-11-28" while the
    recorder itself reports 4.005.0000000.3.R. Sixteen slots on that recorder,
    fifteen enabled, one of those reached over Onvif. The channel number is the
    index plus one.

    **Both spellings, because this used to read only the first and its two
    callers disagreed with it.** `remote_device_model` and
    `remote_device_protocol` in coordinator.py have always accepted either, so
    on bracket-form firmware the coordinator correctly warned that a channel
    was reached over Onvif while this returned {} -- which meant the add flow
    offered no channels to discover and `async_channel_refusal` accepted any
    channel number the user typed, including ones the recorder disowns. One
    table, one shape rule.
    """
    if not isinstance(data, dict):
        return {}
    found = {}
    for key, value in data.items():
        match = _REMOTE_DEVICE_FIELD.search(str(key))
        if not match:
            continue
        slot = found.setdefault(int(match.group(1)), {})
        field = match.group(2)
        if field == "Enable":
            slot["enabled"] = str(value).strip().lower() == "true"
        elif field == "ProtocolType":
            slot["protocol"] = str(value).strip().lower()
        elif field in _CAMERA_FIELDS:
            slot[_CAMERA_FIELDS[field]] = str(value or "").strip()
    for slot in found.values():
        slot.setdefault("enabled", False)
        slot.setdefault("protocol", "")
        for name in _CAMERA_FIELDS.values():
            slot.setdefault(name, "")
    return found


def labels_for_device(existing, previously_chosen, chosen) -> set:
    """The labels a device should end up with.

    Three sets, and the middle one is why this is not just a union. `existing`
    is what the device has now, including anything somebody added by hand, and
    `previously_chosen` is what this option said last time. Take away what the
    option used to ask for, then add what it asks for now.

    So unticking a label removes it, which is what somebody expects of a list
    they have just edited, while a label they put on a camera themselves was
    never in `previously_chosen` and is left alone -- by that and by a reload.

    Written as `existing - (previously_chosen - chosen)` at first, to say "drop
    only what we have stopped asking for". That is the same set: anything in all
    three is re-added by the union either way. A mutation test could not tell
    the two apart, which is the argument for the shorter one.
    """
    existing = set(existing or ())
    previously_chosen = set(previously_chosen or ())
    chosen = set(chosen or ())
    return (existing - previously_chosen) | chosen


# One channel's streams in the Encode table. MainFormat[0] is the stream the
# integration calls index 0, and ExtraFormat[n] is index n+1, which is the
# numbering `to_stream_name` already uses.
_MAIN_FORMAT = "table.Encode[{0}].MainFormat[0]."
_EXTRA_FORMAT = re.compile(r"table\.Encode\[(\d+)\]\.ExtraFormat\[(\d+)\]\.")


def parse_stream_codecs(encode, channel: int) -> dict:
    """What each of one channel's streams carries: codec, and whether audio is on.

        {0: {"codec": "H.265", "audio": False}, 1: {...}}

    Keyed by the stream index this integration uses everywhere else, so a camera
    entity can look up its own stream without translating anything.

    The audio half matters as much as the codec and is easy to miss. A browser
    cannot take H.265 over WebRTC, so go2rtc falls back to a transcoding source
    that Home Assistant registers as `ffmpeg:<stream>#audio=opus`, and that
    source asks go2rtc for the stream's **audio-only** view. A camera with
    `AudioEnable` false has no audio track, go2rtc answers 404, and the whole
    attempt fails. Measured on a DHI-NVR5464-16P-EI: with both true the WebRTC
    offer returned 500 and the recorded error, and moving that channel to H.264
    turned it into a 200 carrying video with the audio line marked inactive.

    Returns {} for anything that is not a table, so a device that refuses Encode
    is told apart from one that answers emptily by the caller, not here.
    """
    if not isinstance(encode, dict):
        return {}
    found = {}
    prefix = _MAIN_FORMAT.format(channel)
    for key, value in encode.items():
        key = str(key)
        index = None
        if key.startswith(prefix):
            index, field = 0, key[len(prefix) :]
        else:
            match = _EXTRA_FORMAT.match(key)
            if match and int(match.group(1)) == channel:
                index, field = int(match.group(2)) + 1, key[match.end() :]
        if index is None:
            continue
        stream = found.setdefault(index, {"codec": None, "audio": None})
        if field == "Video.Compression":
            stream["codec"] = str(value).strip() or None
        elif field == "AudioEnable":
            stream["audio"] = str(value).strip().lower() == "true"
    return found


def webrtc_cannot_play(streams: dict, index: int = 0) -> bool:
    """Whether this stream is the combination a browser cannot be given.

    H.265 **and** no audio track. Deliberately both: H.265 on its own is not
    universally broken -- it depends on the player, and the same recorder serves
    a working H.264 sub stream beside an unplayable H.265 main one -- so saying
    so on the codec alone would be wrong often enough to be worse than silence.
    This pair is not a matter of taste: there is no H.264 producer for the
    browser and no audio track for the transcoder, so the attempt fails.
    """
    stream = (streams or {}).get(index) or {}
    codec = str(stream.get("codec") or "")
    return "265" in codec and stream.get("audio") is False


def parse_channel_titles(data) -> dict:
    """The name a recorder holds for each channel, by index.

        table.ChannelTitle[0].Name=FRONT STREET

    Only useful for labelling. Unconfigured slots carry defaults, and those
    defaults are not consistent even within one recorder: `Channel11`,
    `Channel 1`, `Channel16` and `IPC` all appeared on the same device. So a
    name says nothing about whether a camera is there, and nothing here tries
    to read anything into it.
    """
    if not isinstance(data, dict):
        return {}
    titles = {}
    for key, value in data.items():
        match = re.search(r"ChannelTitle\[(\d+)\]\.Name$", str(key))
        if match:
            titles[int(match.group(1))] = str(value).strip()
    return titles


def channels_worth_offering(devices: dict) -> list:
    """The channel indexes worth probing, in order.

    A slot that is switched off has no camera. A slot reached over Onvif
    has one, and this integration cannot drive it: the recorder does not
    serve such a channel on its own Dahua paths, so snapshot answers 400
    and RTSP times out (#710). Offering it would add an entry that can
    never work.

    Being enabled is necessary and not sufficient. A camera removed from
    the recorder leaves its slot enabled with a stale serial, and only a
    probe tells the difference, which is why the caller probes.
    """
    return sorted(
        index
        for index, slot in devices.items()
        if slot.get("enabled") and slot.get("protocol") != "onvif"
    )


def parse_overlay_lines(value) -> list:
    """Split a stored overlay value into the lines it was written from.

    Dahua separates the lines of a title with a pipe, which is why the
    writer escapes the parts and leaves the separator alone. Reading is
    the same rule backwards.

    An empty value is no lines rather than one empty line, so a camera
    with nothing set reads back as nothing set.
    """
    if not value:
        return []
    return str(value).split("|")


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

    # We will split on "--myboundary" and then find the line the event starts on
    event_blocks = re.split(r"--myboundary\r?\n", data)

    events = []

    for event_block in event_blocks:
        # Find "Code=" wherever it falls, rather than counting header lines.
        #
        # This skipped exactly three lines and required the fourth to be the
        # event. Most blocks do look like that -- Content-Type, Content-Length,
        # blank, Code= -- but the header count is the device's choice and not a
        # rule, and a block carrying one header line fewer was dropped without
        # a word. @jaaneo reported it in #587 on a DHI-TPC-BF1241, whose thermal
        # channel does not use the same header shape as its visual one, so those
        # events never arrived and nothing said why.
        #
        # A block that stops inside its headers still carries no event and is
        # still skipped, which is what a chunk ending mid-block looks like:
        # stream_events hands on whatever iter_chunks gives it, so that is
        # ordinary rather than exceptional. Indexing blindly there raised
        # IndexError out of on_receive and took the whole stream down (#475).
        start = re.search(r"^Code=", event_block, re.MULTILINE)
        if start is None:
            continue
        event_block = event_block[start.start() :].strip()

        # At this point we'll have something that looks like this...
        # Code=VideoMotion;action=Start;index=0;data={
        #    "Id" : [ 0 ],
        #    "RegionName" : [ "Region1" ],
        #    "SmartMotionEnable" : true
        # }
        # And we want to put each key/value pair into a dictionary...
        event = dict()
        for key_value in event_block.split(";"):
            if "=" not in key_value:
                # Not a key=value pair. Either the device cut the block short,
                # or the JSON payload carries a semicolon of its own -- a rule
                # or region the user named "Drive; Gate" is enough. Unpacking
                # it raised ValueError, which lost every event in the batch and
                # ended the stream; skipping it loses only this fragment.
                continue
            key, value = key_value.split("=", 1)
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
                    event.get("Code", "?"),
                    exc_info=True,
                )
        events.append(event)

    return events


HOMOGLYPHS = {
    "Α": "A",
    "Β": "B",
    "Ε": "E",
    "Ζ": "Z",
    "Η": "H",
    "Ι": "I",
    "Κ": "K",
    "Μ": "M",
    "Ν": "N",
    "Ο": "O",
    "Ρ": "P",
    "Τ": "T",
    "Υ": "Y",
    "Χ": "X",
}


def normalize_plate(plate_text: str | None) -> str:
    """Clean and standardize plate: uppercase, remove non-alphanumeric, convert common Greek/Cyrillic homoglyphs to Latin."""
    if not plate_text:
        return ""
    clean = str(plate_text).upper()
    for gr, lat in HOMOGLYPHS.items():
        clean = clean.replace(gr, lat)
    clean = re.sub(r"[^A-Z0-9]", "", clean)
    # Standard 7-character plate format: 3 letters + 4 digits (e.g. ABO1234, XYZ5670)
    # Correct common OCR confusions between letter O and digit 0 based on position:
    if len(clean) == 7:
        letters_part = clean[:3].replace("0", "O")
        digits_part = clean[3:].replace("O", "0")
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
            if (
                best_plate is None
                and any(c.isdigit() for c in norm_c)
                and any(c.isalpha() for c in norm_c)
            ):
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


def first_direction(value):
    """The first usable direction out of whatever shape the camera used.

    `DrivingDirection` arrives as a list whose later entries are blank:

        DrivingDirection:
          - Approach
          - ''

    so the first non-empty string is the answer. A plain string is returned as
    it stands, and anything else, a number or a nested structure, is ignored
    rather than guessed at.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


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
        for k in [
            "PlateNumber",
            "plateNumber",
            "PlateText",
            "plateText",
            "PlateNo",
            "plateNo",
        ]:
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
        vehicle_brand = (
            tc.get("Brand")
            or tc.get("VehicleSign")
            or tc.get("VehicleLogo")
            or tc.get("Logo")
        )
        vehicle_series = tc.get("SubBrand") or tc.get("Series")
        # Measured on a DHI-ITC413-PW4D-IZ1 (#757), driven each way past it.
        # Four fields look like a direction and only three of them move:
        #
        #   DrivingDirection  ["Approach", ""] -> ["Leave", ""]
        #   JunctionDirection "Obverse"        -> "Reverse"
        #   VehicleDirection  "Head"           -> "Tail"
        #   Direction         0                -> 0            (never changes)
        #
        # DrivingDirection first, because Approach and Leave say what happened,
        # where Obverse and Head describe which end of the car was photographed
        # and need Dahua's vocabulary to read. Direction and DirectionName stay
        # ahead of all of it so a camera already reporting them is unaffected;
        # here Direction is 0, which is falsy, so it falls through rather than
        # reporting a meaningless zero.
        direction = (
            tc.get("Direction")
            or tc.get("DirectionName")
            or first_direction(tc.get("DrivingDirection"))
            or first_direction(tc.get("JunctionDirection"))
            or first_direction(tc.get("VehicleDirection"))
        )

    veh = data.get("Vehicle") or data.get("vehicle")
    if isinstance(veh, dict):
        if not vehicle_type:
            vehicle_type = veh.get("Category") or veh.get("VehicleType")
        if not vehicle_color:
            c = veh.get("MainColor")
            if isinstance(c, str):
                vehicle_color = c
        if not vehicle_brand:
            vehicle_brand = (
                veh.get("Brand")
                or veh.get("VehicleSign")
                or veh.get("VehicleLogo")
                or veh.get("Logo")
                or veh.get("Text")
            )
        if not vehicle_series:
            vehicle_series = veh.get("SubBrand") or veh.get("Series")

    if not vehicle_type and isinstance(veh, dict):
        vehicle_type = veh.get("Text")

    if not vehicle_brand:
        vehicle_brand = (
            data.get("Brand")
            or data.get("VehicleSign")
            or data.get("VehicleLogo")
            or data.get("Logo")
        )
    if not direction:
        # This camera reports JunctionDirection at the top level rather than
        # inside TrafficCar, so the same list is checked in both places.
        direction = (
            data.get("Direction")
            or data.get("DirectionName")
            or first_direction(data.get("DrivingDirection"))
            or first_direction(data.get("JunctionDirection"))
            or first_direction(data.get("VehicleDirection"))
        )

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


def parse_ptz_presets(data) -> list:
    """Which preset positions a device reports over `ptz.cgi?action=getPresets`.

    The preset dropdown offers `1` to `10` to every camera, whatever it
    actually holds. So selecting a preset the camera does not have sends a
    `GotoPreset` for it, the device answers 400, and Home Assistant reports a
    failed action that looks like the integration being broken (#713).

    The device will say. A camera with presets answers with a row each:

        presets[0].Index=1
        presets[0].Name=Gate
        presets[1].Index=3

    Only `Index` matters: the numbers need not be contiguous, because deleting
    preset 2 leaves 1 and 3.

    An empty result means this device holds no presets. It is **not** the same
    as the device declining to answer, which #762's version of this docstring
    got wrong by claiming the two "cannot be told apart". They can, by status:

        no PTZ motor, HFW3449E-S-IL and HFW3449T-ZS-IL   200, zero bytes
        fixed cameras behind a DHI-NVR5464-16P-EI        200, empty body
        does not implement getPresets, Intelbras IM7      400, "Bad Request"

    A 400 raises before this is reached, so everything arriving here is a
    device that answered. The caller is what decides; see `_async_preset_ids`
    in select.py, which returns None for the refusal and this list otherwise.
    """
    if not isinstance(data, dict):
        return []
    found = set()
    for key, value in data.items():
        if not re.search(r"\.Index$", str(key)):
            continue
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if number > 0:
            found.add(number)
    return sorted(found)


# Field names whose *value* is somebody's business rather than ours. The names
# are kept, because "which field carries this" is the usual question; only the
# contents go.
EVENT_PRIVATE_FIELDS = frozenset(
    {
        # Spelled without separators: the lookup strips underscores, so "raw_plate"
        # and "rawplate" both land here and only one spelling is listed.
        "plate",
        "platenumber",
        "rawplate",
        "platedata",
        "plates",
        "card",
        "cardno",
        "cardnumber",
        "cardname",
        "user",
        "userid",
        "username",
        "usertype",
        "password",
        "token",
        "secret",
        "key",
        "uuid",
        "serialnumber",
        "defendcode",
        "imei",
        "phonenumber",
        "tel",
        "faceid",
        "personid",
        "facefeature",
        "similarity",
    }
)

EVENT_VALUE_LIMIT = 80
EVENT_LIST_LIMIT = 6
EVENT_DEPTH_LIMIT = 6


def summarise_event(value, _depth: int = 0):
    """An event with its shape intact and its contents cut down to size.

    Written for diagnostics, and worth explaining because it looks like it
    throws away the interesting part.

    What people are asked for, over and over, is a raw event: which `Code` the
    device sent, what `action` it carried, which field holds the direction or
    the state or the object type. That is all structure. The values are rarely
    the question, and some of them are a number plate, a card number or a
    person's name.

    So field names survive in full, values do not:

    - a field named in EVENT_PRIVATE_FIELDS becomes `<redacted>`
    - any other long value is truncated, keeping its start
    - long lists keep their first few entries and say how many were dropped

    The result publishes strictly less than the raw event people currently
    paste into public issues by hand, while answering the same questions.
    """
    if _depth > EVENT_DEPTH_LIMIT:
        return "<too deep>"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).strip().lower().replace("_", "") in EVENT_PRIVATE_FIELDS:
                out[key] = "<redacted>"
            else:
                out[key] = summarise_event(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        kept = [
            summarise_event(item, _depth + 1) for item in list(value)[:EVENT_LIST_LIMIT]
        ]
        dropped = len(value) - len(kept)
        if dropped > 0:
            kept.append("<%d more>" % dropped)
        return kept
    if isinstance(value, str) and len(value) > EVENT_VALUE_LIMIT:
        return value[:EVENT_VALUE_LIMIT] + "<truncated>"
    return value


def clean_firmware_version(version: str | None) -> str:
    """The version part of a reported firmware string, without its build date.

    ``/cgi-bin/magicBox.cgi?action=getSoftwareVersion`` answers
    ``2.800.0000016.0.R,build:2020-06-05``. The build date is a separate fact
    (``get_build_date`` peels it off); the version to compare against a cloud
    record is the part before it.
    """
    if not version:
        return ""
    return version.split(",", 1)[0].strip()


def cloud_upgrade_version(table: dict | None) -> str | None:
    """The newest firmware named by a ``_DHCloudUpgrade_`` record, or None.

    The device stores what its own cloud check found as ``LastVersion`` plus a
    ``LastSubVersion`` (``R`` on the firmwares seen so far). The two are joined
    only when the sub-version is not already part of the main one, so a
    firmware that puts the whole string in ``LastVersion`` is not doubled up.
    A build date the record happens to carry is dropped, the same as on the
    installed side: it would otherwise compare as extra numeric components and
    read as a newer firmware.
    """
    if not isinstance(table, dict):
        return None
    main = table.get("LastVersion")
    if not isinstance(main, str) or not main.strip():
        return None
    main = clean_firmware_version(main).rstrip(".")
    sub = table.get("LastSubVersion")
    if isinstance(sub, str) and sub.strip() and not main.endswith(sub.strip()):
        return f"{main}.{sub.strip()}"
    return main


def firmware_is_newer(latest: str | None, installed: str | None) -> bool:
    """Whether ``latest`` is a newer Dahua firmware than ``installed``.

    Compared on the numeric components, because these strings are not versions
    Home Assistant's ``AwesomeVersion`` can order: it calls
    ``2.800.0000016.0.R`` unknown and raises instead of comparing, and the
    update entity reads a raised comparison as "an update is available" -- a
    false alarm on a camera that is up to date. Missing trailing components
    compare equal (``2.800.0000016.0`` is not newer than ``2.800.0000016.0.R``),
    so a cloud record that omits the sub-version cannot trigger one either.
    A change to the trailing letter alone (``R`` to ``S``) is deliberately not
    treated as an update.
    """
    latest_parts = _firmware_numbers(latest)
    installed_parts = _firmware_numbers(installed)
    if not latest_parts or not installed_parts:
        return False
    width = max(len(latest_parts), len(installed_parts))
    latest_parts = latest_parts + (0,) * (width - len(latest_parts))
    installed_parts = installed_parts + (0,) * (width - len(installed_parts))
    return latest_parts > installed_parts


def _firmware_numbers(version: str | None) -> tuple[int, ...]:
    """The integer runs of a firmware string: 2.800.0000016.0.R -> (2, 800, 16, 0)."""
    if not version:
        return ()
    return tuple(int(part) for part in re.findall(r"\d+", version))
