"""Runtime state lives on the config entry, not in hass.data.

`runtime-data` is a Bronze rule on Home Assistant's integration quality scale:
"Use ConfigEntry.runtime_data to store runtime data". This integration kept its
coordinator in `hass.data[DOMAIN][entry.entry_id]`, and ten places knew that
shape, including two that only wanted to know whether the entry was loaded.

`runtime_data` is better than a dict keyed by entry id for a reason beyond the
rule: Home Assistant deletes the attribute itself when an entry unloads, so there
is nothing to pop and no way to leave a stale coordinator behind for a reload to
find.

It holds a mapping of channel to coordinator rather than a bare coordinator. One
member today, because a recorder is one config entry per channel, which is why
removing a 64 channel NVR takes 64 deletions (#827). Making an entry able to own
several is the change that fixes that, and every platform asking "what is this
entry's coordinator?" was the obstacle.
"""

import re
from pathlib import Path

import pytest

from custom_components.dahua import (
    DahuaConfigEntry,
    entry_coordinator,
    entry_coordinators,
)

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "dahua"

# Distinguishes "no runtime_data attribute" from "runtime_data is None".
_UNSET = object()


class _Entry:
    """A config entry, with runtime_data only when it is loaded.

    Deliberately not a mock: the behaviour under test is what happens when the
    attribute is *absent*, which is what an unloaded entry looks like after Home
    Assistant deletes it, and a mock would invent one.
    """

    entry_id = "entry-1"

    def __init__(self, runtime_data=_UNSET):
        # A sentinel, not None. `_Entry(None)` has to mean "the attribute exists
        # and is None", which is a different state from "no attribute at all" --
        # and with a None default the two were the same object, so the falsy case
        # below was passing without testing anything. A mutation that replaced
        # `or {}` with a getattr default went unnoticed until that was fixed.
        if runtime_data is not _UNSET:
            self.runtime_data = runtime_data


# --- an unloaded entry, which is the case that used to raise ----------------

def test_an_unloaded_entry_has_no_channels():
    """Home Assistant deletes runtime_data on unload, so the attribute is gone
    rather than empty. Callers during teardown must not get an AttributeError."""
    assert entry_coordinators(_Entry()) == {}


def test_asking_for_the_one_coordinator_of_an_unloaded_entry_raises():
    """Platforms are only set up for an entry whose runtime data is in place, so
    absence there is a bug worth hearing about rather than a None to propagate
    into an entity constructor."""
    with pytest.raises(KeyError, match="entry-1"):
        entry_coordinator(_Entry())


def test_runtime_data_set_to_nothing_reads_as_no_channels():
    """`or {}` rather than a plain getattr default, so a falsy value left behind
    by a failed setup behaves like absence."""
    assert entry_coordinators(_Entry({})) == {}
    assert entry_coordinators(_Entry(None)) == {}


# --- a loaded entry ---------------------------------------------------------

def test_a_loaded_entry_yields_its_coordinator():
    entry = _Entry({0: "channel-0"})

    assert entry_coordinators(entry) == {0: "channel-0"}
    assert entry_coordinator(entry) == "channel-0"


def test_an_entry_can_own_several_channels():
    """The shape #827 needs. Keyed by channel index, and the keys are the channel
    numbers rather than positions, so channel 9 is 9 and not 2."""
    entry = _Entry({0: "ch0", 1: "ch1", 9: "ch9"})

    assert list(entry_coordinators(entry)) == [0, 1, 9]
    assert entry_coordinators(entry)[9] == "ch9"


def test_the_single_accessor_is_the_first_channel():
    """Well defined only while an entry owns one channel. Named so that #827 has
    one place to revisit instead of nine inlined `next(iter(...))` calls."""
    entry = _Entry({0: "ch0"})

    assert entry_coordinator(entry) == "ch0"


# --- the typed entry -------------------------------------------------------

def test_the_typed_entry_alias_resolves():
    """A PEP 695 alias is evaluated lazily, which is what lets it name
    DahuaDataUpdateCoordinator before that class is defined. Touching __value__
    is what proves the forward reference is real rather than a string nobody ever
    resolves."""
    value = str(DahuaConfigEntry.__value__)

    assert "ConfigEntry" in value
    assert "DahuaDataUpdateCoordinator" in value
    # Keyed by channel index, which is what lets one entry own several.
    assert "dict[int" in value.replace(" ", "")


# --- and the rule itself, so it cannot quietly come back -------------------

def test_no_module_stores_runtime_state_in_hass_data_under_the_domain():
    """The Bronze rule, asserted rather than trusted.

    `hass.data[DOMAIN]` is gone entirely. `flow_preview` still uses hass.data,
    correctly and under its own key, because a config *flow* has no entry yet and
    so has no runtime_data to put a preview on. That is why this looks for the
    domain keyed access specifically and not for hass.data at all.
    """
    pattern = re.compile(
        r"hass\.data(?:\.get\(|\.setdefault\(|\[)\s*DOMAIN")

    offenders = []
    for path in sorted(PACKAGE.glob("*.py")):
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line):
                offenders.append("%s:%d %s" % (path.name, number, line.strip()))

    assert not offenders, "runtime state belongs on the entry:\n  " + "\n  ".join(
        offenders)
