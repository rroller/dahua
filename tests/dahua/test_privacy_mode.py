"""Privacy mode reads and writes the LeLensMask config over RPC2."""

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.rpc2 import DahuaRpc2Client
from custom_components.dahua.switch import DahuaPrivacyModeBinarySwitch


class _FakeRpc2(DahuaRpc2Client):
    """An RPC2 client whose transport is replaced by a recorded config table."""

    def __init__(self, table):
        super().__init__("user", "pass", "192.168.1.10", 80, 554, None, False)
        self._table = table
        self.written = None
        self._session_id = "session"

    async def request(self, method, params=None, **kwargs):
        if method == "configManager.getConfig":
            return {"params": {"table": self._table}}
        if method == "configManager.setConfig":
            self.written = params
            return {"result": True}
        raise AssertionError(f"unexpected RPC2 method {method}")


async def test_get_privacy_mode_reads_the_enable_flag():
    assert await _FakeRpc2([{"Enable": True}]).async_get_privacy_mode() is True
    assert await _FakeRpc2([{"Enable": False}]).async_get_privacy_mode() is False


async def test_get_privacy_mode_defaults_to_off_when_enable_is_absent():
    assert await _FakeRpc2([{}]).async_get_privacy_mode() is False


async def test_set_privacy_mode_keeps_the_cameras_own_schedule():
    """Only Enable changes; TimeSection and LastPosition are written back intact."""
    schedule = [["1 00:00:00-23:59:59"]]
    position = [-0.586, -0.206, 0.0078125]
    rpc2 = _FakeRpc2([{"Enable": False, "TimeSection": schedule, "LastPosition": position}])

    await rpc2.async_set_privacy_mode(True)

    assert rpc2.written["name"] == "LeLensMask"
    entry = rpc2.written["table"][0]
    assert entry["Enable"] is True
    assert entry["TimeSection"] == schedule
    assert entry["LastPosition"] == position


async def test_malformed_lelensmask_response_is_rejected():
    for table in ([], None, ["not-a-dict"]):
        try:
            await _FakeRpc2(table).async_get_privacy_mode()
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for table={table!r}")


class _FailingClient:
    async def async_get_privacy_mode(self):
        raise ConnectionError("camera went away")


async def test_a_failed_poll_keeps_the_last_known_state():
    """A transient RPC2 failure must not flip the switch off in the UI."""
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _FailingClient()
    coordinator.data = {"privacy_mode_enabled": True}

    assert await coordinator._async_fetch_privacy_mode() == {"privacy_mode_enabled": True}


async def test_a_failed_poll_before_any_data_reports_off():
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _FailingClient()
    coordinator.data = None

    assert await coordinator._async_fetch_privacy_mode() == {"privacy_mode_enabled": False}


def test_the_switch_reflects_the_coordinator_state():
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.data = {"privacy_mode_enabled": True}
    switch = object.__new__(DahuaPrivacyModeBinarySwitch)
    switch._coordinator = coordinator

    assert switch.is_on is True

    coordinator.data = {}
    assert switch.is_on is False
