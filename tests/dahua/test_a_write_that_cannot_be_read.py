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
    """What is left of a table that a one-field write REPLACED (the VTH shape)."""
    node = value
    for step in reversed(_steps(address)[1:]):
        node = ([None] * step + [node]) if isinstance(step, int) else {step: node}
    return node


class _Rpc2Device:
    """The tables a device holds, and what a field write does to them.

    State, not a prepared answer: the write fix reads, writes and reads back, so
    a double that answered the same table twice could not fail the way firmware
    does. `mode="edit"` is the DHI-NVR5464 and the VTO2000A. `mode="replace"` is
    the VTH5221D, where the table becomes only what was written. `applies=False`
    is the third shape, #647's: the call is accepted and nothing changes.
    """

    def __init__(
        self,
        tables=None,
        *,
        mode="edit",
        applies=True,
        also_changes=None,
        get_raises=None,
        set_raises=None,
        get_delay=0,
        set_delay=0,
    ):
        self.tables = deepcopy(MOTION_TABLE if tables is None else tables)
        self.mode = mode
        self.applies = applies
        self.also_changes = also_changes
        self.get_raises = get_raises
        self.set_raises = set_raises
        self.get_delay = get_delay
        self.set_delay = set_delay
        self.reads = []
        self.writes = []

    async def get_config(self, name, *, allow_missing=False, as_table=False):
        self.reads.append(name)
        if self.get_delay:
            await asyncio.sleep(self.get_delay)
        if self.get_raises is not None:
            raise self.get_raises
        table = deepcopy(self.tables.get(name))
        return table if as_table else flatten_rpc2_config(name, table)

    async def set_config_field(self, address, value):
        self.writes.append((address, value))
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
        return {"result": True}

    def holds(self, address):
        """One field of a held table, as flat text, the way a read would give it."""
        name = _config_table_of(address)
        flat = flatten_rpc2_config(name, self.tables.get(name))
        return flat.get("table." + address)


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
    assert device.writes == [("MotionDetect[0].Enable", True)]
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

    address, value = device.writes[0]
    assert value is True, repr(value)


async def test_an_integer_field_goes_out_as_an_integer(socket_enabled):
    """The other half of the typing, from a table that holds a number."""
    device = _Rpc2Device({"SmartMotionDetect": [{"Sensitivity": 2, "Enable": True}]})
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, SENSITIVITY, device)

    assert "OK" in answer, answer
    assert device.writes == [("SmartMotionDetect[0].Sensitivity", 4)]
    assert isinstance(device.writes[0][1], int)


async def test_a_write_that_already_landed_sends_nothing(socket_enabled):
    """The CGI write may well have happened: the junk arrives after a correct 200.

    One read, no write, and the caller is told what a CGI `OK` would have told
    it. This is the cheapest outcome and the only one with nothing to undo.
    """
    device = _Rpc2Device({"MotionDetect": [{"Enable": True, "Sensitivity": 3}]})
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, ENABLE_ALONE, device)

    assert "OK" in answer, answer
    assert device.writes == []
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


async def test_a_table_the_write_replaced_is_reported_as_refused(socket_enabled):
    """The VTH5221D shape: `result: true`, and fifteen keys of sixteen gone.

    The field asked for reads back correctly, so checking only that would call
    this a success. The write is reported as refused instead, and the host is
    written off for writes because the next one would do the same.
    """
    device = _Rpc2Device(mode="replace")
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
    """These tables carry cloud credentials and server lists. Keys only."""
    with caplog.at_level("WARNING"):
        async with _serving(OK_THEN_PHANTOM) as server:
            await _write(server, ENABLE_ALONE, _Rpc2Device(mode="replace"))

    assert "table.MotionDetect[0].Sensitivity" in caplog.text
    assert "missing" in caplog.text


async def test_a_host_that_lost_a_table_is_not_written_to_again(socket_enabled):
    """Latched for the life of the process, so the second write never goes out."""
    async with _serving(OK_THEN_PHANTOM, OK_THEN_PHANTOM) as server:
        device = _Rpc2Device(mode="replace")
        async with aiohttp.ClientSession() as session:
            client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            client._rpc2_get_config = device.get_config
            client._rpc2_set_config_field = device.set_config_field
            with pytest.raises(aiohttp.ClientResponseError):
                await client.get(ENABLE_ALONE, True)
            after_first = list(device.writes)
            with pytest.raises(aiohttp.ClientResponseError):
                await client.get(ENABLE_ALONE, True)

    assert device.writes == after_first
    assert len(after_first) == 1


async def test_a_neighbour_that_changed_is_reported_without_writing_the_host_off(
    socket_enabled,
):
    """A changed neighbour is not a replaced table, so it must not latch.

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
    assert device.writes == [("MotionDetect[0].Enable", True)]


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
    assert device.writes == []


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
            client._rpc2_set_config_field = device.set_config_field
            answer = await client.enable_motion_detection(0, True)

    assert "OK" in answer, answer
    assert device.writes == [("MotionDetect[0].Enable", True)]
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
    assert device.writes == []


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
    assert device.writes == []


async def test_a_write_that_is_not_a_config_write_is_not_routed(socket_enabled):
    """A reboot has no field to address and no RPC2 equivalent measured here."""
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, REBOOT, verify_ok=False)

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert device.writes == []


async def test_a_read_is_never_written(socket_enabled):
    """The read fallback answers a read. Nothing about it may write anything."""
    async with _serving(UNREADABLE) as server:
        answer, device = await _write(server, MOTION_DETECT, verify_ok=False)

    assert device.writes == []
    assert device.reads == ["MotionDetect"]


async def test_a_host_rpc2_cannot_serve_is_not_routed(socket_enabled):
    """The host-wide verdict applies to writes as much as to reads."""
    async with _serving(OK_THEN_PHANTOM) as server:
        client_module._HOST_RPC2_UNAVAILABLE.add(("127.0.0.1:%d" % server.port, "u"))
        answer, device = await _write(server)

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert device.writes == []


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
    assert device.writes == []


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
    assert device.writes == []


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
    assert device.writes == [
        ("Lighting[0][0].Mode", "Auto"),
        ("Lighting[0][0].MiddleLight[0].Light", 100),
    ]


async def test_two_tables_in_one_write_are_both_read_back(socket_enabled):
    """Each table named is read before and after, and verified the same way."""
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


async def test_a_percent_encoded_value_is_written_decoded(socket_enabled):
    """Two write methods encode their value. The device is sent the value."""
    device = _Rpc2Device({"ChannelTitle": [{"Name": "Camera 1"}]})
    url = SET_CONFIG + "&ChannelTitle[0].Name=Front%20Door"
    async with _serving(OK_THEN_PHANTOM) as server:
        answer, device = await _write(server, url, device)

    assert "OK" in answer, answer
    assert device.writes == [("ChannelTitle[0].Name", "Front Door")]


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
