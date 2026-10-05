"""Every service the integration registers is described in services.yaml, and vice versa.

`services.yaml` is what Home Assistant shows in **Developer tools -> Actions**: the name,
the description and every field, with its selector. A service missing from it still works
if you know its exact name and payload, and is invisible to anyone who does not, which
for a UI-first integration means it effectively does not exist.

The two lists happen to agree today. Nothing kept them agreeing. Adding a
twenty-fourth service is one `async_register_entity_service` call, the tests all pass,
and the only symptom is that it never appears in the UI. That is the gap this closes.

The check runs three ways, because each direction is a different mistake:

* **registered but not described** is a service nobody can find;
* **described but not registered** is documentation promising something that raises
  `Service not found`;
* **a constant defined and never registered** is dead code, or a registration someone
  meant to add and did not.

Read out of the source rather than imported, because importing `camera.py` needs Home
Assistant, and a test that only runs under the full suite is one that gets skipped when
it matters.
"""

import ast
import pathlib

import yaml

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "dahua"
CAMERA = PACKAGE / "camera.py"
SERVICES_YAML = PACKAGE / "services.yaml"


def _described():
    """The service names services.yaml describes."""
    loaded = yaml.safe_load(SERVICES_YAML.read_text(encoding="utf-8"))
    assert (
        isinstance(loaded, dict) and loaded
    ), "services.yaml did not parse to a mapping"
    return set(loaded)


def _constants(tree):
    """Every `SERVICE_X = "name"` assignment, as {constant name: service name}.

    The whole tree, not just module level: one defined inside a function is unusual but
    not wrong, and reading only the top level turned that into a confusing failure about
    an unknown constant rather than the real question of whether it is documented.
    """
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id.startswith("SERVICE_")
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[target.id] = node.value.value
    return found


def _registered(tree, constants):
    """The service names handed to `async_register_entity_service`.

    Accepts a constant or a bare string, so a registration that skipped the constant
    is still counted rather than silently missed.
    """
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "async_register_entity_service"
        ):
            continue
        assert node.args, "a registration with no arguments at line %d" % node.lineno
        first = node.args[0]
        if isinstance(first, ast.Name):
            assert first.id in constants, (
                "registered with %s, which is not a SERVICE_ constant in this file"
                % first.id
            )
            names.add(constants[first.id])
        elif isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
        else:
            raise AssertionError(
                "a registration at line %d names its service in a way this test cannot "
                "read; widen the test rather than leaving it unchecked" % node.lineno
            )
    return names


TREE = ast.parse(CAMERA.read_text(encoding="utf-8"))
CONSTANTS = _constants(TREE)
REGISTERED = _registered(TREE, CONSTANTS)
DESCRIBED = _described()


def test_the_scan_found_the_services():
    """The guard against every comparison below holding because both sides are empty."""
    assert len(REGISTERED) > 15, "only found %d registered services" % len(REGISTERED)
    assert len(DESCRIBED) > 15, "only found %d described services" % len(DESCRIBED)


def test_every_registered_service_is_described():
    """Otherwise it never appears in Developer tools, and for a UI-first integration
    that means it effectively does not exist."""
    missing = sorted(REGISTERED - DESCRIBED)

    assert not missing, (
        "registered but absent from services.yaml, so invisible in the UI: %s" % missing
    )


def test_every_described_service_is_registered():
    """Otherwise the UI offers an action that raises `Service not found` when called."""
    extra = sorted(DESCRIBED - REGISTERED)

    assert not extra, "described in services.yaml but never registered: %s" % extra


def test_no_service_constant_is_left_unregistered():
    """A constant nobody registers is either dead or a registration someone forgot."""
    unused = sorted(
        name for constant, name in CONSTANTS.items() if name not in REGISTERED
    )

    assert not unused, "SERVICE_ constants defined but never registered: %s" % unused


def test_each_described_service_has_a_name_and_a_description():
    """Home Assistant renders both. An entry with neither is a row in the UI that says
    nothing about what it does."""
    loaded = yaml.safe_load(SERVICES_YAML.read_text(encoding="utf-8"))
    bare = sorted(
        name
        for name, body in loaded.items()
        if not isinstance(body, dict)
        or not body.get("name")
        or not body.get("description")
    )

    assert not bare, "described with no name or no description: %s" % bare


def test_every_described_field_has_a_selector():
    """A field without a selector renders as a bare text box, so a boolean becomes
    somewhere to type `true` and a choice becomes somewhere to mistype one."""
    loaded = yaml.safe_load(SERVICES_YAML.read_text(encoding="utf-8"))
    missing = []
    for service, body in loaded.items():
        for field, spec in ((body or {}).get("fields") or {}).items():
            if not isinstance(spec, dict) or "selector" not in spec:
                missing.append("%s.%s" % (service, field))

    assert not missing, "fields with no selector: %s" % sorted(missing)
