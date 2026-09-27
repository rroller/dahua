"""Three things the add flow left unfinished.

**A ticked channel could fail completely silently.** The channels step spawns one
import-sourced flow per extra channel, and an import flow renders no card. Its abort
reason was one of the *error* keys, and `config.abort` had none of them, so there was
nothing to render even if it had. Tick sixteen channels, get twelve, and nothing
anywhere says which four or why.

**Reconfigure moved the channel but not the unique_id.** Two consequences, and the
second is the nastier: another entry could be added for the channel this one had
moved to, so two entries read one camera; and the channel it moved *away* from became
unaddable for good, because a fresh add computes the id this entry is still holding.

**The options form opened with its eight least useful fields.** Eight
"<platform> enabled" toggles ahead of everything somebody actually came to change.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import config_flow
from custom_components.dahua.config_flow import (OPTIONS_SECTION_PLATFORMS,
                                                 DahuaFlowHandler,
                                                 DahuaOptionsFlowHandler,
                                                 _flatten_sections,
                                                 channel_unique_id)

SERIAL = "BC0A198PAJ779DF"


# --- the id, now computed in one place ---------------------------------------

def test_channel_zero_is_the_bare_serial():
    assert channel_unique_id(SERIAL, 0) == SERIAL


def test_a_higher_channel_is_suffixed():
    assert channel_unique_id(SERIAL, 3) == SERIAL + "_3"


def test_a_string_channel_counts_the_same():
    """entry.data has carried strings here before now."""
    assert channel_unique_id(SERIAL, "3") == SERIAL + "_3"


def test_no_channel_is_channel_zero():
    assert channel_unique_id(SERIAL, None) == SERIAL


# --- a channel that cannot be added has to say so ----------------------------

class _Session:
    def __init__(self, *args, **kwargs):
        pass

    async def close(self):
        pass


def _import_flow(monkeypatch, raises=None):
    """A flow whose device refuses, and whose repair calls are captured."""
    raised = []

    class _Device:
        def __init__(self, *args, **kwargs):
            self.identity_derived_from_credentials = False

        async def get_machine_name(self):
            raise raises or Exception("no route to host")

        async def async_get_system_info(self, strict_auth=False):
            raise raises or Exception("no route to host")

    monkeypatch.setattr(config_flow, "DahuaClient", _Device)
    monkeypatch.setattr(config_flow, "ClientSession", _Session)
    monkeypatch.setattr(config_flow, "TCPConnector", lambda **kwargs: None)
    monkeypatch.setattr(config_flow.ir, "async_create_issue",
                        lambda *args, **kwargs: raised.append(kwargs))
    # The refinement probes TCP after a connection failure; not the subject here.
    async def _no_probe(address, port, timeout=None):
        return False

    monkeypatch.setattr(config_flow, "_async_probe_tcp", _no_probe)

    handler = DahuaFlowHandler()
    handler.hass = SimpleNamespace()
    return handler, raised


def _import_data(channel=7):
    return {"username": "u", "password": "p", "address": "10.0.0.5",
            "port": "80", "rtsp_port": "554", "channel": channel,
            "name": "Channel 8"}


async def test_a_channel_that_cannot_be_added_raises_a_repair(monkeypatch):
    """The whole point. Previously this path produced nothing at all."""
    handler, raised = _import_flow(monkeypatch)

    await handler.async_step_import(_import_data())

    assert len(raised) == 1


async def test_the_repair_names_the_channel_the_user_ticked(monkeypatch):
    """One higher than the index stored here, because that is the number on the
    checkbox and in the recorder's own interface."""
    handler, raised = _import_flow(monkeypatch)

    await handler.async_step_import(_import_data(channel=7))

    assert raised[0]["translation_placeholders"]["channel"] == "8"
    assert raised[0]["translation_placeholders"]["address"] == "10.0.0.5"


async def test_the_repair_carries_the_reason(monkeypatch):
    handler, raised = _import_flow(monkeypatch)

    await handler.async_step_import(_import_data())

    assert raised[0]["translation_placeholders"]["reason"]


async def test_the_repair_survives_a_restart(monkeypatch):
    """It arrives while the other channels are still being added, and a card that
    vanishes on the next restart is no use to somebody reading it afterwards."""
    handler, raised = _import_flow(monkeypatch)

    await handler.async_step_import(_import_data())

    assert raised[0]["is_persistent"] is True


async def test_the_repair_is_not_offered_as_fixable(monkeypatch):
    """What went wrong is on the device or the network. Retrying it for them would
    just fail again."""
    handler, raised = _import_flow(monkeypatch)

    await handler.async_step_import(_import_data())

    assert raised[0]["is_fixable"] is False


async def test_the_abort_reason_is_a_reason_and_not_an_error_key(monkeypatch):
    """It used to pass one of the error keys, and config.abort had none of them, so
    there was no string to render."""
    handler, _raised = _import_flow(monkeypatch)

    result = await handler.async_step_import(_import_data())

    assert result["reason"] == "channel_not_added"


def test_the_abort_reason_has_a_translation():
    import json
    import pathlib

    path = (pathlib.Path(__file__).parents[2]
            / "custom_components" / "dahua" / "translations" / "en.json")
    strings = json.loads(path.read_text(encoding="utf-8"))["config"]

    assert "channel_not_added" in strings["abort"]


def test_the_repair_has_a_translation():
    import json
    import pathlib

    path = (pathlib.Path(__file__).parents[2]
            / "custom_components" / "dahua" / "translations" / "en.json")
    issues = json.loads(path.read_text(encoding="utf-8"))["issues"]

    assert "channel_not_added" in issues
    for placeholder in ("{channel}", "{address}", "{reason}"):
        blob = issues["channel_not_added"]["title"] + \
            issues["channel_not_added"]["description"]
        assert placeholder in blob, placeholder


# --- reconfigure has to move the id with the channel -------------------------

def _reconfigure_flow(monkeypatch, entries=()):
    class _Device:
        def __init__(self, *args, **kwargs):
            self.identity_derived_from_credentials = False

        async def get_machine_name(self):
            return {"name": "Front"}

        async def async_get_system_info(self, strict_auth=False):
            return {"serialNumber": SERIAL}

        async def async_get_remote_devices(self):
            raise Exception("no table")

    monkeypatch.setattr(config_flow, "DahuaClient", _Device)
    monkeypatch.setattr(config_flow, "ClientSession", _Session)
    monkeypatch.setattr(config_flow, "TCPConnector", lambda **kwargs: None)

    entry = SimpleNamespace(
        entry_id="this", unique_id=SERIAL + "_3",
        data={"username": "u", "password": "p", "address": "10.0.0.5",
              "port": "80", "rtsp_port": "554", "channel": 3})

    seen = {}
    handler = DahuaFlowHandler()
    handler.hass = SimpleNamespace()
    handler._get_reconfigure_entry = lambda: entry
    handler._async_current_entries = lambda: [entry, *entries]

    def _abort(target, **kwargs):
        seen.update(kwargs)
        return {"type": "abort", "reason": kwargs.get("reason", "reconfigure_successful")}

    handler.async_update_reload_and_abort = _abort
    return handler, entry, seen


def _submitted(channel):
    return {"address": "10.0.0.5", "port": "80", "rtsp_port": "554",
            "channel": channel, "use_https": False}


async def test_changing_the_channel_moves_the_unique_id(monkeypatch):
    """Otherwise the entry reads channel 5 while its id still says channel 3."""
    handler, _entry, seen = _reconfigure_flow(monkeypatch)

    await handler.async_step_reconfigure(_submitted(5))

    assert seen["unique_id"] == SERIAL + "_5"


async def test_leaving_the_channel_alone_leaves_the_id_alone(monkeypatch):
    """No reason to rewrite an id that is already right."""
    handler, _entry, seen = _reconfigure_flow(monkeypatch)

    await handler.async_step_reconfigure(_submitted(3))

    assert "unique_id" not in seen


async def test_a_channel_another_entry_already_holds_is_refused(monkeypatch):
    """Two entries on one id would read one camera twice, with duplicate entities
    and nothing to tell them apart."""
    other = SimpleNamespace(entry_id="other", unique_id=SERIAL + "_5",
                            data={"address": "10.0.0.5", "channel": 5})
    handler, _entry, seen = _reconfigure_flow(monkeypatch, entries=[other])

    result = await handler.async_step_reconfigure(_submitted(5))

    assert "unique_id" not in seen, "it must not move onto an id in use"
    assert result["type"] == "form"
    assert result["errors"]["channel"] == "already_configured"


async def test_moving_to_channel_zero_uses_the_bare_serial(monkeypatch):
    handler, _entry, seen = _reconfigure_flow(monkeypatch)

    await handler.async_step_reconfigure(_submitted(0))

    assert seen["unique_id"] == SERIAL


# --- the options form --------------------------------------------------------

def _options_handler(monkeypatch):
    """config_entry is a read-only property on OptionsFlow, and __init__ reads it, so
    it is patched on the class before the handler exists."""
    entry = SimpleNamespace(data={}, options={}, entry_id="e")
    monkeypatch.setattr(DahuaOptionsFlowHandler, "config_entry",
                        property(lambda self: entry))
    handler = DahuaOptionsFlowHandler()
    # async_step_init is what sets this in the real flow, and it only forwards to
    # async_step_user, so the tests go straight to the step they are about.
    handler.options = dict(entry.options)
    return handler


async def test_the_platform_toggles_are_in_a_section(monkeypatch):
    result = await _options_handler(monkeypatch).async_step_user()

    keys = [str(marker.schema) for marker in result["data_schema"].schema]

    assert OPTIONS_SECTION_PLATFORMS in keys
    for platform in ("binary_sensor", "camera", "switch"):
        assert platform not in keys, "%s should be inside the section" % platform


async def test_the_section_is_collapsed(monkeypatch):
    """An expanded section is the nineteen field form again with extra furniture."""
    result = await _options_handler(monkeypatch).async_step_user()

    marker = next(m for m in result["data_schema"].schema
                  if str(m.schema) == OPTIONS_SECTION_PLATFORMS)

    assert result["data_schema"].schema[marker].options["collapsed"] is True


async def test_saving_stores_the_toggles_flat(monkeypatch):
    """The wiring, not the helper. Home Assistant hands a section back nested, and
    everything that reads these does entry.options.get("binary_sensor")."""
    handler = _options_handler(monkeypatch)

    async def _no_move(area_id):
        return None

    handler._async_move_device = _no_move
    handler.async_create_entry = lambda **kwargs: {"type": "create_entry"}

    await handler.async_step_user({
        OPTIONS_SECTION_PLATFORMS: {"binary_sensor": False, "camera": True},
        "scan_interval": 120,
    })

    assert handler.options["binary_sensor"] is False
    assert handler.options["camera"] is True
    assert handler.options["scan_interval"] == 120
    assert OPTIONS_SECTION_PLATFORMS not in handler.options, (
        "the nesting must not reach the stored options")


def test_a_section_is_flattened_back_to_the_stored_shape():
    """Everything that reads these does entry.options.get("binary_sensor"), so the
    nesting Home Assistant returns must not reach the stored options."""
    flat = _flatten_sections({
        OPTIONS_SECTION_PLATFORMS: {"binary_sensor": False, "camera": True},
        "scan_interval": 120,
    })

    assert flat == {"binary_sensor": False, "camera": True, "scan_interval": 120}


def test_flattening_leaves_everything_else_where_it_is():
    flat = _flatten_sections({"scan_interval": 120, "area": "Garden"})

    assert flat == {"scan_interval": 120, "area": "Garden"}


def test_only_the_declared_sections_are_lifted():
    """Flattening anything that happens to be a dict would eventually swallow an
    option whose value is legitimately one."""
    flat = _flatten_sections({"something": {"nested": 1}})

    assert flat == {"something": {"nested": 1}}


def test_the_section_has_a_translation():
    import json
    import pathlib

    path = (pathlib.Path(__file__).parents[2]
            / "custom_components" / "dahua" / "translations" / "en.json")
    step = json.loads(path.read_text(encoding="utf-8"))["options"]["step"]["user"]

    assert OPTIONS_SECTION_PLATFORMS in step.get("sections", {})
    moved = step["sections"][OPTIONS_SECTION_PLATFORMS]["data"]
    assert "binary_sensor" in moved
    assert "binary_sensor" not in step["data"], (
        "a label left at the top level renders nowhere and misleads the next reader")


def test_why_the_entry_id_check_is_belt_and_braces():
    """`_async_id_taken_by_another` also skips the entry being reconfigured, and that
    is redundant today: the caller only asks when the new id differs from the entry's
    own, so it cannot match itself. Recorded here rather than left looking
    load-bearing, and it is the thing that would still hold if that caller changed.
    """
    entry = SimpleNamespace(entry_id="this", unique_id=SERIAL + "_3", data={})
    handler = DahuaFlowHandler()
    handler._async_current_entries = lambda: [entry]

    assert handler._async_id_taken_by_another(entry, SERIAL + "_3") is False
    assert handler._async_id_taken_by_another(entry, SERIAL + "_5") is False
