"""Every per-channel setting must be in CHANNEL_OPTION_KEYS, or the merge drops it.

`CHANNEL_OPTION_KEYS` is what the #827 migration carries from an entry's options into
its channel's subentry. A per-channel option missing from it is silently reverted to
whatever was stored when the camera was added, and because `channel_option` reads the
subentry before the entry's options, the stale value then shadows the user's setting
permanently. That is the bug alpha520098 reported on #825, and it was invisible: the
migration reported success, the entry still had the option, and only the behaviour
changed.

Adding a per-channel option is a two-file change, and nothing made the second file
obvious. So the set is checked against the source: every key handed to
`channel_option` or `_channel_first` has to be a member. `events_for_channel`
applies the same precedence without taking a key argument, so `events` is asserted
on its own below.

The scan is over the package rather than named modules, for the reason
`integration_source` exists: a definition that moves must still be found.
"""

import ast

import pytest

from custom_components.dahua import const
from custom_components.dahua.const import CHANNEL_OPTION_KEYS

from .integration_source import modules

# The functions that prefer a channel's own answer to the entry's. A key read
# through any of them is per-channel by definition, which is the same definition
# the migration has to act on.
PER_CHANNEL_READERS = ("channel_option", "_channel_first")


def _constant_names_to_values():
    """CONF_X -> "x", for every string constant in const.py."""
    return {
        name: value
        for name, value in vars(const).items()
        if name.startswith("CONF_") and isinstance(value, str)
    }


def _keys_read_per_channel():
    """Every CONF_* handed to a per-channel reader, with where it was found."""
    names = _constant_names_to_values()
    found = {}
    for path, source in modules().items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            called = (
                function.attr
                if isinstance(function, ast.Attribute)
                else getattr(function, "id", None)
            )
            if called not in PER_CHANNEL_READERS or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id in names:
                found.setdefault(names[first.id], "%s:%d" % (path.name, node.lineno))
            elif isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.setdefault(first.value, "%s:%d" % (path.name, node.lineno))
    return found


def test_the_scan_finds_something():
    """A scan that matched nothing would let every other assertion here pass
    vacuously, which is how a source-reading test quietly stops testing."""
    found = _keys_read_per_channel()

    assert len(found) >= 3, found


def test_every_per_channel_key_is_carried_by_the_merge():
    found = _keys_read_per_channel()
    missing = {
        key: where for key, where in found.items() if key not in CHANNEL_OPTION_KEYS
    }

    assert not missing, (
        "these keys are read per channel but are not in CHANNEL_OPTION_KEYS, so the "
        "#827 migration drops them and the value stored when the camera was added "
        "shadows the user's setting for good: %s" % missing
    )


def test_the_configured_events_key_is_carried_too():
    """`events` does not go through `channel_option`. `events_for_channel` reads the
    subentry's `events` key directly, which is the same precedence by other means,
    and it is the key the bug was reported against."""
    assert const.CONF_EVENTS in CHANNEL_OPTION_KEYS


def test_nothing_host_wide_crept_in():
    """The set is also a promise about what is *not* carried. A host-wide key here
    would be pinned onto each channel at migration and would stop following the
    host, so the poll interval is named explicitly rather than left to judgement."""
    for key in (
        const.CONF_SCAN_INTERVAL,
        const.CONF_ADDRESS,
        const.CONF_PORT,
        const.CONF_USERNAME,
        const.CONF_PASSWORD,
        const.CONF_USE_HTTPS,
    ):
        assert key not in CHANNEL_OPTION_KEYS, key


@pytest.mark.parametrize("key", sorted(CHANNEL_OPTION_KEYS))
def test_every_carried_key_is_a_real_constant(key):
    """Guards against a typo in the set, which would fail open: a key that matches
    no real option simply never copies anything."""
    assert key in set(_constant_names_to_values().values()), key
