"""#1176, the write half: a config write whose answer cannot be read.

#1183 gave this camera its identity back and #1189 its config reads. Writes are
what is left. Every write still goes over CGI, and on this firmware every
authenticated CGI answer carries a whole second HTTP response behind it, so a
write is refused by the parser exactly as a read is -- which means the roughly
thirty controls the read fix makes visible would error when pressed.

**A write is not the same question as a read, and the difference is the design
here.** A read that cannot be read has told us nothing. A write that cannot be
read may already have been carried out: the junk arrives *after* a complete,
correct response, so the request reached the device and was answered. So the
fallback reads the table first and sends nothing at all when the CGI write
already landed, and only writes over RPC2 when it did not.

**And a write has to be proved, not announced.** A one-field
`configManager.setConfig` edits the table in place on a DHI-NVR5464 and a
VTO2000A, and on a VTH5221D it *replaced* the table, answered `result: true`
and left one key of sixteen -- the monitor had to be rebuilt from a copy. These
writes are not reversible. So the fallback reads the table back and requires
that every field asked for holds what was asked and that nothing else moved,
and the double below models both firmware behaviours rather than only the one
that suits the code.

The bytes and the loopback server come from the read half's tests. Imported
rather than copied: they are @defre92's measurement, and two copies of a
measurement drift.
"""

import asyncio
import re
from copy import deepcopy

import aiohttp
import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import (
    DahuaClient,
    DahuaWriteDeclined,
    _as_rpc2_value,
    _config_table_of,
    addressed_config_write,
    flatten_rpc2_config,
)
from custom_components.dahua.rpc2 import Rpc2MethodRefused

from .test_a_config_read_that_cannot_be_read import (
    A_REAL_400,
    A_REAL_404,
    MOTION_DETECT,
    UNREADABLE,
    _PHANTOM_400,
    _serving,
)

# ---------------------------------------------------------------------------
# The bytes a WRITE comes back as
# ---------------------------------------------------------------------------

# The same fault as the read half, with the body a write gets. Built from the
# read tests' phantom so there is one copy of it, and pinned against aiohttp in
# the first test below rather than assumed to behave like the read's 25 bytes.
OK_THEN_PHANTOM = (
    b"HTTP/1.1 200 OK\r\n"
    b"Cache-Control: no-store\r\n"
    b"Connection: close\r\n"
    b"Content-Length: 2\r\n"
    b"Content-Type: text/plain;charset=utf-8\r\n"
    b"X-Content-Type-Options: nosniff\r\n"
    b"\r\n"
    b"OK" + _PHANTOM_400
)


# ---------------------------------------------------------------------------
# The URLs, as the integration really builds them
# ---------------------------------------------------------------------------

SET_CONFIG = "/cgi-bin/configManager.cgi?action=setConfig"

# What the Motion Detection switch sends. Two fields, and the second one is a
# key some firmware does not have at all (#1115, a VTO4202F-P-S2), which is why
# `_enable_motion_detection_cgi` has a second shape to fall through to.
ENABLE_AND_VERSION = (
    SET_CONFIG + "&MotionDetect[0].Enable=true&MotionDetect[0].DetectVersion=V3.0"
)
ENABLE_ALONE = SET_CONFIG + "&MotionDetect[0].Enable=true"
SENSITIVITY = SET_CONFIG + "&SmartMotionDetect[0].Sensitivity=4"
REBOOT = "/cgi-bin/magicBox.cgi?action=reboot"


# ---------------------------------------------------------------------------
# A device that holds a table, and applies a write one of the two ways
# ---------------------------------------------------------------------------

MOTION_TABLE = {
    "MotionDetect": [
        {
            "Enable": False,
            "DetectVersion": "V3.0",
            "Sensitivity": 3,
            "MotionDetectWindow": [{"Threshold": 5, "Region": [8, 8]}],
        }
    ]
}

# The same table as the firmware in #1115 serves: no DetectVersion key at all.
MOTION_TABLE_WITHOUT_VERSION = {"MotionDetect": [{"Enable": False, "Sensitivity": 3}]}

_STEP = re.compile(r"\[(\d+)\]|\.?([A-Za-z_][A-Za-z0-9_]*)")


def _steps(address: str) -> list:
    """`MotionDetect[0].Enable` as `["MotionDetect", 0, "Enable"]`."""
    return [int(index) if index else key for index, key in _STEP.findall(address)]


def _assign(table, address: str, value) -> None:
    """Change one addressed field of a nested table, leaving the rest alone."""
    steps = _steps(address)[1:]
    node = table
    for step in steps[:-1]:
        node = node[step]
    node[steps[-1]] = value


def _only(address: str, value):
    """What is left of a table a ONE-FIELD write replaced (the VTH5221D shape).

    Only reachable through `set_config_field` below, which the fix no longer
    calls. Both are kept so that a break reintroducing the one-field write
    reproduces @rroller's check rather than failing for want of a double.
    """
    node = value
    for step in reversed(_steps(address)[1:]):
        node = ([None] * step + [node]) if isinstance(step, int) else {step: node}
    return node


def _drop(table, address: str) -> None:
    """Remove an addressed field from a table, modelling a device that discards one."""
    steps = _steps(address)[1:]
    node = table
    for step in steps[:-1]:
        node = node[step]
    del node[steps[-1]]


class _Rpc2Device:
    """The tables a device holds, and what a whole-table write does to them.

    State, not a prepared answer: the write fix reads, writes and reads back, so
    a double that answered the same table twice could not fail the way firmware
    does.

    `mode` is the firmware semantics for a **one-field** `setConfig`: `"edit"` is
    the DHI-NVR5464 and the VTO2000A, `"replace"` is the VTH5221D, where the
    table becomes only what was written. The fix sends whole tables, so the two
    now behave identically here, and that is the point rather than an
    simplification: under a whole-table write there is no difference to be at
    the mercy of, which is what @rroller's review asked for.

    `drops` is the harder device, and the only one left that can still lose a
    setting: it discards that field from whatever it is sent, complete table or
    not. `applies=False` is #647's shape, the call accepted and nothing changed.
    """

    def __init__(
        self,
        tables=None,
        *,
        mode="edit",
        applies=True,
        also_changes=None,
        drops=None,
        get_raises=None,
        set_raises=None,
        get_delay=0,
        set_delay=0,
    ):
        self.tables = deepcopy(MOTION_TABLE if tables is None else tables)
        self.mode = mode
        self.applies = applies
        self.also_changes = also_changes
        self.drops = drops
        self.get_raises = get_raises
        self.set_raises = set_raises
        self.get_delay = get_delay
        self.set_delay = set_delay
        self.reads = []
        self.sent = []

    async def get_config(self, name, *, allow_missing=False, as_table=False):
        self.reads.append(name)
        if self.get_delay:
            await asyncio.sleep(self.get_delay)
        if self.get_raises is not None:
            raise self.get_raises
        table = deepcopy(self.tables.get(name))
        return table if as_table else flatten_rpc2_config(name, table)

    async def set_config_tables(self, tables):
        self.sent.append(deepcopy(tables))
        if self.set_delay:
            await asyncio.sleep(self.set_delay)
        if self.set_raises is not None:
            raise self.set_raises
        if not self.applies:
            return {"result": True}
        for name, table in deepcopy(tables).items():
            # A whole-table write replaces the named table under either
            # semantics, which is why `mode` no longer changes what happens.
            if self.drops is not None and _config_table_of(self.drops) == name:
                _drop(table, self.drops)
            self.tables[name] = table
            if self.also_changes:
                _assign(self.tables[name], *self.also_changes)
        return {"result": True}

    async def set_config_field(self, address, value):
        """The one-field write the first version of the fix used.

        Not called by the code any more. Kept so that a break which puts the
        one-field write back is measured against the firmware that loses a
        table to it, which is what the preservation tests are about.
        """
        if self.set_delay:
            await asyncio.sleep(self.set_delay)
        if self.set_raises is not None:
            raise self.set_raises
        name = _config_table_of(address)
        if self.mode == "replace":
            self.tables[name] = _only(address, value)
        elif self.applies:
            _assign(self.tables[name], address, value)
            if self.also_changes:
                _assign(self.tables[name], *self.also_changes)
        self.sent.append({name: deepcopy(self.tables[name])})
        return {"result": True}

    def holds(self, address):
        """One field of a held table, as flat text, the way a read would give it."""
        name = _config_table_of(address)
        flat = flatten_rpc2_config(name, self.tables.get(name))
        return flat.get("table." + address)

    def was_sent(self, address):
        """The value at one address in the last table the device was sent."""
        name = _config_table_of(address)
        table = self.sent[-1][name]
        node = table
        for step in _steps(address)[1:]:
            node = node[step]
        return node


async def _write(server, url=ENABLE_ALONE, device=None, verify_ok=True):
    """Drive a real write against the loopback server. Returns (answer, device).

    Through `get`, not `_request`: that is how every write reaches the client,
    it is what sets `reject_declined`, and a write is not shared or cached, so
    there is nothing for the cache to answer twice.
    """
    if device is None:
        device = _Rpc2Device()
    async with aiohttp.ClientSession() as session:
        client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
        client._rpc2_get_config = device.get_config
        client._rpc2_set_config_tables = device.set_config_tables
        client._rpc2_set_config_field = device.set_config_field
        try:
            return await client.get(url, verify_ok), device
        except Exception as exception:  # pylint: disable=broad-except
            return exception, device


# ---------------------------------------------------------------------------
# What aiohttp does with a write's answer, pinned against the library
# ---------------------------------------------------------------------------


async def test_a_write_answered_with_a_phantom_response_refuses_from_the_request(
    socket_enabled,
):
    """The read half measured a 25 byte body. A write's is `OK`, so measure that.

    Same conclusion -- the refusal comes out of the request and carries the
    message, not the headers -- but it is measured here rather than carried over
    from a different Content-Length.
    """
    async with _serving(OK_THEN_PHANTOM) as server:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientResponseError) as raised:
                await session.get("http://127.0.0.1:%d/x" % server.port)

    assert raised.value.status == 400
    assert client_module.refusal_is_mangled_framing(raised.value) is True


# ---------------------------------------------------------------------------
# The fix
# ---------------------------------------------------------------------------


async def test_an_unreadable_write_is_written_over_rpc2(socket_enabled):
    """The fix. Without it the switch reports an error and nothing changes."""
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server)

    assert not isinstance(answer, Exception), answer
    assert "OK" in answer, answer
    assert len(device.sent) == 1
    assert device.was_sent("MotionDetect[0].Enable") is True
    assert device.holds("MotionDetect[0].Enable") == "true"


async def test_the_value_goes_out_typed_rather_than_as_the_text_cgi_sent(
    socket_enabled,
):
    """A boolean field sent the string "true" is #647: accepted, and ignored.

    RPC2 is JSON. The type comes from what the device is already holding at that
    address, so this asserts the object, not its truthiness -- `"true"` is
    truthy too, which is exactly how this bug hides.
    """
    async with _serving(OK_THEN_PHANTOM) as server:
        _, device = await _write(server)

    value = device.was_sent("MotionDetect[0].Enable")
    assert value is True, repr(value)


async def test_an_integer_field_goes_out_as_an_integer(socket_enabled):
    """The other half of the typing, from a table that holds a number."""
    device = _Rpc2Device({"SmartMotionDetect": [{"Sensitivity": 2, "Enable": True}]})
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, SENSITIVITY, device)

    assert "OK" in answer, answer
    assert device.was_sent("SmartMotionDetect[0].Sensitivity") == 4
    assert isinstance(device.was_sent("SmartMotionDetect[0].Sensitivity"), int)


async def test_a_write_that_already_landed_sends_nothing(socket_enabled):
    """The CGI write may well have happened: the junk arrives after a correct 200.

    One read, no write, and the caller is told what a CGI `OK` would have told
    it. This is the cheapest outcome and the only one with nothing to undo.
    """
    device = _Rpc2Device({"MotionDetect": [{"Enable": True, "Sensitivity": 3}]})
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, ENABLE_ALONE, device)

    assert "OK" in answer, answer
    assert device.sent == []
    assert device.reads == ["MotionDetect"]


async def test_the_two_outcomes_are_logged_in_different_words(socket_enabled, caplog):
    """So a reporter's debug log answers "does this camera apply the write".

    Nobody can measure that from this end, and the log is where the answer
    shows up without anyone being asked to test anything.
    """
    landed = _Rpc2Device({"MotionDetect": [{"Enable": True}]})
    with caplog.at_level("INFO"):
        async with _serving(OK_THEN_PHANTOM) as server:
            await _write(server, ENABLE_ALONE, landed)
    assert "did land" in caplog.text

    caplog.clear()
    with caplog.at_level("INFO"):
        async with _serving(OK_THEN_PHANTOM) as server:
            await _write(server, ENABLE_ALONE, _Rpc2Device())
    assert "written over RPC2" in caplog.text
    assert "did land" not in caplog.text


# ---------------------------------------------------------------------------
# What it must refuse to claim
# ---------------------------------------------------------------------------


async def test_a_table_is_not_lost_on_firmware_that_replaces_rather_than_edits(
    socket_enabled,
):
    """@rroller's finding, as a test: the FIRST write must not cost a setting.

    The VTH5221D answers `result: true` to a one-field `setConfig` and leaves
    one key of sixteen. The first version of this fallback wrote one field and
    reported a refusal once the readback showed the damage, which protects the
    second write and not the first, and these writes are not reversible.

    Sending the table that was just read cannot lose a setting under either
    semantics, so this device keeps everything and the write lands. Nothing is
    latched, because nothing went wrong.
    """
    device = _Rpc2Device(mode="replace")
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, ENABLE_ALONE, device)

    assert "OK" in answer, answer
    assert device.holds("MotionDetect[0].Enable") == "true"
    assert device.holds("MotionDetect[0].Sensitivity") == "3"
    assert device.holds("MotionDetect[0].DetectVersion") == "V3.0"
    assert device.holds("MotionDetect[0].MotionDetectWindow[0].Threshold") == "5"
    assert not client_module._HOST_RPC2_WRITES_UNSAFE


async def test_the_whole_table_goes_back_exactly_as_it_was_read(socket_enabled):
    """The preservation guarantee, asserted on what went out rather than on the end state.

    Every key the device reported is in the request, with its own value, and the
    one field asked for is the only difference. A device cannot drop what it was
    sent unless it chooses to, and the test below covers the one that does.
    """
    async with _serving(OK_THEN_PHANTOM) as server:
        _, device = await _write(server)

    was_read = flatten_rpc2_config("MotionDetect", MOTION_TABLE["MotionDetect"])
    went_out = flatten_rpc2_config("MotionDetect", device.sent[-1]["MotionDetect"])
    assert set(went_out) == set(was_read)
    assert {
        key: value for key, value in went_out.items() if was_read[key] != value
    } == {"table.MotionDetect[0].Enable": "true"}


async def test_a_device_that_drops_a_setting_it_was_sent_is_reported_as_refused(
    socket_enabled,
):
    """The one device left that can still lose a setting, and the latch is for it.

    It discards a field from a complete table, so nothing this sends can save
    it. The field asked for reads back correctly, so checking only that would
    call it a success.
    """
    device = _Rpc2Device(drops="MotionDetect[0].Sensitivity")
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, ENABLE_ALONE, device)

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert answer.status == 400
    assert device.holds("MotionDetect[0].Enable") == "true"
    assert device.holds("MotionDetect[0].Sensitivity") is None
    assert client_module._HOST_RPC2_WRITES_UNSAFE


async def test_a_lost_table_is_named_in_the_warning_by_key_not_by_value(
    socket_enabled, caplog
):
    """These tables carry cloud credentials and server lists. Keys only.

    Both halves asserted. "By key" on its own is met by a log line that prints
    the whole table, which is the shape of mistake that puts a password in
    somebody's issue report.
    """
    secret = "zz-not-for-the-log-zz"
    device = _Rpc2Device(
        {"MotionDetect": [{"Enable": False, "Sensitivity": 3, "Token": secret}]},
        drops="MotionDetect[0].Token",
    )
    with caplog.at_level("WARNING"):
        async with _serving(OK_THEN_PHANTOM) as server:
            await _write(server, ENABLE_ALONE, device)

    assert "table.MotionDetect[0].Token" in caplog.text
    assert "missing" in caplog.text
    assert secret not in caplog.text


async def test_a_host_that_lost_a_table_is_not_written_to_again(socket_enabled):
    """Latched for the life of the process, so the second write never goes out."""
    async with _serving(OK_THEN_PHANTOM, OK_THEN_PHANTOM) as server:
        device = _Rpc2Device(drops="MotionDetect[0].Sensitivity")
        async with aiohttp.ClientSession() as session:
            client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            client._rpc2_get_config = device.get_config
            client._rpc2_set_config_tables = device.set_config_tables
            client._rpc2_set_config_field = device.set_config_field
            with pytest.raises(aiohttp.ClientResponseError):
                await client.get(ENABLE_ALONE, True)
            after_first = list(device.sent)
            with pytest.raises(aiohttp.ClientResponseError):
                await client.get(ENABLE_ALONE, True)

    assert device.sent == after_first
    assert len(after_first) == 1


async def test_a_neighbour_that_changed_is_reported_without_writing_the_host_off(
    socket_enabled,
):
    """A changed neighbour is not a dropped setting, so it must not latch.

    It is still not a write this can promise, so the refusal is reported --
    deliberately strict. Nothing asked for that setting to move.
    """
    device = _Rpc2Device(also_changes=("MotionDetect[0].Sensitivity", 9))
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, ENABLE_ALONE, device)

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert answer.status == 400
    assert not client_module._HOST_RPC2_WRITES_UNSAFE


async def test_a_write_the_device_accepts_and_ignores_is_reported_as_refused(
    socket_enabled,
):
    """#647's shape, and the reason `result: true` is not what this trusts."""
    device = _Rpc2Device(applies=False)
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, ENABLE_ALONE, device)

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert answer.status == 400
    assert len(device.sent) == 1
    assert device.was_sent("MotionDetect[0].Enable") is True


async def test_a_field_the_table_does_not_have_is_declined_rather_than_refused(
    socket_enabled,
):
    """A CGI setConfig naming an absent field answers 200 and the word Error.

    So this is the device declining, not the transport failing, and saying so is
    what lets a caller try its second shape. Nothing is written.
    """
    device = _Rpc2Device(MOTION_TABLE_WITHOUT_VERSION)
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, ENABLE_AND_VERSION, device)

    assert isinstance(answer, DahuaWriteDeclined), type(answer).__name__
    assert "DetectVersion" in str(answer)
    assert device.sent == []


async def test_the_motion_switch_falls_through_to_its_second_shape(socket_enabled):
    """End to end, on the control @defre92 reported reading `off`.

    `MotionDetect[0].Enable` with `DetectVersion` is declined on a table that
    has no such key, and `Enable` on its own then lands over RPC2. This is the
    test that says the switch works on that camera, rather than that the
    fallback works.
    """
    device = _Rpc2Device(MOTION_TABLE_WITHOUT_VERSION)
    async with _serving(OK_THEN_PHANTOM, OK_THEN_PHANTOM) as server:
        async with aiohttp.ClientSession() as session:
            client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            client._rpc2_get_config = device.get_config
            client._rpc2_set_config_tables = device.set_config_tables
            client._rpc2_set_config_field = device.set_config_field
            answer = await client.enable_motion_detection(0, True)

    assert "OK" in answer, answer
    assert len(device.sent) == 1
    assert device.was_sent("MotionDetect[0].Enable") is True
    assert device.holds("MotionDetect[0].Enable") == "true"


# ---------------------------------------------------------------------------
# What must not be routed
# ---------------------------------------------------------------------------


async def test_a_correctly_framed_400_is_not_routed(socket_enabled):
    """A device's own 400 means it declined, and that is not a parser failure."""
    async with _serving(A_REAL_400) as server:
        answer, device = await _write(server, ENABLE_ALONE, verify_ok=False)

    assert isinstance(answer, Exception), answer
    assert device.reads == []
    assert device.sent == []


async def test_an_absent_config_endpoint_is_left_to_the_callers_that_handle_it(
    socket_enabled,
):
    """Deliberately out of scope, and a test rather than a sentence.

    A device whose `configManager.cgi` is absent answers 404, and three write
    methods already fall back to RPC2 themselves on exactly that. Those are
    measured and working; routing them here would change their behaviour for
    devices nobody is reporting a problem on.
    """
    async with _serving(A_REAL_404) as server:
        answer, device = await _write(server, ENABLE_ALONE, verify_ok=False)

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert answer.status == 404
    assert device.sent == []


async def test_a_write_that_is_not_a_config_write_is_not_routed(socket_enabled):
    """A reboot has no field to address and no RPC2 equivalent measured here."""
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, REBOOT, verify_ok=False)

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert device.sent == []


async def test_a_rule_addressed_by_its_index_is_not_routed(socket_enabled):
    """The write half of the pin `_request` already puts on this table's read.

    The same IVS rule can report a different index between read shapes on this
    firmware, so an index taken from the CGI URL and applied to an RPC2 read can
    name a different rule -- and a read back of the row that was asked for would
    agree it worked. Unreachable on this camera, since the pinned CGI read fails
    and no rule entity appears, but a write to the wrong rule is not a thing to
    leave resting on that.
    """
    device = _Rpc2Device({"VideoAnalyseRule": [[{"Enable": False}, {"Enable": False}]]})
    url = SET_CONFIG + "&VideoAnalyseRule[0][1].Enable=true"
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, url, device, verify_ok=False)

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert device.reads == []
    assert device.sent == []


async def test_a_read_is_never_written(socket_enabled):
    """The read fallback answers a read. Nothing about it may write anything."""
    async with _serving(UNREADABLE) as server:
        answer, device = await _write(server, MOTION_DETECT, verify_ok=False)

    assert device.sent == []
    assert device.reads == ["MotionDetect"]


async def test_a_host_rpc2_cannot_serve_is_not_routed(socket_enabled):
    """The host-wide verdict applies to writes as much as to reads."""
    async with _serving(OK_THEN_PHANTOM) as server:
        client_module._HOST_RPC2_UNAVAILABLE.add(("127.0.0.1:%d" % server.port, "u"))
        answer, device = await _write(server)

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert device.sent == []


# ---------------------------------------------------------------------------
# The floor: nothing here may be worse than the refusal it is holding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), ConnectionResetError("reset"), ValueError("nonsense")],
    ids=["timeout", "reset", "nonsense"],
)
async def test_a_read_that_fails_before_the_write_reports_the_original_refusal(
    socket_enabled, failure
):
    """Whatever the fallback's own read does, the caller hears today's answer."""
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(
            server, ENABLE_ALONE, _Rpc2Device(get_raises=failure)
        )

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert answer.status == 400
    assert device.sent == []


async def test_an_rpc2_write_that_is_refused_reports_the_original_refusal(
    socket_enabled,
):
    """A device that declines the RPC2 write has declined the write."""
    refusal = Rpc2MethodRefused("configManager.setConfig", 287638033, "no")
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, _ = await _write(server, ENABLE_ALONE, _Rpc2Device(set_raises=refusal))

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert answer.status == 400


async def test_an_rpc2_write_that_hangs_reports_the_original_refusal(
    socket_enabled, monkeypatch
):
    """The hole the read half fell into, in the write path and measured again.

    The fallback runs inside the request's own `asyncio.timeout`. Without a
    deadline of its own the outer one cancels the task, `CancelledError` is not
    an `Exception`, and the caller gets a `TimeoutError` instead of the refusal.
    The write path has three calls inside that one deadline, which is why the
    margin wraps the whole sequence rather than each call.
    """
    monkeypatch.setattr(client_module, "TIMEOUT_SECONDS", 0.4)
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, _ = await _write(server, ENABLE_ALONE, _Rpc2Device(set_delay=5))

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert answer.status == 400


async def test_a_read_that_hangs_before_the_write_reports_the_original_refusal(
    socket_enabled, monkeypatch
):
    """The same deadline, measured on the first of the three calls."""
    monkeypatch.setattr(client_module, "TIMEOUT_SECONDS", 0.4)
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, ENABLE_ALONE, _Rpc2Device(get_delay=5))

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert answer.status == 400
    assert device.sent == []


# ---------------------------------------------------------------------------
# More than one field, and more than one table
# ---------------------------------------------------------------------------


async def test_a_multi_field_write_sends_each_field_as_it_is_held(socket_enabled):
    """The infrared write's shape: a mode and a brightness in one URL."""
    device = _Rpc2Device(
        {"Lighting": [[{"Mode": "Off", "MiddleLight": [{"Light": 50}]}]]}
    )
    url = (
        SET_CONFIG + "&Lighting[0][0].Mode=Auto&Lighting[0][0].MiddleLight[0].Light=100"
    )
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, url, device)

    assert "OK" in answer, answer
    # One call, both fields, in the table the device itself gave us.
    assert len(device.sent) == 1
    assert device.was_sent("Lighting[0][0].Mode") == "Auto"
    assert device.was_sent("Lighting[0][0].MiddleLight[0].Light") == 100


async def test_two_tables_in_one_write_go_back_in_one_call(socket_enabled):
    """Each table named is read before and after, and both go back together.

    One `system.multicall` rather than two requests, so there is no state where
    the device has taken one table and not the other.
    """
    device = _Rpc2Device(
        {"MotionDetect": [{"Enable": False}], "LightGlobal": [{"Enable": False}]}
    )
    url = SET_CONFIG + "&MotionDetect[0].Enable=true&LightGlobal[0].Enable=true"
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, url, device)

    assert "OK" in answer, answer
    assert device.reads == [
        "MotionDetect",
        "LightGlobal",
        "MotionDetect",
        "LightGlobal",
    ]
    assert device.holds("LightGlobal[0].Enable") == "true"
    assert len(device.sent) == 1
    assert sorted(device.sent[0]) == ["LightGlobal", "MotionDetect"]


async def test_a_table_too_large_to_write_reports_the_refusal_and_changes_nothing(
    socket_enabled,
):
    """What sending the whole table costs, and the failure is the safe one.

    A DHI-NVR5464 refuses a 9KB to 22KB `setConfig` with `errCode 287638033,
    "Request length error!"`. On a device with tables that large this fallback
    declines rather than working, and that is the trade: the device keeps every
    setting it had, and the caller hears the refusal it hears today. There is no
    second attempt with one field, because one field is the shape that can lose
    the table.
    """
    refusal = Rpc2MethodRefused(
        "configManager.setConfig", 287638033, "Request length error!"
    )
    device = _Rpc2Device(set_raises=refusal)
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, ENABLE_ALONE, device)

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert answer.status == 400
    assert len(device.sent) == 1
    assert device.holds("MotionDetect[0].Enable") == "false"
    assert device.holds("MotionDetect[0].Sensitivity") == "3"
    assert not client_module._HOST_RPC2_WRITES_UNSAFE


async def test_a_percent_encoded_value_is_written_decoded(socket_enabled):
    """Two write methods encode their value. The device is sent the value."""
    device = _Rpc2Device({"ChannelTitle": [{"Name": "Camera 1"}]})
    url = SET_CONFIG + "&ChannelTitle[0].Name=Front%20Door"
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, url, device)

    assert "OK" in answer, answer
    assert device.was_sent("ChannelTitle[0].Name") == "Front Door"


# ---------------------------------------------------------------------------
# The pure parts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (ENABLE_ALONE, [("MotionDetect[0].Enable", "true")]),
        (
            ENABLE_AND_VERSION,
            [
                ("MotionDetect[0].Enable", "true"),
                ("MotionDetect[0].DetectVersion", "V3.0"),
            ],
        ),
        (
            SET_CONFIG + "&Lighting_V2[0][0][1].Mode=Manual",
            [("Lighting_V2[0][0][1].Mode", "Manual")],
        ),
        (SET_CONFIG + "&VSP_PaaS.Enable=false", [("VSP_PaaS.Enable", "false")]),
        (
            SET_CONFIG + "&ChannelTitle[0].Name=Front%20Door",
            [("ChannelTitle[0].Name", "Front Door")],
        ),
    ],
)
def test_addressed_config_write_reads_a_url_it_can_carry(url, expected):
    assert addressed_config_write(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        MOTION_DETECT,
        REBOOT,
        SET_CONFIG,
        SET_CONFIG + "&Encode[0]=whatever",
        SET_CONFIG + "&name=Encode",
        SET_CONFIG + "&MotionDetect[0].Enable=true&Encode[0]=whatever",
        "/cgi-bin/configManager.cgi?action=setConfig&MotionDetect[0].Enable",
    ],
    ids=[
        "a read",
        "not a config write",
        "no parameters",
        "a whole table",
        "a named table",
        "one field and one table",
        "no value",
    ],
)
def test_addressed_config_write_refuses_a_url_it_cannot_read_completely(url):
    """All or nothing. Writing some of what was asked is worse than reporting
    the refusal, and guessing at a shape nothing has measured is worse again."""
    assert addressed_config_write(url) is None


@pytest.mark.parametrize(
    "held,text,expected",
    [
        (False, "true", True),
        (True, "FALSE", False),
        (3, "4", 4),
        (1.5, "2.5", 2.5),
        ("Off", "Auto", "Auto"),
        ("3", "4", "4"),
    ],
)
def test_a_value_is_typed_the_way_the_device_holds_it(held, text, expected):
    """Including the last pair: a string field holding digits stays a string."""
    typed = _as_rpc2_value(held, text)
    assert typed == expected
    assert type(typed) is type(expected)


@pytest.mark.parametrize(
    "held,text",
    [(False, "Auto"), (3, "Auto"), (None, "true"), ([], "true")],
    ids=["a word for a boolean", "a word for a number", "nothing", "a list"],
)
def test_a_value_that_cannot_be_typed_raises(held, text):
    with pytest.raises(ValueError):
        _as_rpc2_value(held, text)


def test_flatten_can_keep_the_json_types_under_the_same_keys():
    """What the write path needs: the same keys, the device's own types.

    Both shapes from one answer, so the types a write goes out with and the text
    it is checked against cannot come from two different reads.
    """
    table = MOTION_TABLE["MotionDetect"]
    text = flatten_rpc2_config("MotionDetect", table)
    typed = flatten_rpc2_config("MotionDetect", table, stringify=False)

    assert set(text) == set(typed)
    assert text["table.MotionDetect[0].Enable"] == "false"
    assert typed["table.MotionDetect[0].Enable"] is False
    assert text["table.MotionDetect[0].Sensitivity"] == "3"
    assert typed["table.MotionDetect[0].Sensitivity"] == 3


@pytest.mark.parametrize(
    "address,name",
    [
        ("MotionDetect[0].Enable", "MotionDetect"),
        ("VSP_PaaS.Enable", "VSP_PaaS"),
        ("Lighting_V2[0][0][1].Mode", "Lighting_V2"),
    ],
)
def test_a_field_address_names_its_table(address, name):
    assert _config_table_of(address) == name


async def test_the_real_read_can_hand_back_the_nested_table():
    """`as_table=True`, which every test above replaces with a double.

    The one seam the double hides. It shares `_rpc2_get_config` so the
    stale-login retry is not reimplemented beside it, which means this flag is
    the only new thing in a well-tested method and worth one test of its own.
    """

    class _Rpc2:
        async def get_config(self, params):
            assert params == {"name": "MotionDetect"}
            return {"table": deepcopy(MOTION_TABLE["MotionDetect"])}

    class _Holder:
        client = _Rpc2()
        task = None

    async with aiohttp.ClientSession() as session:
        client = DahuaClient("u", "p", "127.0.0.1", 80, 554, session)

        async def shared():
            return _Holder()

        client._shared_rpc2 = shared

        nested = await client._rpc2_get_config("MotionDetect", as_table=True)
        flat = await client._rpc2_get_config("MotionDetect")

    assert nested == MOTION_TABLE["MotionDetect"]
    assert nested[0]["Enable"] is False
    assert flat["table.MotionDetect[0].Enable"] == "false"


# ---------------------------------------------------------------------------
# Two writes at once, which is how @rroller broke the first whole-table version
# ---------------------------------------------------------------------------

# The second half of @rroller's pair. The same table as ENABLE_ALONE, a
# different field, and a value the held table does not already have.
SENSITIVITY_ALONE = SET_CONFIG + "&MotionDetect[0].Sensitivity=9"


class _Turnstile:
    """Holds each caller just after its first read until the others arrive.

    Two writes only lose one another's change if both have READ the table before
    either writes it, so the wait goes between the read and the write rather
    than in front of the read. The first version waited in front of it and
    proved nothing: by the time the second caller was released the first had
    already written, so its read was fresh and nothing was lost -- the test
    stayed green with the lock taken out, and only the turnstile's own
    bookkeeping went red.

    The wait cannot be indefinite: with the lock in place the second caller is
    held at the lock and never reaches the read at all, and that is the passing
    case, which must not hang.

    So the wait is bounded and `released` records, per caller in arrival order,
    whether it left because everyone had arrived (`True`) or because it gave up
    waiting (`False`). That list says which way the run went -- `[True, True]`
    is two transactions inside one another, `[False, True]` is one and then the
    next -- but it is never the only assertion in a test, because what matters
    is the setting that survives.
    """

    def __init__(self, parties: int, patience: float = 0.25):
        self.parties = parties
        self.patience = patience
        self.arrived = 0
        self.released = []
        self._open = asyncio.Event()

    async def wait(self) -> None:
        self.arrived += 1
        if self.arrived >= self.parties:
            self._open.set()
        try:
            await asyncio.wait_for(self._open.wait(), self.patience)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        self.released.append(self._open.is_set())


def _reads_through(device, turnstile):
    """`device.get_config`, each caller held at the turnstile after its FIRST read.

    Per task rather than per call, because the read back is a read too: counting
    calls would hold a caller that had already been through at its own read back
    and prove nothing about the two of them.
    """
    real = device.get_config
    waited = set()

    async def get_config(name, *, allow_missing=False, as_table=False):
        answer = await real(name, allow_missing=allow_missing, as_table=as_table)
        task = asyncio.current_task()
        if task not in waited:
            waited.add(task)
            await turnstile.wait()
        return answer

    return get_config


async def _write_at_once(server, device, urls, turnstile=None, usernames=None):
    """Drive one write per url at the same moment, each from a client of its own.

    One `DahuaClient` per write and **one port for all of them**, because that
    is the scope the lock has: two entries on one device, which is what a
    recorder with two channels or a camera added twice already is. Two clients
    on two ports are two devices and take two locks, which is the next test.

    `server` therefore answers every one of them, and its script needs an entry
    per write.
    """
    if turnstile is None:
        turnstile = _Turnstile(len(urls))
    device.get_config = _reads_through(device, turnstile)
    async with aiohttp.ClientSession() as session:
        clients = []
        for user in usernames or ["u"] * len(urls):
            client = DahuaClient(user, "p", "127.0.0.1", server.port, 554, session)
            client._rpc2_get_config = device.get_config
            client._rpc2_set_config_tables = device.set_config_tables
            clients.append(client)

        async def one(client, url):
            try:
                return await client.get(url, True)
            except Exception as exception:  # pylint: disable=broad-except
                return exception

        answers = await asyncio.gather(
            *[one(client, url) for client, url in zip(clients, urls)]
        )
    return answers, turnstile


async def test_two_writes_at_once_keep_both_changes(socket_enabled):
    """@rroller's measurement on the first whole-table version, as a test.

    Both callers were told OK and motion detection ended up off: each read the
    table, the first wrote and read back its own field, and the second then sent
    the copy it had taken before that write. A whole-table write makes the
    second one's stale copy destructive rather than merely useless, which is why
    this arrived with the fix for the replacement semantics and not before it.
    """
    async with _serving(OK_THEN_PHANTOM, OK_THEN_PHANTOM) as server:
        device = _Rpc2Device()
        answers, turnstile = await _write_at_once(
            server, device, [ENABLE_ALONE, SENSITIVITY_ALONE]
        )

    assert all("OK" in answer for answer in answers), answers
    # The point of the whole thing: neither write cost the other one.
    assert device.holds("MotionDetect[0].Enable") == "true"
    assert device.holds("MotionDetect[0].Sensitivity") == "9"
    # And they were serialized rather than merely lucky.
    assert turnstile.released == [False, True]


async def test_the_second_write_sends_the_table_the_first_one_left(socket_enabled):
    """It re-reads inside its turn, so its copy is not the pre-write one.

    `holds` alone cannot tell a fresh read from an ordering that happened to
    suit: a device that applied the two writes in the other order would also
    hold both. What the lock buys is that the table the second caller SENDS
    already carries the first caller's field.
    """
    async with _serving(OK_THEN_PHANTOM, OK_THEN_PHANTOM) as server:
        device = _Rpc2Device()
        await _write_at_once(server, device, [ENABLE_ALONE, SENSITIVITY_ALONE])

    assert len(device.sent) == 2
    # Which of the two goes first is the event loop's business, so this reads
    # the pair rather than naming them: one table carries one change and the
    # other carries both. Written the other way round -- naming the Enable
    # caller as the first -- this failed on the order the loop happened to pick.
    changed = [
        (
            table["MotionDetect"][0]["Enable"] is True,
            table["MotionDetect"][0]["Sensitivity"] == 9,
        )
        for table in device.sent
    ]
    assert changed[1] == (True, True), changed
    assert changed[0] in ((True, False), (False, True)), changed


async def test_a_write_already_in_flight_elsewhere_does_not_delay_another_device(
    socket_enabled,
):
    """The lock is per device, as every other shared thing here is.

    Two boxes behind one address on different ports are not each other (#1084),
    and a lock that forgot that would queue every device in the installation
    behind one slow camera. Two separate devices, so the reads are expected to
    overlap -- which is the same turnstile reading the opposite way round.
    """
    async with _serving(OK_THEN_PHANTOM) as first, _serving(OK_THEN_PHANTOM) as second:
        turnstile = _Turnstile(2)
        one = _Rpc2Device()
        two = _Rpc2Device()
        async with aiohttp.ClientSession() as session:
            clients = []
            for server, device in ((first, one), (second, two)):
                client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
                client._rpc2_get_config = _reads_through(device, turnstile)
                client._rpc2_set_config_tables = device.set_config_tables
                clients.append(client)
            answers = await asyncio.gather(
                clients[0].get(ENABLE_ALONE, True),
                clients[1].get(SENSITIVITY_ALONE, True),
            )

    assert all("OK" in answer for answer in answers), answers
    assert one.holds("MotionDetect[0].Enable") == "true"
    assert two.holds("MotionDetect[0].Sensitivity") == "9"
    assert turnstile.released == [True, True]


async def test_a_turn_that_does_not_come_reports_the_original_refusal(
    socket_enabled, monkeypatch
):
    """Waiting for the lock cannot be worse than not having the fallback.

    The wait is inside the request's own deadline by a margin, so a turn that
    never comes raises where the handlers are reachable and the caller reports
    the refusal it was holding. Outside that margin the request's own timeout
    would cancel the task instead, and `CancelledError` is not an `Exception`:
    it would reach the poll's gather, which has no `return_exceptions`.

    The device is asserted untouched as well as the answer, because a version
    that never took the lock would pass on the answer alone only by writing.
    """
    monkeypatch.setattr(
        client_module,
        "TIMEOUT_SECONDS",
        client_module.RPC2_FALLBACK_MARGIN_SECONDS + 0.4,
    )
    async with _serving(OK_THEN_PHANTOM) as server:
        held = client_module._rpc2_config_write_lock(
            client_module._device_key("127.0.0.1", server.port)
        )
        await held.acquire()
        try:
            answer, device = await _write(server)
        finally:
            held.release()

    assert isinstance(answer, aiohttp.ClientResponseError)
    assert answer.status == 400
    assert device.reads == []
    assert device.sent == []


async def test_the_lock_is_released_when_the_write_is_reported_as_refused(
    socket_enabled,
):
    """A refused write must not leave the device's writes blocked for good.

    `asyncio.Lock` releases on the way out of `async with` however the block
    ends, so this is pinning that the lock really is held that way rather than
    acquired by hand -- a release left to a success path is the shape that
    wedges every later write on the host.
    """
    async with _serving(OK_THEN_PHANTOM) as first, _serving(OK_THEN_PHANTOM) as second:
        device = _Rpc2Device(applies=False)
        refused, _ = await _write(first, device=device)
        assert isinstance(refused, aiohttp.ClientResponseError)

        lock = client_module._rpc2_config_write_lock(
            client_module._device_key("127.0.0.1", first.port)
        )
        assert not lock.locked()

        landed, _ = await _write(second, device=_Rpc2Device())

    assert "OK" in landed, landed


async def test_two_entries_with_different_credentials_still_take_turns(socket_enabled):
    """The lock is keyed on the device, not on `_rpc2_key()`.

    Everything session-shaped here is keyed by device AND username, because a
    successful login is not scoped to anyone else. A config table is the
    opposite: it is the device's, and two entries added with different accounts
    write the same rows. Keying this lock the way the sessions are keyed would
    read as consistent and serialize nothing in exactly the case @rroller named,
    two entries on one device.
    """
    async with _serving(OK_THEN_PHANTOM, OK_THEN_PHANTOM) as server:
        device = _Rpc2Device()
        answers, turnstile = await _write_at_once(
            server,
            device,
            [ENABLE_ALONE, SENSITIVITY_ALONE],
            usernames=["admin", "viewer"],
        )

    assert all("OK" in answer for answer in answers), answers
    assert device.holds("MotionDetect[0].Enable") == "true"
    assert device.holds("MotionDetect[0].Sensitivity") == "9"
    assert turnstile.released == [False, True]


async def test_the_lock_spans_the_write_and_the_read_back(socket_enabled):
    """The turn covers the whole transaction, not only the read.

    Serializing the reads is not enough. A lock given up between the read and
    the put-back, or before the read back, starts the other caller's turn while
    this table is half written, and the stale copy lands after the fresh one
    exactly as before. The fake write suspends here, which is what a real RPC2
    round trip does and what an in-memory double does not, so a gap anywhere
    inside the transaction gives the other caller a turn to find.
    """
    async with _serving(OK_THEN_PHANTOM, OK_THEN_PHANTOM) as server:
        device = _Rpc2Device(set_delay=0.05)
        answers, _ = await _write_at_once(
            server, device, [ENABLE_ALONE, SENSITIVITY_ALONE]
        )

    assert all("OK" in answer for answer in answers), answers
    assert device.holds("MotionDetect[0].Enable") == "true"
    assert device.holds("MotionDetect[0].Sensitivity") == "9"


def _bare_client(device="10.9.9.9:80", username="admin"):
    """A client with only the attributes an RPC2 write reads, as the suite builds them."""
    client = object.__new__(DahuaClient)
    client._address = "10.9.9.9"
    client._device = device
    client._username = username
    return client


async def test_the_other_read_modify_write_path_takes_the_same_turn():
    """`_rpc2_set_config_value` is the same transaction and had the same hole.

    It is the route a device with no `configManager.cgi` at all takes -- a 404,
    not this camera's unreadable answer -- and it reads a table and puts it back
    exactly as the fallback does, so two at once lose one of the two changes the
    same way. Nothing in #1191 put that there. It is named here because the lock
    the fallback needs is the one that closes it, and a lock on one of two paths
    into the same table is not a guarantee of anything.
    """
    table = {"MotionDetect": [{"Enable": "false", "Sensitivity": "3"}]}
    turnstile = _Turnstile(2)
    waited = set()

    class _Session:
        async def get_config(self, payload):
            answer = {"table": deepcopy(table[payload["name"]])}
            task = asyncio.current_task()
            if task not in waited:
                waited.add(task)
                await turnstile.wait()
            return answer

        async def set_configs(self, pairs):
            # A real round trip suspends; an in-memory double that does not
            # gives the other caller no turn to take and proves nothing.
            await asyncio.sleep(0)
            for name, value in deepcopy(pairs):
                table[name] = value
            return {"result": True}

    class _Holder:
        client = _Session()
        task = None

    async def shared():
        return _Holder()

    clients = []
    for _ in range(2):
        client = _bare_client()
        client._shared_rpc2 = shared
        clients.append(client)

    await asyncio.gather(
        clients[0]._rpc2_set_config_value("MotionDetect", 0, "Enable", "true"),
        clients[1]._rpc2_set_config_value("MotionDetect", 0, "Sensitivity", "9"),
    )

    assert table["MotionDetect"][0]["Enable"] == "true"
    assert table["MotionDetect"][0]["Sensitivity"] == "9"
    assert turnstile.released == [False, True]


def _widens_the_window(device, reached, window=0.05):
    """`device.get_config`, announcing the first read and then pausing in it.

    The turnstile cannot stage this pair: the bare write performs no read, so it
    has nothing to be held at, and left to the loop it finished before the
    fallback's first read had even been answered -- which is the harmless order,
    and the first version of this test passed with the lock taken out because of
    it. So the fallback says when it has read, and holds the transaction open
    long enough for the other write to land inside it.
    """
    real = device.get_config

    async def get_config(name, *, allow_missing=False, as_table=False):
        answer = await real(name, allow_missing=allow_missing, as_table=as_table)
        if not reached.is_set():
            reached.set()
            await asyncio.sleep(window)
        return answer

    return get_config


async def test_a_bare_field_write_is_not_undone_by_a_whole_table_put_back(
    socket_enabled,
):
    """The one-field RPC2 write takes the turn too, though it reads nothing.

    `_rpc2_set_config_field` sends one field and never reads the table, so it
    cannot lose anyone else's change by itself. It can have its own change lost:
    a whole-table put-back already in flight read this table before this field
    moved, and sending that copy afterwards puts the old value back. Nothing
    answers for it -- the put-back's untouched-field check compares the table
    against its own snapshot, where the old value is what it expects.
    """
    async with _serving(OK_THEN_PHANTOM) as server:
        device = _Rpc2Device()
        reached = asyncio.Event()

        class _Holder:
            client = device
            task = None

        async def shared():
            return _Holder()

        async def one_field_write(client):
            # Inside the fallback's transaction, which is the only moment this
            # write can be lost in.
            await reached.wait()
            return await client._rpc2_set_config_field("MotionDetect[0].Sensitivity", 9)

        async with aiohttp.ClientSession() as session:
            putting_back = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            putting_back._rpc2_get_config = _widens_the_window(device, reached)
            putting_back._rpc2_set_config_tables = device.set_config_tables

            one_field = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            one_field._shared_rpc2 = shared

            answers = await asyncio.gather(
                putting_back.get(ENABLE_ALONE, True),
                one_field_write(one_field),
            )

    assert "OK" in answers[0], answers[0]
    assert device.holds("MotionDetect[0].Enable") == "true"
    assert device.holds("MotionDetect[0].Sensitivity") == "9"


async def test_the_lighting_paths_take_the_device_config_write_lock_as_well():
    """The lighting writers are the same transaction on tables CGI also reaches.

    `async_set_lighting_scheme_illuminator` and the white light override read
    LightingScheme and Lighting_V2 and put them back, and on this camera a CGI
    `setConfig` naming a field of either goes through the fallback above. So
    `_lighting_scheme_lock`, which keeps those writers from interleaving with
    each other, is not enough on its own: `_lighting_lock` takes the device's
    config-write lock behind it.

    This asserts what is held, and in which order: a request slot is taken
    before the config lock, never after it, which is what the test below is
    about. The lost update itself is measured on the paths above, which use the
    same lock on the same device.
    """
    client = _bare_client()
    client._lighting_scheme_lock = client_module._lighting_scheme_lock(client._device)
    client._host_limit = client_module._host_limiter(client._address)
    config = client_module._rpc2_config_write_lock(client._device)

    # One slot already taken, so this path's own slot exhausts the limiter and
    # holding one is visible from outside rather than read off a private count.
    await client._host_limit.acquire()
    try:
        assert not client._host_limit.locked()
        assert not config.locked()
        async with client._lighting_lock():
            assert client._host_limit.locked(), "no request slot was taken"
            assert config.locked()
            assert client._lighting_scheme_lock.locked()
        assert not config.locked()
        assert not client._lighting_scheme_lock.locked()
        assert not client._host_limit.locked()
    finally:
        client._host_limit.release()


async def test_a_lighting_writer_cannot_wedge_two_fallback_writes():
    """Nothing may hold the config-write lock while waiting for a request slot.

    The fallback is the reason. It is already inside a request slot when it
    learns it needs the lock, because that is when the CGI write failed, and it
    cannot give the slot back. So anything that holds the lock and queues for a
    slot closes a cycle: with `MAX_CONCURRENT_REQUESTS_PER_HOST` at 2, two
    fallback writes hold both slots and wait for the lock, the lighting writer
    holds the lock and waits for a slot, and nothing releases anything.

    Measured on the first version of this change, where `_lighting_lock` took
    the lock before the slot: both writes sat there until the inner budget gave
    up, came back refused, and the device was never written. `_lighting_lock`
    now takes the slot first, so a holder of the lock always has one already.

    Driven at the lock rather than through the loopback server, because what has
    to be staged is the moment two writes hold both slots while the lock is
    somewhere else, and that is the slot each write holds for its CGI attempt.
    """
    device = _Rpc2Device()
    refusal = aiohttp.ClientResponseError(
        request_info=None,
        history=(),
        status=400,
        message="not enough data for satisfy content length header",
    )
    holding_both_slots = asyncio.Event()
    ask_for_the_lock = asyncio.Event()

    def _fallback_client():
        client = _bare_client()
        client._host_limit = client_module._host_limiter(client._address)
        client._rpc2_get_config = device.get_config
        client._rpc2_set_config_tables = device.set_config_tables
        return client

    async def write(fields, last):
        client = _fallback_client()
        # The slot and the budget are what `_request` is holding when it reaches
        # the fallback, so the model has to hold them too.
        async with client._request_slot(), asyncio.timeout(3) as budget:
            if last:
                holding_both_slots.set()
            await ask_for_the_lock.wait()
            return await client._rpc2_fallback_write(fields, budget, refusal)

    async def lighting():
        client = _bare_client()
        client._host_limit = client_module._host_limiter(client._address)
        client._lighting_scheme_lock = client_module._lighting_scheme_lock(
            client._device
        )
        await holding_both_slots.wait()
        async with client._lighting_lock():
            return "took its turn"

    writes = [
        asyncio.ensure_future(write([("MotionDetect[0].Enable", "true")], False)),
        asyncio.ensure_future(write([("MotionDetect[0].Sensitivity", "9")], True)),
    ]
    turn = asyncio.ensure_future(lighting())
    await holding_both_slots.wait()
    # Long enough for the lighting writer to get as far as it is going to get,
    # which with the lock before the slot was the lock itself.
    await asyncio.sleep(0.1)
    ask_for_the_lock.set()

    answers = await asyncio.gather(*writes)
    assert await turn == "took its turn"

    assert all("OK" in answer for answer in answers), answers
    assert device.holds("MotionDetect[0].Enable") == "true"
    assert device.holds("MotionDetect[0].Sensitivity") == "9"


async def test_the_lost_table_latch_is_the_devices_and_not_one_accounts(
    socket_enabled,
):
    """What a write does to a table is firmware, so the verdict is the device's.

    `_HOST_RPC2_WRITES_UNSAFE` used to be keyed the way the RPC2 session is, by
    device and username, which reads as consistent and is the wrong scope: a
    second entry signed in as another user would be told nothing and would take
    the same route on a device already known to lose keys to it, on the way to
    losing a second table.
    """
    async with _serving(OK_THEN_PHANTOM, OK_THEN_PHANTOM) as server:
        device = _Rpc2Device(drops="MotionDetect[0].Sensitivity")
        async with aiohttp.ClientSession() as session:
            admin = DahuaClient("admin", "p", "127.0.0.1", server.port, 554, session)
            admin._rpc2_get_config = device.get_config
            admin._rpc2_set_config_tables = device.set_config_tables
            with pytest.raises(aiohttp.ClientResponseError):
                await admin.get(ENABLE_ALONE, True)

            assert client_module._HOST_RPC2_WRITES_UNSAFE == {admin._device}

            viewer = DahuaClient("viewer", "p", "127.0.0.1", server.port, 554, session)
            viewer._rpc2_get_config = device.get_config
            viewer._rpc2_set_config_tables = device.set_config_tables
            sent_before = len(device.sent)
            with pytest.raises(aiohttp.ClientResponseError):
                await viewer.get(ENABLE_ALONE, True)

    assert len(device.sent) == sent_before, "the other account took the route again"


async def test_a_cgi_write_that_landed_inside_another_turn_is_put_back(socket_enabled):
    """The lock cannot cover the CGI write that started the other transaction.

    On this firmware a write reaches the device and is carried out; it is only
    the answer that cannot be read. So a second caller's CGI write can land
    while the first caller is between its read and its put-back, and the
    put-back then reverts it. Nothing sees that: the first caller's
    untouched-field check compares the table against its own snapshot, where
    the old value is exactly what it expects.

    What saves the setting is the second caller's own fallback. It waits for its
    turn, reads the table the first one left, finds its value gone and sends it
    again. So the window is real and it heals, by this route and not by the
    lock, which is worth a test of its own because it is the sequence the camera
    in #1176 actually produces.
    """
    async with _serving(OK_THEN_PHANTOM, OK_THEN_PHANTOM) as server:
        device = _Rpc2Device()
        reached = asyncio.Event()

        async with aiohttp.ClientSession() as session:
            putting_back = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            putting_back._rpc2_get_config = _widens_the_window(device, reached)
            putting_back._rpc2_set_config_tables = device.set_config_tables

            second = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            second._rpc2_get_config = device.get_config
            second._rpc2_set_config_tables = device.set_config_tables

            async def inside_the_window():
                await reached.wait()
                # Its CGI write reaching the device, which is what this firmware
                # does before mangling the answer to it.
                _assign(device.tables["MotionDetect"], "MotionDetect[0].Sensitivity", 9)
                return await second.get(SENSITIVITY_ALONE, True)

            answers = await asyncio.gather(
                putting_back.get(ENABLE_ALONE, True), inside_the_window()
            )

    assert all("OK" in answer for answer in answers), answers
    assert device.holds("MotionDetect[0].Enable") == "true"
    assert device.holds("MotionDetect[0].Sensitivity") == "9"
    # Two tables sent: the put-back that reverted it, and the one that put it
    # back. If the second caller had taken the "it already holds this" early
    # return it would have sent nothing and the setting would be gone.
    assert len(device.sent) == 2, device.sent
