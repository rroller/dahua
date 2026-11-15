"""`async_add_and_keep_watching` awaits `build`, so `build` has to be async.

Reported by @defre92 on #1176, on 3.1.0-rc.3, against every Dahua entry he has
including the cameras that otherwise work:

    Failed to set up the dahua time platform
    late.py line 509: for entity in await build(coordinator)
    TypeError: 'list' object can't be awaited

`time.py` arrived with #1196 and is the one platform whose `build` is a plain
`def`. The other nine are `async def`, and `late.py`'s own module docstring
gives `async def build(coordinator)` as the shape a platform adopts. Awaiting a
list raises whatever the list holds, so this is not conditional on the
capability: `supports_profile_schedule()` being False returns `[]` and `await
[]` raises just the same. Every entry loses the whole time platform.

It shipped because nothing drove `time.async_setup_entry`.
`test_time_entities.py` builds the entities with `object.__new__` and exercises
them directly, which is the right test for what they do and cannot reach the
platform's entry point. So there are two tests here: the entry point, and a
scan that holds the contract for the next platform to adopt `late.py`.

Reintroduced against the real source, with the tests that failed:

| reintroduced | caught by |
| --- | --- |
| `async def build` back to `def build` in time.py | both of them |
"""

import ast
import importlib

import pytest

from custom_components.dahua import late
from custom_components.dahua.const import PLATFORMS

from . import adds_entities
from .integration_source import modules

# `from custom_components.dahua import time` hands back the **standard library**
# `time`: `__init__.py` imports it, which binds that name as an attribute of the
# package and shadows the submodule of the same name. Nothing warns, and the
# failure is an AttributeError about `async_setup_entry` rather than about the
# import, so it reads like the platform is missing a function.
time_module = importlib.import_module("custom_components.dahua.time")


class _Coordinator:
    """The least a platform's build needs to decide and to file its entities."""

    subentry_id = None

    def __init__(self, profile_schedule=True):
        self._profile_schedule = profile_schedule

    def supports_profile_schedule(self):
        return self._profile_schedule

    def get_serial_number(self):
        return "SERIAL1"

    def get_device_name(self):
        return "Front Door"

    def get_address(self):
        return "10.0.0.1"


class _Entry:
    entry_id = "entry-1"

    def __init__(self, coordinator):
        self.runtime_data = {0: coordinator}
        self.unloads = []

    def async_on_unload(self, callback):
        self.unloads.append(callback)


class _Hass:
    def __init__(self):
        self.data = {}

    def async_create_task(self, coro):
        coro.close()


@pytest.fixture
def _dispatcher(monkeypatch):
    """`late.py` subscribes to the capability signal, which wants a real hass.

    Patched on the module rather than at the source, because `late.py` binds
    the name at import.
    """
    monkeypatch.setattr(
        late, "async_dispatcher_connect", lambda hass, signal, cb: (lambda: None)
    )
    monkeypatch.setattr(late, "async_dispatcher_send", lambda hass, signal, *a: None)


def _hass_and_entry(coordinator):
    return _Hass(), _Entry(coordinator)


# --- the entry point, which is what went unrun -------------------------------


async def test_the_time_platform_sets_up(_dispatcher):
    """@defre92's traceback. The platform is reached on every entry, so this
    failing is the whole platform missing rather than two entities missing."""
    added = []
    hass, entry = _hass_and_entry(_Coordinator(profile_schedule=True))

    await time_module.async_setup_entry(hass, entry, adds_entities(added))

    assert [type(entity).__name__ for entity in added] == [
        "DahuaProfileDayStartTime",
        "DahuaProfileNightStartTime",
    ]


async def test_the_time_platform_sets_up_where_there_is_no_schedule(_dispatcher):
    """The empty return is the half that proves the bug is not about the
    capability: `await []` raises exactly as `await [entity]` does, so a camera
    with no profile schedule lost the platform too."""
    added = []
    hass, entry = _hass_and_entry(_Coordinator(profile_schedule=False))

    await time_module.async_setup_entry(hass, entry, adds_entities(added))

    assert added == []


# --- and the contract, for the next platform to adopt late.py ----------------


def test_every_platform_build_handed_to_late_is_async():
    """`late.py` awaits what it is given. Nine platforms got this right and the
    tenth did not, which is a contract worth asserting rather than a thing to
    remember."""
    offenders = []
    for path, text in modules().items():
        if path.stem not in PLATFORMS:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "async_add_and_keep_watching":
                continue
            # The third positional argument is `build`.
            if len(node.args) < 3 or not isinstance(node.args[2], ast.Name):
                continue
            wanted = node.args[2].id
            for candidate in ast.walk(tree):
                if isinstance(candidate, ast.FunctionDef) and candidate.name == wanted:
                    offenders.append("%s: %s is not async" % (path.name, wanted))

    assert offenders == []
