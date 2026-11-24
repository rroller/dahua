"""Adding one device twice says so, instead of going quiet.

The add flow stops a duplicate by unique_id, and the unique_id is the serial:

    if "serialNumber" in data and data["serialNumber"] is not None:
        ...
        self._abort_if_unique_id_configured(...)

So the guard runs only on a device that told us its serial. A device whose
identity cannot be read returns none, no unique_id is set, and nothing stops it
being added again. That is the wrong way round, because an unidentified entry
is the one somebody re-adds, having assumed the first attempt failed. #1176 is
exactly that: a camera whose answers are unreadable, which ended up with two
entries.

A warning rather than an abort. Two entries on one address and port is a real
configuration: different credentials for one device is the case
`_rpc2_config_write_lock` already accommodates, which is why it is keyed by
device rather than by `_rpc2_key()`, and a recorder added before #827 merged
its channels looks the same from here. Blocking would break both.

Address **and** port, because two devices can sit behind one address on
different ports, which is the distinction `device_key` exists for.

What the duplicate costs is narrower than it looks, and the card says so rather
than overstating it. Reads, the digest challenge and the RPC2 login are shared
per device, keyed on `(device, username)`, so a second entry does not double
the request rate while things work. It doubles the entities, and once a shared
login fails the cached result is dropped so every read retries, which is when
two entries push twice as hard.

And it comes down when the duplicate does, which the first version of this did
not do at all (@rroller, #1212). One function withdraws it, from the removal
hook and from setup, which is what a reconfiguration reloads through. It sweeps
the cards that are up and keeps the ones the live entries still justify, rather
than sweeping the pairs the entries hold: removing both entries at once leaves
a pair no entry carries in, and @rroller reached that in one `asyncio.gather`
after the first fix. It never raises: only the add step decides a pair is a
duplicate, because a recorder whose merge refused has an entry per channel on
one address and port and is not one.

Reintroduced against the real source, with the tests that failed:

| reintroduced | caught by |
| --- | --- |
| the call removed from the add step | `adding_it_again_warns` |
| matching on address alone | `two_devices_behind_one_address_are_fine` |
| the card raised for a recorder's channels | `the_channel_path_stays_quiet` |
| the removal reconciling nothing | 6, from `deleting_the_duplicate...` |
| that call nested in the last-entry branch | the same 6 |
| the setup sweep removed | `every_setup_sweeps_for_stale_cards` |
| withdrawing whether or not a duplicate remains | 4, from `the_card_stays...` |
| the sweep raising as well as withdrawing | `a_legacy_per_channel_recorder...` |
| the key dropping the port | 8, from `two_devices_on_one_address...` |
| the key taking the address as typed | `one_device_spelled_two_ways...` |
| the sweep reading live pairs instead of cards | `both_entries_removed_at_once`, `two_stale_cards...` |
| the prefix filter dropped | `the_sweep_leaves_the_other_repairs_alone` |
| the still-true check dropped | 4, from `the_card_stays...` |
| the registry read without a copy | 2, from `the_sweep_leaves_the_other...` |
"""

import asyncio
from types import SimpleNamespace

import pytest

from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import dahua as dahua_module
from custom_components.dahua import (
    ISSUE_DUPLICATE_ADDRESS,
    async_reconcile_duplicate_addresses,
)
from custom_components.dahua import config_flow
from custom_components.dahua.config_flow import DahuaFlowHandler
from custom_components.dahua.const import DOMAIN

ADDRESS = "10.0.0.5"
PORT = "80"


def _entry(address=ADDRESS, port=PORT, title="Front Door"):
    return SimpleNamespace(
        data={"address": address, "port": port},
        title=title,
        entry_id="e-" + title,
    )


def _handler(monkeypatch, existing=()):
    """A flow handler carrying the warning and nothing else.

    `object.__new__` because building a real one wants a hass and a flow
    manager, and the repair call is captured where the module looks it up.
    """
    raised = []
    monkeypatch.setattr(
        config_flow.ir,
        "async_create_issue",
        lambda *args, **kwargs: raised.append((args, kwargs)),
    )
    handler = object.__new__(DahuaFlowHandler)
    handler.hass = SimpleNamespace()
    handler._async_current_entries = lambda: list(existing)
    return handler, raised


# --- the case the card exists for --------------------------------------------


def test_adding_it_again_warns(monkeypatch):
    """The whole point: a device with no serial can be added twice, and until
    now nothing anywhere said so."""
    handler, raised = _handler(monkeypatch, existing=[_entry()])

    handler._async_warn_duplicate_address({"address": ADDRESS, "port": PORT})

    assert len(raised) == 1
    kwargs = raised[0][1]
    assert kwargs["translation_key"] == "duplicate_address"
    assert kwargs["translation_placeholders"] == {
        "address": ADDRESS,
        "port": PORT,
        "existing": "Front Door",
    }


def test_the_card_does_not_offer_to_fix_it(monkeypatch):
    """Which entry to keep is the owner's choice, and deleting one for them is
    not something this can get right. Persistent because it is raised while
    setup is still running, and a card that vanishes on the next restart is no
    use to somebody reading it afterwards."""
    handler, raised = _handler(monkeypatch, existing=[_entry()])

    handler._async_warn_duplicate_address({"address": ADDRESS, "port": PORT})

    kwargs = raised[0][1]
    assert kwargs["is_fixable"] is False
    assert kwargs["is_persistent"] is True
    assert kwargs["severity"] is config_flow.ir.IssueSeverity.WARNING


# --- and stays quiet everywhere else ------------------------------------------


def test_the_first_entry_warns_about_nothing(monkeypatch):
    handler, raised = _handler(monkeypatch, existing=[])

    handler._async_warn_duplicate_address({"address": ADDRESS, "port": PORT})

    assert raised == []


def test_two_devices_behind_one_address_are_fine(monkeypatch):
    """Matching on the address alone would call this a duplicate. It is two
    devices on one NAT address, which `device_key` is written for."""
    handler, raised = _handler(monkeypatch, existing=[_entry(port="8000")])

    handler._async_warn_duplicate_address({"address": ADDRESS, "port": PORT})

    assert raised == []


def test_a_different_device_is_not_a_duplicate(monkeypatch):
    handler, raised = _handler(monkeypatch, existing=[_entry(address="10.0.0.9")])

    handler._async_warn_duplicate_address({"address": ADDRESS, "port": PORT})

    assert raised == []


def test_no_address_warns_about_nothing(monkeypatch):
    """Nothing in the dict to compare. Raising on that would put a card up
    naming an empty address."""
    handler, raised = _handler(monkeypatch, existing=[_entry()])

    handler._async_warn_duplicate_address({"port": PORT})

    assert raised == []


# --- the call sites -----------------------------------------------------------


def test_the_channel_path_stays_quiet():
    """A recorder's extra channels arrive on the import step and share the
    address of the entry that spawned them, so warning there would fire on
    every legitimate channel. Asserted on the source because the one thing
    worth pinning is that the call is not added back."""
    source = config_flow.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    # The definition and exactly one caller.
    assert text.count("_async_warn_duplicate_address") == 2
    assert "self._async_warn_duplicate_address(self.init_info)" in text


def test_the_card_has_its_strings():
    """A missing translation key ships a card showing the raw key."""
    import json
    from pathlib import Path

    en = json.loads(
        (Path(config_flow.__file__).parent / "translations" / "en.json").read_text(
            encoding="utf-8"
        )
    )
    card = en["issues"]["duplicate_address"]
    for name in ("address", "port", "existing"):
        assert "{%s}" % name in card["title"] + card["description"]


# --- and comes down when the duplicate does -----------------------------------
#
# @rroller on #1212: the card told the owner to delete the entry they did not
# want, and then stayed when they did. Nothing withdrew it, so Repairs went on
# describing a duplicate that no longer existed, deleting both entries left it
# up too, and being persistent it survived a restart.
#
# These drive the real issue registry, the real add-flow call that raises the
# card, and Home Assistant's own removal, rather than formatting the issue id
# here. An id built one way on the way up and another on the way down is the
# shape of the bug (`_async_forget_host`'s docstring), and a test that formats
# the id itself cannot see that.
#
# `enable_custom_integrations` is load-bearing: `ConfigEntry.async_remove`
# resolves the integration first and returns early on `IntegrationNotFound`, so
# without it the removal hook never runs and all of this passes vacuously.


def _mock_entry(hass, *, address=ADDRESS, port=PORT, title="Front Door"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={
            "username": "u",
            "password": "p",
            "address": address,
            "port": port,
            "rtsp_port": "554",
            "channel": 0,
            "name": title,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _raise_the_card(hass, address=ADDRESS, port=PORT):
    """Through the add flow's own call, so the id is the one production writes."""
    handler = object.__new__(DahuaFlowHandler)
    handler.hass = hass
    handler._async_current_entries = lambda: list(
        hass.config_entries.async_entries(DOMAIN)
    )
    handler._async_warn_duplicate_address({"address": address, "port": port})


def _card_is_up(hass, address=ADDRESS, port=PORT):
    issue_id = ISSUE_DUPLICATE_ADDRESS.format(address, port)
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_deleting_the_duplicate_withdraws_the_card(
    hass, enable_custom_integrations
):
    """What the card asks for. Doing it left the card up, which is worse than
    not having raised it: the owner has resolved the thing it describes and has
    no way to dismiss it."""
    _mock_entry(hass, title="Front Door")
    second = _mock_entry(hass, title="Front Door 2")
    _raise_the_card(hass)
    assert _card_is_up(hass)

    await hass.config_entries.async_remove(second.entry_id)
    await hass.async_block_till_done()

    assert not _card_is_up(hass)


async def test_deleting_both_entries_withdraws_the_card(
    hass, enable_custom_integrations
):
    """The other half of @rroller's reproduction. The card is gone by the first
    of the two deletions, because one entry is no longer a duplicate, and the
    second finds nothing to do. Worth having as its own test: the reported
    symptom was the card surviving both, and this order is the one where a
    sweep of live pairs is enough. Doing the same two deletions at once is not,
    which is the test below."""
    first = _mock_entry(hass, title="Front Door")
    second = _mock_entry(hass, title="Front Door 2")
    _raise_the_card(hass)

    await hass.config_entries.async_remove(second.entry_id)
    await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()

    assert not _card_is_up(hass)


async def test_both_entries_removed_at_once_withdraws_the_card(
    hass, enable_custom_integrations
):
    """@rroller's second reproduction on #1212. Deleting both at once is not the
    same shape as deleting them one after the other: there, the first removal
    leaves one entry on the pair and that entry is what carries the pair into
    the sweep. Here both are out of the registry before either hook runs, so a
    sweep of what the entries hold visits the pair not at all, finds nothing to
    withdraw, and the persistent card survives the restart as well."""
    first = _mock_entry(hass, title="Front Door")
    second = _mock_entry(hass, title="Front Door 2")
    _raise_the_card(hass)
    assert _card_is_up(hass)

    await asyncio.gather(
        hass.config_entries.async_remove(first.entry_id),
        hass.config_entries.async_remove(second.entry_id),
    )
    await hass.async_block_till_done()

    assert not _card_is_up(hass)


async def test_two_stale_cards_both_come_down(hass, enable_custom_integrations):
    """Two devices, each added twice, each pair then emptied at once. The same
    shape as the test above with a second card beside it, which is what says
    the sweep withdraws every stale card rather than the one it happens to
    reach first.

    Not the test for iterating the registry while withdrawing from it. That
    raises a `RuntimeError`, and Home Assistant catches whatever the removal
    hook raises and logs it, so the cards still come down here; the two tests
    that call the sweep directly are what turn red."""
    other = "10.0.0.6"
    entries = [
        _mock_entry(hass, address=address, title=title)
        for address, title in (
            (ADDRESS, "Front Door"),
            (ADDRESS, "Front Door 2"),
            (other, "Garage"),
            (other, "Garage 2"),
        )
    ]
    _raise_the_card(hass)
    _raise_the_card(hass, address=other)
    assert _card_is_up(hass) and _card_is_up(hass, address=other)

    await asyncio.gather(
        *(hass.config_entries.async_remove(entry.entry_id) for entry in entries)
    )
    await hass.async_block_till_done()

    assert not _card_is_up(hass)
    assert not _card_is_up(hass, address=other)


async def test_the_sweep_leaves_the_other_repairs_alone(
    hass, enable_custom_integrations
):
    """The sweep reads every issue this integration has raised, not only the
    duplicate ones. None of the others is its to withdraw: a leftover row, a
    serial conflict or a channel that would not add describes something the
    sweep has not looked at and cannot have resolved."""
    entry = _mock_entry(hass, address="10.0.0.9")
    bystanders = [
        (dahua_module.ISSUE_SIBLINGS_REMAIN.format("10.0.0.5"), "siblings_remain"),
        (dahua_module.ISSUE_LEFTOVER_ROWS.format(entry.entry_id), "leftover_rows"),
        (dahua_module.ISSUE_SERIAL_CONFLICT.format("ABC123"), "serial_conflict"),
        (
            dahua_module.ISSUE_CHANNEL_NOT_ADDED.format(entry.entry_id, 3),
            "channel_not_added",
        ),
    ]
    for issue_id, translation_key in bystanders:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=translation_key,
        )
    _raise_the_card(hass, address="10.0.0.9")
    assert _card_is_up(hass, address="10.0.0.9")

    async_reconcile_duplicate_addresses(hass)

    assert not _card_is_up(hass, address="10.0.0.9")
    registry = ir.async_get(hass)
    assert [
        issue_id
        for issue_id, _key in bystanders
        if registry.async_get_issue(DOMAIN, issue_id) is None
    ] == []


async def test_the_card_stays_while_a_duplicate_remains(
    hass, enable_custom_integrations
):
    """Three entries on one device, one deleted. Still a duplicate, and
    withdrawing on any removal would take the card down while the thing it
    describes is still true."""
    _mock_entry(hass, title="Front Door")
    _mock_entry(hass, title="Front Door 2")
    third = _mock_entry(hass, title="Front Door 3")
    _raise_the_card(hass)

    await hass.config_entries.async_remove(third.entry_id)
    await hass.async_block_till_done()

    assert _card_is_up(hass)


async def test_another_devices_card_is_untouched(hass, enable_custom_integrations):
    _mock_entry(hass, title="Front Door")
    second = _mock_entry(hass, title="Front Door 2")
    _mock_entry(hass, address="10.0.0.9", title="Garage")
    _mock_entry(hass, address="10.0.0.9", title="Garage 2")
    _raise_the_card(hass)
    _raise_the_card(hass, address="10.0.0.9")

    await hass.config_entries.async_remove(second.entry_id)
    await hass.async_block_till_done()

    assert not _card_is_up(hass)
    assert _card_is_up(hass, address="10.0.0.9"), "the other device still has two"


async def test_two_devices_on_one_address_keep_their_own_cards(
    hass, enable_custom_integrations
):
    """The port is half the key on the way down as well as on the way up.
    Withdrawing by address alone would take the card off the device on 8000,
    which still has two entries."""
    _mock_entry(hass, title="Front Door")
    second = _mock_entry(hass, title="Front Door 2")
    _mock_entry(hass, port="8000", title="Gate")
    _mock_entry(hass, port="8000", title="Gate 2")
    _raise_the_card(hass)
    _raise_the_card(hass, port="8000")

    await hass.config_entries.async_remove(second.entry_id)
    await hass.async_block_till_done()

    assert not _card_is_up(hass)
    assert _card_is_up(hass, port="8000")


async def test_one_device_spelled_two_ways_is_one_duplicate(
    hass, enable_custom_integrations
):
    """A trailing slash is the same device, and `normalize_address` is how
    everything host scoped says so: one left a single device holding two
    differently keyed request pools.

    Taking the address as typed counts these two entries as two devices. The
    card still goes up, because the add step finds the entry spelled the way
    this one was, but it is then keyed on a pair holding one entry, and
    deleting that entry leaves the pair holding none, which a sweep of what
    the entries hold cannot reach. That is the card nothing can take down,
    and it is the withdrawal that fails rather than the raising."""
    _mock_entry(hass, address=ADDRESS + "/", title="Front Door")
    second = _mock_entry(hass, title="Front Door 2")
    _raise_the_card(hass)
    assert _card_is_up(hass)

    await hass.config_entries.async_remove(second.entry_id)
    await hass.async_block_till_done()

    assert not _card_is_up(hass), "the other spelling is the same device"


async def test_moving_one_entry_elsewhere_withdraws_the_card(
    hass, enable_custom_integrations
):
    """Reconfiguration, which @rroller named alongside removal. The entry that
    moves takes its own pair with it, so the pair the card is on is the one
    place that entry can no longer be found. The entry that stayed is what
    finds it, which is why the sweep is over every pair the entries hold."""
    _mock_entry(hass, title="Front Door")
    second = _mock_entry(hass, title="Front Door 2")
    _raise_the_card(hass)

    hass.config_entries.async_update_entry(
        second, data={**second.data, "address": "10.0.0.6"}
    )
    async_reconcile_duplicate_addresses(hass)

    assert not _card_is_up(hass)


async def test_a_reload_keeps_a_card_that_is_still_true(
    hass, enable_custom_integrations
):
    """The sweep runs on every setup, so it must not take down a card for a
    duplicate that is still there."""
    _mock_entry(hass, title="Front Door")
    _mock_entry(hass, title="Front Door 2")
    _raise_the_card(hass)

    async_reconcile_duplicate_addresses(hass)

    assert _card_is_up(hass)


async def test_a_legacy_per_channel_recorder_is_not_given_a_card(
    hass, enable_custom_integrations
):
    """The sweep withdraws and never raises. A recorder whose merge refused has
    an entry per channel on one address and port, and so does any setup from
    before #827; raising from here would put a card on every one of them, which
    is what the add step was careful not to do."""
    _mock_entry(hass, title="Ch1")
    _mock_entry(hass, title="Ch2")

    async_reconcile_duplicate_addresses(hass)

    assert not _card_is_up(hass)


def test_the_withdrawal_runs_outside_the_last_entry_block():
    """The one case where something must be withdrawn while the host still has
    entries. Deleting the second entry is what the card asked for, `siblings` is
    then not empty, and `_async_forget_host` does not run. Pinned on the source
    because nesting the call inside that block passes every test above: they all
    leave the host with entries only where the card must stay up."""
    with open(dahua_module.__file__, encoding="utf-8") as handle:
        text = handle.read()

    removal = text.split("async def async_remove_entry")[1].split("\nasync def ")[0]
    called = [
        line
        for line in removal.splitlines()
        if "async_reconcile_duplicate_addresses(hass)" in line
    ]
    assert len(called) == 1
    # Four spaces and no more: the body of the hook, not the branch above it.
    assert called[0].startswith("    async_reconcile")
    assert not called[0].startswith("     ")


def test_every_setup_sweeps_for_stale_cards():
    """The sweep's other call site, and the only one reconfiguration reaches:
    changing an address reloads the entry. Pinned on the source because the
    sweep is tested here by calling it, which cannot tell whether anything in
    production ever does.
    """
    with open(dahua_module.__file__, encoding="utf-8") as handle:
        text = handle.read()

    setup = text.split("async def async_setup_entry")[1].split("\nasync def ")[0]
    assert "async_reconcile_duplicate_addresses(hass)" in setup
