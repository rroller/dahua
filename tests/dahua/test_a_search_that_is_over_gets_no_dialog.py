"""A progress dialog nobody can be told to close, and how not to raise one.

@Dieghito72 on #949: adding a VTH2421F-P sat on "Looking for other channels on
this device. This can take up to 30 seconds." for ever. His own log shows the
search answering in **six milliseconds**, which rules out the slow-search
reading that #1000 was closed on. The monitor does answer with a RemoteDevice
table; none of its rows is spelled as a slot, so there is nothing to probe.
test_discover_channels.py pins that half.

The dialog is the part that never moved. A progress step tells the browser it
is over by firing one `data_entry_flow_progressed` bus event, which core fires
whether or not anyone is listening and never replays. The frontend subscribes
in `willUpdate`, after it has received and rendered the progress step, so an
event fired a few milliseconds after the POST response went out lands before
the subscription exists. home-assistant/frontend#54866 added a catch-up fetch
for exactly this on 2026-10-19, which no release carries as of 2026-10-27
(the newest is 20260930.5), and the 2023 attempt at it was reverted.

So these tests are about not raising the dialog in the first place. They drive
the real flow manager rather than calling the step, because the manager is
where the progress plumbing is: `test_add_several_channels.py` calls
`async_step_discover()` directly and so never sees a step transition at all.
"""

import asyncio

import pytest

from homeassistant.data_entry_flow import FlowResultType

import custom_components.dahua.config_flow as flow_module
from custom_components.dahua.config_flow import DahuaFlowHandler
from custom_components.dahua.const import (
    CONF_ADDRESS,
    CONF_CHANNEL,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def _integration(enable_custom_integrations):
    """The manager loads the handler by domain, so the component must exist."""
    yield


@pytest.fixture(autouse=True)
def _no_preview(monkeypatch):
    """The naming step fetches a still. There is no device at the far end of
    these tests, and a five second wait for one is not what is under test."""

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(flow_module, "async_fetch_preview", nothing)


@pytest.fixture(autouse=True)
def _credentials_accepted(monkeypatch):
    async def accepted(*args, **kwargs):
        return {"name": "VTH", "serialNumber": "SER-949"}, None

    monkeypatch.setattr(DahuaFlowHandler, "_test_credentials", accepted)


def _typed_in():
    return {
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "pw",
        CONF_ADDRESS: "192.168.1.201",
        CONF_CHANNEL: 0,
    }


def _search_returning(found, gate=None):
    async def search(self, user_input, exclude):
        if gate is not None:
            await gate.wait()
        return dict(found)

    return search


async def _submit(hass, monkeypatch, search):
    """Fill in the add form, the way the browser does, and return the step."""
    monkeypatch.setattr(DahuaFlowHandler, "_async_discover_channels", search)
    started = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert started["type"] == FlowResultType.FORM
    return started["flow_id"], await hass.config_entries.flow.async_configure(
        started["flow_id"], _typed_in()
    )


async def test_a_search_that_is_already_over_never_shows_a_dialog(hass, monkeypatch):
    """The #949 device. Nothing to search, so nothing to watch.

    The assertion is on the *type*: a form is something the browser renders
    from the response it already has, and needs no event to arrive.
    """
    _, result = await _submit(hass, monkeypatch, _search_returning({}))

    assert result["type"] == FlowResultType.FORM, result
    assert result["step_id"] == "name"


async def test_a_search_that_is_already_over_still_offers_what_it_found(
    hass, monkeypatch
):
    """Skipping the dialog must not skip the answer. A recorder on a quiet
    network can finish inside the grace too, and its channels still have to be
    offered."""
    _, result = await _submit(hass, monkeypatch, _search_returning({1: "BACKYARD"}))

    assert result["type"] == FlowResultType.FORM, result
    assert result["step_id"] == "channels"
    assert result["description_placeholders"]["count"] == "1"


async def test_a_search_still_running_after_the_grace_gets_the_dialog(
    hass, monkeypatch
):
    """The case the dialog exists for is untouched: fifteen channels probed two
    at a time is slow, and the user is told so rather than watching a submit
    button."""
    monkeypatch.setattr(flow_module, "PROGRESS_GRACE_SECONDS", 0.01)
    gate = asyncio.Event()

    flow_id, result = await _submit(
        hass, monkeypatch, _search_returning({2: "DRIVE"}, gate)
    )

    assert result["type"] == FlowResultType.SHOW_PROGRESS, result
    assert result["step_id"] == "discover"
    assert result["description_placeholders"]["seconds"] == str(
        flow_module.DISCOVERY_TIMEOUT_SECONDS
    )

    # The grace expiring must not cancel the search underneath it.
    gate.set()
    await hass.async_block_till_done()

    # What the browser does when it hears the step has moved on, and what it
    # now also does once after subscribing. Either way it is this GET, which
    # the manager serves by running the flow on.
    arrived = await hass.config_entries.flow.async_configure(flow_id)

    assert arrived["type"] == FlowResultType.FORM, arrived
    assert arrived["step_id"] == "channels"
    assert arrived["description_placeholders"]["count"] == "1"


async def test_the_grace_does_not_charge_a_fast_search_for_its_length(
    hass, monkeypatch
):
    """The wait is a ceiling, not a delay. Two seconds added to every add that
    has nothing to search would be the fix costing more than the bug."""
    loop = asyncio.get_running_loop()
    before = loop.time()

    _, result = await _submit(hass, monkeypatch, _search_returning({}))

    assert result["step_id"] == "name"
    assert loop.time() - before < flow_module.PROGRESS_GRACE_SECONDS
