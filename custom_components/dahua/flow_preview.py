"""Serve one still image to the config flow dialog, for as long as the flow lasts.

Somebody adding a camera off a recorder types a channel number, and nothing in the
flow ever shows them what that channel is pointing at. Channel numbers are the one
field a user cannot check: the recorder numbers from one, this integration indexes
from zero, and a sixteen channel recorder gives sixteen chances to be off by one.
#583 and #647 both start with an entry that was added successfully and turned out to
be the wrong camera. A picture on the naming step settles it in the one second it
takes to look, and it is also what tells somebody what to call the thing.

## Why not Home Assistant's own preview API

`async_show_form(preview=...)` exists for exactly this, and it cannot be used by an
integration outside core. Measured on 2026.9.3, frontend 20260826.7:

    const o = ["generic_camera", "template"];
    F: e => o.includes(e) ? e : "generic",
    Q: (hass, domain, flow_id, flow_type, user_input, cb) =>
         hass.connection.subscribeMessage(cb, {type: `${domain}/start_preview`, ...})

The component that renders an *image* is `flow-preview-generic_camera`, and the only
string that selects it is `"generic_camera"` -- which is also what the websocket
command name is built from. So an integration that wants a picture has to register
`generic_camera/start_preview`, which belongs to the built-in `generic` integration.
`websocket_api.async_register_command` is a dict assignment, and core sets up each
handler's preview at most once per domain per process
(`data_entry_flow._async_setup_preview` guards on `flow.handler not in self._preview`),
so which of the two survives depends on whether a Dahua or a generic camera flow was
opened first. Silently breaking a core integration's preview is not worth a picture.
Any other name, `"dahua"` included, falls through to `flow-preview-generic`, which
renders an entity state row and no image at all.

## What is used instead

A step description is rendered as markdown -- `<ha-markdown allow-svg breaks>` -- and
the sanitiser's whitelist carries `img: ["src", "alt", "title", "width", "height",
"loading"]`. So an image reaches the dialog through the description, and this module is
the URL behind it.

A `data:image/` URL would need no HTTP at all, and the whitelist does allow one, but
measured on the DHI-NVR5464 `snapshot.cgi` returns 516 KB to 1.4 MB per channel and
ignores both `subtype` and `type`, so there is no smaller image to ask the device for.
Base64 of that inside a flow response is not a reasonable thing to send on every add.

## About requires_auth

The frontend puts the user's token in an Authorization header from JavaScript. A plain
`<img src>` sends no such header, so an authenticated view would 401 for the one
request that matters. Core's own camera image views have the same problem and solve it
the same way: `requires_auth = False`, with an unguessable token in the path standing
in for the credential. Here the token is 32 random bytes, it is minted per flow, it
expires, and what it unlocks is a still already in memory -- no device call happens on
the request, and no credential is anywhere near the URL.
"""

import logging
import secrets
import time

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)

# Where the still images live. Its own key rather than anything entry scoped,
# because a preview belongs to a config *flow*: there is no entry yet, so there is
# no `runtime_data` to put it on. Keeping it in hass.data rather than in a module
# global is what makes the registration guard below survive an integration reload,
# which a module global would not.
DATA_FLOW_PREVIEWS = "dahua_flow_previews"

PREVIEW_PATH = "/api/dahua/flow_preview"

# Long enough to read a form, short enough that an abandoned flow is not holding half a
# megabyte for the life of the process. A picture that has expired renders as its alt
# text, which says so.
PREVIEW_TTL_SECONDS = 300

# An abandoned flow leaves one entry behind and nothing prunes until the next add, so
# the store is capped as well as timed.
MAX_STORED_PREVIEWS = 8


def preview_url(token: str) -> str:
    """The path the dialog should ask for."""
    return "{0}/{1}".format(PREVIEW_PATH, token)


@callback
def _async_prune(store: dict, now: float, keep: str) -> None:
    """Drop what has expired, then the oldest of whatever is left over the cap.

    Runs after the insert rather than before it, so `len(store) <= MAX_STORED_PREVIEWS`
    is true whenever this returns. Pruning first left the store one over the cap for
    ever, which is a cap that does not hold. `keep` is the image just stored: it is the
    newest, so trimming the oldest cannot reach it, but saying so beats relying on a
    clock whose two readings could come back equal.
    """
    for token in [
        token
        for token, (expires, _, _) in store.items()
        if expires <= now and token != keep
    ]:
        del store[token]
    while len(store) > MAX_STORED_PREVIEWS:
        candidates = [token for token in store if token != keep]
        del store[min(candidates, key=lambda token: store[token][0])]


@callback
def async_store_preview(
    hass: HomeAssistant, image: bytes, content_type: str = "image/jpeg"
) -> str:
    """Hold one image for a flow to point at, and return its token."""
    store = hass.data.get(DATA_FLOW_PREVIEWS)
    if store is None:
        # The store's absence is the registration flag. Registering the same path twice
        # raises inside aiohttp's router, and hass.data outlives a module reload while a
        # module global does not, so the two facts have to be recorded in the same place.
        store = hass.data[DATA_FLOW_PREVIEWS] = {}
        hass.http.register_view(DahuaFlowPreviewView(hass))
    now = time.monotonic()
    token = secrets.token_urlsafe(32)
    store[token] = (now + PREVIEW_TTL_SECONDS, content_type, image)
    _async_prune(store, now, token)
    return token


@callback
def async_drop_preview(hass: HomeAssistant, token: str) -> None:
    """Forget an image, because the flow holding it has finished or been cancelled."""
    store = hass.data.get(DATA_FLOW_PREVIEWS)
    if store is not None:
        store.pop(token, None)


class DahuaFlowPreviewView(HomeAssistantView):
    """Serve a still a config flow captured a moment ago.

    See the module docstring for why this is not authenticated, and why it is not the
    websocket preview API.
    """

    url = PREVIEW_PATH + "/{token}"
    name = "api:dahua:flow_preview"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Keep hass, because the store lives there rather than on this instance."""
        self.hass = hass

    async def get(self, request: web.Request, token: str) -> web.Response:
        """Return the image, or 404 once it has expired or been dropped."""
        held = self.hass.data.get(DATA_FLOW_PREVIEWS, {}).get(token)
        if held is None:
            raise web.HTTPNotFound
        expires, content_type, image = held
        if expires <= time.monotonic():
            async_drop_preview(self.hass, token)
            raise web.HTTPNotFound
        return web.Response(
            body=image,
            content_type=content_type,
            # One camera still, valid for one flow. Nothing downstream should keep it,
            # and a cached copy would outlive the token that authorised it.
            headers={"Cache-Control": "no-store"},
        )
