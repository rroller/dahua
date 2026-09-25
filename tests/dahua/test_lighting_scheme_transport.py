"""The lighting scheme, read however the device will answer it.

#654 added a check at light-command time: a Smart Dual Light camera decides
separately which emitter it will use, and while that says AIMode or InfraredMode
the white light stays off however correct the write was.

#647 is that fault in the wild, and the check could not run. The reporter's
camera sits behind a recorder, every Lighting_V2 write was accepted, the LED
stayed dark for weeks -- and when they finally read LightingScheme themselves
they found `LightingMode=AIMode`. Setting it to WhiteMode lit the lamp
immediately, and the ordinary Home Assistant Illuminator entity then worked.

The reason nothing could tell them is the transport. Recorders answer
`getConfig&name=LightingScheme` with 400 -- measured on a DHI-NVR5464-16P-EI and
on theirs -- while the same table reads perfectly over RPC2 on that device. The
read asked only over CGI, which is the one their recorder refuses.

There were also two definitions of this method in the class. Python keeps the
later one, so the first was dead: the docstring explaining why it is not polled
sat on the copy nobody called.
"""

import aiohttp
import pytest

from custom_components.dahua.client import DahuaClient

SCHEME = {"table.LightingScheme[0][0].LightingMode": "AIMode"}


class _Client(DahuaClient):
    """The real method, with each transport replaced by a recorder."""

    def __init__(self, cgi, rpc2=None):
        self._cgi = cgi
        self._rpc2 = rpc2
        self.tried = []

    async def _request(self, url, allow_rpc2=True, **kwargs):
        self.tried.append("cgi")
        if isinstance(self._cgi, Exception):
            raise self._cgi
        return dict(self._cgi)

    async def _rpc2_get_config(self, name):
        self.tried.append("rpc2:%s" % name)
        if isinstance(self._rpc2, Exception):
            raise self._rpc2
        return dict(self._rpc2 or {})


def _refused():
    return aiohttp.ClientResponseError(None, None, status=400, message="Bad Request")


# --- a camera, which answers over CGI ------------------------------------------

async def test_a_camera_is_read_over_cgi():
    client = _Client(SCHEME)

    assert await client.async_get_lighting_scheme() == SCHEME
    assert client.tried == ["cgi"], "RPC2 must not be opened when CGI answered"


# --- a recorder, which does not ------------------------------------------------

async def test_a_refused_read_falls_back_to_rpc2():
    """400 Bad Request is what both measured recorders answer."""
    client = _Client(_refused(), rpc2=SCHEME)

    assert await client.async_get_lighting_scheme() == SCHEME
    assert client.tried == ["cgi", "rpc2:LightingScheme"]


async def test_an_empty_cgi_reply_also_falls_back():
    """A device can answer 200 with nothing for a table it does not have, and
    an empty table is not a scheme."""
    client = _Client({}, rpc2=SCHEME)

    assert await client.async_get_lighting_scheme() == SCHEME
    assert client.tried == ["cgi", "rpc2:LightingScheme"]


async def test_the_mode_survives_the_fallback():
    """What the caller actually needs: the mode that was holding the light off."""
    client = _Client(_refused(), rpc2=SCHEME)

    data = await client.async_get_lighting_scheme()

    assert data["table.LightingScheme[0][0].LightingMode"] == "AIMode"


# --- when neither transport can answer -----------------------------------------

async def test_a_device_with_no_scheme_at_all_returns_nothing():
    client = _Client({}, rpc2={})

    assert await client.async_get_lighting_scheme() == {}


async def test_an_rpc2_failure_is_not_swallowed():
    """The caller decides what a total failure means -- #684 stops asking again,
    and it can only do that if it hears about it."""
    client = _Client(_refused(), rpc2=ValueError("no session"))

    with pytest.raises(ValueError):
        await client.async_get_lighting_scheme()


def test_the_method_is_defined_once():
    """It was defined twice, and Python kept the later one."""
    import inspect

    source = inspect.getsource(DahuaClient)

    # Match the whole method name, not async_get_lighting_scheme_mode.
    assert source.count("async def async_get_lighting_scheme(") == 1
