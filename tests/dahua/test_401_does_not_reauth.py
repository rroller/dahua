"""A 401 during a poll must not start a reauth flow on every channel.

#702 made the identity calls re-raise a 401 instead of synthesising an id, so
that a wrong password would fail setup rather than adding a broken camera. That
is right for the config flow, which is deciding whether a password is correct.

But `async_get_system_info` is shared with the coordinator's one-time init, and
there a 401 is not proof of anything. Eight channels of one NVR share a digest
challenge, and a nonce that races between them is refused exactly like a bad
credential. The init block turns any 401 into:

    _LOGGER.warning("Authentication failed for %s, starting reauth", ...)
    self.config_entry.async_start_reauth(self.hass)

so #714 reports all eight entries starting reauth within milliseconds of each
other, on every restart, with credentials that were never wrong. 0.10.7 was
fine because the 401 was swallowed there.

So the strictness belongs to the caller that asked for it, not to the method.
"""

import aiohttp
import pytest

from custom_components.dahua.client import DahuaClient


def _client(status):
    c = DahuaClient("admin", "pw", "10.0.0.5", 80, 554, None, False)

    async def get(url, verify_ok=False):
        raise aiohttp.ClientResponseError(None, None, status=status, message="x")

    c.get = get
    return c


# --- the coordinator's path: a 401 must not be fatal ------------------------

async def test_a_401_falls_back_by_default():
    """What the coordinator calls, and what 0.10.7 did."""
    result = await _client(401).async_get_system_info()

    assert result["serialNumber"], "no identity, so the entry cannot start"


async def test_the_fallback_is_marked_as_derived():
    c = _client(401)

    await c.async_get_system_info()

    assert c.identity_derived_from_credentials is True


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_no_status_is_fatal_by_default(status):
    assert await _client(status).async_get_system_info()


# --- the config flow's path: a 401 is exactly what it is testing for --------

async def test_a_401_is_raised_when_the_caller_asked():
    """#702: a wrong password must fail setup, not add a broken camera."""
    with pytest.raises(aiohttp.ClientResponseError) as caught:
        await _client(401).async_get_system_info(strict_auth=True)

    assert caught.value.status == 401


async def test_a_missing_endpoint_still_falls_back_even_when_strict():
    """404 means no such path, which is not a credential problem."""
    result = await _client(404).async_get_system_info(strict_auth=True)

    assert result["serialNumber"]


async def test_a_restricted_account_still_falls_back_when_strict():
    """403 is "logged in but not allowed", which a restricted user really hits."""
    assert await _client(403).async_get_system_info(strict_auth=True)
