"""A stale device row should be deletable; the live one should not be.

Defining `async_remove_config_entry_device` at all is what puts a Delete button on a
device card. Without it `entry.supports_remove_device` is False and there is no
button, so removing one camera meant going and finding its config entry -- which on a
recorder means finding the right one of sixteen.

But it must say no to the device the entry currently creates. One entry is one channel
is one device, and the identifier is derived from the serial the device reports, so
Home Assistant would delete the row and the next reload would put it straight back.
A button that appears to do nothing is worse than no button.

The case it says yes to is real. A camera that answered with a synthesised identity
and later reported its real serial leaves the old row behind: #583 has two device rows
for one camera, the live one carrying the fallback identity and the stale one carrying
the real model name, and nobody has been able to remove either.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import async_remove_config_entry_device
from custom_components.dahua.const import DOMAIN

SERIAL = "BC0A198PAJ779DF"


def _hass(entry_id="e1", serial=SERIAL, loaded=True):
    coordinator = SimpleNamespace(get_serial_number=lambda: serial)
    data = {DOMAIN: {entry_id: coordinator} if loaded else {}}
    return SimpleNamespace(data=data)


def _device(*identifiers):
    return SimpleNamespace(identifiers=set(identifiers))


def _entry(entry_id="e1"):
    return SimpleNamespace(entry_id=entry_id)


# --- the live device must stay ------------------------------------------------

async def test_the_device_this_entry_creates_cannot_be_removed():
    """Home Assistant would delete the row and the next reload would recreate it."""
    allowed = await async_remove_config_entry_device(
        _hass(), _entry(), _device((DOMAIN, SERIAL)))

    assert allowed is False


async def test_the_live_channel_device_cannot_be_removed():
    """Above channel 0 the identifier carries the channel."""
    allowed = await async_remove_config_entry_device(
        _hass(serial=SERIAL + "_3"), _entry(), _device((DOMAIN, SERIAL + "_3")))

    assert allowed is False


async def test_extra_identifiers_do_not_matter_if_one_is_the_live_one():
    """A device row can carry more than one identifier, and matching any of them
    means this is the device the entry is currently filling."""
    allowed = await async_remove_config_entry_device(
        _hass(), _entry(),
        _device((DOMAIN, SERIAL), ("other_integration", "something")))

    assert allowed is False


# --- and the stale one can go ------------------------------------------------

async def test_a_row_from_an_earlier_identity_can_be_removed():
    """#583: the camera used to answer with a synthesised id and now reports its
    real serial, so the old row sits there for ever with the real model name on it."""
    allowed = await async_remove_config_entry_device(
        _hass(serial=SERIAL), _entry(),
        _device((DOMAIN, "4f3a9c8ecafe4f3a9c8ecafe4f3a9c8e")))

    assert allowed is True


async def test_another_channels_row_can_be_removed_from_this_entry():
    """Each channel has its own entry, so a row for a different channel is not this
    entry's to keep alive."""
    allowed = await async_remove_config_entry_device(
        _hass(serial=SERIAL + "_3"), _entry(), _device((DOMAIN, SERIAL + "_4")))

    assert allowed is True


async def test_a_row_with_no_identifier_of_ours_can_be_removed():
    allowed = await async_remove_config_entry_device(
        _hass(), _entry(), _device(("some_other_domain", SERIAL)))

    assert allowed is True


# --- and when we cannot tell, say no ----------------------------------------

async def test_an_unloaded_entry_refuses():
    """Setup failed or the entry is unloaded, so which device is current cannot be
    known. Deleting the live one on a guess is worse than leaving a stale row."""
    allowed = await async_remove_config_entry_device(
        _hass(loaded=False), _entry(), _device((DOMAIN, SERIAL)))

    assert allowed is False


async def test_an_unloaded_entry_refuses_even_a_stale_looking_row():
    """The same reasoning: without a coordinator, "stale" is a guess."""
    allowed = await async_remove_config_entry_device(
        _hass(loaded=False), _entry(), _device((DOMAIN, "something-else")))

    assert allowed is False


async def test_an_entry_the_domain_has_never_seen_refuses():
    hass = SimpleNamespace(data={})

    allowed = await async_remove_config_entry_device(
        hass, _entry(), _device((DOMAIN, SERIAL)))

    assert allowed is False


# --- and the button only exists because this is defined ----------------------

def test_the_hook_is_exported_under_the_name_home_assistant_looks_for():
    """Home Assistant sets entry.supports_remove_device by checking the integration
    module for this exact attribute. A rename silently removes the button again."""
    from custom_components import dahua

    assert callable(getattr(dahua, "async_remove_config_entry_device", None))
