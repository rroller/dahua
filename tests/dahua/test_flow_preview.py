"""The naming step should show what the channel is actually looking at.

A channel number is the one field in this flow that a user cannot check. The recorder
numbers its channels from one and this integration indexes from zero, so a sixteen
channel box gives sixteen chances to be off by one, and the add succeeds either way:
credentials are right, the connection is right, and the entry that appears is simply
the wrong camera. #583 and #647 both begin there.

So the last step before the entry exists shows a still from the channel. It catches a
wrong number by looking, and it is also what tells somebody what to call the thing --
which is why it goes on the step that asks for the name rather than on a step of its
own.

**Why not `async_show_form(preview=...)`.** Measured on 2026.9.3 with frontend
20260826.7: the frontend picks the preview component from a two-item allowlist
(`["generic_camera", "template"]`, anything else falls back to an entity state row) and
builds the websocket command name from that same string. The only value that renders an
*image* is `generic_camera`, which would mean registering `generic_camera/start_preview`
-- the built-in `generic` integration's command -- over the top of it, with which one
wins decided by whichever flow was opened first. `flow_preview.py`'s docstring has the
extracted code. So the picture arrives through the step description, which is rendered
as markdown and whose sanitiser whitelists `img`.

**What the tests here are really for.** Four previous changes in this area passed with
a mutation alive because the test drove the helper and not the caller: the helper built
the right thing and nothing checked that the form carried it. So the wiring tests below
call the real `_show_config_form_name` and assert on the form result, and
`test_every_placeholder_the_description_uses_is_supplied` derives the expected keys
from `en.json` so that either side going missing fails.
"""

import asyncio
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from custom_components.dahua import config_flow as flow_module
from custom_components.dahua import flow_preview
from custom_components.dahua.config_flow import DahuaFlowHandler
from custom_components.dahua.const import (
    CONF_ADDRESS,
    CONF_CHANNEL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_RTSP_PORT,
    CONF_USERNAME,
    CONF_USE_HTTPS,
)
from custom_components.dahua.flow_preview import (
    DATA_FLOW_PREVIEWS,
    MAX_STORED_PREVIEWS,
    DahuaFlowPreviewView,
    async_drop_preview,
    async_store_preview,
    preview_url,
)

IMAGE = b"\xff\xd8\xffthis is a jpeg"

EN = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "dahua"
    / "translations"
    / "en.json"
)


# --- fixtures ----------------------------------------------------------------


def _hass():
    registered = []
    return SimpleNamespace(
        data={},
        http=SimpleNamespace(register_view=registered.append),
        _registered=registered,
    )


def _flow(hass=None, channel=3, use_https=None):
    handler = DahuaFlowHandler()
    handler.hass = hass or _hass()
    handler._errors = {}
    handler.init_info = {
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "pw",
        CONF_ADDRESS: "10.0.0.5",
        CONF_PORT: "80",
        CONF_RTSP_PORT: "554",
        CONF_CHANNEL: channel,
        CONF_NAME: "Driveway",
    }
    if use_https is not None:
        handler.init_info[CONF_USE_HTTPS] = use_https
    return handler


def _stub_fetch(monkeypatch, image=IMAGE):
    """Replace the device call, and record what the flow asked it for."""
    calls = []

    async def _fetch(*args, **kwargs):
        calls.append(args)
        return image

    monkeypatch.setattr(flow_module, "async_fetch_preview", _fetch)
    return calls


# --- the form has to carry it ------------------------------------------------


async def test_the_name_form_carries_the_picture(monkeypatch):
    """The whole point. A helper that builds the right markdown and a form that does
    not pass it is the failure this file exists to catch."""
    _stub_fetch(monkeypatch)
    handler = _flow()

    result = await handler._show_config_form_name(handler.init_info)

    preview = result["description_placeholders"]["preview"]
    assert preview.startswith("!["), preview
    assert flow_preview.PREVIEW_PATH in preview


async def test_the_form_still_asks_for_a_name_alongside_the_picture():
    """The picture is decoration on a form that has a job to do."""
    handler = _flow()
    handler._preview_markdown = ""

    result = await handler._show_config_form_name(handler.init_info)

    assert [str(key) for key in result["data_schema"].schema] == [CONF_NAME]
    assert result["step_id"] == "name"


async def test_a_device_that_gives_no_picture_still_gets_a_form(monkeypatch):
    """A doorbell, an ONVIF channel on a recorder, a camera refusing under load. The
    add must go through unchanged, with the description reading as prose."""
    _stub_fetch(monkeypatch, image=None)
    handler = _flow()

    result = await handler._show_config_form_name(handler.init_info)

    assert result["description_placeholders"] == {"preview": ""}
    assert [str(key) for key in result["data_schema"].schema] == [CONF_NAME]


async def test_nothing_is_stored_when_there_is_no_picture(monkeypatch):
    """An empty placeholder should not mint a token pointing at nothing."""
    _stub_fetch(monkeypatch, image=None)
    handler = _flow()

    await handler._show_config_form_name(handler.init_info)

    assert handler._preview_token is None
    assert handler.hass.data.get(DATA_FLOW_PREVIEWS) in (None, {})


# --- and the string and the code have to agree on the placeholder name -------


def _placeholders_in(text):
    """The `{name}` tokens Home Assistant will try to substitute."""
    return set(re.findall(r"\{([a-z_]+)\}", text))


def test_every_placeholder_the_description_uses_is_supplied():
    """Derived rather than trusted, in both directions: a renamed key in the code
    leaves `{preview}` rendering literally on screen, and a description that stops
    using it leaves the picture built and thrown away. Neither shows up in a test
    that only looks at one side."""
    with io.open(EN, encoding="utf-8") as handle:
        description = json.load(handle)["config"]["step"]["name"]["description"]

    assert _placeholders_in(description) == {"preview"}


def test_the_description_reads_as_prose_when_there_is_no_picture():
    """The placeholder is "" on a device that gave nothing, and the dialog renders this
    with `<ha-markdown breaks>`, so a placeholder at the front would leave a blank line
    above the text -- on exactly the devices that already get the worst of this flow.
    Prose first and the picture after it removes the case entirely, and reads better
    when there is one: instruction, then picture, then the field."""
    with io.open(EN, encoding="utf-8") as handle:
        description = json.load(handle)["config"]["step"]["name"]["description"]

    assert description.rstrip().endswith("{preview}"), (
        "the picture goes last: %r" % description
    )
    without = description.replace("{preview}", "")
    assert without == without.lstrip(), "a missing picture leaves a blank line"


async def test_the_description_is_what_the_picture_is_placed_into(monkeypatch):
    """The key the form supplies is the key the shipped string asks for."""
    _stub_fetch(monkeypatch)
    with io.open(EN, encoding="utf-8") as handle:
        description = json.load(handle)["config"]["step"]["name"]["description"]

    handler = _flow()
    result = await handler._show_config_form_name(handler.init_info)

    assert _placeholders_in(description) == set(result["description_placeholders"])


# --- asked for once ---------------------------------------------------------


async def test_the_device_is_only_asked_once(monkeypatch):
    """This form comes back whenever the name is empty, and a half megabyte snapshot
    per keystroke-corrected retry is not a reasonable thing to do to a recorder."""
    calls = _stub_fetch(monkeypatch)
    handler = _flow()

    await handler._show_config_form_name(handler.init_info)
    await handler._show_config_form_name(handler.init_info)

    assert len(calls) == 1


async def test_a_device_that_refused_is_not_asked_again(monkeypatch):
    """Remembering the refusal is the point of storing "" rather than None."""
    calls = _stub_fetch(monkeypatch, image=None)
    handler = _flow()

    await handler._show_config_form_name(handler.init_info)
    await handler._show_config_form_name(handler.init_info)

    assert len(calls) == 1


async def test_the_second_form_carries_the_same_picture(monkeypatch):
    """Caching must not mean the re-shown form loses the image."""
    _stub_fetch(monkeypatch)
    handler = _flow()

    first = await handler._show_config_form_name(handler.init_info)
    second = await handler._show_config_form_name(handler.init_info)

    assert (
        first["description_placeholders"]["preview"]
        == second["description_placeholders"]["preview"]
    )


# --- what the device is asked for -------------------------------------------


async def test_the_channel_being_added_is_the_channel_shown(monkeypatch):
    """A preview of channel 0 while channel 3 is being added would actively mislead:
    it would confirm a number the user got wrong."""
    calls = _stub_fetch(monkeypatch)
    handler = _flow(channel=3)

    await handler._show_config_form_name(handler.init_info)

    username, password, address, port, rtsp_port, channel, use_https = calls[0]
    assert (username, password, address) == ("admin", "pw", "10.0.0.5")
    assert (port, rtsp_port) == ("80", "554")
    assert channel == 3
    assert use_https is None


async def test_https_is_passed_through_when_it_is_set(monkeypatch):
    """A device only reachable over HTTPS has to be reachable for the picture too."""
    calls = _stub_fetch(monkeypatch)
    handler = _flow(use_https=True)

    await handler._show_config_form_name(handler.init_info)

    assert calls[0][6] is True


# --- the fetch never raises -------------------------------------------------


class _Refusing:
    def __init__(self, *args, **kwargs):
        pass

    async def async_get_snapshot(self, channel):
        raise OSError("device refused the snapshot")


class _Slow:
    def __init__(self, *args, **kwargs):
        pass

    async def async_get_snapshot(self, channel):
        await asyncio.sleep(5)
        return IMAGE


async def test_a_refused_snapshot_is_not_an_error(monkeypatch):
    """Every way of not getting a picture has to end with the flow carrying on."""
    monkeypatch.setattr(flow_module, "DahuaClient", _Refusing)

    assert (
        await flow_module.async_fetch_preview("admin", "pw", "10.0.0.5", "80", "554", 0)
        is None
    )


async def test_a_slow_snapshot_is_given_up_on(monkeypatch):
    """camera.py already records that these devices refuse snapshots under load. A
    still that has not arrived is one to do without, not to wait for: the add is
    already slow on a sixteen channel recorder."""
    monkeypatch.setattr(flow_module, "DahuaClient", _Slow)
    monkeypatch.setattr(flow_module, "PREVIEW_TIMEOUT_SECONDS", 0.01)

    assert (
        await flow_module.async_fetch_preview("admin", "pw", "10.0.0.5", "80", "554", 0)
        is None
    )


# --- the url the dialog gets is the url the view answers --------------------


def test_the_url_handed_out_is_the_url_the_view_serves():
    """Two halves that must not drift: the markdown points at a path, and the view
    declares one. A change to either alone renders a broken image."""
    assert preview_url("abc") == DahuaFlowPreviewView.url.replace("{token}", "abc")


async def test_the_image_url_is_root_relative(monkeypatch):
    """The markdown sanitiser accepts only http://, https://, data:image/, ./, ../,
    # and /-prefixed values for an img src. Anything else is silently emptied, so the
    picture would vanish with no error anywhere."""
    _stub_fetch(monkeypatch)
    handler = _flow()

    result = await handler._show_config_form_name(handler.init_info)

    url = re.search(r"\]\((.*)\)", result["description_placeholders"]["preview"])
    assert url and url.group(1).startswith("/")


# --- the store --------------------------------------------------------------


def test_the_view_serves_what_was_stored():
    hass = _hass()
    token = async_store_preview(hass, IMAGE)

    held = hass.data[DATA_FLOW_PREVIEWS][token]

    assert held[1] == "image/jpeg"
    assert held[2] == IMAGE


async def test_the_view_returns_the_image():
    hass = _hass()
    token = async_store_preview(hass, IMAGE)

    response = await DahuaFlowPreviewView(hass).get(None, token)

    assert response.body == IMAGE
    assert response.content_type == "image/jpeg"


async def test_a_served_image_is_not_cached():
    """One camera still, authorised by one token, for one flow. A cached copy would
    outlive the token that unlocked it."""
    hass = _hass()
    token = async_store_preview(hass, IMAGE)

    response = await DahuaFlowPreviewView(hass).get(None, token)

    assert response.headers["Cache-Control"] == "no-store"


async def test_an_unknown_token_is_not_found():
    with pytest.raises(web.HTTPNotFound):
        await DahuaFlowPreviewView(_hass()).get(None, "made-up")


async def test_a_view_asked_before_anything_was_stored_is_not_found():
    """The store does not exist until the first preview, so this is a missing key
    rather than a missing token."""
    hass = _hass()

    with pytest.raises(web.HTTPNotFound):
        await DahuaFlowPreviewView(hass).get(None, "made-up")


def test_a_stored_image_is_given_a_deadline():
    """Without one an abandoned flow holds its image for the life of the process."""
    hass = _hass()
    token = async_store_preview(hass, IMAGE)

    expires = hass.data[DATA_FLOW_PREVIEWS][token][0]

    assert (
        0 < expires - flow_preview.time.monotonic() <= flow_preview.PREVIEW_TTL_SECONDS
    )


async def test_an_expired_token_is_dropped_rather_than_left_holding_the_image():
    """Otherwise a flow nobody finished keeps half a megabyte until the next add."""
    hass = _hass()
    token = async_store_preview(hass, IMAGE)
    hass.data[DATA_FLOW_PREVIEWS][token] = (0.0, "image/jpeg", IMAGE)

    with pytest.raises(web.HTTPNotFound):
        await DahuaFlowPreviewView(hass).get(None, token)

    assert token not in hass.data[DATA_FLOW_PREVIEWS]


def test_two_previews_do_not_share_a_token():
    hass = _hass()

    assert async_store_preview(hass, IMAGE) != async_store_preview(hass, IMAGE)


def test_a_token_is_not_guessable():
    """It is the only thing standing in for authentication on this path, which is why
    it is 32 random bytes and not a counter or the flow id."""
    token = async_store_preview(_hass(), IMAGE)

    assert len(token) >= 40


def test_the_view_is_registered_once():
    """Registering the same path twice raises inside aiohttp's router."""
    hass = _hass()

    async_store_preview(hass, IMAGE)
    async_store_preview(hass, IMAGE)

    assert len(hass._registered) == 1


def test_the_view_is_registered_before_a_url_is_handed_out():
    hass = _hass()

    async_store_preview(hass, IMAGE)

    assert [type(view) for view in hass._registered] == [DahuaFlowPreviewView]


def test_the_registration_flag_lives_where_a_reload_cannot_lose_it():
    """A module global would reset when the integration is reloaded while the route
    stayed registered on the running http app, and the next store would raise."""
    hass = _hass()
    async_store_preview(hass, IMAGE)

    assert DATA_FLOW_PREVIEWS in hass.data


def test_expired_entries_are_pruned_by_the_next_store():
    hass = _hass()
    stale = async_store_preview(hass, IMAGE)
    hass.data[DATA_FLOW_PREVIEWS][stale] = (0.0, "image/jpeg", IMAGE)

    async_store_preview(hass, IMAGE)

    assert stale not in hass.data[DATA_FLOW_PREVIEWS]


def test_the_store_is_capped():
    """Timing alone does not bound it: an abandoned flow is only pruned by a later
    add, so a run of them would each be holding an image."""
    hass = _hass()

    tokens = [async_store_preview(hass, IMAGE) for _ in range(MAX_STORED_PREVIEWS + 3)]

    assert len(hass.data[DATA_FLOW_PREVIEWS]) == MAX_STORED_PREVIEWS
    assert tokens[-1] in hass.data[DATA_FLOW_PREVIEWS]
    assert tokens[0] not in hass.data[DATA_FLOW_PREVIEWS]


def test_dropping_a_token_stops_it_serving():
    hass = _hass()
    token = async_store_preview(hass, IMAGE)

    async_drop_preview(hass, token)

    assert token not in hass.data[DATA_FLOW_PREVIEWS]


def test_dropping_before_anything_was_stored_is_harmless():
    async_drop_preview(_hass(), "made-up")


# --- and the flow lets go of it --------------------------------------------


async def test_the_flow_drops_its_image_when_it_ends(monkeypatch):
    """Called for a created entry, an abort and a cancelled dialog alike, so it is the
    one place that covers all three."""
    _stub_fetch(monkeypatch)
    handler = _flow()
    await handler._show_config_form_name(handler.init_info)
    token = handler._preview_token
    assert token and token in handler.hass.data[DATA_FLOW_PREVIEWS]

    handler.async_remove()

    assert token not in handler.hass.data[DATA_FLOW_PREVIEWS]


async def test_ending_a_flow_that_never_showed_a_picture_is_harmless():
    """Every abort before the naming step, which is most of them."""
    _flow().async_remove()


async def test_ending_a_flow_twice_is_harmless():
    """Nothing promises async_remove runs once."""
    handler = _flow()
    handler._preview_token = async_store_preview(handler.hass, IMAGE)

    handler.async_remove()
    handler.async_remove()


async def test_one_flow_does_not_drop_anothers_image(monkeypatch):
    """Two cameras being added in two browser tabs is ordinary."""
    _stub_fetch(monkeypatch)
    hass = _hass()
    first, second = _flow(hass), _flow(hass)
    await first._show_config_form_name(first.init_info)
    await second._show_config_form_name(second.init_info)

    first.async_remove()

    assert second._preview_token in hass.data[DATA_FLOW_PREVIEWS]


# --- and the reason it is unauthenticated is recorded ---------------------


def test_the_view_is_deliberately_unauthenticated():
    """Not an oversight, and not safe to "fix": the frontend puts the user's token in
    an Authorization header from JavaScript, and a plain <img src> sends no header at
    all, so requiring auth would 401 the one request that matters. Core's own camera
    image views do the same and gate on an unguessable path instead. Turning this on
    silently breaks the picture; the token is what carries the authorisation."""
    assert DahuaFlowPreviewView.requires_auth is False
