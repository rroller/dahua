"""Which channel an arriving event belongs to, and closing a session that will not close.

Two things the coordinator does that nothing exercised, both on paths where a fault is
invisible from Home Assistant.

**`on_receive` is the front door for every event on a shared stream.** Since #615 one
connection serves every channel on a host, so each coordinator is handed every event and
decides whether it is one of its own by comparing the event's `index` to its channel.
Get that comparison wrong in either direction and the result is silent: too strict and a
camera's sensors never fire, too loose and channel 0's motion raises channel 9's. Neither
logs anything, because from the integration's point of view the event arrived and was
handled.

The `index` arrives as text off the wire, so it is parsed, and a value that is not a
number falls back to 0 rather than raising. That fallback matters more than it looks:
`on_receive` is called from the stream reader, so an exception here would take down the
event connection for **every** channel on the host, not just the one the event was for.

**`_close_session` runs while everything is being torn down**, which is exactly when
things are already failing. It has two independent failure paths and the contract is
that neither stops the rest: a client whose RPC2 session will not close must not prevent
the aiohttp session closing, and a session that raises on close must still release the
host connector. That last one is in a `finally` for that reason -- leaking the connector
leaks sockets on every reload.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator

CHANNEL = 3


def _coordinator(**attrs):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = CHANNEL
    coordinator.handled = []
    coordinator.handle_event = coordinator.handled.append
    for name, value in attrs.items():
        setattr(coordinator, name, value)
    return coordinator


def _wire(code="VideoMotion", action="Start", index="3"):
    """One event in the wire format the stream delivers."""
    return ("Code=%s;action=%s;index=%s;data={}\r\n\r\n"
            % (code, action, index)).encode("utf-8")


# --- an event reaches only the channel it is for ------------------------------


def test_an_event_for_this_channel_is_handled():
    coordinator = _coordinator()

    coordinator.on_receive(_wire(index=str(CHANNEL)), CHANNEL)

    assert len(coordinator.handled) == 1
    assert coordinator.handled[0]["Code"] == "VideoMotion"


def test_an_event_for_another_channel_is_not_handled():
    """The half that matters on a recorder. One connection carries every channel, so
    without this every camera would report every other camera's motion."""
    coordinator = _coordinator()

    coordinator.on_receive(_wire(index="0"), CHANNEL)

    assert coordinator.handled == []


@pytest.mark.parametrize("index", ["0", "1", "2", "4", "9", "11"])
def test_no_other_channel_index_gets_through(index):
    coordinator = _coordinator()

    coordinator.on_receive(_wire(index=index), CHANNEL)

    assert coordinator.handled == [], "channel %s reached channel %d" % (index, CHANNEL)


# --- and the index is text off the wire --------------------------------------


@pytest.mark.parametrize("index", ["", "x", "1.5", "0x2", " "])
def test_an_index_that_is_not_a_number_is_treated_as_channel_0(index):
    """It must not raise. `on_receive` is called from the stream reader, so an
    exception here takes the event connection down for every channel on the host."""
    coordinator = _coordinator()

    coordinator.on_receive(_wire(index=index), 0)

    assert coordinator.handled == [], "channel 0 is not this coordinator's channel"


def test_a_channel_0_coordinator_does_receive_an_unparsable_index():
    """The other side of the same fallback, so it is pinned as a real value rather
    than as 'anything but this channel'."""
    coordinator = _coordinator(_channel=0)

    coordinator.on_receive(_wire(index="x"), 0)

    assert len(coordinator.handled) == 1


def test_an_event_with_no_index_at_all_is_channel_0():
    coordinator = _coordinator(_channel=0)

    coordinator.on_receive(b"Code=VideoMotion;action=Start;data={}\r\n\r\n", 0)

    assert len(coordinator.handled) == 1


def test_undecodable_bytes_do_not_raise():
    """`errors="ignore"` is there because the stream is bytes from a camera. #855 was
    a doorbell event dropped for carrying a non-ASCII character, so this path is not
    hypothetical."""
    coordinator = _coordinator(_channel=0)

    coordinator.on_receive(
        b"Code=VideoMotion;action=Start;index=0;data={}\xff\r\n\r\n", 0)

    assert len(coordinator.handled) == 1


# --- closing a session while everything is already failing -------------------


class _Client:
    def __init__(self, raises=False):
        self.raises = raises
        self.closed = 0

    async def close(self):
        self.closed += 1
        if self.raises:
            raise RuntimeError("the RPC2 session was already gone")


class _Session:
    def __init__(self, raises=False):
        self.raises = raises
        self.closed = 0

    async def close(self):
        self.closed += 1
        if self.raises:
            raise RuntimeError("the connector is already closed")


@pytest.fixture
def released(monkeypatch):
    """Records the connector release, which is the one that must always happen."""
    from custom_components.dahua import coordinator as coordinator_module

    addresses = []

    async def _release(address):
        addresses.append(address)

    monkeypatch.setattr(coordinator_module, "_release_connector", _release)
    return addresses


async def test_both_are_closed_and_the_connector_released(released):
    coordinator = _coordinator(
        client=_Client(), _session=_Session(), _address="10.0.0.5")

    await coordinator._close_session()

    assert coordinator.client.closed == 1
    assert coordinator._session is None, "the reference must be dropped"
    assert released == ["10.0.0.5"]


async def test_a_client_that_will_not_close_does_not_block_the_session(released):
    """The two are independent. An RPC2 session the device has already dropped is the
    normal case on teardown, and it must not leak the aiohttp session."""
    coordinator = _coordinator(
        client=_Client(raises=True), _session=_Session(), _address="10.0.0.5")

    await coordinator._close_session()

    assert coordinator._session is None
    assert released == ["10.0.0.5"]


async def test_a_session_that_raises_on_close_still_releases_the_connector(released):
    """Why the release is in a `finally`. Leaking the connector leaks sockets on every
    reload, and a reload is exactly when this runs."""
    coordinator = _coordinator(
        client=_Client(), _session=_Session(raises=True), _address="10.0.0.5")

    await coordinator._close_session()

    assert released == ["10.0.0.5"], "the connector was leaked"


async def test_a_session_that_raises_leaves_the_reference_alone(released):
    """Measured rather than desired: `self._session = None` sits *before* the except,
    so a raising close leaves the old session in place. Pinned so that changing it is
    a deliberate act, since a retry would then call close on it a second time."""
    session = _Session(raises=True)
    coordinator = _coordinator(
        client=_Client(), _session=session, _address="10.0.0.5")

    await coordinator._close_session()

    assert coordinator._session is session


async def test_no_session_means_nothing_to_release(released):
    """Setup can fail before a session exists, and teardown still runs."""
    coordinator = _coordinator(client=_Client(), _session=None, _address="10.0.0.5")

    await coordinator._close_session()

    assert coordinator.client.closed == 1
    assert released == [], "there was no connector to release"
