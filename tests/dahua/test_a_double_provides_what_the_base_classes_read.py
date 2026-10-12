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


# The attribute a double is assigned to when it is handed to an entity.
_COORDINATOR_ATTRS = {"coordinator", "_coordinator"}


def _namespace_doubles(tree) -> list:
    """Every coordinator double built with SimpleNamespace, as (label, provided).

    The class scan below cannot see these: `SimpleNamespace(get_model=...)` has
    no class body to read members from, and it is the commoner idiom in this
    suite. So this guard passed while test_device_page_link.py's double was
    short of what `device_info` had just started reading, and the failure
    arrived from CI as an AttributeError instead -- the exact thing this file
    exists to prevent, missed for a shape rather than for a reason.

    **Judged on where the namespace is handed, not on what it answers.** A
    namespace that happens to answer `get_channel` may be standing in for an
    argument to an ordinary function, as `_reporter_candidate` in
    test_the_recorders_load.py does, and nothing inherited is ever read off
    that. What matters is being given to an entity as its coordinator, which is
    one of two shapes:

        entity._coordinator = SimpleNamespace(...)
        DahuaSomethingEntity(SimpleNamespace(...), entry)
    """
    found = []
    for node in ast.walk(tree):
        namespace = None

        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Attribute) and target.attr in _COORDINATOR_ATTRS
            for target in node.targets
        ):
            namespace = node.value

        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name and name.startswith("Dahua") and node.args:
                namespace = node.args[0]

        if not isinstance(namespace, ast.Call):
            continue
        built = getattr(namespace.func, "id", None) or getattr(
            namespace.func, "attr", None
        )
        if built != "SimpleNamespace":
            continue
        provided = {keyword.arg for keyword in namespace.keywords if keyword.arg}
        found.append(("SimpleNamespace at line %d" % namespace.lineno, provided))
    return found


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

        tree = ast.parse(source)
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        by_name = {node.name: node for node in classes}

        for label, provided in _namespace_doubles(tree):
            if (path.name, label) in EXEMPT:
                continue
            missing = sorted(wanted - provided)
            if missing:
                short.append(
                    "%s::%s is read for %s and does not answer %s"
                    % (path.name, label, sorted(exercised), missing)
                )

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


def test_the_namespace_scan_really_finds_one():
    """The guard on the guard.

    `test_device_page_link.py` builds its coordinator with SimpleNamespace and
    reads `device_info` off an entity, which is the shape that got past this
    file once. If the scan stops finding it, this file goes back to passing for
    the wrong reason and nothing says so.
    """
    source = io.open(TESTS / "test_device_page_link.py", encoding="utf-8").read()
    tree = ast.parse(source)

    found = _namespace_doubles(tree)

    assert found, "the SimpleNamespace scan found no double in a file that has one"
    provided = set().union(*(names for _label, names in found))
    assert "get_serial_number" in provided
    # Named from what device_info reads today rather than from a member that
    # happens to exist: if the scan finds a namespace but not the one standing
    # in for a coordinator, this is the assertion that notices.
    assert "get_model" in provided, provided


# --- and what the namespace scan does and does not count --------------------
#
# Written because a mutation showed the constructor half of the rule was not
# exercised by any file in the suite, and an untested branch in a guard is worse
# than no branch at all. These pin the rule itself rather than relying on some
# file continuing to be written a particular way.


def _scan(source):
    return {
        name
        for _label, provided in _namespace_doubles(ast.parse(source))
        for name in provided
    }


def test_a_namespace_assigned_as_a_coordinator_is_counted():
    assert _scan("entity._coordinator = SimpleNamespace(get_model=lambda: 'x')") == {
        "get_model"
    }


def test_a_namespace_handed_to_an_entity_constructor_is_counted():
    """`DahuaSomething(SimpleNamespace(...), entry)` is the other way a double
    reaches an entity, and nothing in the suite is written that way today."""
    assert _scan("sensor = DahuaMemoryUsedSensor(SimpleNamespace(data={}), entry)") == {
        "data"
    }


def test_a_namespace_that_is_not_a_coordinator_is_ignored():
    """The false positive the first version of this had. `_reporter_candidate`
    in test_the_recorders_load.py answers `get_channel` and is a stand-in for an
    argument to an ordinary function: nothing inherited is ever read off it, so
    demanding `data` of it would be asking for a member it has no use for.

    Judged on where a namespace is handed, never on what it answers.
    """
    source = """
def candidate():
    return SimpleNamespace(get_channel=lambda: 0, get_serial_number=lambda: "x")
"""

    assert _scan(source) == set()


def test_nothing_is_exempt_without_a_reason():
    for key, reason in EXEMPT.items():
        assert len(reason) > 40, "%s is exempt with no real reason given" % (key,)
