"""A PTZ camera should be steerable, not only sent to saved positions.

#534 (Dec 2025) and #720 (Sep 2026) asked for the same thing nine months apart.
Everything here already drove `ptz.cgi`, but only ever with `GotoPreset`, so a
camera that can pan and tilt could only be sent to positions somebody had
already saved on it.

`ptz.cgi` has no move-by-an-amount: a start begins the motion and it runs until
a matching stop. So the duration is how far it travels, and the stop is the part
that must not be skipped.
"""
import asyncio

import yaml

from custom_components.dahua.camera import PTZ_MOVE_CODES
from custom_components.dahua.client import DahuaClient


class _Client(DahuaClient):
    """The real client, with only `get` replaced so the URLs can be read."""

    def __init__(self, fail_on=None):
        self.urls = []
        self.fail_on = fail_on

    async def get(self, url, verify_ok=False):
        self.urls.append(url)
        if self.fail_on and self.fail_on in url:
            raise RuntimeError("device refused")
        return {}


def _params(url):
    query = url.split("?", 1)[1]
    return dict(p.split("=", 1) for p in query.split("&"))


# --- the move itself --------------------------------------------------------

async def test_it_starts_and_then_stops():
    client = _Client()

    await client.async_ptz_move(1, "Left", 4, 0.01)

    actions = [_params(u)["action"] for u in client.urls]
    assert actions == ["start", "stop"], "a start with no stop leaves it turning"


async def test_both_halves_name_the_same_move():
    client = _Client()

    await client.async_ptz_move(2, "RightUp", 6, 0.01)

    start, stop = (_params(u) for u in client.urls)
    for field in ("channel", "code", "arg2"):
        assert start[field] == stop[field], \
            "%s differs between start and stop" % field
    assert start["channel"] == "2"
    assert start["code"] == "RightUp"
    assert start["arg2"] == "6", "speed is not being sent"


async def test_it_stops_even_when_the_wait_is_cancelled():
    """A camera left turning because Home Assistant restarted is the bad case."""
    client = _Client()

    task = asyncio.ensure_future(client.async_ptz_move(1, "Up", 4, 5))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert [_params(u)["action"] for u in client.urls] == ["start", "stop"], \
        "the camera was left moving"


async def test_a_refused_start_does_not_leave_a_stop_owing():
    """Nothing started, so nothing should be stopped."""
    client = _Client(fail_on="action=start")

    try:
        await client.async_ptz_move(1, "Up", 4, 0.01)
    except RuntimeError:
        pass

    assert [_params(u)["action"] for u in client.urls] == ["start"]


# --- the directions ---------------------------------------------------------

def test_every_direction_has_a_dahua_code():
    assert PTZ_MOVE_CODES["left"] == "Left"
    assert PTZ_MOVE_CODES["up_right"] == "RightUp", \
        "Dahua spells the diagonals the other way round"
    assert PTZ_MOVE_CODES["zoom_in"] == "ZoomTele"
    assert PTZ_MOVE_CODES["zoom_out"] == "ZoomWide"


def test_the_codes_are_distinct():
    assert len(set(PTZ_MOVE_CODES.values())) == len(PTZ_MOVE_CODES)


def test_the_service_offers_exactly_the_directions_it_can_do():
    """A name in services.yaml that the table does not have is a KeyError.

    The service picker builds its dropdown from this, so the two have to be the
    same set or the user can choose something that cannot run.
    """
    with open("custom_components/dahua/services.yaml", encoding="utf-8") as f:
        services = yaml.safe_load(f)

    offered = services["ptz_move"]["fields"]["direction"]["selector"]["select"]["options"]

    assert sorted(offered) == sorted(PTZ_MOVE_CODES)
