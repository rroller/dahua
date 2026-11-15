"""Why a camera has a siren switch that does not work (#942).

@gabberpocky's AD410 claims a siren and refuses the call that operates it:

    CoaxialControlIO.control returned result=false
    (code=268894210, message=Method not found!)

#945 made that legible and stopped the switch asking twice. What it could not
do is decide whether the entity should exist at all, and that decision has been
blocked since on one missing measurement: whether `CoaxialControlIO.getCaps`
**raises** "Method not found" on that device, or **returns** without listing
speaker control. Those want different fixes, and the diagnostics download could
not tell them apart.

Not because the probe does not record it. `_async_probe_direct_deterrence`
writes a distinct reason for each case into `_siren_detection_failures`. The
reason never reached a reader: `get_siren_detection_sources` returns failures
only when there is no source, and on the AD410 there **is** one,
`AudioFileManager.SirenFileManager.Support=true`. So the evidence that the
control transport is missing was suppressed by the evidence that the hardware
is present, which is the one combination where both halves matter.

Two changes, both device independent:

- `get_siren_detection_failures` and its security-light twin return the raw
  list whatever the sources say, and diagnostics reports them beside the
  sources rather than instead of them.
- the getCaps reason carries the RPC2 code, so "Method not found" is not just
  `Rpc2MethodRefused`, which a timeout could also be.

This unblocks #942 rather than fixing it. What the AD410 answers is still
@gabberpocky's to report, and deliberately not guessed at here: a previous
attempt on this issue inferred it and was wrong.

Reintroduced against the real source, with the tests that failed:

| reintroduced | caught by |
| --- | --- |
| failures suppressed when a source exists | `the_ad410_shape_reports_both` |
| the RPC2 code dropped from the reason | `the_refusal_carries_its_code` |
| the diagnostics keys removed | `the_dump_carries_both_halves` |
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.rpc2 import Rpc2MethodRefused

METHOD_NOT_FOUND = 268894210

# What @gabberpocky's AD410 reports: the hardware claim and nothing from
# getCaps. `AudioFileManager` is an audio-file interface, which is the whole
# point of the issue, since it says nothing about CoaxialControlIO.
AD410_DEFINITION = {"AudioFileManager": {"SirenFileManager": {"Support": True}}}


class _Client:
    """Answers the two reads the deterrence probe makes."""

    def __init__(self, definition, caps):
        self._definition = definition
        self._caps = caps
        self.caps_calls = 0

    async def async_get_product_definition_rpc2(self, name=None):
        if name is None:
            return dict(self._definition)
        if name not in self._definition:
            raise Rpc2MethodRefused("no such block", code=268959743, method=name)
        return self._definition[name]

    async def async_get_coaxial_control_io_caps_rpc2(self):
        self.caps_calls += 1
        if isinstance(self._caps, Exception):
            raise self._caps
        return dict(self._caps)


def _coordinator(definition, caps):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _Client(definition, caps)
    coordinator.model = "AD410"
    coordinator._channel = 0
    coordinator._channel_number = 1
    coordinator._address = "10.0.0.7"
    coordinator.is_doorbell = lambda: True
    coordinator.uses_recorder_deterrence = lambda: False
    coordinator.is_ptz3e10x_t180 = lambda: False
    return coordinator


# --- the shape the issue is about --------------------------------------------


async def test_the_ad410_shape_reports_both():
    """A source and a failure at once. The sources reader keeps answering what
    it always did, and the failure is no longer invisible beside it."""
    coordinator = _coordinator(
        AD410_DEFINITION,
        Rpc2MethodRefused(
            "Method not found!",
            code=METHOD_NOT_FOUND,
            method="CoaxialControlIO.getCaps",
        ),
    )

    await coordinator._async_probe_direct_deterrence()

    assert coordinator.client.caps_calls == 1
    assert coordinator.get_siren_detection_sources() == [
        "ProductDefinition: AudioFileManager.SirenFileManager.Support=true"
    ]
    failures = coordinator.get_siren_detection_failures()
    assert any(str(METHOD_NOT_FOUND) in reason for reason in failures), failures


async def test_the_refusal_carries_its_code():
    """`Rpc2MethodRefused` alone does not separate "no such method" from the
    other things that raise it, and the code is what #942 turns on."""
    coordinator = _coordinator(
        AD410_DEFINITION,
        Rpc2MethodRefused("Method not found!", code=METHOD_NOT_FOUND),
    )

    await coordinator._async_probe_direct_deterrence()

    assert (
        "getCaps: query failed (Rpc2MethodRefused, code %d)" % METHOD_NOT_FOUND
        in coordinator.get_siren_detection_failures()
    )


async def test_an_answer_without_the_flag_is_a_different_reason():
    """The other branch, and the reason this is worth measuring rather than
    inferring: a device whose getCaps answers and omits speaker control needs a
    different fix from one that has no such method."""
    coordinator = _coordinator(AD410_DEFINITION, {})

    await coordinator._async_probe_direct_deterrence()

    failures = coordinator.get_siren_detection_failures()
    assert "getCaps: SupportControlSpeaker is missing or not true" in failures
    assert not any(str(METHOD_NOT_FOUND) in reason for reason in failures)


async def test_a_timeout_carries_no_code():
    """`getattr`, not a bare read. Only `method` is a class default on
    `Rpc2MethodRefused`, and a TimeoutError carries neither."""
    coordinator = _coordinator(AD410_DEFINITION, TimeoutError())

    await coordinator._async_probe_direct_deterrence()

    assert (
        "getCaps: query failed (TimeoutError)"
        in coordinator.get_siren_detection_failures()
    )


async def test_the_security_light_gets_the_same_reason():
    """The getCaps handler writes into both lists, so the light was as
    invisible as the siren."""
    coordinator = _coordinator(
        AD410_DEFINITION,
        Rpc2MethodRefused("Method not found!", code=METHOD_NOT_FOUND),
    )

    await coordinator._async_probe_direct_deterrence()

    assert any(
        str(METHOD_NOT_FOUND) in reason
        for reason in coordinator.get_security_light_detection_failures()
    )


# --- and it reaches the person reading the dump -------------------------------


def test_the_dump_carries_both_halves():
    """A reason nobody can read is the fault this file exists to remove, so the
    keys are asserted by name."""
    from custom_components.dahua import diagnostics as diagnostics_module

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._siren_detection_failures = ["getCaps: query failed (X, code 1)"]
    coordinator._security_light_detection_failures = ["light reason"]

    assert coordinator.get_siren_detection_failures() == [
        "getCaps: query failed (X, code 1)"
    ]
    assert coordinator.get_security_light_detection_failures() == ["light reason"]

    source = diagnostics_module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert '"supports_siren_failures"' in text
    assert '"supports_security_light_failures"' in text
    assert "get_siren_detection_failures()" in text
    assert "get_security_light_detection_failures()" in text
