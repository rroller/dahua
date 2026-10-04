"""A test double that answers some of a platform's setup questions answers all of them.

This is the most frequent CI break on this repo, and it has now happened five
times. `async_setup_entry` asks a coordinator which capabilities a device has and
adds an entity per yes. Adding an entity adds a question. Every hand-built
stand-in that reaches that function then has to answer it, and one that does not
fails with

    AttributeError: '_SetupCoordinator' object has no attribute
                    'supports_infrared_light'

which says nothing about the change that caused it.

Greping for the double does not reliably find them, because the name is not
predictable: `test_select_entities.py` has a `_Coordinator` for entity tests *and*
a `_SetupCoordinator` subclassing it for platform tests. Checking the first and
missing the second is exactly how the fifth break happened.

So this does not look for a name. For each test file that actually calls a
platform's `async_setup_entry`, it asks which capability questions that platform's
setup puts to a coordinator, and requires every class in the file that answers
**any** of them to answer all of them.

Answering some is what identifies a class as a setup double; a class answering
none is an entity-level double and not this file's business. The scope matters:
capability names overlap between platforms -- `is_amcrest_doorbell` is asked by
both `light` and `select` -- so without restricting to the platforms a file really
sets up, every camera double looks like a broken light double.

Inheritance counts, so a subclass inherits its parent's answers.

Pure ast: no Home Assistant and no importing the modules under test, so it runs
wherever the repo does.
"""

import ast
import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "custom_components" / "dahua"
TESTS = ROOT / "tests" / "dahua"

PLATFORMS = (
    "binary_sensor",
    "button",
    "camera",
    "event",
    "light",
    "number",
    "select",
    "sensor",
    "switch",
)

# A capability question, as opposed to a plain read like `get_channel()`. Setup
# branches on these to decide whether an entity exists at all, which is why a
# missing one is an AttributeError rather than a wrong value.
CAPABILITY_PREFIXES = ("supports_", "is_", "has_", "uses_")


def _setup_capability_reads(platform):
    """Which capability questions this platform's async_setup_entry asks."""
    tree = ast.parse(io.open(PACKAGE / ("%s.py" % platform), encoding="utf-8").read())
    setup = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "async_setup_entry"
        ),
        None,
    )
    if setup is None:
        return set()
    found = set()
    for node in ast.walk(setup):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        # Only calls on the loop's coordinator, not on its client or on hass.
        if not (isinstance(target, ast.Name) and "coordinator" in target.id.lower()):
            continue
        if node.func.attr.startswith(CAPABILITY_PREFIXES):
            found.add(node.func.attr)
    return found


def _platforms_a_file_sets_up(source):
    """Which platforms' async_setup_entry this test file actually calls."""
    found = set()
    for platform in PLATFORMS:
        called = re.search(r"\b" + platform + r"(_module)?\.async_setup_entry", source)
        imported = re.search(
            r"dahua\." + platform + r" import[^\n]*async_setup_entry", source
        )
        if called or imported:
            found.add(platform)
    return found


def _answered_by_assignment(source):
    """Capabilities the file answers by assigning onto an instance.

    `test_switch.py` does `coordinator.supports_disarming_linkage = lambda: False`
    in the test body rather than on the class, which is a legitimate way to vary
    one answer per case. It is file-scoped rather than class-scoped because which
    instance an assignment lands on is not something ast can tell.
    """
    return {
        name
        for name in re.findall(r"\.(\w+)\s*=", source)
        if name.startswith(CAPABILITY_PREFIXES)
    }


def _classes_in_tests():
    """(path, platforms the file sets up, ClassDef, names assigned) per class."""
    for path in sorted(TESTS.glob("*.py")):
        source = io.open(path, encoding="utf-8").read()
        platforms = _platforms_a_file_sets_up(source)
        if not platforms:
            continue
        assigned = _answered_by_assignment(source)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ClassDef):
                yield path, platforms, node, assigned


def _methods(cls):
    return {
        member.name
        for member in cls.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _answers(path, cls, by_name, seen=None):
    """Every method this class answers, following base classes in the same file."""
    seen = seen if seen is not None else set()
    if cls.name in seen:
        return set()
    seen.add(cls.name)
    found = set(_methods(cls))
    for base in cls.bases:
        name = getattr(base, "id", None) or getattr(base, "attr", None)
        parent = by_name.get((path, name))
        if parent is not None:
            found |= _answers(path, parent, by_name, seen)
    return found


def test_every_setup_double_answers_the_whole_question_set():
    """The check that would have caught all five breaks."""
    asked = {platform: _setup_capability_reads(platform) for platform in PLATFORMS}

    # The scan really found the questions. Empty sets would pass this file
    # vacuously while proving nothing at all.
    assert asked["select"], "found no capability reads in select.async_setup_entry"
    assert asked["light"], "found no capability reads in light.async_setup_entry"

    classes = list(_classes_in_tests())
    assert classes, "found no test file calling a platform's async_setup_entry"

    by_name = {(path, cls.name): cls for path, _p, cls, _a in classes}

    incomplete = []
    for path, platforms, cls, assigned in classes:
        answers = _answers(path, cls, by_name)
        for platform in sorted(platforms):
            questions = asked[platform]
            # What the class itself answers is what identifies it as a double.
            # The file's per-test assignments only count towards completeness --
            # folding them into the overlap would make every unrelated class in
            # the file look like a double for this platform.
            if not answers & questions:
                continue
            missing = questions - answers - assigned
            if missing:
                incomplete.append(
                    "%s::%s reaches %s.async_setup_entry and does not answer %s"
                    % (path.name, cls.name, platform, sorted(missing))
                )

    assert not incomplete, "\n".join(incomplete)
