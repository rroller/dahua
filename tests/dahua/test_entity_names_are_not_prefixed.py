"""An entity says its own half of the name, and Home Assistant says the device's.

`has-entity-name` is a Bronze rule on the integration quality scale. Every entity
here used to compose the whole thing itself:

    return self._coordinator.get_device_name() + " Motion Detection"

Home Assistant composes DEVICE + ENTITY when `has_entity_name` is True, so the
names it renders are unchanged. What changes is that the entity now states only
its own half, which is what makes the name translatable, keeps it correct when
somebody renames the device, and stops the device name being baked into the
entity_id of everything created from now on. `config_flow.py:152` records what
that cost: a fallback identity hash ended up in every entity_id for good.

The danger in the change is asymmetric. Miss one and its name is rendered twice --
"FRONT VERANDAH FRONT VERANDAH Motion Detection" -- and nothing fails, because a
doubled string is still a string. So this is a static check across the source
rather than a test of one entity: it reads every `name` property and every
`_attr_name` assignment and insists none of them reaches for the device.
"""

import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "dahua"

# The files that define entities. entity.py holds the base and is where the flag
# lives, so it is checked separately below.
PLATFORMS = ["binary_sensor", "button", "camera", "event", "light", "select",
             "sensor", "switch"]

# Ways the device's name can be reached from an entity.
DEVICE_NAME = re.compile(
    r"get_device_name\(\)|self\._device_name|config_entry\.title|entry\.title")


def _source(name):
    return (PACKAGE / ("%s.py" % name)).read_text(encoding="utf-8")


def _name_expressions(body):
    """Every `name` property body and every `_attr_name` assignment.

    Three entities used to set `_attr_name` in __init__ and return it from the
    property, so checking only the property would have missed them. None does now:
    `entity-translations` took two of them and `_attr_name` would beat the
    translation anyway. The `_attr_name` half is kept because it is the shape
    somebody reaches for next, and a check that has nothing to find today is
    exactly the one that has to still work tomorrow.
    """
    found = []
    for match in re.finditer(r"    def name\(self\)[^\n]*:\n((?:        .*\n)+)",
                             body):
        found.append(("name property", match.group(1)))
    for match in re.finditer(r"^\s*self\._attr_name\s*=\s*(.+)$", body,
                             re.MULTILINE):
        found.append(("_attr_name", match.group(1)))
    return found


# --- the flag ---------------------------------------------------------------

def test_the_base_entity_sets_has_entity_name():
    """Once, on the class every entity inherits, rather than per platform where
    one could be forgotten."""
    assert "_attr_has_entity_name = True" in _source("entity")


def test_every_entity_class_inherits_the_base():
    """Which is what makes setting it once sufficient. A class that did not would
    silently keep the old behaviour."""
    offenders = []
    for name in PLATFORMS:
        for match in re.finditer(r"^class (Dahua\w+)\(([^)]*)\)", _source(name),
                                 re.MULTILINE):
            classname, bases = match.groups()
            # DahuaEventSensor inherits DahuaEventDrivenEntity, and per-rule
            # IVS sensors inherit DahuaEventSensor.
            if not any(base in bases for base in (
                "DahuaBaseEntity", "DahuaEventDrivenEntity", "DahuaEventSensor"
            )):
                offenders.append("%s.%s" % (name, classname))

    assert not offenders, "these do not inherit the base: %s" % offenders


# --- and the thing that breaks silently -------------------------------------

@pytest.mark.parametrize("platform", PLATFORMS)
def test_no_entity_name_includes_the_device_name(platform):
    """A leftover prefix renders the device name twice and raises nothing."""
    offenders = [
        "%s: %s" % (kind, expression.strip())
        for kind, expression in _name_expressions(_source(platform))
        if DEVICE_NAME.search(expression)
    ]

    assert not offenders, (
        "%s.py composes the device name into an entity name, which Home Assistant "
        "will then prefix again:\n  %s" % (platform, "\n  ".join(offenders)))


def test_the_check_above_can_actually_see_a_prefix():
    """A static check that matched nothing would pass for ever. This proves the
    pattern finds the shape it is looking for."""
    sample = 'return self._coordinator.get_device_name() + " Motion Detection"'

    assert DEVICE_NAME.search(sample)


def test_it_also_sees_the_indirect_ways():
    """Three entities assign `_attr_name` in __init__, the binary sensors keep the
    device name in `_device_name`, and the camera used the config entry's title.
    All three were real and all three had to be found."""
    for sample in ['self._attr_name = f"{coordinator.get_device_name()} X"',
                   'return f"{self._device_name} {self._name}"',
                   'f"{config_entry.title} {display_name}"']:
        assert DEVICE_NAME.search(sample), sample
