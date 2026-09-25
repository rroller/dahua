"""Ask the device what it is, instead of reading its model name.

Every doorbell capability is decided by `is_doorbell`, and `is_doorbell` is a
list of model-name prefixes: VTO, AD, DB6, DB2X, AV-V. A rebadge nobody has
added to that list gets no Button Pressed sensor, no Call No Answered, no door
entities, and no obvious reason why. #690 is that, for a Lorex B451AJ; it is the
same shape as #570 and #676, where a capability was gated on a model prefix
rather than on what the device reports.

Dahua devices will answer the question directly. Measured 2026-09-25 on two
devices on one network:

    VTO2000A doorbell            magicBox.cgi?action=getDeviceClass -> class=VTO
    DHI-NVR5464-16P-EI recorder  magicBox.cgi?action=getDeviceClass -> class=NVR

**Deliberately additive, and that is the whole design.** A `VTO` answer makes a
device a doorbell whatever its model string says. Anything else, including no
answer at all, leaves the model-name list exactly as it was. No Amcrest or Imou
doorbell has been measured here, so an authoritative reading could take every
doorbell entity away from the people who have them working today -- which is the
failure this is meant to prevent, arriving from the other direction.

The answer also goes into diagnostics, so the unmeasurable half of this stops
being a guess the next time a reporter posts one.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import DahuaClient


def _coordinator(model="", device_class=None):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.model = model
    if device_class is not None:
        c._device_class = device_class
    return c


# --- what the device says wins when it says doorbell -------------------------

def test_a_device_that_says_it_is_a_vto_is_a_doorbell():
    """#690: the model name is on no list, and the device says VTO anyway."""
    assert _coordinator(model="B451AJ", device_class="VTO").is_doorbell()


def test_the_model_name_alone_would_have_said_no():
    """The other half of the test above, so it cannot pass for the wrong reason."""
    assert not _coordinator(model="B451AJ", device_class="").is_doorbell()


# --- and never takes a doorbell away -----------------------------------------

@pytest.mark.parametrize("model", ["AD410", "DB61i", "DB2X-WP", "AV-VTA05-22AV2",
                                   "VTO2202F-P-S2", "DHI-VTO3211D-P4-S2"])
@pytest.mark.parametrize("answer", ["", "IPC", "NVR", "DVR"])
def test_a_known_doorbell_stays_one_whatever_the_device_answers(model, answer):
    """No measurement exists for what an Amcrest or Imou doorbell reports, so a
    non-VTO answer must not be read as a denial."""
    assert _coordinator(model=model, device_class=answer).is_doorbell()


def test_a_camera_is_still_not_a_doorbell():
    assert not _coordinator(model="IPC-HDW5831R-ZE", device_class="IPC").is_doorbell()


def test_a_recorder_is_still_not_a_doorbell():
    assert not _coordinator(model="DHI-NVR5464-16P-EI", device_class="NVR").is_doorbell()


# --- and works on a coordinator that never ran the probe ---------------------

def test_a_coordinator_without_the_attribute_falls_back_to_the_model():
    """Most tests and every older entry build one of these without the probe
    having run. A capability check must never be what raises."""
    c = _coordinator(model="VTO2000A")
    assert not hasattr(c, "_device_class")

    assert c.is_doorbell()


def test_no_attribute_and_no_matching_model_is_simply_not_a_doorbell():
    assert not _coordinator(model="IPC-HDW5831R-ZE").is_doorbell()


# --- the reading itself ------------------------------------------------------

class _Client:
    def __init__(self, body):
        self.body = body
        self.asked = []

    async def get(self, url, verify_ok=False):
        self.asked.append(url)
        return self.body


async def _read(body):
    return await DahuaClient.async_get_device_class(_Client(body))


async def test_the_class_is_read_from_the_class_field():
    client = _Client({"class": "VTO"})

    assert await DahuaClient.async_get_device_class(client) == "VTO"
    assert client.asked == ["/cgi-bin/magicBox.cgi?action=getDeviceClass"]


async def test_the_answer_is_folded_so_one_comparison_covers_every_spelling():
    """The comparison is a single ==, so the folding is what makes it work."""
    assert await _read({"class": "vto"}) == "VTO"
    assert await _read({"class": " VTO"}) == "VTO"


async def test_a_device_that_answers_without_the_field_reads_as_no_information():
    assert await _read({}) == ""
    assert await _read({"Error": "Error"}) == ""
