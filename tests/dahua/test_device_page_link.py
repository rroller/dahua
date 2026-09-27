"""The Visit link on a device page has to go where the device actually is.

`device_info["configuration_url"]` was built as:

    "http://" + self._coordinator.get_address()

No port, and always plain HTTP. So it was broken for every device on a port other
than 80 and every device that only serves HTTPS, on the first page somebody opens
after adding a camera. #504 and #537 are both people whose devices were on 443.

The integration already knows the answer: the client composes the same base URL to
talk to the device, and `test_client_base_url` pins its shape. This just hands that
to the device registry instead of guessing.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua.client import DahuaClient
from custom_components.dahua.entity import DahuaBaseEntity


def _client(port, use_https=None):
    return DahuaClient("u", "p", "1.2.3.4", port, 554, session=None,
                       use_https=use_https)


# --- the client already knows, and now says so ------------------------------

@pytest.mark.parametrize("port,use_https,expected", [
    (80, None, "http://1.2.3.4:80"),
    (443, None, "https://1.2.3.4:443"),
    (8000, None, "http://1.2.3.4:8000"),
    (8443, True, "https://1.2.3.4:8443"),
    (80, True, "https://1.2.3.4:80"),
])
def test_the_client_reports_the_url_it_uses(port, use_https, expected):
    assert _client(port, use_https).base_url() == expected


def test_the_accessor_is_the_same_string_the_client_talks_to():
    """A second implementation would drift from the one that does the requests."""
    client = _client(8443, True)

    assert client.base_url() == client._base  # pylint: disable=protected-access


def test_the_coordinator_hands_on_the_clients_url():
    """It must pass the client's answer through rather than compose its own.

    Two implementations of the same URL can disagree, and only one of them is the
    one actually talking to the device. Called unbound so no coordinator has to be
    constructed for a one line accessor.
    """
    from custom_components.dahua import DahuaDataUpdateCoordinator

    stand_in = SimpleNamespace(client=_client(8443, True), _address="1.2.3.4")

    url = DahuaDataUpdateCoordinator.get_configuration_url(stand_in)

    assert url == "https://1.2.3.4:8443"


# --- and the device page uses it --------------------------------------------

def _device_info(url):
    entity = object.__new__(DahuaBaseEntity)
    entity._coordinator = SimpleNamespace(
        get_serial_number=lambda: "SER123",
        get_device_name=lambda: "Front Door",
        get_model=lambda: "IPC-HDW5831R",
        get_firmware_version=lambda: "2.800",
        get_configuration_url=lambda: url,
        configured_area_name=lambda: None,
    )
    return entity.device_info


def test_the_visit_link_carries_the_port():
    """The whole point. "http://1.2.3.4" on a device serving 8000 goes nowhere."""
    assert _device_info("http://1.2.3.4:8000")["configuration_url"] == \
        "http://1.2.3.4:8000"


def test_the_visit_link_keeps_https():
    assert _device_info("https://1.2.3.4:443")["configuration_url"] == \
        "https://1.2.3.4:443"


def test_the_link_is_not_rebuilt_from_the_address():
    """It must come from the coordinator rather than be composed here, or the two
    can disagree and only one of them is talking to the device."""
    info = _device_info("https://1.2.3.4:8443")

    assert not info["configuration_url"].startswith("http://"), (
        "an https device must not be linked over plain http"
    )
    assert ":8443" in info["configuration_url"]


def test_the_rest_of_the_device_info_is_untouched():
    """This change is one key. Everything else on the device page stays put."""
    info = _device_info("http://1.2.3.4:80")

    assert info["name"] == "Front Door"
    assert info["model"] == "IPC-HDW5831R"
    assert info["manufacturer"] == "Dahua"
    assert info["sw_version"] == "2.800"
    assert ("dahua", "SER123") in info["identifiers"]


def test_no_area_is_still_omitted_rather_than_passed_as_none():
    """Every key here is handed to async_get_or_create as given."""
    assert "suggested_area" not in _device_info("http://1.2.3.4:80")
