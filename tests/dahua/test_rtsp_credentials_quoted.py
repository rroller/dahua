"""A password with punctuation in it must not rewrite the RTSP URL.

#362. A password containing `@`, `:` or `/` changes where the URL's authority
ends, so the parser reads part of the password as the host or the port. It
surfaces from Home Assistant's stream worker as:

    ValueError: Port could not be cast to integer value

which says nothing about a password, and sent the reporter looking at the port
setting instead.

The main and sub stream URLs were fixed by percent-encoding both credentials.
The fourth stream was built by a separate branch a few lines below and was
missed, and that branch is reachable: the camera platform creates a stream per
index in `range(get_max_streams())`, so any device reporting four streams uses
it.
"""
from urllib.parse import urlparse

from custom_components.dahua.client import DahuaClient


AWKWARD = "p@ss:w/rd#1"


def _client(password=AWKWARD, username="admin"):
    return DahuaClient(username, password, "10.0.0.5", "80", "554", None, None)


def _authority_is_intact(url, expected_host="10.0.0.5"):
    """The host must survive whatever the password contains."""
    parsed = urlparse(url)
    assert parsed.hostname == expected_host, url
    return parsed


def test_the_main_stream_survives_a_punctuated_password():
    _authority_is_intact(_client().get_rtsp_stream_url(0, 0))


def test_the_sub_stream_survives_a_punctuated_password():
    _authority_is_intact(_client().get_rtsp_stream_url(0, 1))


def test_the_fourth_stream_survives_a_punctuated_password():
    """The branch that was missed. Reachable on any device with four streams."""
    _authority_is_intact(_client().get_rtsp_stream_url(0, 3))


def test_the_port_is_still_the_port():
    """The reported symptom: the password shifted what parsed as the port."""
    parsed = urlparse(_client().get_rtsp_stream_url(0, 0))

    assert parsed.port == 554


def test_a_username_with_punctuation_is_encoded_too():
    parsed = _authority_is_intact(
        _client(username="ad:min@host").get_rtsp_stream_url(0, 3))

    assert "@host" not in (parsed.username or "")


def test_an_ordinary_password_is_unchanged():
    """Encoding must not alter credentials that needed nothing doing to them."""
    url = _client(password="plainpassword").get_rtsp_stream_url(0, 0)

    assert "plainpassword" in url
