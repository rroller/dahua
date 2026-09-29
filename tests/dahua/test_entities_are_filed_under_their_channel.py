"""Every platform must file its entities under the channel's subentry.

Found on a live Home Assistant by adding a 16 channel recorder from scratch on
1.0.0-beta-11. The config flow created the entry and **twelve subentries**, one per
channel, correctly. Then:

    devices: 12 | with a subentry: 0
    entities: 276 | with a subentry: 0

Not one of them was filed under the channel it belongs to. No migration was involved;
this was the ordinary add path, which is what every new user takes.

The cause was that `async_add_entities` was called with the entities and nothing else.
Home Assistant needs `config_subentry_id` to know which subentry an entity belongs to,
and a device follows the entities added to it, so both ended up attached to the entry and
to no channel. Nine call sites across eight platforms, none of which passed it, and
`config_subentry_id` appeared nowhere in the integration outside `migrate.py`.

What it costs: the channels do not group under their subentry in the UI, which is the
visible shape of the whole #827 change; removing one channel's subentry leaves that
channel's device and entities behind, since nothing links them to it; and Home Assistant
warns that assigning a device across subentries "will stop working in Home Assistant
2027.8.0".

The scan below is the test that would have caught it. Asserting on one platform would
not have: this was wrong in all eight at once, and a ninth platform added tomorrow would
be wrong too. So it reads the source and requires every call to pass the argument, which
is a property of the integration rather than of any one module.
"""

import ast

import pytest

from .integration_source import modules

# What a platform's setup is handed to publish its entities. The name is the
# parameter's, and this integration spells it two ways.
ADD_CALLBACKS = ("async_add_entities", "async_add_devices")


def _add_calls():
    """Every call to an add-entities callback, as (module, line, has_subentry)."""
    found = []
    for path, source in modules().items():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", None))
            if name not in ADD_CALLBACKS:
                continue
            passed = any(kw.arg == "config_subentry_id" for kw in node.keywords)
            found.append((path.name, node.lineno, passed))
    return found


def test_the_scan_finds_the_platforms():
    """A scan that matched nothing would make the assertion below pass while saying
    nothing, which is how a source-reading test quietly stops testing. There are
    eight platforms and one of them adds cameras twice."""
    calls = _add_calls()

    assert len(calls) >= 8, calls
    assert len({name for name, _line, _passed in calls}) >= 7, sorted(
        {name for name, _line, _passed in calls})


def test_every_platform_files_its_entities_under_a_subentry():
    """The regression test. A channel of a merged recorder belongs to its subentry,
    and Home Assistant only knows that if it is told at the moment the entity is
    added."""
    missing = ["%s:%d" % (name, line)
               for name, line, passed in _add_calls() if not passed]

    assert not missing, (
        "these calls add entities without saying which channel they belong to, so "
        "they land on the entry and under no subentry: %s" % missing)


def test_the_coordinator_carries_its_subentry():
    """Where the platforms read it from. `channel_configs` already yields the
    subentry id beside each channel's config; before this it was unpacked and
    dropped, so nothing downstream could know which channel it was looking at."""
    from custom_components.dahua import DahuaDataUpdateCoordinator

    coordinator = object.__new__(DahuaDataUpdateCoordinator)

    assert coordinator.subentry_id is None, (
        "a coordinator built without one must read None rather than raise: the "
        "platforms pass this on every entity they add, and None is also what "
        "async_add_entities wants for an entry with no subentries")


def test_a_single_camera_files_under_no_subentry():
    """The other half. A single camera entry has no subentries at all, and passing
    an id it does not have would raise inside platform setup. `channel_configs`
    returns None there, which is exactly what `async_add_entities` defaults to."""
    from custom_components.dahua import channel_configs

    class _Entry:
        subentries = {}
        data = {"channel": 0, "address": "10.0.0.1", "name": "Cam"}
        options = {}
        entry_id = "e0"

    pairs = channel_configs(_Entry())

    assert len(pairs) == 1
    subentry_id, _config = pairs[0]
    assert subentry_id is None
