"""A switch that operates a device output says it is a switch.

`entity-device-class` on Home Assistant's quality scale: use device classes where
possible. For switches the vocabulary is only two words, SWITCH and OUTLET, so the
question is not which one but whether the entity is a switch at all.

Five of these are not. They configure the camera -- motion detection, the two
disarming linkages, smart motion, an IVS rule -- and they already carry
`EntityCategory.CONFIG`, which is what says so. Calling a configuration toggle a
switch adds nothing: it is a checkbox for a setting, not an output. Home Assistant's
own vocabulary has no word for it, and inventing one by using the generic value
everywhere would make the attribute carry no information at all.

Three are. The alarm output is a physical relay, the siren drives the camera's
sounder, and privacy mode closes the lens. Those are things the device does, and they
are the three with no entity category precisely because they are controls rather
than settings.

So the rule here is the pairing, and it is written as an invariant rather than a list
of three: every switch is either a configuration entity or declares a device class.
A switch added later that is neither fails this, which is the point -- the choice has
to be made deliberately rather than defaulted into.
"""

import inspect

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory

from custom_components.dahua import switch as switch_module
from custom_components.dahua.switch import (
    DahuaAlarmOutputSwitch,
    DahuaPrivacyModeBinarySwitch,
    DahuaSirenBinarySwitch,
)

# The three that operate something on the device rather than configure it.
OUTPUT_SWITCHES = [
    DahuaSirenBinarySwitch,
    DahuaAlarmOutputSwitch,
    DahuaPrivacyModeBinarySwitch,
]


def _switch_classes():
    """Every switch entity this integration defines."""
    found = [
        obj for _, obj in inspect.getmembers(switch_module, inspect.isclass)
        if issubclass(obj, SwitchEntity)
        and obj is not SwitchEntity
        and obj.__module__ == switch_module.__name__
    ]
    assert found, "found no switch classes, so nothing below is being checked"
    return found


def _built(cls):
    """An instance with no __init__ run.

    Read from an instance rather than the class dict: Home Assistant's
    CachedProperties metaclass turns an `_attr_` class attribute into a property
    object, so the class dict holds the descriptor and not the value, and every
    assertion against it passes whatever the value is.
    """
    return object.__new__(cls)


def test_the_output_switches_say_they_are_switches():
    for cls in OUTPUT_SWITCHES:
        assert _built(cls).device_class is SwitchDeviceClass.SWITCH, cls.__name__


def test_every_switch_is_either_configuration_or_declares_a_device_class():
    """The invariant. Neither is a decision nobody made."""
    undecided = [
        cls.__name__ for cls in _switch_classes()
        if _built(cls).device_class is None
        and _built(cls).entity_category is not EntityCategory.CONFIG
    ]

    assert undecided == [], (
        "these switches are neither configuration entities nor device outputs, so "
        "nothing says which they are: %s" % undecided)


def test_the_configuration_switches_are_not_given_a_device_class():
    """The other half. A configuration toggle labelled as a switch would make the
    attribute meaningless -- it would be on every switch in the integration and
    distinguish nothing, which is the opposite of what a device class is for."""
    labelled = [
        cls.__name__ for cls in _switch_classes()
        if _built(cls).entity_category is EntityCategory.CONFIG
        and _built(cls).device_class is not None
    ]

    assert labelled == [], (
        "a configuration entity was given a device class: %s" % labelled)


def test_no_switch_is_both_an_output_and_a_configuration_entity():
    """They are meant to be two disjoint sets, so a class in both would mean the
    reasoning in this file no longer describes the code."""
    for cls in OUTPUT_SWITCHES:
        assert _built(cls).entity_category is not EntityCategory.CONFIG, cls.__name__


def test_the_output_switches_are_all_of_them():
    """OUTPUT_SWITCHES is written out by hand above, so this is what stops it going
    stale: any switch that is not a configuration entity has to be listed there."""
    not_configuration = {
        cls.__name__ for cls in _switch_classes()
        if _built(cls).entity_category is not EntityCategory.CONFIG
    }

    assert not_configuration == {cls.__name__ for cls in OUTPUT_SWITCHES}, (
        "the list at the top of this file no longer matches the switches that are "
        "controls rather than settings")
