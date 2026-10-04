"""A VTH returns the T2UServer serial table as a list, not the dict a VTO returns (#949).

Sibling to test_vth_access_control_table: #964 guarded handle_access_control for the
list shape a monitor sends, but handle_serial_number had the same assumption and was
not reached by it. Measured over DHIP on a VTH5221D with no HTTP: login succeeds, then
handle_serial_number does table.get("UUID") on a list and raises 'list' object has no
attribute 'get' at vto.py:446. data_received swallows it, so it was silent apart from a
traceback on every login, and the serial read as None regardless.

Driven like test_vto_handshake: a real DahuaVTOClient, a recording transport, frames
through data_received. The observable is the log, not the serial value, for the same
reason as the access-control tests -- a swallowed crash leaves the serial at None, which
is also its value when a monitor legitimately carries no UUID. handle_version and
handle_device_type share the params-shape assumption and are covered here too.
"""

import json
import logging

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


def _drive(load, params, *, with_params=True):
    """Register one load_* handler, feed it a reply, capture logs."""
    client = DahuaVTOClient(HOST, USERNAME, PASSWORD, False, lambda event: None)
    client.connection_made(_Transport())
    getattr(client, load)()
    reply = {"id": client.request_id, "result": True, "session": 1}
    if with_params:
        reply["params"] = params
    with _Caught() as caught:
        client.data_received(_frame(reply))
    return client, caught


# --- serial number: the case that crashed ------------------------------------


def test_a_list_serial_table_does_not_crash_the_handler():
    """The VTH shape. Before the guard this logged a failure and aborted the packet."""
    client, caught = _drive("load_serial_number", {"table": [["x"], [1, 2]]})

    assert not caught.handler_failures, caught.handler_failures
    assert client.dahua_details.get("serialNumber") is None


def test_a_dict_serial_table_is_still_read():
    client, caught = _drive("load_serial_number", {"table": {"UUID": "ABC123"}})

    assert not caught.handler_failures, caught.handler_failures
    assert client.dahua_details.get("serialNumber") == "ABC123"


def test_a_missing_serial_params_does_not_crash_the_handler():
    client, caught = _drive("load_serial_number", None, with_params=False)

    assert not caught.handler_failures, caught.handler_failures
    assert client.dahua_details.get("serialNumber") is None


# --- version and device type share the params-shape assumption ----------------


def test_version_with_no_params_does_not_crash_the_handler():
    client, caught = _drive("load_version", None, with_params=False)

    assert not caught.handler_failures, caught.handler_failures
    assert client.dahua_details.get("version") is None


def test_version_is_still_read():
    client, caught = _drive(
        "load_version", {"version": {"Version": "1.2.3", "BuildDate": "2026-01-01"}}
    )

    assert not caught.handler_failures, caught.handler_failures
    assert client.dahua_details.get("version") == "1.2.3"


def test_device_type_with_no_params_does_not_crash_the_handler():
    client, caught = _drive("load_device_type", None, with_params=False)

    assert not caught.handler_failures, caught.handler_failures
    assert client.dahua_details.get("deviceType") is None


def test_device_type_is_still_read():
    client, caught = _drive("load_device_type", {"type": "VTH5221D"})

    assert not caught.handler_failures, caught.handler_failures
    assert client.dahua_details.get("deviceType") == "VTH5221D"
