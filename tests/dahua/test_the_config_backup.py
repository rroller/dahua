"""Reading back the settings this integration can change.

Asked for because a Dahua config write is frequently not reversible from here.
`VideoAnalyseRule` enables over CGI and will not disable; several writes are
accepted and then ignored. Once a setting has moved there is often no way to
discover what it used to be, and the device keeps no history either.

So what this is: a record of the values. Not a restore -- the write side
cannot put several of them back, and a service that implied otherwise would be
the more dangerous thing to ship.

`test_the_backup_covers_what_can_be_changed.py` checks the *list* against the
code. This checks the behaviour around it.
"""

import json
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua.camera import DahuaCamera
from custom_components.dahua.const import BACKED_UP_TABLES

PASSWORD = "hunter2-must-never-appear"


class _Client:
    """A device that serves some tables and refuses the rest.

    Refusing is the ordinary case: every model lacks most of these, and
    `async_get_config` returns {} rather than raising for a table the device
    will not serve.
    """

    def __init__(self, serves=("VideoColor", "MotionDetect")):
        self.serves = set(serves)
        self.asked = []

    async def async_get_config(self, name):
        self.asked.append(name)
        if name not in self.serves:
            return {}
        return {"table.%s[0].Enable" % name: "true"}


class _Coordinator:
    def __init__(self, client=None):
        self.client = client or _Client()

    def get_device_name(self):
        return "Front Door"

    def get_model(self):
        return "IPC-HDW1234"

    def get_firmware_version(self):
        return "2.800.0000016.0.R"

    def get_channel(self):
        return 0


def _camera(client=None, *, allowed=True, writer=None):
    camera = object.__new__(DahuaCamera)
    camera._coordinator = _Coordinator(client)

    def _run(func, *args):
        return (writer or func)(*args)

    async def _executor(func, *args):
        return _run(func, *args)

    camera.hass = SimpleNamespace(
        config=SimpleNamespace(is_allowed_path=lambda path: allowed),
        async_add_executor_job=_executor,
    )
    return camera


# --- what it reads ----------------------------------------------------------


async def test_it_asks_for_every_table_that_can_be_changed():
    """The point of the feature. A table missing from the request is a setting
    somebody cannot get back."""
    camera = _camera()

    await camera.async_backup_config()

    assert camera._coordinator.client.asked == list(BACKED_UP_TABLES)


async def test_a_table_the_device_serves_is_in_the_backup():
    camera = _camera(_Client(serves=("VideoColor",)))

    backup = await camera.async_backup_config()

    assert backup["tables"]["VideoColor"] == {"table.VideoColor[0].Enable": "true"}


async def test_a_table_the_device_refuses_is_named_not_dropped():
    """ "This model has no such table" and "the backup skipped it" look the same
    from the outside, and the difference is the whole value of the thing."""
    camera = _camera(_Client(serves=("VideoColor",)))

    backup = await camera.async_backup_config()

    assert "VideoColor" not in backup["refused"]
    assert "MotionDetect" in backup["refused"]
    assert set(backup["tables"]) | set(backup["refused"]) == set(BACKED_UP_TABLES)


async def test_it_says_which_device_and_when():
    """A backup nobody can identify later is not one."""
    camera = _camera()

    backup = await camera.async_backup_config()

    assert backup["device"]["model"] == "IPC-HDW1234"
    assert backup["device"]["firmware"] == "2.800.0000016.0.R"
    assert backup["device"]["channel"] == 0
    assert backup["created"]


async def test_the_backup_carries_no_credential():
    """It is going to be attached to issues. None of these tables holds a
    password, and this is the assertion that keeps it that way."""
    camera = _camera()

    backup = await camera.async_backup_config()

    assert PASSWORD not in json.dumps(backup)


# --- writing it out ---------------------------------------------------------


async def test_without_a_filename_nothing_is_written():
    written = []
    camera = _camera(writer=lambda *args: written.append(args))

    backup = await camera.async_backup_config()

    assert written == []
    assert "written_to" not in backup


async def test_with_a_filename_it_writes_and_says_where(tmp_path):
    target = str(tmp_path / "dahua.json")
    camera = _camera()

    backup = await camera.async_backup_config(filename=target)

    assert backup["written_to"] == target
    on_disk = json.loads((tmp_path / "dahua.json").read_text(encoding="utf-8"))
    assert on_disk["tables"] == backup["tables"]


async def test_a_path_home_assistant_may_not_write_to_is_refused(tmp_path):
    """The same allowlist a camera snapshot goes through. Without it a service
    call writes anywhere the Home Assistant process can reach."""
    target = str(tmp_path / "nope.json")
    written = []
    camera = _camera(allowed=False, writer=lambda *args: written.append(args))

    with pytest.raises(HomeAssistantError) as refused:
        await camera.async_backup_config(filename=target)

    assert refused.value.translation_key == "backup_path_not_allowed"
    assert written == [], "it wrote the file it had just refused to write"


async def test_a_write_that_fails_says_so_in_a_translatable_error(tmp_path):
    """A full disk or a bad directory. Reporting success for a backup that was
    not written is the failure worth avoiding here."""

    def _boom(*args):
        raise OSError(28, "No space left on device")

    camera = _camera(writer=_boom)

    with pytest.raises(HomeAssistantError) as failed:
        await camera.async_backup_config(filename=str(tmp_path / "x.json"))

    assert failed.value.translation_key == "backup_could_not_be_written"


async def test_a_refused_path_still_does_not_leave_a_half_backup(tmp_path):
    """The read has already happened by then; the service must raise rather
    than return something that looks like a successful backup."""
    camera = _camera(allowed=False)

    with pytest.raises(HomeAssistantError):
        await camera.async_backup_config(filename=str(tmp_path / "x.json"))

    assert not list(tmp_path.iterdir())
