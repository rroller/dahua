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

Reintroduced against the real source, with the tests that failed:

| reintroduced | caught by |
| --- | --- |
| the call removed from the add step | `adding_it_again_warns` |
| matching on address alone | `two_devices_behind_one_address_are_fine` |
| the card raised for a recorder's channels | `the_channel_path_stays_quiet` |
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import config_flow
from custom_components.dahua.config_flow import DahuaFlowHandler

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
