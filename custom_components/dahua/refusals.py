"""What a device has refused outright, so it is not asked a second time.

Some Dahua devices advertise a capability and then refuse the call that operates
it. Measured:

    DHI-NVR5464-16P-EI   every Lighting write          HTTP 403 "Authority:check failure."
    DHI-NVR5464-16P-EI   same, over RPC2               errCode 285278249, same message
    AD410 (#942)         CoaxialControlIO.control      errCode 268894210 "Method not found!"

In each case the control can never work on that device, so asking again on every
press spends a request to hear the same answer and leaves somebody pressing a
button that looks like it might work. That is the shape #525 was about, and the
answer there was not to create a control the device cannot serve.

Here the refusal is the only way to find out -- nothing readable predicts it -- so
it is learnt from the first one instead.

This module imports nothing but the standard library on purpose: it is the part of
the decision worth testing without Home Assistant, and the entities translate its
answer into something a user reads.
"""

import logging

_LOGGER = logging.getLogger(__name__)

# A device answering and declining, as opposed to one that could not be reached.
#
# 403 is a Dahua recorder refusing a config write, with `Authority:check failure.`
# in the body. 285278249 is the same answer over RPC2, and 268894210 is "Method
# not found!" -- a method either exists on a device or it does not.
#
# Deliberately absent:
#   401               the credentials, which reauth exists for and which a later
#                     write may well succeed with
#   287637504         an expired session, which the shared-login retry handles
#   268959743         "Unknown error! error code was not set in service!" -- a
#                     device declining without saying why. #943 has one that
#                     answers this where it used to work, so it is not permanent
#                     and must not be remembered as if it were
#   400, 500          the request or the device, not the capability
REFUSED_STATUSES = frozenset({403})
REFUSED_RPC2_CODES = frozenset({285278249, 268894210})

# (address, channel, control) the device has refused. `control` names what was
# being operated -- "infrared", "siren" -- so refusing one does not silence
# another on the same channel.
#
# Not persisted, and dropped as an entry unloads: a permissions or firmware change
# on the device should cost one refusal to discover rather than being invisible
# until somebody thinks to reload. Same reasoning as `_HOST_CGI_CONFIG_ABSENT` in
# client.py.
_REFUSED: set = set()


def refusal_is_outright(error) -> bool:
    """Whether the device answered and declined, rather than failing to answer.

    Judged on what it said, not on the class of the exception: the same
    `aiohttp.ClientResponseError` is raised for a 403 and for a 500, and only one
    of those means the call will never be accepted. Duck-typed on `status` and
    `code` so this module needs neither aiohttp nor rpc2 imported.
    """
    # `status` is read first, and `code` only when there is no status.
    #
    # The order matters and the obvious one is wrong. `aiohttp.ClientResponseError`
    # carries a deprecated `code` attribute that aliases `status`, so reading
    # `code` first sends every HTTP refusal to the RPC2 table -- a 403 looked up
    # among the RPC2 numbers, found absent, and reported as not a refusal. An
    # `Rpc2MethodRefused` has a `code` and no `status`, so checking `status` first
    # separates them correctly.
    #
    # That is also why the test for this uses a real `ClientResponseError` rather
    # than a stand-in: a hand-built one does not have aiohttp's alias, and the
    # first version of this function passed every test with the order reversed.
    status = getattr(error, "status", None)
    if status is not None:
        return status in REFUSED_STATUSES
    return getattr(error, "code", None) in REFUSED_RPC2_CODES


def key_for(coordinator, control: str) -> tuple:
    """(address, channel, control).

    The address rather than the serial: `_serial_number` is a bare annotation on
    the coordinator until the device answers, so `get_serial_number()` raises on
    one whose setup did not finish -- and the unload hook meets exactly those.
    """
    return (coordinator.get_address(), coordinator.get_channel(), control)


def is_refused(coordinator, control: str) -> bool:
    """Whether this control on this channel has already been refused outright."""
    return key_for(coordinator, control) in _REFUSED


def remember(coordinator, control: str, described: str) -> None:
    """Record a refusal, and say so once rather than on every press."""
    _REFUSED.add(key_for(coordinator, control))
    _LOGGER.warning(
        "%s will not operate its %s on channel %s (%s), so Home Assistant will "
        "stop asking. Reload the entry to try again after changing anything on "
        "the device",
        coordinator.get_address(), control, coordinator.get_channel(), described)


def forget(coordinator=None, control: str | None = None) -> None:
    """Drop what was learnt, so the device is asked again.

    Called per channel as an entry unloads, which is what makes "reload the entry
    to try again" true rather than aspirational, and per channel rather than
    wholesale so reloading one recorder does not make every other host pay a
    refusal again.
    """
    if coordinator is None:
        _REFUSED.clear()
        return
    if control is not None:
        _REFUSED.discard(key_for(coordinator, control))
        return
    address, channel, _ = key_for(coordinator, "")
    for entry in [k for k in _REFUSED if k[0] == address and k[1] == channel]:
        _REFUSED.discard(entry)
