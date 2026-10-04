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

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.dahua.client import DahuaClient
from custom_components.dahua.rpc2 import Rpc2MethodRefused

SCHEME = {"table.LightingScheme[0][0].LightingMode": "AIMode"}


class _Client(DahuaClient):
    """The real method, with each transport replaced by a recorder."""

    def __init__(self, cgi, rpc2=None):
        self._address = "camera"
        self._cgi = cgi
        self._rpc2 = rpc2
        self.tried = []

    async def _request(self, url, allow_rpc2=True, **kwargs):
        self.tried.append("cgi")
        if isinstance(self._cgi, Exception):
            raise self._cgi
        return dict(self._cgi)

    async def _rpc2_get_config(self, name, **kwargs):
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


async def test_live_mode_read_falls_back_when_cgi_is_refused():
    """Illuminator restore capture must use the same fallback as table reads."""
    client = _Client(_refused(), rpc2=SCHEME)

    assert await client.async_get_lighting_scheme_mode(0, "0") == "AIMode"
    assert client.tried == ["cgi", "rpc2:LightingScheme"]


async def test_mode_is_optional_when_both_transports_have_no_table():
    client = _Client(_refused(), rpc2={})
    assert await client.async_get_lighting_scheme_mode(0, "0") is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 501])
async def test_cgi_failure_is_resolved_by_actual_rpc2_table_refusal(status):
    client = object.__new__(DahuaClient)
    client._request = AsyncMock(
        side_effect=aiohttp.ClientResponseError(None, None, status=status)
    )
    client._shared_rpc2 = AsyncMock(
        return_value=SimpleNamespace(
            client=SimpleNamespace(
                get_config=AsyncMock(
                    side_effect=Rpc2MethodRefused("unsupported table", code=268959743)
                )
            )
        )
    )
    assert await client.async_get_lighting_scheme_mode(0, "0") is None
    client._shared_rpc2.return_value.client.get_config.assert_awaited_once_with(
        {"name": "LightingScheme"}
    )


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientResponseError(None, None, status=401),
        Rpc2MethodRefused("expired", code=287637504),
        TimeoutError(),
    ],
)
async def test_cgi_500_does_not_hide_rpc2_failure(error):
    client = _Client(aiohttp.ClientResponseError(None, None, status=500), rpc2=error)
    with pytest.raises(type(error)) as caught:
        await client.async_get_lighting_scheme_mode(0, "0")
    assert caught.value is error
    assert client.tried == ["cgi", "rpc2:LightingScheme"]


async def test_mode_read_bypasses_shared_cache_each_time():
    client = object.__new__(DahuaClient)
    client.get = AsyncMock(side_effect=AssertionError("must bypass shared cache"))
    client._request = AsyncMock(
        side_effect=[
            SCHEME,
            {"table.LightingScheme[0][0].LightingMode": "WhiteMode"},
        ]
    )
    assert await client.async_get_lighting_scheme_mode(0, "0") == "AIMode"
    assert await client.async_get_lighting_scheme_mode(0, "0") == "WhiteMode"
    assert client._request.await_count == 2
    client.get.assert_not_awaited()


@pytest.mark.parametrize(
    "code,missing", [(268959743, True), (268632064, True), (287637504, False)]
)
async def test_only_table_refusals_allow_a_missing_scheme(code, missing):
    client = object.__new__(DahuaClient)
    client._shared_rpc2 = AsyncMock(
        return_value=SimpleNamespace(
            client=SimpleNamespace(
                get_config=AsyncMock(
                    side_effect=Rpc2MethodRefused("refused", code=code)
                )
            )
        )
    )
    if missing:
        assert await client._rpc2_get_config("LightingScheme", allow_missing=True) == {}
    else:
        with pytest.raises(Rpc2MethodRefused):
            await client._rpc2_get_config("LightingScheme", allow_missing=True)


async def test_login_refusal_is_not_treated_as_missing_table():
    client = object.__new__(DahuaClient)
    client._shared_rpc2 = AsyncMock(
        side_effect=Rpc2MethodRefused("login failed", code=268959743)
    )
    with pytest.raises(Rpc2MethodRefused):
        await client._rpc2_get_config("LightingScheme", allow_missing=True)
