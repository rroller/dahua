"""Entity icons live in icons.json, and stay in step with the code.

`icon-translations` on Home Assistant's quality scale, which could not be done
before entity names were: the `entity` section of icons.json is keyed by
`translation_key`, so until #839 there was nothing to key it on.

The failure modes are the same shape as the names, and just as quiet. An icon in
the file whose key no entity declares is never shown and reads as coverage. An
entity that declares a key and still sets `_attr_icon` keeps the code's icon,
because `Entity.icon` returns `_attr_icon` when it is set, so the file would be
written, validated by hassfest, and ignored.

Nothing here needs Home Assistant running: it is the class bodies read with ast
against a JSON file, which is also what makes it immune to the metaclass that
turns `_attr_icon` into a property object on the class.
"""

import ast
import io
import json
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "dahua"

PLATFORMS = ("binary_sensor", "button", "camera", "event", "light", "select",
             "sensor", "switch")

# The mdi name each entity's code produced before it moved. Home Assistant shows
# the same glyph either way, so a difference here is a changed icon on a
# dashboard somebody arranged.
WAS = {
    ("binary_sensor", "authorized_vehicle"): "mdi:car-check",
    ("select", "day_night_mode"): "mdi:theme-light-dark",
    ("sensor", "license_plate"): "mdi:car-back",
    ("sensor", "profile"): "mdi:theme-light-dark",
    ("switch", "alarm_output"): "mdi:alarm-light",
    ("switch", "disarming"): "mdi:alarm-check",
    ("switch", "event_notifications"): "mdi:bell-ring",
    ("switch", "motion_detection"): "mdi:motion-sensor",
    ("switch", "privacy_mode"): "mdi:shield-lock",
    ("switch", "smart_motion_detection"): "mdi:motion-sensor",
}

# Entities that still carry their icon in code, each because it has no
# translation key to hang one off. Listed rather than detected, so that adding a
# hard coded icon to a translated entity fails here.
ICON_STAYS_IN_CODE = {
    # One icon per event code, chosen from a map.
    "DahuaEventSensor",
    # These three are named by the platform rather than by a key, so they have
    # nothing to key an icon off either. They go together with their names.
    "DahuaSirenBinarySwitch",
    "DahuaInfraredLight",
    "DahuaSecurityLight",
}


def _classes():
    for platform in PLATFORMS:
        tree = ast.parse(io.open(PACKAGE / ("%s.py" % platform),
                                 encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                yield platform, node


def _translation_key(cls):
    for item in cls.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if (getattr(target, "id", None) == "_attr_translation_key"
                        and isinstance(item.value, ast.Constant)):
                    return item.value.value
    return None


def _declares_an_icon(cls):
    """An `icon` property, a class level `_attr_icon`, or one set in __init__."""
    for item in cls.body:
        if isinstance(item, ast.FunctionDef) and item.name == "icon":
            return "icon property"
    for node in ast.walk(cls):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = (target.attr if isinstance(target, ast.Attribute)
                        else getattr(target, "id", None))
                if name == "_attr_icon":
                    return "_attr_icon"
    return None


def _icons_in_file():
    data = json.load(io.open(PACKAGE / "icons.json", encoding="utf-8"))
    return {(platform, key): entry["default"]
            for platform, keys in data.get("entity", {}).items()
            for key, entry in keys.items()}


def _keys_in_code():
    return {(platform, key): cls.name
            for platform, cls in _classes()
            if (key := _translation_key(cls)) is not None}


# --- the two sides agree -----------------------------------------------------

def test_every_icon_in_the_file_belongs_to_an_entity():
    """An icon under a key nothing declares is never shown, and reads as
    coverage while it sits there."""
    unknown = sorted(set(_icons_in_file()) - set(_keys_in_code()))

    assert not unknown, "in icons.json with nothing declaring the key: %s" % unknown


def test_the_icons_are_the_ones_the_code_produced():
    """Moving an icon is meant to be invisible. A different mdi name here is a
    changed icon on a dashboard somebody arranged deliberately."""
    icons = _icons_in_file()

    assert icons == WAS, "changed: %s" % sorted(
        key for key in set(icons) | set(WAS) if icons.get(key) != WAS.get(key))


# --- and the code does not override the file ---------------------------------

def test_nothing_with_an_icon_in_the_file_also_sets_one_in_code():
    """`Entity.icon` returns `_attr_icon` when it is set, and a subclass's own
    `icon` property beats both. Either one leaves the file written, validated by
    hassfest, and never read."""
    in_file = set(_icons_in_file())
    both = []
    for platform, cls in _classes():
        key = _translation_key(cls)
        if key is None or (platform, key) not in in_file:
            continue
        how = _declares_an_icon(cls)
        if how:
            both.append("%s.%s (%s)" % (platform, cls.name, how))

    assert not both, "icon in icons.json and in code: %s" % both


def test_an_entity_with_an_icon_in_code_is_one_that_cannot_have_a_key():
    """The gap this closes is a new entity with `_attr_icon` and a translation
    key: it works, it looks right, and the icon can never be overridden per
    state or per language the way the file allows."""
    offenders = []
    for platform, cls in _classes():
        if not _declares_an_icon(cls):
            continue
        if cls.name in ICON_STAYS_IN_CODE:
            continue
        offenders.append("%s.%s" % (platform, cls.name))

    assert not offenders, (
        "sets an icon in code and is not listed in ICON_STAYS_IN_CODE: %s"
        % offenders)


def test_every_platform_in_the_file_is_one_this_integration_has():
    """The first level is the platform domain. A typo there is not an error: the
    frontend looks the icon up under the platform the entity was added with, so
    `sensors` simply never matches."""
    unknown = sorted({platform for platform, _ in _icons_in_file()}
                     - set(PLATFORMS))

    assert not unknown, "not a platform this integration has: %s" % unknown


def test_the_file_has_no_section_but_entity():
    """Service and trigger icons key off names this integration does not have, so
    a stray section would be validated and then never read."""
    data = json.load(io.open(PACKAGE / "icons.json", encoding="utf-8"))

    assert set(data) == {"entity"}, sorted(data)
