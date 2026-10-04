"""A VTH's AccessControl table nests lists where a VTO's holds dicts (#949).

Driven like test_vto_handshake: a real DahuaVTOClient, a recording transport, and
frames through data_received. No Home Assistant, no socket.

Measured over DHIP on a VTH5221D with no HTTP. A doorbell (VTO) returns AccessControl
as a list of per-door dicts and handle_access_control reads UnlockReloadInterval from
the 'Local' one. The monitor, having no door, returns a list whose entries are
themselves lists, so item.get('AccessProtocol') raised 'list' object has no attribute
'get'.

The observable has to be the log, not hold_time. data_received wraps the whole packet
loop in one try/except and only logs on failure, so a crash leaves hold_time at its
default 0 -- which is also its value when nothing matched. Asserting on hold_time alone
would pass whether or not the handler crashed (the crash is swallowed), so every test
here also asserts that data_received logged no failure. That log line aborting is the
real bug: a doorbell event batched in the same packet is lost with it.
"""

import logging

import json

import pytest

from custom_components.dahua.vto import DahuaVTOClient

HOST = "10.0.0.7"
USERNAME = "admin"
PASSWORD = "not-a-real-password"


class _Transport:
    def __init__(self):
        self.written = []
        self.closing = False

    def is_closing(self):
        return self.closing

    def write(self, message):
        self.written.append(message)


def _frame(payload):
    return (
        b"\x00\x00\x00DHIP\x8c-\x96{\x08\x00\x00\x00{\x01\x00\x00\x00\x00\x00\x00"
        + json.dumps(payload).encode("utf-8")
        + b"\n"
    )


class _Caught:
    """Records ERROR logs from vto.py so a swallowed handler crash is observable."""

    def __init__(self):
        self.records = []
        self._logger = logging.getLogger(
            "custom_components.dahua"
        )  # vto.py uses getLogger(__package__)
        self._handler = logging.Handler()
        self._handler.emit = self.records.append

    def __enter__(self):
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        return False

    @property
    def handler_failures(self):
        return [r for r in self.records if "Failed to handle message" in r.getMessage()]


def _drive(table_value, *, with_params=True):
    """Run the real handle_access_control closure against a reply, capturing logs."""
    client = DahuaVTOClient(HOST, USERNAME, PASSWORD, False, lambda event: None)
    client.connection_made(_Transport())
    client.load_access_control()
    reply = {"id": client.request_id, "result": True, "session": 1}
    if with_params:
        reply["params"] = {"table": table_value}
    with _Caught() as caught:
        client.data_received(_frame(reply))
    return client, caught


VTO_TABLE = [
    {"AccessProtocol": "Local", "UnlockReloadInterval": 4, "Name": "Door1"},
    {"AccessProtocol": "Remote", "Name": "Door2"},
]
VTH_TABLE = [["something"], [1, 2, 3]]


# --- the case that crashed, asserted on the log not the default --------------


def test_a_table_of_lists_does_not_crash_the_handler():
    """The VTH shape. Before the guard this logged a failure and aborted the packet."""
    client, caught = _drive(VTH_TABLE)

    assert not caught.handler_failures, caught.handler_failures
    assert client.hold_time == 0


def test_a_dict_table_does_not_crash_the_handler():
    """Iterating a mapping would walk string keys, and str.get raises like list.get."""
    _, caught = _drive({"0": {"AccessProtocol": "Local", "UnlockReloadInterval": 9}})

    assert not caught.handler_failures, caught.handler_failures


def test_a_null_table_does_not_crash_the_handler():
    _, caught = _drive(None)

    assert not caught.handler_failures, caught.handler_failures


def test_missing_params_does_not_crash_the_handler():
    """A reply with no params at all: params.get would raise on None."""
    _, caught = _drive(None, with_params=False)

    assert not caught.handler_failures, caught.handler_failures


# --- the case that must keep working -----------------------------------------


def test_a_real_vto_door_is_still_read():
    client, caught = _drive(VTO_TABLE)

    assert not caught.handler_failures, caught.handler_failures
    assert client.hold_time == 4


def test_a_non_dict_entry_is_skipped_but_a_later_door_is_read():
    """A valid door after a bad entry must survive the guard, and nothing logs."""
    client, caught = _drive(
        [["junk"], {"AccessProtocol": "Local", "UnlockReloadInterval": 7}]
    )

    assert not caught.handler_failures, caught.handler_failures
    assert client.hold_time == 7
