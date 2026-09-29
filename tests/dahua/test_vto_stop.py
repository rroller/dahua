"""Unloading a doorbell entry must close its doorbell connection.

`async_stop` cancelled the stream task and stopped there. That task is parked on
`await protocol.disconnected`, and cancelling a task does not close an asyncio
transport, so the socket to port 5000 stayed open with its event subscription
and its keep-alive -- which reschedules itself on every reply -- while the entry
it belonged to was gone. Every options change leaked one, and the doorbell kept
pushing events for an unloaded entry.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator
from custom_components.dahua.vto import DahuaVTOClient


class _Transport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _Handle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


async def test_closing_drops_the_transport_and_the_keepalive():
    client = object.__new__(DahuaVTOClient)
    client.transport = _Transport()
    client._keep_alive_handle = _Handle()
    client.disconnected = asyncio.get_running_loop().create_future()

    client.close()

    assert client.transport.closed is True
    assert client._keep_alive_handle is None
    assert client.disconnected.done()


async def test_stopping_closes_the_vto_connection(monkeypatch):
    monkeypatch.setattr(coordinator_module, "_release_host_stream", AsyncMock())

    vto = Mock()
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._address = "10.0.0.5"
    coordinator.client = SimpleNamespace(close=AsyncMock())
    coordinator._session = None
    coordinator._vto_client = vto
    coordinator._vto_task = asyncio.create_task(asyncio.sleep(3600))

    await coordinator.async_stop()

    vto.close.assert_called_once()
    assert coordinator._vto_task is None
