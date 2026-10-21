"""A monitor that does not describe itself still has to be read correctly.

#949 decides whether an indoor monitor gets camera entities from its own entry
in `RemoteDevice`:

    table.Local0 = {"DeviceClass": "VTH", "SupportVideo": false, ...}

measured on a VTH2421F-P. A VTH5221D on 3.000.0012000.0.R carries no such row:

    configManager.getConfig {"name": "RemoteDevice"}
      rows: Vto00 .. Vto19, and no Local0 at all

So the read answered None, the monitor was taken for one with a camera, and on
the live device that produced a `camera.*` entity answering 500 through Home
Assistant's own proxy and a Motion Detection switch reading `off` out of a key
the coordinator never has, because the device refuses the table it comes from:

    configManager.getConfig {"name": "MotionDetect"}  refused 268959743
    configManager.getConfig {"name": "VideoInput"}    refused 268959743
    devVideoInput.getCaps                             Method not found
    magicBox.getProductDefinition  VideoInputChannels 1

The product definition claims a video input channel either way, which is why it
is not the signal. The refusal is.
"""

import pytest

from custom_components.dahua.client import DahuaClient
from custom_components.dahua.rpc2 import Rpc2MethodRefused

REMOTE_DEVICE = "RemoteDevice"
VIDEO_INPUT = "VideoInput"


def _client(answers):
    """A client whose RPC2 calls are answered from `answers`.

    Keyed by table name. A value that is an exception is raised, so a refusal
    reads the way the device really gives one.
    """
    c = object.__new__(DahuaClient)
    c._address = "10.0.0.230"
    c.asked = []

    async def shared(call):
        class _Recorder:
            def request(self, method, params=None):
                name = (params or {}).get("name")
                c.asked.append(name)
                answer = answers.get(name, KeyError(name))

                async def _answer():
                    if isinstance(answer, BaseException):
                        raise answer
                    return answer

                return _answer()

        return await call(_Recorder())

    c._rpc2_shared_call = shared
    return c


def _rows(**rows):
    return {"params": {"table": dict(rows)}}


def _refused():
    return Rpc2MethodRefused("configManager.getConfig", 268959743, "refused")


# --- the monitor that does say ------------------------------------------------


async def test_a_monitor_that_says_it_has_no_camera_is_believed():
    c = _client(
        {REMOTE_DEVICE: _rows(Local0={"DeviceClass": "VTH", "SupportVideo": False})}
    )

    assert await c._vth_own_video() is False
    assert c.asked == [REMOTE_DEVICE], "asked a second question it did not need"


async def test_a_monitor_that_says_it_has_one_is_believed_too():
    c = _client(
        {REMOTE_DEVICE: _rows(Local0={"DeviceClass": "VTH", "SupportVideo": True})}
    )

    assert await c._vth_own_video() is True
    assert c.asked == [REMOTE_DEVICE]


async def test_a_local_row_with_no_boolean_still_answers_nothing():
    """Unchanged. Only a real boolean counts, and the fallback is for a monitor
    with no row rather than for one whose row is unhelpful: it has described
    itself, and asking again would second-guess it."""
    c = _client(
        {REMOTE_DEVICE: _rows(Local0={"DeviceClass": "VTH", "SupportVideo": "yes"})}
    )

    assert await c._vth_own_video() is None


# --- the monitor that does not ------------------------------------------------


async def test_a_monitor_with_no_local_row_is_asked_about_video_input():
    """The VTH5221D. Twenty VTO rows and nothing about itself."""
    c = _client(
        {
            REMOTE_DEVICE: _rows(**{"Vto{0:02d}".format(n): {} for n in range(20)}),
            VIDEO_INPUT: _refused(),
        }
    )

    assert await c._vth_own_video() is False
    assert c.asked == [REMOTE_DEVICE, VIDEO_INPUT]


async def test_a_monitor_that_serves_video_input_keeps_its_cameras():
    """The negative control, and the one that decides whether this is safe.
    Removing a camera is breaking, so only a refusal may do it: a monitor that
    answers the question is left exactly as it was."""
    c = _client(
        {
            REMOTE_DEVICE: _rows(Vto00={}),
            VIDEO_INPUT: {"params": {"table": {"Format": "PAL"}}},
        }
    )

    assert await c._vth_own_video() is None


async def test_a_read_that_fails_for_another_reason_changes_nothing():
    """A dropped session is not a measurement. Treating it as one would take a
    working camera away on a bad afternoon and put it back on the next reload,
    which is worse than the bug this fixes."""
    c = _client({REMOTE_DEVICE: _rows(Vto00={}), VIDEO_INPUT: ConnectionError("gone")})

    assert await c._vth_own_video() is None


async def test_a_table_with_no_rows_at_all_is_asked_too():
    c = _client({REMOTE_DEVICE: {"params": {"table": {}}}, VIDEO_INPUT: _refused()})

    assert await c._vth_own_video() is False


@pytest.mark.parametrize("local", [None, "Local0", 7, []])
async def test_anything_that_is_not_a_row_falls_through_to_the_question(local):
    c = _client({REMOTE_DEVICE: _rows(Local0=local), VIDEO_INPUT: _refused()})

    assert await c._vth_own_video() is False


async def test_a_local_row_for_something_other_than_a_vth_is_not_this_device():
    """`DeviceClass` is checked because Local0 is not guaranteed to be the
    monitor. Where it is not, the monitor has still said nothing about itself,
    so the same question is asked."""
    c = _client(
        {
            REMOTE_DEVICE: _rows(Local0={"DeviceClass": "VTO", "SupportVideo": True}),
            VIDEO_INPUT: _refused(),
        }
    )

    assert await c._vth_own_video() is False


# --- and the whole table read failing is still silent -------------------------


async def test_no_remote_device_table_asks_nothing_further():
    """Unchanged, and deliberately: a device that would not serve RemoteDevice
    at all has given no reason to think the next read would mean more."""
    c = _client({REMOTE_DEVICE: ConnectionError("no session")})

    assert await c._vth_own_video() is None
    assert c.asked == [REMOTE_DEVICE]
