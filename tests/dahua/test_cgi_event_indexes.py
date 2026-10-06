"""The CGI event-state response is converted to recorder channel indexes."""

from custom_components.dahua.client import DahuaClient


async def test_get_event_indexes_parses_all_active_channels(monkeypatch):
    client = DahuaClient("u", "p", "recorder", 80, 554, object())
    requested = []

    async def get_bytes(url):
        requested.append(url)
        return b"channels[0]=0\r\nchannels[1]=5\r\nchannels[2]=7\r\n"

    monkeypatch.setattr(client, "get_bytes", get_bytes)

    assert await client.async_get_event_indexes_cgi("VideoMotion") == {0, 5, 7}
    assert requested == [
        "/cgi-bin/eventManager.cgi?action=getEventIndexes&code=VideoMotion"
    ]


async def test_get_event_indexes_accepts_an_empty_response(monkeypatch):
    client = DahuaClient("u", "p", "recorder", 80, 554, object())

    async def get_bytes(_url):
        return b""

    monkeypatch.setattr(client, "get_bytes", get_bytes)

    assert await client.async_get_event_indexes_cgi("VideoMotion") == set()
