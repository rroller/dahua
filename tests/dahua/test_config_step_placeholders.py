"""A placeholder the flow does not supply renders literally on the form.

There is a guard for this on the issue side -- `test_issue_translations.py`,
whose docstring records it catching a confirm step that named `{removed}` with
nothing supplying it. There was none for the config and options flows, which
are the forms every user sees before anything else works.

Nothing behavioural can see it either: showing a form does not resolve its
text, so a step that names `{count}` with no count renders `{count}` on screen
and every test still passes.

Both halves are read from the code and the file rather than listed here, for
the reason the issue-side guard had to learn twice today: a hand-written set of
what another file does is a second source of truth, and it goes stale the
moment either side grows.

Pure `ast` and `json`, so it runs without Home Assistant.
"""

import ast
import io
import json
import pathlib
import re

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "dahua"
EN = json.loads((PACKAGE / "translations" / "en.json").read_text(encoding="utf-8"))

PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

# A step whose placeholders are built rather than written out. Nothing uses
# this yet; it exists so that the honest way to handle one is to name it here
# with a reason, not to delete the step from the scan.
BUILT_ELSEWHERE: dict = {}


def _text_of(step: dict) -> str:
    """Every string a user sees on one step, including its fields."""
    parts = [str(step.get(key, "")) for key in ("title", "description")]
    for group in ("data", "data_description"):
        parts += [str(value) for value in (step.get(group) or {}).values()]
    # A field inside a section() is looked up under the section, so its text
    # belongs to the same step.
    for section in (step.get("sections") or {}).values():
        parts += [str(section.get(key, "")) for key in ("name", "description")]
        for group in ("data", "data_description"):
            parts += [str(value) for value in (section.get(group) or {}).values()]
    return " ".join(parts)


def _used() -> dict:
    """{"config.channels": {"count", ...}} for every step whose text names one."""
    out = {}
    for flow in ("config", "options"):
        for name, step in (EN.get(flow, {}).get("step") or {}).items():
            found = set(PLACEHOLDER.findall(_text_of(step)))
            if found:
                out["%s.%s" % (flow, name)] = found
    return out


def _supplied() -> dict:
    """What each async_show_form / async_show_progress call passes, by step_id.

    A call that passes something other than a literal dict is recorded as
    supplying anything, because this file cannot know what is in it and a
    guess either way would be wrong.
    """
    tree = ast.parse((PACKAGE / "config_flow.py").read_text(encoding="utf-8"))
    out: dict = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not ast.unparse(node.func).endswith(
            ("async_show_form", "async_show_progress")
        ):
            continue
        step = None
        keys = set()
        for keyword in node.keywords:
            if keyword.arg == "step_id" and isinstance(keyword.value, ast.Constant):
                step = keyword.value.value
            if keyword.arg == "description_placeholders":
                if isinstance(keyword.value, ast.Dict):
                    keys |= {
                        key.value
                        for key in keyword.value.keys
                        if isinstance(key, ast.Constant)
                    }
                else:
                    keys.add("*anything*")
        if step is not None:
            out.setdefault(step, set()).update(keys)
    return out


def test_every_placeholder_a_step_names_is_supplied():
    """The guard. An unsupplied one is `{count}` on somebody's screen."""
    supplied = _supplied()
    unsupplied = {}
    for key, used in _used().items():
        step = key.split(".", 1)[1]
        have = supplied.get(step, set()) | BUILT_ELSEWHERE.get(key, set())
        if "*anything*" in have:
            continue
        missing = used - have
        if missing:
            unsupplied[key] = sorted(missing)

    assert not unsupplied, (
        "these steps name placeholders the flow does not pass to "
        "async_show_form, so they render literally on the form: %s.\n"
        "Supply them in description_placeholders, or if they are built "
        "somewhere this file cannot see, add the step to BUILT_ELSEWHERE with "
        "a reason." % unsupplied
    )


def test_the_scan_found_the_forms():
    """A scan that found nothing would pass the test above for ever."""
    supplied = _supplied()

    assert "user" in supplied, (
        "config_flow.py no longer shows a form for the `user` step, which means "
        "this scan has stopped seeing async_show_form rather than that the "
        "flow lost its first step: %s" % sorted(supplied)
    )


def test_the_scan_found_the_strings():
    """And the other half of it."""
    used = _used()

    assert used, "no config or options step names a placeholder at all, which is "
    "a change in en.json this file has stopped following"


def test_the_channels_step_names_what_it_skipped():
    """#577: six channels, four offered, and the only explanation was a generic
    sentence about channels that "are not listed". The one reached over ONVIF
    is now named by number, which means the step needs that placeholder and the
    flow needs to pass it -- both of which the guard above now holds."""
    description = EN["config"]["step"]["channels"]["description"]

    assert "{skipped_note}" in description
    assert "skipped_note" in _supplied()["channels"]
