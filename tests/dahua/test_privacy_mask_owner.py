from types import SimpleNamespace

import aiohttp
import pytest

from custom_components.dahua.client import DahuaClient, clear_host_cache
from tests.dahua.test_a_privacy_mask_the_camera_keeps_elsewhere import (
    MASKING_IS_LIVE,
    _covers,
    _empty_masking_rows,
)


@pytest.fixture
def masked_camera():
    client = DahuaClient(
        "u", "p", "privacy-review.invalid", 80, 554, SimpleNamespace(closed=False)
    )
    state = {
        "masking": dict(MASKING_IS_LIVE["PrivacyMasking"]),
        "widget": _covers(),
        "error": None,
        "writes": [],
    }

    async def request(url, *args, **kwargs):
        if url == "/cgi-bin/configManager.cgi?action=getConfig&name=PrivacyMasking":
            if state["error"]:
                raise state["error"]
            return dict(state["masking"])
        if url == "/cgi-bin/configManager.cgi?action=getConfig&name=VideoWidget":
            return dict(state["widget"])
        state["writes"].append(url)
        return {"OK": "OK"}

    client._request = request
    clear_host_cache(client._device)
    yield client, state
    clear_host_cache(client._device)


async def test_mask_write_uses_current_geometry_instead_of_warm_cache(masked_camera):
    client, state = masked_camera
    await client.async_get_config("PrivacyMasking")
    state["masking"] = _empty_masking_rows()

    await client.async_setprivacymask(2, True)

    assert state["writes"] == [
        "/cgi-bin/configManager.cgi?action=setConfig&VideoWidget[0].Covers[2].EncodeBlend=true"
    ]


@pytest.mark.parametrize("status", [400, 401, 403, 500])
async def test_a_primary_read_failure_does_not_authorize_another_table(
    masked_camera, status
):
    client, state = masked_camera
    state["error"] = aiohttp.ClientResponseError(
        SimpleNamespace(
            real_url="http://privacy-review.invalid/cgi-bin/configManager.cgi"
        ),
        (),
        status=status,
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_setprivacymask(2, True)
    assert state["writes"] == []


@pytest.mark.parametrize("status", [404, 501])
async def test_an_absent_primary_table_can_use_its_configured_covers(
    masked_camera, status
):
    client, state = masked_camera
    state["error"] = aiohttp.ClientResponseError(
        SimpleNamespace(
            real_url="http://privacy-review.invalid/cgi-bin/configManager.cgi"
        ),
        (),
        status=status,
    )

    await client.async_setprivacymask(2, True)

    assert state["writes"] == [
        "/cgi-bin/configManager.cgi?action=setConfig&VideoWidget[0].Covers[2].EncodeBlend=true"
    ]


async def test_newly_drawn_covers_are_not_hidden_by_a_warm_widget_cache(masked_camera):
    client, state = masked_camera
    state["masking"] = _empty_masking_rows()
    state["widget"] = {}
    await client.async_get_config("VideoWidget")
    state["widget"] = _covers()

    await client.async_setprivacymask(2, True)

    assert state["writes"] == [
        "/cgi-bin/configManager.cgi?action=setConfig&VideoWidget[0].Covers[2].EncodeBlend=true"
    ]


async def test_a_failed_secondary_read_stops_the_write(masked_camera):
    client, state = masked_camera
    state["masking"] = _empty_masking_rows()
    original = client._request

    async def request(url, *args, **kwargs):
        if url.endswith("name=VideoWidget"):
            raise aiohttp.ClientResponseError(
                SimpleNamespace(
                    real_url="http://privacy-review.invalid/cgi-bin/configManager.cgi"
                ),
                (),
                status=500,
            )
        return await original(url, *args, **kwargs)

    client._request = request
    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_setprivacymask(2, True)
    assert state["writes"] == []
