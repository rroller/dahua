"""A coaxial probe nobody ran reads None, not False (#942, #1211).

Setup asks for the coaxial control status only where something reads it and
deterrence is not driven over RPC2. Where it is not asked, the flag used to sit
at its constructor default, False, which is also what a device saying no
leaves behind. A diagnostics dump could not tell "the device refused" from
"the question was never put", and that was misread once on #942.

None is falsy, so every reader that only asks "may I poll the status" behaves
exactly as before. What changes is the dump.
"""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.const import DOMAIN
from custom_components.dahua.diagnostics import _capabilities_block

from .test_what_setup_probes import _coordinator


async def test_a_new_coordinator_has_not_asked(hass):
    """The constructor, not a double: what a real channel starts with."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    coordinator = DahuaDataUpdateCoordinator(
        hass,
        entry,
        events=[],
        address="10.0.0.5",
        port=80,
        rtsp_port=554,
        username="u",
        password="p",
        name="Front",
        channel=0,
    )

    assert coordinator._supports_coaxial_control is None
    probed = _capabilities_block(coordinator)["probed"]
    assert "coaxial_control" in probed, "an unasked probe must stay in the dump"
    assert probed["coaxial_control"] is None


def _unasked(hass):
    coordinator = _coordinator(hass)
    # The double presets every flag False, which is the old default this file
    # is about; put back what the constructor now gives.
    coordinator._supports_coaxial_control = None
    return coordinator


async def test_nothing_reads_the_status_so_it_stays_unasked(hass):
    coordinator = _unasked(hass)
    coordinator.reads_coaxial_status = lambda: False

    await coordinator._async_update_data()

    assert "async_get_coaxial_control_io_status" not in coordinator.client.asked
    assert coordinator._supports_coaxial_control is None


async def test_deterrence_over_rpc2_leaves_it_unasked(hass):
    """The AD410 on #942: a siren driven over RPC2, so setup skips this probe."""
    coordinator = _unasked(hass)
    coordinator.uses_rpc2_deterrence = lambda *a, **k: True

    await coordinator._async_update_data()

    assert "async_get_coaxial_control_io_status" not in coordinator.client.asked
    assert coordinator._supports_coaxial_control is None


async def test_a_refusal_still_reads_false(hass):
    coordinator = _coordinator(hass, refusing="async_get_coaxial_control_io_status")
    coordinator._supports_coaxial_control = None

    await coordinator._async_update_data()

    assert coordinator._supports_coaxial_control is False


async def test_an_answer_still_reads_true(hass):
    coordinator = _unasked(hass)

    await coordinator._async_update_data()

    assert coordinator._supports_coaxial_control is True


@pytest.mark.parametrize("flag, polled", [(None, False), (True, True)])
async def test_only_an_answered_probe_polls_the_status(hass, flag, polled):
    """None must stay falsy where it matters: the poll reads the status only
    for a channel whose probe answered. True is the control, so a poll that
    never reaches this branch cannot pass the None case for free."""
    coordinator = _unasked(hass)
    coordinator.reads_coaxial_status = lambda: False
    await coordinator._async_update_data()
    coordinator.reads_coaxial_status = lambda: True
    coordinator._supports_coaxial_control = flag
    read = []

    async def _record(*args, **kwargs):
        read.append(args)

    coordinator._async_coaxial_status = _record

    await coordinator._async_update_data()

    assert bool(read) is polled
