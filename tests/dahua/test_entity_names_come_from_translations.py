"""An entity's own name comes from the translation file, and stays in step with it.

`entity-translations` on Home Assistant's quality scale. Eighteen entities used to
return an English string from a `name` property, which no language file can reach.
They declare a `translation_key` now and the string lives in
`translations/en.json`. The eighteenth was found by the last test in this file, on
its first run, in a platform I had not thought to look at.

Two things can go wrong silently and neither raises:

A key with no string in the file is not an error. `Entity._name_internal` looks the
key up, misses, and falls through, and the entity ends up with no name of its own
at all. On a device called "Garage" that is an entity called "Garage", which is
also what its neighbour is called.

A string in the file with no key in the code is dead weight that reads as coverage.
It is how a translator comes to spend an afternoon on names nothing shows.

So the two sides are compared here rather than trusted, and the strings are
compared against what the properties used to return, because the point of this
change was that nobody's dashboard moves.
"""

import ast
import io
import json
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "dahua"

PLATFORMS = (
    "binary_sensor",
    "button",
    "camera",
    "event",
    "light",
    "number",
    "select",
    "sensor",
    "switch",
    "update",
)

# What each entity's `name` property returned before it was translated. The whole
# point of the change was that Home Assistant composes "<device> <entity>" either
# way, so a difference here is a renamed entity on somebody's dashboard.
WAS = {
    ("binary_sensor", "authorized_vehicle"): "Authorized Vehicle",
    ("event", "doorbell"): "Doorbell",
    ("button", "reboot"): "Reboot",
    ("button", "open_door"): "Open Door",
    ("button", "cancel_call"): "Cancel Call",
    ("select", "security_light"): "Security Light",
    ("select", "preset_position"): "Preset Position",
    ("select", "day_night_mode"): "Day/Night Mode",
    # New with the infrared mode control, so there is no earlier property for it
    # to match. Listed because this table is asserted as equal to the file, which
    # is what makes a rename fail rather than pass quietly.
    ("select", "infrared_mode"): "Infrared Mode",
    ("select", "smart_motion_sensitivity"): "Smart Motion Sensitivity",
    ("select", "vth_camera_link"): "Camera for {vto} calls",
    ("sensor", "firmware_version"): "Firmware Version",
    ("sensor", "serial_number"): "Serial Number",
    ("sensor", "profile"): "Profile",
    ("sensor", "configured_channels"): "Configured Channels",
    ("sensor", "license_plate"): "License Plate",
    # New with the picture adjustments being translated. These are the
    # words the _attr_name they replaced returned, so no entity is renamed.
    ("number", "image_brightness"): "Brightness",
    ("number", "image_contrast"): "Contrast",
    ("number", "image_saturation"): "Saturation",
    ("number", "image_hue"): "Hue",
    ("update", "firmware_update"): "Firmware Update",
    ("switch", "motion_detection"): "Motion Detection",
    ("switch", "disarming"): "Disarming",
    ("switch", "event_notifications"): "Event Notifications",
    ("switch", "smart_motion_detection"): "Smart Motion Detection",
    ("switch", "alarm_output"): "Alarm Output",
    ("switch", "privacy_mode"): "Privacy Mode",
    # Named by the platform until the six below moved; the two pairs are a
    # capability choice, not an index.
    ("light", "infrared"): "Infrared",
    ("light", "illuminator"): "Illuminator",
    ("light", "flood_light"): "Flood Light",
    ("light", "ring_light"): "Ring Light",
    ("light", "security_light"): "Security Light",
    ("light", "warning_light"): "Warning Light",
    ("switch", "siren"): "Siren",
    ("switch", "alarm"): "Alarm",
}

# Entities whose name is not this integration's to translate. Listed rather than
# detected, so that adding a new hard coded English name fails here.
NAMED_ELSEWHERE = {
    # The rule's own name, read off the device. User data, not a string this
    # integration can translate.
    "DahuaIVSRuleSwitch",
    # The stream's name, from the client's own to_stream_name.
    "DahuaCamera",
}


def _classes():
    """(platform, class node) for every class in every platform module."""
    for platform in PLATFORMS:
        path = PACKAGE / ("%s.py" % platform)
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                yield platform, node


def _declared(cls):
    """The class's own _attr_translation_key, if it sets one."""
    for item in cls.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "_attr_translation_key"
                    and isinstance(item.value, ast.Constant)
                ):
                    return item.value.value
    return None


def _keys_passed_in(platform):
    """Literal `translation_key="..."` arguments in this platform's module.

    Two entities are not one name: the security light is "Warning Light" on a
    recorder and "Security Light" otherwise, and the siren is "Alarm" or "Siren".
    That is a choice between two fixed strings, so the class declares neither and
    the platform passes the key. The literals sit in `async_setup_entry`, which is
    where the choice is made, so they are as findable as a class attribute.
    """
    tree = ast.parse(io.open(PACKAGE / ("%s.py" % platform), encoding="utf-8").read())
    # Only calls that build an entity. `HomeAssistantError` takes a translation_key
    # too, and collecting those reported five exception keys as entity names with
    # nothing behind them. Keyed on the callee being a class in this module rather
    # than on a list of things to ignore, so it stays right as more is added.
    entity_classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    keys = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) not in entity_classes:
            continue
        for keyword in node.keywords:
            if keyword.arg != "translation_key":
                continue
            for inner in ast.walk(keyword.value):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    keys.add(inner.value)
    return keys


def _keys_in_code():
    found = {
        (platform, key): cls.name
        for platform, cls in _classes()
        if (key := _declared(cls)) is not None
    }
    for platform in PLATFORMS:
        for key in _keys_passed_in(platform):
            found.setdefault((platform, key), "chosen by %s.py" % platform)
    return found


def _owned_by_the_event_sensor_file(pair):
    """The 45 event sensor keys, which this file cannot see and does not own.

    They are derived from the event code in `__init__`, so there is no literal
    to find. test_event_sensor_names_are_translated.py checks them against the
    derivation and against every code the platform can produce, which is more
    than this scan could do.

    Subtraction rather than importing binary_sensor, so this file stays pure ast
    and json and keeps running without Home Assistant.
    """
    platform, _key = pair
    return platform == "binary_sensor" and pair not in WAS


def _names_in_file():
    data = json.load(io.open(PACKAGE / "translations" / "en.json", encoding="utf-8"))
    return {
        (platform, key): entry["name"]
        for platform, keys in data.get("entity", {}).items()
        for key, entry in keys.items()
    }


# --- the two sides have to agree --------------------------------------------


def test_every_key_the_code_declares_has_an_english_name():
    """A key with no string is not an error. The lookup misses and the entity has
    no name of its own, which on a device called Garage is an entity called
    Garage."""
    missing = sorted(set(_keys_in_code()) - set(_names_in_file()))

    assert not missing, "declared in code with no name in en.json: %s" % missing


def test_every_name_in_the_file_belongs_to_an_entity():
    """Dead strings read as coverage, and a translator spends real time on them."""
    unused = sorted(
        pair
        for pair in set(_names_in_file()) - set(_keys_in_code())
        if not _owned_by_the_event_sensor_file(pair)
    )

    assert not unused, "in en.json with nothing declaring it: %s" % unused


def test_the_names_are_what_the_properties_returned():
    """The change was meant to be invisible. Home Assistant composes
    "<device> <entity>" from has_entity_name either way, so a string that differs
    from the old property renames an entity that is already on a dashboard."""
    names = {
        pair: text
        for pair, text in _names_in_file().items()
        if not _owned_by_the_event_sensor_file(pair)
    }

    assert names == WAS, "renamed: %s" % sorted(
        key for key in set(names) | set(WAS) if names.get(key) != WAS.get(key)
    )


# --- and nothing quietly keeps naming itself --------------------------------


def test_an_entity_either_translates_its_name_or_is_listed_as_named_elsewhere():
    """The gap this closes is a new entity with `return "Something"`: it works, it
    reads fine, and it cannot be translated in any language."""
    hard_coded = []
    for platform, cls in _classes():
        if _declared(cls) is not None or cls.name in NAMED_ELSEWHERE:
            continue
        for item in cls.body:
            if isinstance(item, ast.FunctionDef) and item.name == "name":
                hard_coded.append("%s.%s" % (platform, cls.name))

    assert not hard_coded, (
        "names itself in code and is not listed in NAMED_ELSEWHERE: %s" % hard_coded
    )


def test_nothing_that_translates_its_name_also_sets_attr_name():
    """`Entity._name_internal` checks `_attr_name` first and returns it, so a class
    doing both never reads its translation. Both selects used to set it, which is
    why this is asserted: the key would have been there, the string would have been
    there, and the English name would have won in every language."""
    both = []
    for platform, cls in _classes():
        if _declared(cls) is None:
            continue
        for node in ast.walk(cls):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    name = (
                        target.attr
                        if isinstance(target, ast.Attribute)
                        else getattr(target, "id", None)
                    )
                    if name == "_attr_name":
                        both.append("%s.%s" % (platform, cls.name))

    assert not both, "sets _attr_name as well as a translation key: %s" % both


def test_every_platform_with_keys_is_one_home_assistant_knows():
    """The first level of the entity section is the platform domain, and a typo
    there is not an error either: the lookup is built from the platform the entity
    is added under, so `sensors` rather than `sensor` simply never matches."""
    unknown = sorted({platform for platform, _ in _names_in_file()} - set(PLATFORMS))

    assert not unknown, "not a platform this integration has: %s" % unknown
