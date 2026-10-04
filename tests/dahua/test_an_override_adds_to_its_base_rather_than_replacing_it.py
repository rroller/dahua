"""An entity that overrides a base's property builds on it, or says why not.

`DahuaBaseEntity.extra_state_attributes` supplies `id` and `integration`. #938 gave
the infrared light its own `extra_state_attributes` returning `mode` and
`brightness_level` -- a fresh dict -- so both base keys **disappeared from every
infrared light** the moment it shipped, and stayed gone through `1.0.0-rc.3`.

Nothing failed. The new keys were right, the tests asserted the new keys, the
entity worked, and the two that went missing were only ever read by a dashboard
template or a bug report. That is the shape of [[observability-fields-go-dead]]:
the feature looks finished and something quiet stops working.

CI could not catch it because no test asserted the base's keys on that entity, and
the read-vs-double sweep could not either: that inspects what an entity class reads
off the *coordinator*, and this is an entity replacing what a *base class* returns.

So it is caught by reading the source. For every entity class in the integration,
for every member it defines that a base class of its own also defines, the override
has to mention `super` -- or be listed below with a reason, because sometimes
replacing is exactly right.

Pure ast: no Home Assistant, no importing the modules under test.
"""

import ast
import io
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "dahua"

# Every module that can define an entity class. entity.py holds the base classes.
MODULES = (
    "entity",
    "binary_sensor",
    "button",
    "camera",
    "event",
    "light",
    "select",
    "sensor",
    "switch",
)

# Overrides that deliberately replace a base's value rather than adding to it.
# Each carries its reason, because the default is to add and a bare entry here
# would be the bug this file exists to catch, written down instead of fixed.
REPLACES_ON_PURPOSE = {
    (
        "DahuaEventDrivenEntity",
        "available",
    ): "Judged on whether the device has stopped answering altogether, which is "
    "the opposite of the coordinator's view: an event sensor is driven by the "
    "stream, so a slow configManager read must not take it unavailable. The "
    "docstring on it says so at length.",
}


def _classes():
    """{class name: (module, ClassDef)} for every class in those modules."""
    found = {}
    for module in MODULES:
        path = PACKAGE / ("%s.py" % module)
        if not path.exists():
            continue
        for node in ast.parse(io.open(path, encoding="utf-8").read()).body:
            if isinstance(node, ast.ClassDef):
                found[node.name] = (module, node)
    return found


def _members(cls):
    """{name: node} for the properties and methods this class defines itself."""
    return {
        member.name: member
        for member in cls.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _returns_a_mapping(node) -> bool:
    """Whether this member's value is a dict the caller reads keys out of.

    Only these matter. Every entity replaces `unique_id` with its own id and that
    is the whole point of it -- replacing a scalar loses nothing. Replacing a
    *mapping* silently drops whatever keys the base put in it, which is the bug.
    """
    for inner in ast.walk(node):
        if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Dict):
            return True
        # `attrs = {...}` then `return attrs`, which is how the base builds
        # device_info before adding suggested_area to it.
        if isinstance(inner, ast.Assign) and isinstance(inner.value, ast.Dict):
            return True
    return False


def _base_names(cls):
    for base in cls.bases:
        name = getattr(base, "id", None) or getattr(base, "attr", None)
        if name:
            yield name


def _inherited(cls, classes, seen=None):
    """{member name: class that defines it} from this class's own bases only.

    Bases outside the integration are not followed: Home Assistant's own
    `CoordinatorEntity.available` is a different question, and an override of it
    that does not call super is how `DahuaEventDrivenEntity` is meant to work.
    """
    seen = seen if seen is not None else set()
    found = {}
    for name in _base_names(cls):
        if name in seen or name not in classes:
            continue
        seen.add(name)
        _module, base = classes[name]
        for member in _members(base):
            found.setdefault(member, name)
        for member, owner in _inherited(base, classes, seen).items():
            found.setdefault(member, owner)
    return found


def test_the_scan_finds_the_classes_it_is_about():
    """An empty scan would pass every assertion below while proving nothing."""
    classes = _classes()

    assert "DahuaBaseEntity" in classes
    assert "extra_state_attributes" in _members(classes["DahuaBaseEntity"][1])
    subclasses = [
        name
        for name, (_module, cls) in classes.items()
        if "DahuaBaseEntity" in _base_names(cls)
    ]
    assert len(subclasses) > 20, "found only %d DahuaBaseEntity subclasses" % len(
        subclasses
    )


def test_every_override_of_a_base_member_builds_on_it():
    classes = _classes()
    replacing = []

    for name, (module, cls) in sorted(classes.items()):
        inherited = _inherited(cls, classes)
        for member, node in sorted(_members(cls).items()):
            if member not in inherited:
                continue
            base_module, base_cls = classes[inherited[member]]
            if not _returns_a_mapping(_members(base_cls)[member]):
                # Replacing a scalar is normal and lossless. See
                # _returns_a_mapping.
                continue
            if member.startswith("__"):
                # __init__ chaining is its own convention and every one of these
                # already calls super; not this file's subject.
                continue
            if (name, member) in REPLACES_ON_PURPOSE:
                continue
            body = ast.unparse(node)
            if "super" in body:
                continue
            replacing.append(
                "%s.%s.%s replaces %s.%s without building on it"
                % (module, name, member, inherited[member], member)
            )

    assert not replacing, (
        "an override that returns a fresh value drops whatever the base supplied. "
        "Call super, or add it to REPLACES_ON_PURPOSE with the reason:\n  %s"
        % "\n  ".join(replacing)
    )


def test_nothing_is_excused_without_a_reason():
    """The escape hatch has to cost a sentence, or it becomes the default."""
    for (name, member), reason in REPLACES_ON_PURPOSE.items():
        assert len(reason) > 40, "%s.%s is excused with no real reason given" % (
            name,
            member,
        )


def test_the_excused_overrides_still_exist():
    """A stale entry silently stops protecting anything, and reads as coverage."""
    classes = _classes()
    for name, member in REPLACES_ON_PURPOSE:
        assert name in classes, "%s is listed and no longer exists" % name
        assert member in _members(classes[name][1]), "%s no longer overrides %s" % (
            name,
            member,
        )
