"""The picture controls are one family and sort as one.

Home Assistant's device page puts every entity with `EntityCategory.CONFIG`
into one card and sorts that card by name. It offers no other ordering: there
is no position attribute, and the user cannot drag them. So the name is the
only lever, and a family keeps together only if its names do.

They did not. Sorted, the picture controls landed at B, C, G, H, I, M, S and V,
with the reboot button at R in the middle of them, which left `Saturation` and
`Video Color Style` below the reboot button and away from their own siblings on
every camera and doorbell. They share an `Image ` prefix now.

That makes the prefix load-bearing rather than decorative, which is what this
file is for. The exact strings are pinned in
`test_entity_names_come_from_translations.py`, which fails on any rename at
all; what is checked here is the property that made those strings the right
ones, so a later edit that renames one of them away from the family fails with
the reason rather than just the difference.

The six sliders are read out of `number.py` rather than listed, so a seventh
picture adjustment added without the prefix fails here on its first run.
"""

import ast
import io
import json
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "dahua"

# The controls that are not built from a table, with the platform each lives in.
SINGLETON_CONTROLS = {
    ("select", "video_color_style"),
    ("select", "exposure_anti_flicker"),
    ("select", "image_rotation"),
    ("select", "image_wide_dynamic_range"),
    ("select", "image_exposure_mode"),
    ("switch", "mirror"),
}

# What the family has to sort above. The reboot button is the one that was
# actually splitting them, and it is in the same card.
REBOOT = ("button", "reboot")

PREFIX = "Image "


def _names() -> dict:
    """Every entity name in en.json, keyed by (platform, translation key)."""
    with io.open(PACKAGE / "translations" / "en.json", encoding="utf-8") as handle:
        loaded = json.load(handle)
    return {
        (domain, key): body["name"]
        for domain, entries in loaded.get("entity", {}).items()
        for key, body in entries.items()
        if "name" in body
    }


def _slider_keys() -> set:
    """The picture adjustment keys, read out of number.py's own table."""
    tree = ast.parse(io.open(PACKAGE / "number.py", encoding="utf-8").read())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "IMAGE_ADJUSTMENTS"
            for target in node.targets
        ):
            continue
        return {
            ("number", element.elts[0].value)
            for element in node.value.elts
            if isinstance(element, ast.Tuple)
        }
    raise AssertionError("number.py no longer defines IMAGE_ADJUSTMENTS")


def _family() -> set:
    return _slider_keys() | SINGLETON_CONTROLS


def test_the_sliders_are_read_from_the_code_not_listed_here():
    """A guard on the guard. Were this to come back empty, every assertion
    below would hold vacuously and the family could drift apart unnoticed."""
    keys = _slider_keys()

    assert len(keys) >= 5, "found only %d picture adjustments: %s" % (
        len(keys),
        sorted(keys),
    )
    assert ("number", "image_saturation") in keys


def test_every_picture_control_carries_the_prefix():
    names = _names()

    missing = sorted(
        "%s.%s is %r" % (platform, key, names.get((platform, key)))
        for platform, key in _family()
        if not (names.get((platform, key)) or "").startswith(PREFIX)
    )

    assert not missing, (
        "these picture controls do not start with %r, so the device page sorts "
        "them away from the rest of the family: %s" % (PREFIX, ", ".join(missing))
    )


def test_no_picture_control_falls_below_the_reboot_button():
    """The failure that was actually reported, stated as its own claim."""
    names = _names()
    reboot = names[REBOOT]

    below = sorted(
        "%s (%s.%s)" % (names[(platform, key)], platform, key)
        for platform, key in _family()
        if names[(platform, key)].lower() > reboot.lower()
    )

    assert not below, (
        "the device page sorts its configuration card by name, so these sort "
        "below %r and appear under the reboot button rather than with the "
        "other picture controls: %s" % (reboot, ", ".join(below))
    )


def test_the_family_is_contiguous_once_sorted():
    """Above the reboot button is not enough on its own: anything else whose
    name begins between them would still split the family."""
    names = _names()
    family = {names[key] for key in _family()}

    ordered = sorted(names.values(), key=str.lower)
    positions = sorted(ordered.index(name) for name in family)
    intruders = [
        ordered[index]
        for index in range(positions[0], positions[-1] + 1)
        if ordered[index] not in family
    ]

    assert not intruders, (
        "these entities sort in among the picture controls and break the "
        "family up: %s" % ", ".join(intruders)
    )
