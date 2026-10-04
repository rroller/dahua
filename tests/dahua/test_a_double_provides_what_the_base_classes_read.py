"""A coordinator double provides what the entity's base classes read, not just its own.

The most frequent CI break on this repo, by a wide margin, and it broke three times
in one pull request:

    AttributeError: '_SetupCoordinator' object has no attribute 'supports_infrared_light'
    AttributeError: 'DahuaDataUpdateCoordinator' object has no attribute '_serial_number'
    AttributeError: '_Coordinator' object has no attribute 'data'          entity.py:66

The last one is the shape this file is for. Making an entity's
`extra_state_attributes` build on `DahuaBaseEntity`'s made it read
`coordinator.data` for the first time, and that file's hand-built double has no
`data`. Nothing in the changed entity mentions `data`, so a sweep of what the
*entity* reads cannot see it: the read arrives through a base class.

**Demanding the whole base surface of every double does not work.** Measured: 18
doubles in files naming an entity class, and *none* of them provides all nine
things the base classes read -- because most never call `device_info` or
`extra_state_attributes` at all. Requiring it would mean adding about 130 unused
methods across 18 files to satisfy a check, which is how a linter teaches people to
ignore it.

So the demand is scoped to what each file actually exercises. If a file reads
`.device_info` off an entity, its doubles need what `device_info` reads. If it only
ever reads `.available`, they need `get_address` and `last_update_success` and
nothing else. That is precise enough to have caught all three failures above and to
ask for nothing extra anywhere else.

Pure ast and regex: no Home Assistant, no importing the modules under test, so it
runs wherever the repo does -- which is the point, because the alternative is
finding out from CI.
"""

import ast
import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "custom_components" / "dahua"
TESTS = ROOT / "tests" / "dahua"

# Home Assistant's own, inherited by every entity here through CoordinatorEntity.
# Not discoverable from this repo's source, so it is named: `available` returns
# `self.coordinator.last_update_success` unless something overrides it.
INHERITED_FROM_HOME_ASSISTANT = {"available": {"last_update_success"}}

# Doubles that genuinely should not grow a member, with the reason.
EXEMPT: dict = {}


def _coordinator_reads(node) -> set:
    """What this member reads off a coordinator: calls, and the `data` attribute."""
    body = ast.unparse(node)
    reads = set(re.findall(r"_?coordinator\.(\w+)\(", body))
    reads |= set(re.findall(r"coordinator\.(data)\b", body))
    return reads


def _base_class_members() -> dict:
    """{member name: what it reads} for every member of the entity base classes."""
    found = {name: set(reads) for name, reads in INHERITED_FROM_HOME_ASSISTANT.items()}
    tree = ast.parse(io.open(PACKAGE / "entity.py", encoding="utf-8").read())
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        for member in cls.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            reads = _coordinator_reads(member)
            if reads:
                found.setdefault(member.name, set()).update(reads)
    return found


def _looks_like_a_coordinator(node) -> bool:
    """A class standing in for DahuaDataUpdateCoordinator.

    Judged on the two things every one of them answers, rather than on being named
    `_Coordinator` -- `_SetupCoordinator`, `_FakeCoordinator` and `_LightCoordinator`
    are all in use, and grepping for one name is how the first of tonight's three
    failures got through.
    """
    body = ast.unparse(node)
    return "get_serial_number" in body or "get_channel" in body


def _provides(node) -> set:
    """Everything this class answers: methods, class attributes, and `self.x = `.

    Assignments count because `data` and `last_update_success` are attributes
    rather than methods, and a double sets them in `__init__`.
    """
    found = {
        member.name
        for member in node.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    found |= {
        target.id
        for item in node.body
        if isinstance(item, ast.Assign)
        for target in item.targets
        if isinstance(target, ast.Name)
    }
    found |= set(re.findall(r"self\.(\w+)\s*=", ast.unparse(node)))
    return found


def _bases_within(node, by_name) -> set:
    """What this class inherits from another double in the same file."""
    found = set()
    for base in node.bases:
        name = getattr(base, "id", None) or getattr(base, "attr", None)
        parent = by_name.get(name)
        if parent is not None:
            found |= _provides(parent) | _bases_within(parent, by_name)
    return found


def test_the_scan_knows_what_the_base_classes_read():
    """An empty map would pass the real assertion while checking nothing."""
    members = _base_class_members()

    assert "data" in members.get("extra_state_attributes", set()), (
        "DahuaBaseEntity.extra_state_attributes reads coordinator.data; "
        "if that changed, this file's whole premise needs rereading"
    )
    assert "last_update_success" in members.get("available", set())
    assert members.get("device_info"), "device_info reads nothing?"


def test_every_double_answers_what_its_own_file_asks_of_it():
    members = _base_class_members()
    short = []

    for path in sorted(TESTS.glob("test_*.py")):
        source = io.open(path, encoding="utf-8").read()
        # Which inherited members this file really reads off an entity. Scoped to
        # the file rather than to the whole suite, which is what keeps this from
        # demanding 130 unused methods.
        exercised = {name for name in members if re.search(r"\.%s\b" % name, source)}
        if not exercised:
            continue
        wanted = set().union(*(members[name] for name in exercised))

        classes = [
            node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
        ]
        by_name = {node.name: node for node in classes}
        for node in classes:
            if not _looks_like_a_coordinator(node):
                continue
            if (path.name, node.name) in EXEMPT:
                continue
            missing = sorted(wanted - _provides(node) - _bases_within(node, by_name))
            if missing:
                short.append(
                    "%s::%s is read for %s and does not answer %s"
                    % (path.name, node.name, sorted(exercised), missing)
                )

    assert not short, (
        "a double short of what a base class reads fails as an AttributeError "
        "from inside Home Assistant, which says nothing about the change that "
        "caused it:\n  %s" % "\n  ".join(short)
    )


def test_nothing_is_exempt_without_a_reason():
    for key, reason in EXEMPT.items():
        assert len(reason) > 40, "%s is exempt with no real reason given" % (key,)
