"""The picture controls are one family and sort as one, in every language.

Home Assistant's device page groups a device's entities into cards by category
and sorts them **by the name it displays**, in the language the viewer is
reading. From `frontend/src/panels/config/devices/ha-config-device-page.ts`:

    .sort((ent1, ent2) =>
      stringCompare(
        ent1.stateName || "zzz" + ent1.entity_id,
        ent2.stateName || "zzz" + ent2.entity_id,
        this.hass.locale.language
      )
    )

`stateName` is the entity's own display name; the entity id is only the
fallback for an entity that has no name at all, and `_entitiesByCategory`
groups that already-sorted list, so each card keeps that order. There is no
other ordering: no position attribute, and the viewer cannot drag them. The
name is the only lever, and a family keeps together only if its names do.

They did not. In English, sorted, the picture controls landed at B, C, G, H, I,
M, S and V with the reboot button at R in the middle of them, which left
`Saturation` and `Video Color Style` below the reboot button and away from
their own siblings on every camera and doorbell. They share an `Image ` prefix
now.

**And that is a per-language property, which is why this file reads every
translation file rather than only `en.json`.** Four days after the English fix
landed, #1173 added Spanish names for the same eight keys -- `Brillo`,
`Saturacion`, `Tono` and the rest -- and an install reading `es.json` had the
family scattered from B to T again with `Reiniciar` in the middle of it. A
guard that only knew about English passed the whole time.

The six sliders are read out of `number.py` rather than listed, so a seventh
picture adjustment added without the prefix fails here on its first run.

The exact English strings are pinned in
`test_entity_names_come_from_translations.py`, which fails on any rename at
all. What is checked here is the property that made those strings the right
ones, so an edit that renames one of them away from the family fails with the
reason rather than just the difference.
"""

import ast
import io
import json
import unicodedata
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "dahua"
TRANSLATIONS = PACKAGE / "translations"

# Every language shipped, read off disk rather than listed. A language file
# added later is covered on its first run without anyone remembering this file.
LANGUAGES = sorted(path.name for path in TRANSLATIONS.glob("*.json"))

# The controls that are not built from a table, with the platform each lives in.
SINGLETON_CONTROLS = {
    ("select", "video_color_style"),
    ("select", "exposure_anti_flicker"),
    ("number", "exposure_compensation"),
    ("select", "image_rotation"),
    ("select", "image_wide_dynamic_range"),
    ("select", "image_exposure_mode"),
    ("switch", "mirror"),
}

# What the family has to sort above. The reboot button is the one that was
# actually splitting them, and it is in the same card.
REBOOT = ("button", "reboot")

PREFIX = "Image "


def _names(filename: str) -> dict:
    """Every entity name in one translation file, by (platform, key)."""
    with io.open(TRANSLATIONS / filename, encoding="utf-8") as handle:
        loaded = json.load(handle)
    return {
        (domain, key): body["name"]
        for domain, entries in loaded.get("entity", {}).items()
        for key, body in entries.items()
        if "name" in body
    }


def _displayed(filename: str) -> dict:
    """What an install reading this language actually shows.

    English is the per-key fallback, so a language that names six of eight
    picture controls displays its own six and the English two -- and the family
    has to hold together across that mixture, not only within one file.
    """
    names = _names("en.json")
    names.update(_names(filename))
    return names


def _sort_key(name: str):
    """Approximately what `Intl.Collator(language, {numeric: true})` does.

    Not an attempt to reimplement ICU, which would be reimplementing the thing
    under test. Case and accents are folded away because what matters here is
    which letter a name begins with, and on that the two agree: a collator puts
    an accented S name with the other S names whatever it does with the accent
    itself. Names differing only in case or only in an accent could be ordered
    differently by the two, and this file does not claim otherwise.
    """
    stripped = unicodedata.normalize("NFKD", name)
    return "".join(c for c in stripped if not unicodedata.combining(c)).casefold()


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


def test_every_language_shipped_is_checked():
    """The other guard on the guard: the parametrised tests below are only
    worth anything if this list is the translations directory."""
    assert "en.json" in LANGUAGES
    assert len(LANGUAGES) >= 9, "found only %s" % LANGUAGES


def test_an_accented_initial_sorts_with_its_own_letter():
    """Why the folding is there, since no name shipped today needs it.

    A collator puts an accented letter with its base letter, so a French
    `Éclairage` sits at E. A plain `str.lower()` leaves it at U+00E9, after
    `z`, and the checks below would then report a name as sorting under the
    reboot button when the device page shows it in its proper place. The folding
    is here to keep this file from inventing a failure, not to catch one.
    """
    assert _sort_key("Éclairage") < _sort_key("Reboot")
    assert "Éclairage".lower() > "Reboot".lower()

    # And it does not collapse two different letters into one.
    assert _sort_key("Imagen") < _sort_key("Información")


def test_every_picture_control_carries_the_prefix():
    """English, where the prefix is the convention a new control follows."""
    names = _names("en.json")

    missing = sorted(
        "%s.%s is %r" % (platform, key, names.get((platform, key)))
        for platform, key in _family()
        if not (names.get((platform, key)) or "").startswith(PREFIX)
    )

    assert not missing, (
        "these picture controls do not start with %r, so the device page sorts "
        "them away from the rest of the family: %s" % (PREFIX, ", ".join(missing))
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_picture_control_falls_below_the_reboot_button(language):
    """The failure that was actually reported, asked of every language."""
    names = _displayed(language)
    reboot = names[REBOOT]

    below = sorted(
        "%s (%s.%s)" % (names[(platform, key)], platform, key)
        for platform, key in _family()
        if _sort_key(names[(platform, key)]) > _sort_key(reboot)
    )

    assert not below, (
        "the device page sorts its configuration card by the name it displays, "
        "so on an install reading %s these sort below %r and appear under the "
        "reboot button rather than with the other picture controls: %s"
        % (language, reboot, ", ".join(below))
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_family_is_contiguous_once_sorted(language):
    """Above the reboot button is not enough on its own: anything else whose
    name sorts between them would still split the family.

    Positions are taken by translation key rather than by name, because two
    entities can share a name -- es.json has two `Luz Seguridad` -- and looking
    an index up by name would silently find the wrong one.
    """
    names = _displayed(language)
    family = _family()

    ordered = sorted(names.items(), key=lambda item: _sort_key(item[1]))
    positions = [index for index, (key, _) in enumerate(ordered) if key in family]
    intruders = [
        name
        for key, name in ordered[positions[0] : positions[-1] + 1]
        if key not in family
    ]

    assert not intruders, (
        "on an install reading %s these entities sort in among the picture "
        "controls and break the family up: %s" % (language, ", ".join(intruders))
    )
