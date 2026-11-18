"""A siren the device cannot be asked to sound is not offered (#942).

@gabberpocky's AD410 diagnostics, on 3.1.0-rc.8:

    supports_siren_sources:  ProductDefinition: AudioFileManager.SirenFileManager.Support=true
    supports_siren_failures: getCaps: query failed (Rpc2MethodRefused, code 268894210)

and the first report has `CoaxialControlIO.control` answering the same
268894210, "Method not found!". The switch sounds the siren with that control
call, so on this device it could only ever be refused. The hardware is real (it
sounds from the Amcrest app), so the ProductDefinition claim stays in the
sources; it just no longer creates a switch for a transport the device does not
have.

Only that code. A timeout, or a refusal with another code, says nothing about
whether the method exists, and leaves the claim as it was.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.rpc2 import Rpc2MethodRefused

METHOD_NOT_FOUND = 268894210
AD410_DEFINITION = {"AudioFileManager": {"SirenFileManager": {"Support": True}}}


class _Client:
    def __init__(self, caps):
        self._caps = caps

    async def async_get_product_definition_rpc2(self, name=None):
        if name is None:
            return dict(AD410_DEFINITION)
        if name not in AD410_DEFINITION:
            raise Rpc2MethodRefused("no such block", code=268959743, method=name)
        return AD410_DEFINITION[name]

    async def async_get_coaxial_control_io_caps_rpc2(self):
        if isinstance(self._caps, BaseException):
            raise self._caps
        return dict(self._caps)


def _coordinator(caps, *, model="AD410", doorbell=True, manual_siren=False):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.client = _Client(caps)
    c.model = model
    c._channel = 0
    c._channel_number = 1
    c._address = "10.0.0.7"
    c._manual_siren = manual_siren
    c._manual_security_light = False
    c.is_doorbell = lambda: doorbell
    c.uses_recorder_deterrence = lambda: False
    c.is_ptz3e10x_t180 = lambda: False
    return c


def _no_such_method():
    return Rpc2MethodRefused(
        "Method not found!", code=METHOD_NOT_FOUND, method="CoaxialControlIO.getCaps"
    )


async def test_the_ad410_gets_no_siren_switch():
    c = _coordinator(_no_such_method())

    await c._async_probe_direct_deterrence()

    assert c.supports_siren() is False
    assert c.creates_siren_entity() is False
    assert c.uses_rpc2_deterrence(2) is False


async def test_the_claim_stays_and_the_dump_says_why():
    """The hardware claim is still true, so it stays a source; the reason it was
    not offered sits beside it, naming the code."""
    c = _coordinator(_no_such_method())

    await c._async_probe_direct_deterrence()

    assert c.get_siren_detection_sources() == [
        "ProductDefinition: AudioFileManager.SirenFileManager.Support=true"
    ]
    assert (
        "ProductDefinition siren not offered: CoaxialControlIO has no such "
        "method (code %d)" % METHOD_NOT_FOUND in c.get_siren_detection_failures()
    )


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ConnectionError(),
        Rpc2MethodRefused("Unknown error!", code=268959743),
        Rpc2MethodRefused("no code"),
    ],
    ids=["timeout", "connection", "another code", "no code"],
)
async def test_anything_else_keeps_the_claim(error):
    """Not knowing is not "no such method". The switch stays as it was."""
    c = _coordinator(error)

    await c._async_probe_direct_deterrence()

    assert c.supports_siren() is True
    assert c.creates_siren_entity() is True
    assert c.uses_rpc2_deterrence(2) is True


async def test_an_answer_without_the_flag_is_unchanged():
    """getCaps answering at all means the service is there; that case is not
    what this changes."""
    c = _coordinator({})

    await c._async_probe_direct_deterrence()

    assert c.supports_siren() is True


async def test_the_manual_option_still_offers_it():
    """An owner whose camera does answer the control call keeps a way back."""
    c = _coordinator(
        _no_such_method(), model="OEM-IPC", doorbell=False, manual_siren=True
    )

    await c._async_probe_direct_deterrence()

    assert c.supports_siren() is True
    assert c.uses_rpc2_deterrence(2) is True
