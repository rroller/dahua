"""`device_registry.async_get_device` is deprecated and has a deadline.

Reported by @kmich against #1108, who saw Home Assistant warn about it at every
start. The warning is not advisory:

    report_usage(
        "calls `device_registry.async_get_device`, which is deprecated because "
        "device identifiers and connections are no longer unique across config "
        "entries; use `async_get_device_by_identifier`, "
        "`async_get_device_by_connection` or `async_get_devices` instead",
        core_behavior=ReportBehavior.ERROR,
        breaks_in_ha_version="2027.8.0",
    )

He found one call. There were four: `migrate.py`, `__init__.py` and two in
`config_flow.py`.

Three of them mean "the device of this config entry", which is
`async_get_device_by_identifier` and is also the more accurate answer: the
deprecated call resolves a shared identifier by preferring a matching domain
and then taking the first match, so with two entries for one recorder it could
hand back the other entry's device.

The fourth, `migrate.py`'s preflight, really is asking about every entry: it
checks that a serial is not already somebody else's before writing it. That one
takes `async_get_devices` and keeps the behaviour it had, because narrowing it
would have quietly dropped a safety check rather than modernised it.
"""

import pathlib

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dahua import device_by_identifier, devices_with_identifier

DOMAIN = "dahua"
SERIAL = "3E0164EPAN00095"
SOURCE = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "dahua"


async def _entry(hass, address):
    entry = MockConfigEntry(domain=DOMAIN, data={"address": address})
    entry.add_to_hass(hass)
    return entry


# --- what each helper answers -------------------------------------------------


async def test_it_finds_this_entry_s_device(hass):
    entry = await _entry(hass, "10.0.0.230")
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, SERIAL)}
    )

    found = device_by_identifier(registry, (DOMAIN, SERIAL), entry.entry_id)

    assert found is not None and found.id == device.id


async def test_it_does_not_hand_back_another_entry_s_device(hass):
    """The improvement, not just the rename. Two entries for one recorder is
    ordinary here, and the deprecated call resolved the ambiguity by taking a
    first match, which could be the wrong one. The area and the labels are then
    applied to a device the user was not configuring."""
    mine = await _entry(hass, "10.0.0.230")
    theirs = await _entry(hass, "10.0.0.231")
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=theirs.entry_id, identifiers={(DOMAIN, SERIAL)}
    )

    assert device_by_identifier(registry, (DOMAIN, SERIAL), mine.entry_id) is None


async def test_nothing_holding_it_is_not_an_error(hass):
    entry = await _entry(hass, "10.0.0.230")

    assert (
        device_by_identifier(dr.async_get(hass), (DOMAIN, SERIAL), entry.entry_id)
        is None
    )


async def test_every_holder_is_returned_whichever_entry_owns_it(hass):
    """What `migrate.py` needs. A serial about to be written onto this entry's
    device is a conflict if anybody holds it, and only the cross entry question
    can say so."""
    mine = await _entry(hass, "10.0.0.230")
    theirs = await _entry(hass, "10.0.0.231")
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=theirs.entry_id, identifiers={(DOMAIN, SERIAL)}
    )

    held = devices_with_identifier(registry, (DOMAIN, SERIAL))

    assert [d.id for d in held] != []
    assert device_by_identifier(registry, (DOMAIN, SERIAL), mine.entry_id) is None


async def test_nobody_holding_it_is_an_empty_list(hass):
    await _entry(hass, "10.0.0.230")

    assert devices_with_identifier(dr.async_get(hass), (DOMAIN, SERIAL)) == []


# --- the fallback, for the oldest Home Assistant this integration supports ----


class _OldRegistry:
    """A registry with only the deprecated call, as `hacs.json`'s 2026.8.0
    minimum may have. Neither helper may require a method that is not there."""

    def __init__(self, answer=None):
        self.answer = answer
        self.asked = []

    def async_get_device(self, identifiers=None, connections=None):
        self.asked.append(identifiers)
        return self.answer


def test_the_old_api_is_used_when_the_new_one_is_absent():
    old = _OldRegistry(answer="a device")

    assert device_by_identifier(old, (DOMAIN, SERIAL), "entry") == "a device"
    assert old.asked == [{(DOMAIN, SERIAL)}]


def test_the_list_helper_falls_back_too():
    old = _OldRegistry(answer="a device")

    assert devices_with_identifier(old, (DOMAIN, SERIAL)) == ["a device"]


def test_the_fallback_reports_nothing_as_an_empty_list():
    assert devices_with_identifier(_OldRegistry(answer=None), (DOMAIN, SERIAL)) == []


# --- and no call site is left behind ------------------------------------------


def test_only_the_two_fallbacks_still_call_the_deprecated_method():
    """The thing that actually breaks in 2027.8 is a call site, so this counts
    them rather than trusting that four were all of them.

    Two are left on purpose, one inside each helper, for a Home Assistant older
    than the replacement. Everywhere else must go through a helper.

    Scanned across the package rather than in the files that were changed:
    @kmich found one of the four by reading a log, and a fifth added later
    would fail here instead of in somebody's Home Assistant.
    """
    found = []
    for path in sorted(SOURCE.glob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "async_get_device(" in line:
                found.append("{0}:{1}".format(path.name, number))

    assert found, "the scan matched nothing at all, so it proves nothing"
    assert len(found) == 2, "expected one fallback per helper, found " + ", ".join(
        found
    )
    assert {entry.split(":")[0] for entry in found} == {
        "__init__.py"
    }, "a deprecated registry lookup outside the fallbacks: " + ", ".join(found)


@pytest.mark.parametrize("name", ["device_by_identifier", "devices_with_identifier"])
def test_both_helpers_are_importable_from_the_package(name):
    """Platforms, the config flow and migrate reach helpers through the package,
    and the re-export block is where AGENTS.md says they belong."""
    import custom_components.dahua as dahua

    assert callable(getattr(dahua, name))
