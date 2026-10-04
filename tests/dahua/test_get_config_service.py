"""dahua.get_config reads any configuration table, read-only.

Hikvision's integration offers an isapi_request escape hatch; this is the
Dahua analog, scoped to getConfig so it can only read. The integration wraps the
common config tables, but a device has many model-specific ones it does not, and
inspecting those otherwise meant turning on debug logging.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.dahua.camera import DahuaCamera, _CONFIG_NAME


def _camera(reply):
    cam = object.__new__(DahuaCamera)
    cam._coordinator = SimpleNamespace(
        client=SimpleNamespace(async_get_config=AsyncMock(return_value=reply))
    )
    return cam


async def test_it_returns_the_table_under_a_config_key():
    cam = _camera({"table.Encode[0].MainFormat[0].AudioEnable": "true"})

    result = await cam.async_get_config_service("Encode")

    assert result == {"config": {"table.Encode[0].MainFormat[0].AudioEnable": "true"}}
    cam._coordinator.client.async_get_config.assert_awaited_once_with("Encode")


# --- the name is validated so it cannot smuggle another CGI parameter -------


@pytest.mark.parametrize(
    "name", ["Encode", "VideoColor", "Lighting[0][0]", "General.LocalNo", "A_b"]
)
def test_a_real_config_name_is_accepted(name):
    assert _CONFIG_NAME.match(name)


@pytest.mark.parametrize(
    "name",
    [
        "Encode&action=setConfig&Encode[0].MainFormat[0].AudioEnable=false",
        "Encode&name=Other",
        "a b",
        "a/b",
        "a?x",
        "a=1",
        "",
    ],
)
def test_a_name_that_could_inject_is_rejected(name):
    """The name goes straight into the getConfig URL; anything that could append
    another CGI parameter (an &action=setConfig tail, say) must not match, so the
    service schema refuses it before any request is made."""
    assert not _CONFIG_NAME.match(name)
