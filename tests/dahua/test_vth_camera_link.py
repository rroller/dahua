"""Which camera an indoor monitor (VTH) opens on when a VTO calls it.

The VTH manual describes a per-VTO setting: select an IPC, and when this VTO calls you
see that IPC. A VTH2421F-P on 4.800.0000000.1.R does not offer it on its screen, but
keeps it in its VTOInfo table as LinkIPC, naming the camera by its slot key in
VTHRemoteIPCInfo:

    VTOInfo.Vto00            {"Address": "<vto>", "MachineAddress": "Main VTO", "LinkIPC": ""}
    VTHRemoteIPCInfo.Ipc32   {"Address": "<ipc>", "MachineAddress": "Front", ...}

Writing "Ipc32" there (the whole table back, with only that changed) answered
`result: true`, read back, and made the next call from the VTO open on Front on the
main monitor and both extensions. Writing "" back restored the table byte for byte.
On one extension LinkIPC was absent rather than empty.

Pinned closely because a device answers a setConfig it did not apply with OK, and
because VTHRemoteIPCInfo carries each camera's login: none of it may leave the client.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import DahuaClient, vth_camera_links
from custom_components.dahua.coordinator import VTH_CAMERA_LINKS
from custom_components.dahua.select import NO_CAMERA, DahuaVthCameraLinkSelect

EMPTY_IPC = {"Address": "0.0.0.0", "Channel": 0, "MachineAddress": "", "Port": 554,
             "UserName": "admin", "Password": "default"}


def _vto_table(link="", **extra):
    vto = {"Address": "10.0.0.4", "MachineAddress": "Main VTO", "Enable": True,
           "Username": "admin", "Password": "vto-secret", "RingVolume": 70}
    if link is not None:
        vto["LinkIPC"] = link
    vto.update(extra)
    return {"Vto00": vto,
            "Vto01": {"Address": "0.0.0.0", "MachineAddress": "", "LinkIPC": ""}}


def _camera_table():
    table = {"Ipc%02d" % i: dict(EMPTY_IPC) for i in range(64)}
    table["Ipc32"] = {"Address": "10.0.0.8", "Channel": 0, "MachineAddress": "Front",
                      "Port": 554, "UserName": "admin", "Password": "ipc-secret"}
    return table


# --- reading the tables -----------------------------------------------------

def test_the_measured_tables_read_as_one_vto_and_one_camera():
    assert vth_camera_links(_vto_table(), _camera_table()) == {
        "vtos": {"Vto00": {"name": "Main VTO", "link": ""}},
        "cameras": {"Ipc32": "Front"},
    }


def test_a_link_is_read_as_the_camera_slot_key():
    links = vth_camera_links(_vto_table("Ipc32"), _camera_table())

    assert links["vtos"]["Vto00"]["link"] == "Ipc32"


@pytest.mark.parametrize("link", [None, 0, False])
def test_an_absent_or_odd_link_reads_as_no_camera(link):
    """Absent was measured, on one extension. The others are what it could be next."""
    vto = _vto_table(link=None) if link is None else _vto_table(LinkIPC=link)

    assert vth_camera_links(vto, _camera_table())["vtos"]["Vto00"]["link"] == ""


def test_unnamed_devices_are_named_by_their_slot():
    vto = _vto_table(MachineAddress="  ")
    cameras = _camera_table()
    cameras["Ipc33"] = dict(cameras["Ipc32"], Address="10.0.0.3", MachineAddress="")

    links = vth_camera_links(vto, cameras)

    assert links["vtos"]["Vto00"]["name"] == "Vto00"
    assert links["cameras"] == {"Ipc32": "Front", "Ipc33": "Ipc33"}


@pytest.mark.parametrize("address", ["0.0.0.0", "", None])
def test_an_empty_slot_is_not_a_camera(address):
    cameras = _camera_table()
    cameras["Ipc32"]["Address"] = address

    assert vth_camera_links(_vto_table(), cameras)["cameras"] == {}


def test_no_login_leaves_the_tables():
    links = vth_camera_links(_vto_table(), _camera_table())

    text = repr(links)
    assert "secret" not in text and "admin" not in text and "default" not in text


@pytest.mark.parametrize("vto, cameras", [(None, {}), ({}, None), ([], {}), ({}, "x")])
def test_a_missing_table_is_an_error_not_an_empty_answer(vto, cameras):
    """Empty would read as "this VTH knows no VTO" and take the controls away."""
    with pytest.raises(ValueError):
        vth_camera_links(vto, cameras)


# --- the client ---------------------------------------------------------------

class _Device:
    """A VTH's two tables behind RPC2, recording every request."""

    def __init__(self, vto_table, camera_table=None, applies=True, drops_empty=False):
        self.vto_table = vto_table
        self.camera_table = camera_table if camera_table is not None else _camera_table()
        self.applies = applies
        # A VTH that stores no LinkIPC at all rather than an empty one, which is
        # how the measured extension that had no field would read back.
        self.drops_empty = drops_empty
        self.asked = []

    async def request(self, method, params=None, **kwargs):
        self.asked.append((method, params))
        if method == "configManager.getConfig":
            table = {"VTOInfo": self.vto_table,
                     "VTHRemoteIPCInfo": self.camera_table}[params["name"]]
            return {"result": True, "params": {"table": _copy(table)}}
        if method == "configManager.setConfig":
            if self.applies:
                self.vto_table = _copy(params["table"])
                if self.drops_empty:
                    for slot in self.vto_table.values():
                        if slot.get("LinkIPC") == "":
                            del slot["LinkIPC"]
            return {"result": True}
        raise AssertionError(method)


def _copy(table):
    return {key: dict(value) for key, value in table.items()}


def _client(device):
    client = DahuaClient("u", "p", "10.0.0.5", 80, 554, None)

    async def shared_call(action):
        return await action(device)

    client._rpc2_shared_call = shared_call
    return client


async def test_reading_asks_for_exactly_the_two_tables():
    device = _Device(_vto_table())

    links = await _client(device).async_get_vth_camera_links()

    assert device.asked == [
        ("configManager.getConfig", {"name": "VTOInfo"}),
        ("configManager.getConfig", {"name": "VTHRemoteIPCInfo"}),
    ]
    assert links["cameras"] == {"Ipc32": "Front"}


async def test_linking_writes_the_whole_table_with_only_that_link_changed():
    device = _Device(_vto_table())
    before = _copy(device.vto_table)

    assert await _client(device).async_set_vth_camera_link("Vto00", "Ipc32") is True

    methods = [method for method, _ in device.asked]
    assert methods == ["configManager.getConfig", "configManager.setConfig",
                       "configManager.getConfig"]
    written = device.asked[1][1]
    assert written["name"] == "VTOInfo" and written["options"] == []
    expected = _copy(before)
    expected["Vto00"]["LinkIPC"] = "Ipc32"
    assert written["table"] == expected, "changed more than the one link"


async def test_none_writes_an_empty_link():
    device = _Device(_vto_table("Ipc32"))

    assert await _client(device).async_set_vth_camera_link("Vto00", "") is True
    assert device.vto_table["Vto00"]["LinkIPC"] == ""


async def test_none_on_a_vth_without_the_field_reads_back_as_landed():
    """The measured extension had no LinkIPC at all."""
    device = _Device(_vto_table(link=None))

    assert await _client(device).async_set_vth_camera_link("Vto00", "") is True


async def test_none_reads_back_as_landed_on_a_vth_that_stores_no_empty_field():
    device = _Device(_vto_table("Ipc32"), drops_empty=True)

    assert await _client(device).async_set_vth_camera_link("Vto00", "") is True
    assert "LinkIPC" not in device.vto_table["Vto00"]


async def test_a_write_the_device_ignored_is_reported():
    device = _Device(_vto_table(), applies=False)

    assert await _client(device).async_set_vth_camera_link("Vto00", "Ipc32") is False


async def test_only_the_named_slot_is_changed():
    """Vto01 is the neighbouring-but-wrong slot."""
    device = _Device(_vto_table())

    await _client(device).async_set_vth_camera_link("Vto00", "Ipc32")

    assert device.vto_table["Vto01"]["LinkIPC"] == ""


async def test_a_slot_that_is_not_there_is_not_written():
    device = _Device(_vto_table())

    with pytest.raises(ValueError):
        await _client(device).async_set_vth_camera_link("Vto07", "Ipc32")

    assert all(method != "configManager.setConfig" for method, _ in device.asked)


# --- the coordinator ----------------------------------------------------------

def _coordinator(device_class="VTH", data=None, client=None):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._device_class = device_class
    c.data = data
    c.client = client
    return c


@pytest.mark.parametrize("device_class, expected", [
    ("VTH", True), (" vth ", True), ("VTO", False), ("VTHX", False), ("", False)])
def test_only_a_device_that_says_vth_is_an_indoor_monitor(device_class, expected):
    assert _coordinator(device_class).is_indoor_monitor() is expected


def test_a_coordinator_without_a_class_is_not_an_indoor_monitor():
    assert not object.__new__(DahuaDataUpdateCoordinator).is_indoor_monitor()


async def test_the_poll_stores_the_links():
    links = vth_camera_links(_vto_table(), _camera_table())
    client = SimpleNamespace(async_get_vth_camera_links=AsyncMock(return_value=links))

    assert await _coordinator(client=client)._async_fetch_vth_camera_links() == {
        VTH_CAMERA_LINKS: links}


async def test_a_failed_read_keeps_the_last_answer():
    """So one refused read does not empty the select's options."""
    links = vth_camera_links(_vto_table(), _camera_table())
    client = SimpleNamespace(async_get_vth_camera_links=AsyncMock(
        side_effect=ConnectionError("gone")))
    coordinator = _coordinator(data={VTH_CAMERA_LINKS: links}, client=client)

    assert await coordinator._async_fetch_vth_camera_links() == {VTH_CAMERA_LINKS: links}


async def test_a_failed_first_read_adds_nothing():
    client = SimpleNamespace(async_get_vth_camera_links=AsyncMock(
        side_effect=ValueError("no table")))

    assert await _coordinator(data={}, client=client)._async_fetch_vth_camera_links() is None


def test_the_links_are_read_from_the_poll_data():
    links = {"vtos": {}, "cameras": {}}

    assert _coordinator(data={VTH_CAMERA_LINKS: links}).get_vth_camera_links() is links
    assert _coordinator(data=None).get_vth_camera_links() is None


# --- the select ---------------------------------------------------------------

def _select(links, vto="Vto00", landed=True):
    coordinator = SimpleNamespace(
        get_vth_camera_links=lambda: links,
        get_serial_number=lambda: "AA0082DPAJB838E",
        get_device_name=lambda: "Hall VTH",
        client=SimpleNamespace(
            async_set_vth_camera_link=AsyncMock(return_value=landed)),
        async_refresh=AsyncMock(),
        last_update_success=True,
    )
    select = object.__new__(DahuaVthCameraLinkSelect)
    select._coordinator = coordinator
    select.coordinator = coordinator
    select._vto = vto
    return select, coordinator


def _links(link="", cameras=None):
    return {"vtos": {"Vto00": {"name": "Main VTO", "link": link}},
            "cameras": {"Ipc32": "Front"} if cameras is None else cameras}


def test_the_options_are_none_and_the_cameras():
    select, _ = _select(_links())

    assert select.options == [NO_CAMERA, "Front"]


def test_no_link_is_none():
    select, _ = _select(_links(""))

    assert select.current_option == NO_CAMERA


def test_a_link_is_the_camera_by_name():
    select, _ = _select(_links("Ipc32"))

    assert select.current_option == "Front"


def test_a_link_to_a_slot_that_is_not_a_camera_is_unknown_not_none():
    """The VTH has something set. Saying "none" would hide that."""
    select, _ = _select(_links("Ipc40"))

    assert select.current_option is None


def test_cameras_with_the_same_name_are_told_apart():
    select, _ = _select(_links("Ipc33", {"Ipc32": "Gate", "Ipc33": "Gate"}))

    assert select.options == [NO_CAMERA, "Gate", "Gate (Ipc33)"]
    assert select.current_option == "Gate (Ipc33)"


def test_a_camera_called_none_is_not_the_none_option():
    select, _ = _select(_links("Ipc32", {"Ipc32": "None"}))

    assert select.options == [NO_CAMERA, "None (Ipc32)"]
    assert select.current_option == "None (Ipc32)"


def test_the_unique_id_names_the_vto_slot():
    select, _ = _select(_links())

    assert select.unique_id == "AA0082DPAJB838E_camera_link_vto00"


def test_it_is_available_only_while_the_vth_lists_its_vto():
    select, _ = _select(_links())
    assert select.available

    gone, _ = _select({"vtos": {}, "cameras": {}})
    assert not gone.available


def test_before_any_read_it_is_unavailable_not_broken():
    select, _ = _select(None)

    assert not select.available
    assert select.options == [NO_CAMERA]
    assert select.current_option == NO_CAMERA


async def test_choosing_a_camera_writes_its_slot_key_and_refreshes():
    select, coordinator = _select(_links())

    await select.async_select_option("Front")

    coordinator.client.async_set_vth_camera_link.assert_awaited_once_with("Vto00", "Ipc32")
    coordinator.async_refresh.assert_awaited_once()


async def test_choosing_none_writes_an_empty_link():
    select, coordinator = _select(_links("Ipc32"))

    await select.async_select_option(NO_CAMERA)

    coordinator.client.async_set_vth_camera_link.assert_awaited_once_with("Vto00", "")


async def test_an_option_that_is_not_offered_writes_nothing():
    select, coordinator = _select(_links())

    await select.async_select_option("Back")

    coordinator.client.async_set_vth_camera_link.assert_not_awaited()


async def test_a_write_the_vth_ignored_says_so_after_refreshing():
    """Refreshed first, so the select shows what the VTH really has while the user
    reads why."""
    select, coordinator = _select(_links(), landed=False)

    with pytest.raises(HomeAssistantError) as err:
        await select.async_select_option("Front")

    coordinator.async_refresh.assert_awaited_once()
    assert err.value.translation_key == "vth_camera_link_ignored"
    assert err.value.translation_placeholders == {
        "device": "Hall VTH", "vto": "Main VTO", "option": "Front"}


# --- which selects an indoor monitor gets ---------------------------------------

async def test_an_indoor_monitor_gets_one_select_per_vto(monkeypatch):
    from custom_components.dahua import select as select_module

    links = {"vtos": {"Vto00": {"name": "Main VTO", "link": ""},
                      "Vto01": {"name": "Gate VTO", "link": ""}},
             "cameras": {"Ipc32": "Front"}}
    made = []
    monkeypatch.setattr(select_module, "DahuaVthCameraLinkSelect",
                        lambda coordinator, entry, vto: made.append(vto))
    coordinator = _setup_double(links)
    monkeypatch.setattr(select_module, "entry_coordinators", lambda entry: {0: coordinator})

    await select_module.async_setup_entry(None, object(), lambda devices, **kw: None)

    assert made == ["Vto00", "Vto01"]


async def test_anything_else_gets_none(monkeypatch):
    from custom_components.dahua import select as select_module

    made = []
    monkeypatch.setattr(select_module, "DahuaVthCameraLinkSelect",
                        lambda coordinator, entry, vto: made.append(vto))
    coordinator = _setup_double(_links(), indoor_monitor=False)
    monkeypatch.setattr(select_module, "entry_coordinators", lambda entry: {0: coordinator})

    await select_module.async_setup_entry(None, object(), lambda devices, **kw: None)

    assert made == []


def _setup_double(links, indoor_monitor=True):
    """Just enough of a coordinator for select.async_setup_entry to walk past the
    other selects without creating any."""
    async def no_presets(*args):
        # Answered, and empty: the camera has told us it holds no presets, so no
        # preset select is made and the test is only about the VTO selects.
        return {}

    return SimpleNamespace(
        is_amcrest_doorbell=lambda: False,
        supports_security_light=lambda: False,
        get_model=lambda: "VTH2421F-P",
        supports_day_night_color=lambda: False,
        supports_infrared_light=lambda: False,
        is_indoor_monitor=lambda: indoor_monitor,
        is_indoor_monitor_without_video=lambda: indoor_monitor,
        get_vth_camera_links=lambda: links,
        subentry_id=None,
        client=SimpleNamespace(async_get_ptz_presets=no_presets),
        get_channel_number=lambda: 1,
        supports_ptz_position=lambda: False,
    )


# --- the poll asks only when something will read the answer ---------------------

LINKS = "async_get_vth_camera_links"


async def _poll_calls(device_class, **options):
    from tests.dahua.test_poll_skips_unused import _coordinator as poll_coordinator

    c = poll_coordinator(**options)
    c._device_class = device_class
    await c._async_update_data()
    return c.client.calls


async def test_an_indoor_monitor_reads_its_links_every_poll():
    assert LINKS in await _poll_calls("VTH")


async def test_not_with_the_select_platform_switched_off():
    assert LINKS not in await _poll_calls("VTH", select=False)


@pytest.mark.parametrize("device_class", ["VTO", "IPC", ""])
async def test_never_for_anything_else(device_class):
    assert LINKS not in await _poll_calls(device_class)
