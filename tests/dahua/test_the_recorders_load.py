"""The device's own processor and memory, surfaced as sensors.

Measured on a DHI-NVR5464-16P-EI before any of this was written:

    magicBox.getCPUUsage     -> {"usage": 28}
    magicBox.getMemoryInfo   -> {"free": 540459008.0, "total": 1239031808.0}
    magicBox.getUpTime       -> REFUSED, code 268632080

That last line is why this is a probe and not an assumption: the same recorder
answers two of the three, so answering one says nothing about the others. Both
working calls are RPC2 only with no CGI form, so a device with RPC2 switched off
gets no sensors at all.

The shape that needed care is not the reading, it is how often it is asked for.
A recorder is one entry with a coordinator per channel (#827), and
`async_setup_entry` gathers every channel's first refresh, so they ask
*concurrently*. A plain "answer, then remember it for a while" cache collapses
nothing on that cycle -- twelve channels each find nothing remembered and ask,
twenty-four requests for two numbers that describe the box. So the read is a
`_SharedRead`, registered before it is awaited, the same arrangement the config
reads use. `test_twelve_channels_asking_at_once` is the test that distinguishes
the two; the sequential one passes either way.

The mutations these are written against, none of which I can execute here (the
suite needs Home Assistant and the coordinator imports it):

* registering the task after awaiting it, or keeping a (timestamp, answer)
  cache -- the concurrent test counts twenty-four calls
* stamping `expires_at` before the request lands -- the refusal is remembered
  for the TTL, and `test_a_refusal_is_not_remembered` fails
* letting `_async_fetch_system_load` raise -- the poll's gather has no
  `return_exceptions`, and the carries-the-last-answer test fails with the
  refusal rather than returning
* carrying the whole previous poll rather than this read's keys -- the same
  test fails on the foreign key
"""

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.const import EntityCategory

from custom_components.dahua import client as client_module
from custom_components.dahua import sensor as sensor_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator


@pytest.fixture(autouse=True)
def _clear_shared_load():
    """Module global, and the suite runs under -n auto."""
    client_module._HOST_SYSTEM_LOAD.clear()
    yield
    client_module._HOST_SYSTEM_LOAD.clear()


MEASURED = {"usage": 28, "free": 540459008, "total": 1239031808}


def _client(device="10.0.0.1:80", raises=None):
    client = object.__new__(DahuaClient)
    client._device = device
    client.calls = []

    async def cpu():
        client.calls.append("cpu")
        # Yields, so a second caller arriving while this is in flight is a real
        # case here and not an artefact of nothing ever suspending.
        await asyncio.sleep(0)
        if raises is not None:
            raise raises
        return MEASURED["usage"]

    async def memory():
        client.calls.append("memory")
        await asyncio.sleep(0)
        return {"free": MEASURED["free"], "total": MEASURED["total"]}

    client.async_get_cpu_usage = cpu
    client.async_get_memory_info = memory
    return client


def _coordinator(client):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = client
    coordinator._address = "10.0.0.1"
    coordinator._channel = 0
    coordinator.data = {}
    coordinator._note_probe_refusal = lambda *args, **kwargs: None
    return coordinator


# --- one request per device, however many channels ask ----------------------


async def test_twelve_channels_asking_at_once_make_one_request():
    """The case a remember-the-answer cache does not cover.

    Setup gathers every channel's first refresh, so all twelve arrive before
    any of them has an answer to reuse.
    """
    client = _client()

    answers = await asyncio.gather(*[client.async_get_system_load() for _ in range(12)])

    assert client.calls == ["cpu", "memory"], client.calls
    assert answers == [MEASURED] * 12


async def test_each_caller_gets_its_own_copy():
    """One shared dict handed to twelve coordinators is one mutation away from
    a reading that changes behind them."""
    client = _client()

    first, second = await asyncio.gather(
        client.async_get_system_load(), client.async_get_system_load()
    )
    first["usage"] = 99

    assert second["usage"] == 28


async def test_a_later_poll_inside_the_ttl_reuses_the_answer():
    client = _client()

    await client.async_get_system_load()
    await client.async_get_system_load()

    assert client.calls == ["cpu", "memory"]


async def test_the_device_is_asked_again_once_the_answer_is_stale(monkeypatch):
    monkeypatch.setattr(client_module, "SYSTEM_LOAD_TTL_SECONDS", -1)
    client = _client()

    await client.async_get_system_load()
    await client.async_get_system_load()

    assert client.calls == ["cpu", "memory", "cpu", "memory"]


async def test_another_device_does_not_reuse_the_answer():
    """Keyed on the device, not the address: two boxes behind one IP on
    different ports are not each other (#664)."""
    first = _client(device="10.0.0.1:80")
    second = _client(device="10.0.0.1:81")

    await first.async_get_system_load()
    await second.async_get_system_load()

    assert second.calls == ["cpu", "memory"]


async def test_a_refusal_is_not_remembered():
    """A failed read must not answer "no" for the rest of the TTL."""
    client = _client(raises=ConnectionError("result=false"))

    for _ in range(2):
        with pytest.raises(ConnectionError):
            await client.async_get_system_load()

    assert client.calls == ["cpu", "cpu"]


async def test_a_write_clears_it():
    """clear_host_cache runs on every write and on the connector teardown, and
    a stale load reading should not outlive either."""
    client = _client()
    await client.async_get_system_load()

    client_module.clear_host_cache("10.0.0.1:80")

    assert client_module._HOST_SYSTEM_LOAD == {}


# --- the probe --------------------------------------------------------------


async def test_a_device_that_answers_gets_the_sensors():
    coordinator = _coordinator(_client())

    await coordinator._async_probe_system_load()

    assert coordinator.supports_system_load() is True


async def test_a_device_with_rpc2_off_gets_none():
    """`_async_rpc2_value` raises RuntimeError("RPC2 is disabled"), which is
    neither a ClientError nor a timeout, so the broad handler is what catches
    it. Setup must not fail over it."""
    coordinator = _coordinator(_client(raises=RuntimeError("RPC2 is disabled")))

    await coordinator._async_probe_system_load()

    assert coordinator.supports_system_load() is False


async def test_a_device_that_refuses_the_method_gets_none():
    """magicBox.getUpTime is refused on the measured recorder, arriving as an
    Rpc2MethodRefused, which is a ConnectionError. That is the shape a device
    that does not implement these two would answer with."""
    coordinator = _coordinator(_client(raises=ConnectionError("result=false")))

    await coordinator._async_probe_system_load()

    assert coordinator.supports_system_load() is False


# --- the poll read ----------------------------------------------------------


async def test_the_poll_reports_the_percentage_it_worked_out():
    """The device offers free and total and no percentage."""
    coordinator = _coordinator(_client())

    data = await coordinator._async_fetch_system_load()

    assert data["system.cpu_usage"] == 28
    assert data["system.memory_total"] == MEASURED["total"]
    assert data["system.memory_free"] == MEASURED["free"]
    # (1239031808 - 540459008) / 1239031808 == 56.4%
    assert data["system.memory_used_percent"] == pytest.approx(56.4, abs=0.1)


async def test_a_refused_read_keeps_the_last_answer():
    """The fan-out has no return_exceptions, so this must not raise -- and a
    refusal is not evidence the processor stopped (#1006, #1024)."""
    coordinator = _coordinator(_client(raises=ConnectionError("gone")))
    coordinator.data = {"system.cpu_usage": 28, "other.thing": "kept elsewhere"}

    data = await coordinator._async_fetch_system_load()

    assert data == {"system.cpu_usage": 28}, "it carried something that is not its own"


async def test_a_refused_read_with_nothing_remembered_says_nothing():
    coordinator = _coordinator(_client(raises=ConnectionError("gone")))

    assert await coordinator._async_fetch_system_load() is None


async def test_a_total_of_zero_does_not_divide_by_it():
    client = _client()

    async def no_memory():
        client.calls.append("memory")
        return {"free": 0, "total": 0}

    client.async_get_memory_info = no_memory
    coordinator = _coordinator(client)

    data = await coordinator._async_fetch_system_load()

    assert data["system.memory_used_percent"] is None


# --- which channel carries the sensors --------------------------------------
#
# The processor and memory describe the box, so a twelve channel recorder gets
# one pair of sensors, not twelve. _load_reporter is what picks the channel;
# the per-channel loop below it in async_setup_entry adds nothing of this kind.


def _reporter_candidate(channel, answers=True):
    return SimpleNamespace(
        supports_system_load=lambda: answers,
        get_channel=lambda: channel,
    )


def test_the_lowest_answering_channel_carries_them():
    chosen = sensor_module._load_reporter(
        [_reporter_candidate(4), _reporter_candidate(1), _reporter_candidate(9)]
    )

    assert chosen.get_channel() == 1


def test_a_channel_that_did_not_answer_is_not_chosen():
    """Channels of one recorder share a device, so in practice they all answer
    or none do -- but the sensors must come from a coordinator whose poll
    actually reads the figures, or they would report nothing for ever."""
    chosen = sensor_module._load_reporter(
        [_reporter_candidate(0, answers=False), _reporter_candidate(3)]
    )

    assert chosen.get_channel() == 3


def test_nothing_carries_them_when_the_device_does_not_report():
    assert (
        sensor_module._load_reporter(
            [
                _reporter_candidate(0, answers=False),
                _reporter_candidate(1, answers=False),
            ]
        )
        is None
    )


# --- what the two entities report -------------------------------------------


def _entity(cls, data):
    entity = object.__new__(cls)
    coordinator = SimpleNamespace(data=data, get_serial_number=lambda: "SERIAL1_0")
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    return entity


def test_the_processor_sensor_reports_a_percentage():
    entity = _entity(sensor_module.DahuaProcessorUsageSensor, {"system.cpu_usage": 28})

    assert entity.native_value == 28
    assert entity.native_unit_of_measurement == "%"
    assert entity.entity_category is EntityCategory.DIAGNOSTIC


def test_the_memory_sensor_carries_the_bytes_as_attributes():
    """Three entities to say one thing is how an entity list becomes
    unreadable, so the byte figures are attributes of the percentage."""
    entity = _entity(
        sensor_module.DahuaMemoryUsedSensor,
        {
            "id": "SERIAL1",
            "system.memory_used_percent": 56.4,
            "system.memory_total": MEASURED["total"],
            "system.memory_free": MEASURED["free"],
        },
    )

    assert entity.native_value == 56.4
    attributes = entity.extra_state_attributes
    assert attributes["total_bytes"] == MEASURED["total"]
    assert attributes["free_bytes"] == MEASURED["free"]
    # The base class's keys survive the override.
    assert attributes["integration"] == "dahua"


def test_a_device_that_has_not_answered_yet_reports_nothing():
    """Not zero: a nought percent processor is a claim, and an entity that
    makes it before the first poll lands is lying."""
    for cls in (
        sensor_module.DahuaProcessorUsageSensor,
        sensor_module.DahuaMemoryUsedSensor,
    ):
        assert _entity(cls, {}).native_value is None, cls.__name__
