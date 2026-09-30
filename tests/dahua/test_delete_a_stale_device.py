"""A stale device row should be deletable; the live one should not be.

Defining `async_remove_config_entry_device` at all is what puts a Delete button on a
device card. Without it `entry.supports_remove_device` is False and there is no
button, so removing one camera meant going and finding its config entry -- which on a
recorder means finding the right one of sixteen.

But it must say no to every device the entry currently creates, and since #827 that
is one per channel rather than one per entry. The identifier is derived from the
serial each channel reports, so Home Assistant would delete the row and the next
reload would put it straight back. A button that appears to do nothing is worse than
no button, and taking a live device deletes its entities from the registry along
with whatever the user had set on them.

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


def _hass():
    """No Dahua state of its own: runtime data lives on the entry."""
    return SimpleNamespace(data={})


def _device(*identifiers):
    return SimpleNamespace(identifiers=set(identifiers))


def _entry(entry_id="e1", serial=SERIAL, loaded=True, serials=None):
    """An entry carrying its coordinators, or an unloaded one carrying none.

    `serial` and `loaded` describe the entry rather than Home Assistant, which is
    what moving off hass.data makes obvious: an unloaded entry is one with no
    runtime_data at all, because Home Assistant deletes the attribute.

    `serials` is the #827 shape: one coordinator per channel. This fake only ever
    built one, which is why nothing noticed that the hook was reading the first
    coordinator and calling every other channel's live device stale.
    """
    entry = SimpleNamespace(entry_id=entry_id)
    if loaded:
        wanted = serials if serials is not None else [serial]
        entry.runtime_data = {
            channel: SimpleNamespace(get_serial_number=(lambda s=s: s))
            for channel, s in enumerate(wanted)}
    return entry


# --- the live device must stay ------------------------------------------------

async def test_the_device_this_entry_creates_cannot_be_removed():
    """Home Assistant would delete the row and the next reload would recreate it."""
    allowed = await async_remove_config_entry_device(
        _hass(), _entry(), _device((DOMAIN, SERIAL)))

    assert allowed is False


async def test_the_live_channel_device_cannot_be_removed():
    """Above channel 0 the identifier carries the channel."""
    allowed = await async_remove_config_entry_device(
        _hass(), _entry(serial=SERIAL + "_3"), _device((DOMAIN, SERIAL + "_3")))

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
        _hass(), _entry(serial=SERIAL),
        _device((DOMAIN, "4f3a9c8ecafe4f3a9c8ecafe4f3a9c8e")))

    assert allowed is True


async def test_a_channel_this_entry_does_not_own_can_be_removed():
    """A single camera entry owns one channel, so a row for another one is not
    its to keep alive. This used to be titled "another channel's row", which was
    right while every channel had its own entry and became wrong when #827 gave
    one entry all of them."""
    allowed = await async_remove_config_entry_device(
        _hass(), _entry(serial=SERIAL + "_3"), _device((DOMAIN, SERIAL + "_4")))

    assert allowed is True


# --- and on a merged recorder, every channel is live -------------------------

RECORDER = [SERIAL, SERIAL + "_1", SERIAL + "_3", SERIAL + "_9"]


@pytest.mark.parametrize("serial", RECORDER)
async def test_no_channel_of_a_merged_recorder_can_be_removed(serial):
    """The regression #827 introduced. The hook read the first coordinator's
    serial, so on a ten channel recorder nine live devices were offered a Delete
    button. Taking one deletes its entities from the registry, and the next reload
    brings the device back looking as though the button did nothing."""
    allowed = await async_remove_config_entry_device(
        _hass(), _entry(serials=RECORDER), _device((DOMAIN, serial)))

    assert allowed is False, "%s is a live channel of this entry" % serial


async def test_a_merged_recorder_still_gives_up_a_stale_row():
    """The other half: widening the check must not make everything unremovable,
    or #583's duplicate row becomes permanent again."""
    allowed = await async_remove_config_entry_device(
        _hass(), _entry(serials=RECORDER),
        _device((DOMAIN, "4f3a9c8ecafe4f3a9c8ecafe4f3a9c8e")))

    assert allowed is True


async def test_a_channel_the_recorder_no_longer_has_can_be_removed():
    """A channel deselected, or a camera unplugged from the recorder, leaves a
    row behind. That is the case the button exists for on a recorder."""
    allowed = await async_remove_config_entry_device(
        _hass(), _entry(serials=RECORDER), _device((DOMAIN, SERIAL + "_7")))

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
        _hass(), _entry(loaded=False), _device((DOMAIN, SERIAL)))

    assert allowed is False


async def test_an_unloaded_entry_refuses_even_a_stale_looking_row():
    """The same reasoning: without a coordinator, "stale" is a guess."""
    allowed = await async_remove_config_entry_device(
        _hass(), _entry(loaded=False), _device((DOMAIN, "something-else")))

    assert allowed is False


async def test_an_entry_the_domain_has_never_seen_refuses():
    """This used to be a distinct case and no longer is, which is worth saying.

    When the coordinator lived in `hass.data[DOMAIN]` there were two ways to know
    nothing about an entry: the domain key was absent entirely, or it was present
    without this entry in it. Runtime data has one state for both, the absence of
    the attribute, so this now approaches the same condition as
    `test_an_unloaded_entry_refuses` from the other side rather than testing a
    second mechanism. Kept because the answer still matters; narrowed to say so.
    """
    hass = SimpleNamespace(data={})

    allowed = await async_remove_config_entry_device(
        hass, _entry(loaded=False), _device((DOMAIN, SERIAL)))

    assert allowed is False


# --- and the button only exists because this is defined ----------------------

def test_the_hook_is_exported_under_the_name_home_assistant_looks_for():
    """Home Assistant sets entry.supports_remove_device by checking the integration
    module for this exact attribute. A rename silently removes the button again."""
    from custom_components import dahua

    assert callable(getattr(dahua, "async_remove_config_entry_device", None))
