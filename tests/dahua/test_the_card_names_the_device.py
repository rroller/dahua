"""The card that asks for attention has to name the device, not an error about an error.

`config.flow_title` is `"{name} ({address})"`, and Home Assistant renders it on the card
that appears in the integrations list whenever a flow wants something. It is filled from
`context["title_placeholders"]`.

Only the DHCP discovery step was setting that. A flow started from an **existing entry**
therefore rendered the title with nothing to substitute, and the frontend showed

    Translation [formatjs Error: MISSING_VALUE ...]

where the device name belongs. Reported from a live system after a recorder briefly
refused credentials: the card offering to fix it could not say which device it was about.

That is the worst possible moment for it. The cameras have just stopped, the user is
being asked to re-enter a password, and the only label on the card is an error about an
error. It also gives them nothing to tell the difference between two Dahua devices.

The two steps that need it are the two entered from an entry that already exists,
`async_step_reauth` and `async_step_reconfigure`. `async_step_user` deliberately does
not: there is no entry yet, and Home Assistant shows the integration name for a flow the
user started themselves.
"""

import ast
import json
import pathlib
import re

import pytest

from custom_components.dahua.config_flow import DahuaFlowHandler

PACKAGE = (pathlib.Path(__file__).resolve().parents[2]
           / "custom_components" / "dahua")
FLOW_TITLE = json.loads(
    (PACKAGE / "translations" / "en.json").read_text(encoding="utf-8")
)["config"]["flow_title"]


def _handler():
    handler = object.__new__(DahuaFlowHandler)
    handler.context = {}
    return handler


def _entry(title="Gerty New", address="10.0.0.5"):
    from types import SimpleNamespace
    return SimpleNamespace(title=title, data={"address": address})


# --- what the card ends up saying -------------------------------------------


def test_the_entry_title_and_address_reach_the_card():
    handler = _handler()

    handler._set_flow_title(_entry())

    assert handler.context["title_placeholders"] == {
        "name": "Gerty New", "address": "10.0.0.5"}


def test_an_entry_with_no_title_falls_back_to_its_address():
    """Better than an empty pair of brackets, and still identifies the device."""
    handler = _handler()

    handler._set_flow_title(_entry(title=""))

    assert handler.context["title_placeholders"]["name"] == "10.0.0.5"


def test_an_entry_with_neither_still_names_the_integration():
    """Never leaves a placeholder unfilled. A vague card beats a broken one."""
    handler = _handler()

    handler._set_flow_title(_entry(title=None, address=None))

    placeholders = handler.context["title_placeholders"]
    assert placeholders["name"] == "Dahua"
    assert placeholders["address"] == ""


def test_no_entry_at_all_does_not_raise():
    """`async_get_entry` returns None if the entry went away between the failure and
    the flow starting, and a teardown path must not raise into the flow."""
    handler = _handler()

    handler._set_flow_title(None)

    assert set(handler.context["title_placeholders"]) == {"name", "address"}


# --- the guards, which are the point ----------------------------------------


def test_every_placeholder_the_title_needs_is_supplied():
    """Computed from `en.json`, not hardcoded. Adding `{port}` to `flow_title` without
    supplying it is exactly the bug this file exists for, and this is what makes that a
    red test rather than a card nobody can read.
    """
    needed = set(re.findall(r"\{(\w+)\}", FLOW_TITLE))
    assert needed, "flow_title has no placeholders, so this test is checking nothing"

    handler = _handler()
    handler._set_flow_title(_entry())
    supplied = set(handler.context["title_placeholders"])

    assert needed <= supplied, (
        "flow_title needs %s and the flow supplies %s"
        % (sorted(needed), sorted(supplied)))


STEPS_FROM_AN_EXISTING_ENTRY = ("async_step_reauth", "async_step_reconfigure")


@pytest.mark.parametrize("step", STEPS_FROM_AN_EXISTING_ENTRY)
def test_the_steps_started_from_an_entry_set_the_title(step):
    """A source scan, because the alternative is driving each flow end to end for a
    line that has no other observable effect. A new step entered from an existing entry
    that forgets this reproduces the bug exactly, and this is what notices."""
    source = (PACKAGE / "config_flow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "DahuaFlowHandler")
    method = next(m for m in handler.body
                  if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and m.name == step)

    body = ast.unparse(method)
    assert "_set_flow_title" in body or "title_placeholders" in body, (
        "%s renders flow_title with nothing to substitute" % step)


def test_the_scan_would_notice_a_step_that_did_not():
    """The guard on the guard. `async_step_user` genuinely does not set a title,
    because there is no entry yet, so it proves the scan can return False."""
    source = (PACKAGE / "config_flow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "DahuaFlowHandler")
    method = next(m for m in handler.body
                  if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and m.name == "async_step_user")

    body = ast.unparse(method)
    assert "_set_flow_title" not in body and "title_placeholders" not in body, (
        "async_step_user now sets a title, so this control no longer proves anything")
