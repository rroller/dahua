"""Normal IVS rules keep their identity when Dahua rearranges the table."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import DahuaClient, _cache_lifetime
from custom_components.dahua.const import DOMAIN
from custom_components.dahua.entity import DahuaBaseEntity
from custom_components.dahua.ivs import ivs_rule_index, ivs_rules_for_channel
from custom_components.dahua.switch import DahuaIVSRuleSwitch, async_setup_entry

from . import adds_entities


def row(index, rule_id, enabled="true", channel=2, kind="Normal", name="Line"):
    prefix = f"table.VideoAnalyseRule[{channel}][{index}]"
    return {
        prefix + "." + key: value
        for key, value in {
            "Id": rule_id,
            "Class": kind,
            "Name": name,
            "Enable": enabled,
        }.items()
    }


def coordinator(table):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.data = table
    c._channel = 0
    c.model = ""
    c._nvr_active_deterrence = False
    c._ivs_rules = ivs_rules_for_channel(table, 2)
    c.get_serial_number = lambda: "SERIAL"
    c.get_device_name = lambda: "Garden"
    c.async_refresh = AsyncMock()
    c.client = object.__new__(DahuaClient)
    c.client._request = AsyncMock(return_value=table)
    c.client.get = AsyncMock(return_value={})
    c.last_update_success = True
    return c


def entity(c, rule_id="42"):
    rule = next(rule for rule in c.get_ivs_rules() if rule["id"] == rule_id)

    def init(self, coord, entry):
        self._coordinator = coord
        self.coordinator = coord

    with patch.object(DahuaBaseEntity, "__init__", init):
        return DahuaIVSRuleSwitch(c, None, rule)


class TestIVSDiscovery(TestCase):
    def test_multiple_normal_rules_and_sparse_indexes(self):
        table = {
            **row(0, "42"),
            **row(17, "7", "false"),
            **row(2, "8", kind="FaceDetection"),
            **row(3, "42", channel=1),
        }
        rules = ivs_rules_for_channel(table, 2)
        self.assertEqual([(r["id"], r["index"]) for r in rules], [("42", 0), ("7", 17)])

    def test_incomplete_rows_are_not_exposed(self):
        for field in ("Id", "Class", "Enable"):
            table = row(0, "42")
            del table[f"table.VideoAnalyseRule[2][0].{field}"]
            self.assertEqual(ivs_rules_for_channel(table, 2), [])
        self.assertEqual(ivs_rules_for_channel(row(0, ""), 2), [])
        self.assertEqual(ivs_rules_for_channel(row(0, "42", "invalid"), 2), [])

    def test_duplicate_ids_are_not_resolved_even_across_classes(self):
        for kind in ("Normal", "FaceDetection"):
            table = {**row(0, "42"), **row(1, "42", kind=kind)}
            self.assertIsNone(ivs_rule_index(table, 2, "42"))

    def test_missing_name_uses_id_and_zero_id_is_valid(self):
        self.assertEqual(
            ivs_rules_for_channel(row(3, "0", name=""), 2)[0]["name"], "IVS Rule 0"
        )

    def test_state_follows_id_after_reorder_and_insertion(self):
        c = coordinator({**row(0, "42"), **row(1, "7", "false")})
        switch = entity(c)
        identity = switch.unique_id
        c.data = {
            **row(0, "7"),
            **row(1, "99"),
            **row(8, "42", "false", name="Renamed"),
        }
        self.assertFalse(switch.is_on)
        self.assertEqual(switch.unique_id, identity)
        replacement = entity(coordinator(c.data))
        self.assertEqual(replacement.unique_id, identity)

    def test_removed_rule_does_not_read_replacement_at_old_index(self):
        c = coordinator(row(0, "42"))
        switch = entity(c)
        c.data = row(0, "7")
        self.assertIsNone(switch.is_on)
        self.assertFalse(switch.available)

    def test_rules_with_same_name_have_distinct_ids(self):
        c = coordinator({**row(0, "42"), **row(1, "7")})
        self.assertNotEqual(entity(c, "42").unique_id, entity(c, "7").unique_id)

    def test_read_cache_expires_before_next_default_poll(self):
        self.assertEqual(
            _cache_lifetime(
                "/cgi-bin/configManager.cgi?action=getConfig&name=VideoAnalyseRule"
            ),
            5,
        )
        self.assertEqual(
            _cache_lifetime(
                "/cgi-bin/configManager.cgi?action=getConfig&name=VideoInOptions"
            ),
            300,
        )
        # The active day/night profile is live state, and every light command
        # addresses the Lighting row it names.
        self.assertEqual(
            _cache_lifetime(
                "/cgi-bin/configManager.cgi?action=getConfig&name=VideoInMode"
            ),
            5,
        )


class TestIVSActions:
    """Let pytest manage the event loop alongside Home Assistant's fixtures."""

    def __getattr__(self, name):
        return getattr(TestCase(), name)

    async def test_each_write_resolves_fresh_table_even_before_next_poll(self):
        c = coordinator({**row(0, "42"), **row(1, "7", "false")})
        switch = entity(c)
        c.client._request.side_effect = [
            {**row(0, "7"), **row(4, "42", "false")},
            {**row(0, "99"), **row(8, "42")},
        ]
        await switch.async_turn_on()
        c.client.get.assert_awaited_with(
            "/cgi-bin/configManager.cgi?action=setConfig&VideoAnalyseRule[2][4].Enable=true",
            True,
        )
        await switch.async_turn_off()
        c.client.get.assert_awaited_with(
            "/cgi-bin/configManager.cgi?action=setConfig&VideoAnalyseRule[2][8].Enable=false",
            True,
        )
        self.assertEqual(c.client._request.await_count, 2)
        self.assertEqual(c.async_refresh.await_count, 2)

    async def test_removed_ambiguous_or_non_normal_rule_never_writes(self):
        for table in (
            {},
            row(0, "7"),
            row(0, "42", channel=1),
            row(0, "42", kind="FaceDetection"),
            {**row(0, "42"), **row(1, "42")},
        ):
            c = coordinator(row(0, "42"))
            switch = entity(c)
            c.client._request.return_value = table
            with self.assertRaises(HomeAssistantError):
                await switch.async_turn_on()
            c.client.get.assert_not_awaited()

    async def test_failed_fresh_read_does_not_write(self):
        c = coordinator(row(0, "42"))
        c.client._request.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            await entity(c).async_turn_off()
        c.client.get.assert_not_awaited()

    async def test_nvr_switch_uses_remote_setter_and_remote_state(self):
        remote = {
            "table.RemoteVideoAnalyseRule[10][0].Id": "1",
            "table.RemoteVideoAnalyseRule[10][0].Class": "Normal",
            "table.RemoteVideoAnalyseRule[10][0].Name": "IVS-1",
            "table.RemoteVideoAnalyseRule[10][0].Enable": "true",
        }
        c = coordinator({})
        c._channel = 10
        c.model = "NVR"
        c._nvr_active_deterrence = False
        c.data = remote
        c._ivs_rules = ivs_rules_for_channel(remote, 10, "RemoteVideoAnalyseRule")
        c._ivs_rules[0]["remote"] = True
        c.client.async_set_remote_ivs_rule_by_id = AsyncMock()
        switch = entity(c, "1")
        self.assertTrue(switch.is_on)
        await switch.async_turn_off()
        c.client.async_set_remote_ivs_rule_by_id.assert_awaited_once_with(
            10, "1", False
        )
        c.client._request.assert_not_awaited()

    async def test_setup_adds_all_normal_rules_without_network_reads(self):
        c = coordinator({**row(0, "42"), **row(17, "7")})
        for method in (
            "is_nvr_channel",
            "supports_siren",
            "supports_smart_motion_detection",
            "supports_smart_motion_detection_amcrest",
            "supports_privacy_mode",
            "supports_alarm_output",
            "supports_disarming_linkage",
        ):
            setattr(c, method, lambda: False)
        hass = SimpleNamespace(data={})
        added = []

        def init(self, coord, entry):
            self._coordinator = coord
            self.coordinator = coord

        with patch.object(DahuaBaseEntity, "__init__", init):
            await async_setup_entry(
                hass,
                SimpleNamespace(
                    entry_id="entry",
                    runtime_data={0: c},
                    async_on_unload=lambda unsub: None,
                ),
                adds_entities(added),
            )
        rules = [s for s in added if isinstance(s, DahuaIVSRuleSwitch)]
        self.assertEqual(len(rules), 2)
        self.assertEqual(len({s.unique_id for s in rules}), 2)
        c.client._request.assert_not_awaited()

    async def test_poll_reads_current_ivs_table_only_when_switches_enabled(self):
        from tests.dahua.test_poll_skips_unused import _coordinator

        for enabled in (True, False):
            c = _coordinator(switch=enabled)
            c._ivs_rules = ivs_rules_for_channel(row(0, "42"), 2)
            # The strict reader, which is what the poll uses: the lenient
            # `async_get_ivs_rules` turns a refusal into {} and the held rows
            # were dropped with no exception for the handler to see.
            c.client.async_probe_ivs_rules = AsyncMock(
                return_value=row(5, "42", "false")
            )
            data = await c._async_update_data()
            self.assertEqual(c.client.async_probe_ivs_rules.await_count, int(enabled))
            if enabled:
                self.assertEqual(data["table.VideoAnalyseRule[2][5].Id"], "42")


async def test_direct_reads_and_fresh_writes_keep_cgi_when_rpc2_is_enabled():
    from tests.dahua.test_host_read_cache import _Probe, _client

    table = row(0, "42")
    probe = _Probe(
        hold=0, body="\n".join(f"{key}={value}" for key, value in table.items())
    )
    client = _client(probe)
    client._use_rpc2 = True
    client._rpc2_get_config = AsyncMock()
    client.async_set_ivs_rule = AsyncMock()

    assert await client.async_get_ivs_rules() == table
    assert await client.async_get_ivs_rules() == table
    assert probe.calls == 1
    await client.async_set_ivs_rule_by_id(2, "42", False)
    assert probe.calls == 2
    client.async_set_ivs_rule.assert_awaited_once_with(2, 0, False)
    client._rpc2_get_config.assert_not_awaited()


# --- a refused poll must not drop the rules --------------------------------
#
# The same shape @rroller found on #1201, and the comment in
# `_async_fetch_ivs_rules` used to say this branch was safe because
# `async_get_config` returns `{}` for a refusal. It does, and that is the bug:
# `{}` does not fail the refresh, which is what the handler was written to
# prevent, but it drops the held rows just as completely, so every IVS switch
# on a direct camera went unknown on one refused read.
#
# Driven through the real DahuaClient against a session that answers a status,
# because a stub that raises skips the conversion that caused this.


class _RefusedRead:
    def __init__(self, status):
        self.status = status
        self.headers = {}

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url="http://10.0.0.5/x"),
                history=(),
                status=self.status,
            )

    async def text(self):
        return ""

    async def read(self):
        return b""

    def close(self):
        return None


class _RefusingSession:
    def __init__(self, status):
        self.status = status
        self.calls = 0
        self.url = None

    async def request(self, method, url, headers=None, **kwargs):
        self.calls += 1
        self.url = url
        return _RefusedRead(self.status)


def _camera_coordinator(data, address):
    """A direct camera, which is the branch that reads over CGI."""
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.data = dict(data)
    coordinator._channel = 2
    coordinator._address = address
    coordinator.is_nvr_channel = lambda: False
    return coordinator


@pytest.mark.parametrize("status", [400, 403, 500])
async def test_a_refused_poll_keeps_the_rules_it_had(status):
    held = row(1, "7", enabled="true")
    session = _RefusingSession(status)
    coordinator = _camera_coordinator(held, "10.0.0.5")
    coordinator.client = DahuaClient("u", "p", "10.0.0.5", 80, 554, session)

    carried = await coordinator._async_fetch_ivs_rules()

    assert session.calls, "the real client was never reached"
    assert carried, "a refused read dropped the IVS rules"
    assert carried["table.VideoAnalyseRule[2][1].Enable"] == "true"


async def test_a_camera_with_no_rules_is_not_given_the_old_ones():
    """The control, and the reason this is a strict read rather than a check on
    the keys: a camera whose last rule was deleted answers an empty table, and
    carrying the previous rows would leave a switch for a rule that is gone."""
    session = _RefusingSession(200)
    coordinator = _camera_coordinator(row(1, "7"), "10.0.0.6")
    coordinator.client = DahuaClient("u", "p", "10.0.0.6", 80, 554, session)

    answered = await coordinator._async_fetch_ivs_rules()

    assert answered == {}, "an empty 200 was treated as a refusal"


async def test_the_poll_reads_the_rules_by_table_name():
    """A getConfig for a name the device does not have answers 200 and empty,
    so a misspelling here reads as a camera with no rules for ever."""
    session = _RefusingSession(200)
    coordinator = _camera_coordinator({}, "10.0.0.7")
    coordinator.client = DahuaClient("u", "p", "10.0.0.7", 80, 554, session)

    await coordinator._async_fetch_ivs_rules()

    assert "action=getConfig&name=VideoAnalyseRule" in session.url
