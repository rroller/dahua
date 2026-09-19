"""A device that has refused the LightingScheme read is not asked again.

#654 added a check at light-command time: Smart Dual Light cameras decide
separately which emitter they will use, and while that says AIMode or
InfraredMode the white light stays off however correct the write was.

Measured afterwards on two recorders -- a DHI-NVR5464-16P-EI and the one on
#647 -- `getConfig&name=LightingScheme` returns `400 Bad Request`. So on every
NVR channel that check is a round trip that cannot succeed, plus a traceback in
the debug log for a warning that can never fire. One reporter has already gone
chasing that traceback.

Asking once per device keeps the check for everything that answers and costs a
refusing device exactly one request for the life of the entity.
"""

import pytest

from custom_components.dahua.light import DahuaIlluminator


class _Client:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    async def async_get_lighting_scheme(self):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class _Coordinator:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def get_device_name():
        return "Front Street"


def _illuminator(client):
    """The real entity, skipping only Home Assistant's own constructor."""
    entity = object.__new__(DahuaIlluminator)
    entity._coordinator = _Coordinator(client)
    entity._name = "Illuminator"
    entity._scheme_unreadable = False
    return entity


# --- a device that refuses ---------------------------------------------------

async def test_a_refusing_device_is_asked_exactly_once():
    client = _Client(Exception("400, message='Bad Request'"))
    entity = _illuminator(client)

    for _ in range(5):
        await entity._warn_if_the_scheme_blocks_it(3, "0")

    assert client.calls == 1, "a recorder cannot answer this, so asking again is waste"


async def test_the_command_still_succeeds_when_the_read_is_refused():
    """Not being able to check must never fail what the user asked for."""
    entity = _illuminator(_Client(Exception("400")))

    await entity._warn_if_the_scheme_blocks_it(3, "0")
    await entity._warn_if_the_scheme_blocks_it(3, "0")


# --- a device that answers is unaffected -------------------------------------

async def test_a_device_that_answers_is_asked_every_time():
    """The scheme is something the user can change on the camera at any moment,
    so a readable one must be re-read per command."""
    client = _Client({"table.LightingScheme[3][0].LightingMode": "WhiteMode"})
    entity = _illuminator(client)

    for _ in range(4):
        await entity._warn_if_the_scheme_blocks_it(3, "0")

    assert client.calls == 4


async def test_one_entity_refusing_does_not_silence_another():
    """The flag is per entity, so one channel cannot switch the check off for
    a different device that answers perfectly well."""
    refusing = _Client(Exception("400"))
    answering = _Client({"table.LightingScheme[0][0].LightingMode": "WhiteMode"})

    await _illuminator(refusing)._warn_if_the_scheme_blocks_it(3, "0")
    good = _illuminator(answering)
    await good._warn_if_the_scheme_blocks_it(0, "0")
    await good._warn_if_the_scheme_blocks_it(0, "0")

    assert answering.calls == 2
