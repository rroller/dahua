"""HTTPS must be selectable, and connection settings changeable after setup."""

from types import SimpleNamespace

import pytest

from custom_components.dahua import get_configured_use_https
from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.config_flow import DahuaFlowHandler
from custom_components.dahua.rpc2 import DahuaRpc2Client


def _client(port, use_https=None):
    return DahuaClient("u", "p", "1.2.3.4", port, 554, session=None, use_https=use_https)


def _rpc2(port, use_https=None):
    return DahuaRpc2Client("u", "p", "1.2.3.4", port, 554, session=None, use_https=use_https)


@pytest.mark.parametrize(
    "port,use_https,expected",
    [
        # Unspecified keeps the behaviour that shipped before the option.
        (80, None, "http://1.2.3.4:80"),
        (443, None, "https://1.2.3.4:443"),
        (8443, None, "http://1.2.3.4:8443"),
        # Explicitly asked for, on any port. This is the point of the option.
        (8443, True, "https://1.2.3.4:8443"),
        (80, True, "https://1.2.3.4:80"),
        # Explicitly declined, even on 443.
        (443, False, "http://1.2.3.4:443"),
    ],
)
def test_client_base_url(port, use_https, expected):
    assert _client(port, use_https)._base == expected


@pytest.mark.parametrize(
    "port,use_https,expected",
    [
        (80, None, "http://1.2.3.4:80"),
        (443, None, "https://1.2.3.4:443"),
        (8443, True, "https://1.2.3.4:8443"),
        (443, False, "http://1.2.3.4:443"),
    ],
)
def test_rpc2_base_url(port, use_https, expected):
    assert _rpc2(port, use_https)._base == expected


async def test_internally_built_rpc2_client_inherits_the_flag(monkeypatch):
    """client.py builds its own RPC2 client; it must not fall back to the port."""
    captured = {}

    class _CapturingRpc2:
        def __init__(self, username, password, address, port, rtsp_port, session, use_https=None):
            captured["use_https"] = use_https

        async def async_get_ptz_presets(self, channel):
            return []

        async def logout(self):
            return True

    class _NullSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(client_module, "DahuaRpc2Client", _CapturingRpc2)
    monkeypatch.setattr(client_module.DahuaClient, "_new_rpc2_session", staticmethod(_NullSession))

    # HTTPS on a non-443 port is exactly where deriving from the port is wrong.
    await _client(8443, True).async_get_ptz_preset_ids(1)

    assert captured["use_https"] is True


def test_unticked_means_derive_from_the_port_not_force_http():
    """False must not force HTTP on 443, or existing entries would break."""
    assert get_configured_use_https(SimpleNamespace(data={}, options={})) is None
    assert get_configured_use_https(SimpleNamespace(data={"use_https": False}, options={})) is None
    assert get_configured_use_https(SimpleNamespace(data={"use_https": True}, options={}) ) is True


def _schema_defaults(result):
    out = {}
    for marker in result["data_schema"].schema:
        default = getattr(marker, "default", None)
        out[str(marker.schema)] = default() if callable(default) else default
    return out


async def _shown_reconfigure_form(hass, entry):
    handler = DahuaFlowHandler()
    handler.hass = hass
    handler._get_reconfigure_entry = lambda: entry
    return await handler.async_step_reconfigure()


async def test_reconfigure_form_is_prefilled_from_the_entry(hass):
    entry = SimpleNamespace(
        data={
            "address": "192.168.0.210",
            "port": "8443",
            "rtsp_port": "554",
            "channel": 2,
            "use_https": True,
            "username": "u",
            "password": "p",
        },
        options={},
    )

    defaults = _schema_defaults(await _shown_reconfigure_form(hass, entry))

    assert defaults["address"] == "192.168.0.210"
    assert defaults["port"] == "8443"
    assert defaults["rtsp_port"] == "554"
    assert defaults["channel"] == 2
    assert defaults["use_https"] is True


async def test_reconfigure_form_does_not_offer_credentials(hass):
    """Credentials belong to the reauth step, not here."""
    entry = SimpleNamespace(
        data={"address": "1.2.3.4", "port": "80", "rtsp_port": "554",
              "channel": 0, "username": "u", "password": "p"},
        options={},
    )

    defaults = _schema_defaults(await _shown_reconfigure_form(hass, entry))

    assert "username" not in defaults
    assert "password" not in defaults
