"""A Lighting_V2 table that turns up late brings its routing question with it.

`_supports_lighting_v2` is set in two places. Setup asks the question and then
asks a second one straight after it: a recorder channel -- a VSIPP on an NVR --
drives its white light through the channel-scoped two-table contract (#959),
and `_supports_channel_scoped_illuminator` is what routes it there. The retry
sets the same flag and stops.

So a recorder channel whose Lighting_V2 read went *silent* at setup, rather
than being refused, gets its illuminator back from the retry and gets it on the
wrong route. `uses_channel_scoped_illuminator` stays False for the life of the
entry, so `light.py` takes the plain path and writes `Lighting_V2`
`WhiteLight.Mode=Manual` and a brightness. #959 measured exactly that write on
a VSIPP-6DIRMD-I3 on a DHI-NVR5464: accepted, reads back, **and the light
stayed off**, because the scene is in `AIMode` and the camera ignores the
manual row. The entity appears and works; the lamp does not.

This is @rroller's third finding on #1201 in a second place: two call sites of
one gate, where only one of them asks the follow-up. The fix is the one that
PR settled on, `_async_discover_day_night_source`, so the follow-up has a name
and both call sites call it.

Reintroduced against the real source, with the tests that failed:

| reintroduced | caught by |
| --- | --- |
| the chain removed from the retry | `still_routes_to_the_channel`, `stays_whole_table` |
| the chain moved ahead of the transition | both of those, and `already_known_asks_nothing` |
| the discovery inlined at setup again | `the_route_is_decided_in_exactly_one_place` |
| the gate dropped | `without_the_table_is_not_asked`, `colour_4m_route_wins` |
| the setup call site deleted | `test_illumination_select_independent_shape.py`, which drives the real init block |
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module

from .integration_source import source

ADDRESS = "10.0.0.91"
CHANNEL = 5


class _Client:
    """Answers the two reads this pair of questions makes.

    `lighting_answers` is consumed in order and the last one repeats, so a test
    can say "silent, then it answers" without counting calls.
    """

    def __init__(self, *lighting_answers, channel_scoped=True):
        self.lighting_answers = list(lighting_answers)
        self.channel_scoped = channel_scoped
        self.lighting_reads = 0
        self.route_reads = 0
        self.route_channels = []

    async def async_get_lighting_v2(self):
        self.lighting_reads += 1
        answer = (
            self.lighting_answers[0]
            if len(self.lighting_answers) == 1
            else self.lighting_answers.pop(0)
        )
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    async def async_channel_scoped_white_light_supported(self, channel):
        self.route_reads += 1
        self.route_channels.append(channel)
        return self.channel_scoped


# What a recorder channel answers once it is talking: one Lighting_V2 row.
ANSWERED = {
    "table.Lighting_V2[5][0][0].Mode": "Manual",
    "table.Lighting_V2[5][0][0].LightType": "WhiteLight",
}


def _coordinator(*lighting_answers, channel_scoped=True, model="DHI-NVR5464-16P-EI"):
    """A coordinator carrying the two probes, the retry machinery and nothing else.

    `object.__new__` rather than the constructor, as the rest of this suite
    does: building a real one wants a hass, an entry and a device.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _Client(*lighting_answers, channel_scoped=channel_scoped)
    coordinator._channel = CHANNEL
    coordinator._channel_number = CHANNEL + 1
    coordinator._address = ADDRESS
    coordinator.model = model
    coordinator._supports_lighting_v2 = False
    coordinator._supports_lighting_scheme_illuminator = False
    coordinator._supports_channel_scoped_illuminator = False
    coordinator._probe_refusals = {}
    coordinator._capability_retries = {}
    coordinator.hass = None
    coordinator.config_entry = type("E", (), {"entry_id": "entry-1", "options": {}})()
    coordinator.get_serial_number = lambda: "AMR044032F4F06B33E_5"
    return coordinator


@pytest.fixture
def discoveries(monkeypatch):
    """What the coordinator announced, captured where it looks the name up.

    `coordinator.py` does `from .late import async_notify_capability_discovered`,
    so patching `late` would not reach the bound name and the real dispatcher
    would be called with `hass=None`.
    """
    announced = []
    monkeypatch.setattr(
        coordinator_module,
        "async_notify_capability_discovered",
        lambda hass, entry_id, channel_key: announced.append((entry_id, channel_key)),
    )
    return announced


# --- the consequence, as the user meets it -----------------------------------


async def test_a_late_table_still_routes_to_the_channel(discoveries):
    """The point of the file. Silent at setup, answering by the retry, and the
    channel-scoped route is decided rather than left at its default."""
    coordinator = _coordinator(TimeoutError(), ANSWERED)

    # Setup: the read goes silent, so the question stays open and the
    # follow-up is never reached.
    assert await coordinator._async_retry_lighting_v2() is False
    assert coordinator._supports_lighting_v2 is False
    assert coordinator.uses_channel_scoped_illuminator() is False
    assert coordinator.probes_that_did_not_answer() == {"lighting_v2"}

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator._supports_lighting_v2 is True
    assert coordinator.uses_channel_scoped_illuminator() is True
    assert coordinator.client.route_channels == [CHANNEL]


async def test_a_late_table_on_a_whole_table_camera_stays_whole_table(discoveries):
    """The other half of the same question. A camera that answers the
    channel-scoped probe with "no" must keep the route it had, or the fix would
    be a different bug: every late illuminator sent down the recorder path."""
    coordinator = _coordinator(TimeoutError(), ANSWERED, channel_scoped=False)
    await coordinator._async_retry_lighting_v2()

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator._supports_lighting_v2 is True
    assert coordinator.uses_channel_scoped_illuminator() is False
    assert coordinator.client.route_reads == 1


# --- and it is asked once, on the transition ---------------------------------


async def test_an_illuminator_already_known_asks_nothing():
    """A retry that finds the flag already set is not a discovery, so the route
    it already decided is not asked for again. Without this the question is put
    on every poll that reaches the retry."""
    coordinator = _coordinator(ANSWERED)
    coordinator._supports_lighting_v2 = True
    coordinator._supports_channel_scoped_illuminator = True

    assert await coordinator._async_retry_lighting_v2() is False
    assert coordinator.client.route_reads == 0
    assert coordinator.uses_channel_scoped_illuminator() is True


async def test_a_camera_without_the_table_is_not_asked():
    """The gate the setup block applies, applied here too: the channel-scoped
    probe costs two RPC2 reads and is only meaningful for a device that has
    Lighting_V2 at all."""
    coordinator = _coordinator({})
    coordinator._supports_lighting_v2 = False

    await coordinator._async_discover_illuminator_route()

    assert coordinator.client.route_reads == 0


async def test_the_colour_4m_route_wins_over_the_channel_scoped_one():
    """Mutually exclusive, the way the setup block has it: the whole-table
    LightingScheme path is checked first and the channel-scoped probe is not
    put to a device already on it."""
    coordinator = _coordinator(ANSWERED)
    coordinator._supports_lighting_v2 = True
    coordinator._supports_lighting_scheme_illuminator = True

    await coordinator._async_discover_illuminator_route()

    assert coordinator.client.route_reads == 0
    assert coordinator.uses_channel_scoped_illuminator() is False


async def test_a_refused_route_probe_leaves_the_route_alone():
    """A capability question must never be the thing that fails a poll, which
    is what the setup block's own comment says about this call."""

    async def _boom(channel):
        raise RuntimeError("RPC2 is switched off")

    coordinator = _coordinator(ANSWERED)
    coordinator._supports_lighting_v2 = True
    coordinator.client.async_channel_scoped_white_light_supported = _boom

    await coordinator._async_discover_illuminator_route()

    assert coordinator.uses_channel_scoped_illuminator() is False


# --- one gate, one place ------------------------------------------------------


def test_the_route_is_decided_in_exactly_one_place():
    """@rroller on #1201: a gate with two call sites is how the two come to
    disagree. `async_channel_scoped_white_light_supported` is the read behind
    the route, and only the named discovery helper may call it."""
    text = source()

    # Its definition in client.py, and exactly one caller. A second caller is
    # the shape this file exists to stop.
    assert text.count("async_channel_scoped_white_light_supported") == 2
    assert "await self._async_discover_illuminator_route()" in text
