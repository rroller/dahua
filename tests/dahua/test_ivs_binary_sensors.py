"""Normal IVS rules, including StayDetection, have independent event state."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.dahua.binary_sensor import (
    DahuaIVSRuleBinarySensor,
    async_setup_entry,
)
from custom_components.dahua.const import DOMAIN
from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.diagnostics import _ivs_block
from custom_components.dahua.entity import DahuaBaseEntity
from custom_components.dahua.ivs import ivs_rules_for_channel, ivs_discovery_diagnostics
from tests.dahua.test_ivs_rules import coordinator, row


def _rules():
    table = {}
    for index, rule_id, name, kind in (
        (3, "8", "Rule1", "CrossRegionDetection"),
        (4, "9", "Rule2", "CrossRegionDetection"),
        (5, "5", "Rule3", "CrossLineDetection"),
        (6, "17", "Rule4", "CrossLineDetection"),
        (7, "20", "Rule5", "CrossLineDetection"),
        (8, "22", "Rule6", "CrossLineDetection"),
        (9, "23", "Stay Test", "StayDetection"),
    ):
        table.update(row(index, rule_id, channel=0, name=name))
        table[f"table.VideoAnalyseRule[0][{index}].Type"] = kind
    return table


def _coordinator(table=None):
    table = _rules() if table is None else table
    c = coordinator(table)
    c._channel = 0
    c._ivs_rules = ivs_rules_for_channel(table, 0)
    c._dahua_event_timestamp = {}
    c._dahua_event_details = {}
    c._dahua_event_listeners = {}
    c._storage_disks = []
    return c


def _sensor(c, rule):
    def init(self, coord, entry):
        self._coordinator = coord
        self.coordinator = coord

    with patch.object(DahuaBaseEntity, "__init__", init):
        return DahuaIVSRuleBinarySensor(c, None, rule)


@pytest.mark.asyncio
async def test_setup_creates_all_seven_normal_rules_including_stay():
    c = _coordinator()
    c.get_event_list = lambda: []
    c.is_doorbell = lambda: False
    added = []

    def add_devices(entities, **_kwargs):
        added.extend(entities)

    def init(self, coord, entry):
        self._coordinator = coord
        self.coordinator = coord

    with patch.object(DahuaBaseEntity, "__init__", init):
        await async_setup_entry(
            SimpleNamespace(data={DOMAIN: {"entry": c}}),
            SimpleNamespace(entry_id="entry", runtime_data={0: c}),
            add_devices,
        )

    sensors = [s for s in added if isinstance(s, DahuaIVSRuleBinarySensor)]
    assert len(sensors) == 7
    assert len({s.unique_id for s in sensors}) == 7
    stay = next(s for s in sensors if s.extra_state_attributes["rule_id"] == "23")
    assert stay.name == "Stay Test"
    assert stay.unique_id == "SERIAL_ivs_rule_23"
    assert stay.extra_state_attributes["rule_type"] == "StayDetection"


@pytest.mark.parametrize("key", ["RuleId", "RuleID"])
def test_stay_rule_event_only_changes_its_own_sensor(key):
    c = _coordinator()
    sensors = {rule["id"]: _sensor(c, rule) for rule in c.get_ivs_rules()}
    fired = []
    for sensor in sensors.values():
        c.add_dahua_event_listener(
            sensor._event_name, lambda s=sensor: fired.append(s._rule_id)
        )

    event = {"Code": "StayDetection", "data": {"Class": "Normal", key: 23}}
    c._dispatch_event(event, "Start")
    assert sensors["23"].is_on
    assert fired == ["23"]
    assert all(not s.is_on for rule_id, s in sensors.items() if rule_id != "23")

    c._dispatch_event(event, "Stop")
    assert not sensors["23"].is_on
    assert fired == ["23", "23"]


def test_crossline_and_crossregion_keep_their_own_rules_and_smart_motion():
    c = _coordinator()
    fired = []
    for code in (
        "CrossLineDetection",
        "CrossRegionDetection",
        "SmartMotionHuman",
        "IVSRule_5",
        "IVSRule_8",
    ):
        c.add_dahua_event_listener(code, lambda code=code: fired.append(code))

    for code, rule_id in (("CrossLineDetection", 5), ("CrossRegionDetection", 8)):
        c._dispatch_event(
            {
                "Code": code,
                "data": {
                    "Class": "Normal",
                    "RuleId": rule_id,
                    "Object": {"ObjectType": "Human"},
                },
            },
            "Start",
        )

    assert fired == [
        "CrossLineDetection",
        "SmartMotionHuman",
        "IVSRule_5",
        "CrossRegionDetection",
        "SmartMotionHuman",
        "IVSRule_8",
    ]


def test_crossline_stop_clears_all_same_code_rules_on_channel():
    c = _coordinator()
    sensors = {rule["id"]: _sensor(c, rule) for rule in c.get_ivs_rules()}
    for sensor in sensors.values():
        c.add_dahua_event_listener(sensor._event_name, lambda: None)

    for rule_id in (5, 17, 20):
        c._dispatch_event(
            {
                "Code": "CrossLineDetection",
                "data": {
                    "Class": "Normal",
                    "RuleID": rule_id,
                    "Object": {"ObjectID": 129, "ObjectType": "Human"},
                },
            },
            "Start",
        )

    assert sensors["5"].is_on
    assert sensors["17"].is_on
    assert sensors["20"].is_on

    c._dispatch_event(
        {
            "Code": "CrossLineDetection",
            "data": {
                "Class": "Normal",
                "RuleID": 20,
                "Object": {"ObjectID": 129, "ObjectType": "Human"},
            },
        },
        "Stop",
    )

    assert not sensors["5"].is_on
    assert not sensors["17"].is_on
    assert not sensors["20"].is_on


def test_rule_sensors_coexist_with_latest_event_details():
    """#849 independent states survive #974 keeping the latest rule attributes."""
    c = _coordinator()
    sensors = {rule["id"]: _sensor(c, rule) for rule in c.get_ivs_rules()}
    c.add_dahua_event_listener("CrossLineDetection", lambda: None)
    for sensor in sensors.values():
        c.add_dahua_event_listener(sensor._event_name, lambda: None)

    for rule_id, name in ((5, "Rule3"), (17, "Rule4")):
        c._dispatch_event(
            {
                "Code": "CrossLineDetection",
                "data": {
                    "Class": "Normal",
                    "RuleID": rule_id,
                    "Name": name,
                    "Direction": "LeftToRight",
                    "Object": {"ObjectType": "Human"},
                },
            },
            "Start",
        )

    assert sensors["5"].is_on and sensors["17"].is_on
    assert c.get_event_details("CrossLineDetection") == {
        "rule_name": "Rule4",
        "rule_id": 17,
        "direction": "LeftToRight",
        "object_type": "Human",
    }

    c._dispatch_event({"Code": "CrossLineDetection"}, "Stop")
    assert not sensors["5"].is_on and not sensors["17"].is_on
    assert c.get_event_details("CrossLineDetection")["rule_name"] == "Rule4"


def test_crossregion_stop_clears_crossregion_but_not_crossline():
    c = _coordinator()
    sensors = {rule["id"]: _sensor(c, rule) for rule in c.get_ivs_rules()}
    for sensor in sensors.values():
        c.add_dahua_event_listener(sensor._event_name, lambda: None)

    for rule_id in (8, 9):
        c._dispatch_event(
            {
                "Code": "CrossRegionDetection",
                "data": {"Class": "Normal", "RuleID": rule_id},
            },
            "Start",
        )

    c._dispatch_event(
        {
            "Code": "CrossLineDetection",
            "data": {"Class": "Normal", "RuleID": 17},
        },
        "Start",
    )

    c._dispatch_event(
        {
            "Code": "CrossRegionDetection",
            "data": {"Class": "Normal", "RuleID": 8},
        },
        "Stop",
    )

    assert not sensors["8"].is_on
    assert not sensors["9"].is_on
    assert sensors["17"].is_on


def test_same_code_stop_does_not_require_object_id():
    c = _coordinator()
    sensors = {rule["id"]: _sensor(c, rule) for rule in c.get_ivs_rules()}
    for sensor in sensors.values():
        c.add_dahua_event_listener(sensor._event_name, lambda: None)

    for rule_id in (17, 20):
        c._dispatch_event(
            {
                "Code": "CrossLineDetection",
                "data": {"Class": "Normal", "RuleID": rule_id},
            },
            "Start",
        )

    c._dispatch_event(
        {
            "Code": "CrossLineDetection",
            "data": {"Class": "Normal", "RuleID": 17},
        },
        "Stop",
    )

    assert not sensors["17"].is_on
    assert not sensors["20"].is_on


def test_stay_stop_clears_all_same_code_rules_if_multiple_exist():
    table = _rules()
    table.update(row(10, "24", channel=0, name="Stay Two"))
    table["table.VideoAnalyseRule[0][10].Type"] = "StayDetection"
    c = _coordinator(table)
    sensors = {
        rule["id"]: _sensor(c, rule)
        for rule in c.get_ivs_rules()
        if rule["type"] == "StayDetection"
    }
    for sensor in sensors.values():
        c.add_dahua_event_listener(sensor._event_name, lambda: None)

    for rule_id in (23, 24):
        c._dispatch_event(
            {"Code": "StayDetection", "data": {"Class": "Normal", "RuleID": rule_id}},
            "Start",
        )
    assert all(sensor.is_on for sensor in sensors.values())

    c._dispatch_event(
        {"Code": "StayDetection", "data": {"Class": "Normal", "RuleID": 23}},
        "Stop",
    )
    assert all(not sensor.is_on for sensor in sensors.values())


@pytest.mark.parametrize("stop_data", [None, "truncated"])
def test_stop_without_normal_data_clears_same_code_rules(stop_data):
    c = _coordinator()
    sensors = {
        rule["id"]: _sensor(c, rule)
        for rule in c.get_ivs_rules()
        if rule["id"] in ("17", "20")
    }
    for sensor in sensors.values():
        c.add_dahua_event_listener(sensor._event_name, lambda: None)

    for rule_id in (17, 20):
        c._dispatch_event(
            {
                "Code": "CrossLineDetection",
                "data": {"Class": "Normal", "RuleID": rule_id},
            },
            "Start",
        )
    assert all(sensor.is_on for sensor in sensors.values())

    stop = {"Code": "CrossLineDetection"}
    if stop_data is not None:
        stop["data"] = stop_data
    c._dispatch_event(stop, "Stop")
    assert all(not sensor.is_on for sensor in sensors.values())


def test_unknown_non_normal_and_malformed_events_do_not_change_rule_state():
    c = _coordinator()
    fired = []
    c.add_dahua_event_listener("IVSRule_23", lambda: fired.append(1))
    for data in (
        {"Class": "FaceDetection", "RuleId": 23},
        {"Class": "Normal", "RuleID": 99},
        {"Class": "Normal"},
        "truncated",
    ):
        c._dispatch_event({"Code": "StayDetection", "data": data}, "Start")
    assert not fired
    assert c.get_event_timestamp("IVSRule_23") == 0


def _nvr(channel):
    c = _coordinator()
    c._channel = channel
    c._serial_number = "NVR"
    c.get_serial_number = lambda: DahuaDataUpdateCoordinator.get_serial_number(c)
    table = {
        key.replace("VideoAnalyseRule[0]", f"RemoteVideoAnalyseRule[{channel}]"): value
        for key, value in _rules().items()
    }
    table[f"table.RemoteVideoAnalyseRule[{channel}][9].Id"] = "0"
    c._ivs_rules = ivs_rules_for_channel(table, channel, "RemoteVideoAnalyseRule")
    c._ivs_discovery_diagnostics = ivs_discovery_diagnostics(
        table, channel, "RemoteVideoAnalyseRule"
    )
    for rule in c._ivs_rules:
        rule["remote"] = True
    c.get_event_list = lambda: []
    c.is_doorbell = lambda: False
    return c


async def test_nvr_setup_adds_rules_for_every_channel_with_distinct_identities():
    channels = {channel: _nvr(channel) for channel in (0, 9)}
    added = []

    def add_devices(entities, **_kwargs):
        added.extend(entities)

    def init(self, coord, entry):
        self._coordinator = self.coordinator = coord

    with patch.object(DahuaBaseEntity, "__init__", init):
        await async_setup_entry(
            None, SimpleNamespace(runtime_data=channels), add_devices
        )
    sensors = [s for s in added if isinstance(s, DahuaIVSRuleBinarySensor)]
    assert len(sensors) == 14
    assert len({s.unique_id for s in sensors}) == 14
    assert {s.unique_id for s in sensors if s._rule_id == "0"} == {
        "NVR_ivs_rule_0",
        "NVR_9_ivs_rule_0",
    }


@pytest.mark.parametrize("key", ["RuleId", "RuleID"])
@pytest.mark.parametrize("action", ["Start", "Stop", "Pulse"])
def test_nvr_wire_event_routes_id_zero_only_to_its_channel(key, action):
    import json
    import time

    channels = [_nvr(channel) for channel in (0, 9)]
    sensors = []
    for c in channels:
        sensor = _sensor(c, next(r for r in c.get_ivs_rules() if r["id"] == "0"))
        sensors.append(sensor)
        c.add_dahua_event_listener(sensor._event_name, lambda: None)
        c.handle_event = lambda event, coord=c: coord._dispatch_event(
            event, event["action"]
        )
        if action == "Stop":
            c._dispatch_event(
                {"Code": "StayDetection", "data": {"Class": "Normal", key: 0}}, "Start"
            )
    payload = json.dumps({"Class": "Normal", key: 0})
    wire = f"Code=StayDetection;action={action};index=9;data={payload}\r\n".encode()
    for c in channels:
        c.on_receive(wire, 9)
    assert sensors[0].is_on == (action == "Stop")
    assert sensors[1].is_on == (action != "Stop")
    if action == "Pulse":
        assert channels[1].event_is_momentary("IVSRule_0")
        channels[1]._dahua_event_timestamp[channels[1].get_event_key("IVSRule_0")] = (
            int(time.time()) - 6
        )
        assert not sensors[1].is_on


def test_reordering_remote_rules_keeps_identity_and_event_target():
    c = _nvr(9)
    rule = next(r for r in c.get_ivs_rules() if r["id"] == "0")
    sensor = _sensor(c, rule)
    c._ivs_rules = [
        {**r, "index": r["index"] + 20, "name": "Renamed"} for r in c.get_ivs_rules()
    ]
    replacement = _sensor(c, next(r for r in c.get_ivs_rules() if r["id"] == "0"))
    assert replacement.unique_id == sensor.unique_id
    c.add_dahua_event_listener(sensor._event_name, lambda: None)
    c._dispatch_event(
        {"Code": "StayDetection", "Data": {"Class": "Normal", "RuleID": "0"}}, "Start"
    )
    assert sensor.is_on
    assert replacement.is_on


def test_discovery_explains_missing_duplicate_and_invalid_rows():
    table = {
        **row(0, "1", channel=9),
        **row(1, "1", channel=9),
        **row(2, "2", channel=9),
        **row(3, "3", "bad", channel=9),
        **row(4, "0", channel=9),
    }
    del table["table.VideoAnalyseRule[9][2].Id"]
    result = ivs_discovery_diagnostics(table, 9, "VideoAnalyseRule")
    assert result["discovered_count"] == 1
    assert result["skipped"] == [
        {"index": 0, "reason": "duplicate_id"},
        {"index": 1, "reason": "duplicate_id"},
        {"index": 2, "reason": "missing_id"},
        {"index": 3, "reason": "invalid_enable"},
    ]


def test_unmatched_diagnostics_are_bounded_and_do_not_guess(caplog):
    import logging

    c = _nvr(9)
    c.add_dahua_event_listener(
        "IVSRule_0", lambda: pytest.fail("Must not guess by index")
    )
    with caplog.at_level(logging.DEBUG, logger="custom_components.dahua"):
        for _ in range(100):
            c._dispatch_event(
                {
                    "Code": "StayDetection",
                    "data": {"Class": "Normal", "RuleID": 999, "Name": "Stay Test"},
                },
                "Start",
            )
        c._dispatch_event(
            {"Code": "StayDetection", "data": {"Class": "Normal"}}, "Start"
        )
    block = _ivs_block(SimpleNamespace(runtime_data={9: c, 0: _nvr(0)}))
    assert block["9"]["unmatched_event_counts"] == {
        "unknown_rule_id": 100,
        "missing_rule_id": 1,
    }
    assert block["0"]["unmatched_event_counts"] == {}
    assert block["9"]["discovery"]["source"] == "RemoteVideoAnalyseRule"
    assert "Name" not in block["9"]["last_unmatched_event"]
    assert len([r for r in caplog.records if "did not match" in r.message]) == 2
    assert c.get_event_timestamp("IVSRule_0") == 0


def test_stop_clears_rules_lit_by_start_even_if_configured_type_differs():
    """The Stop must not depend on the configured Type matching the event Code."""
    table = _rules()
    table.update(row(10, "30", channel=0, name="No Type"))  # no .Type in the config
    c = _coordinator(table)
    sensors = {rule["id"]: _sensor(c, rule) for rule in c.get_ivs_rules()}
    for sensor in sensors.values():
        c.add_dahua_event_listener(sensor._event_name, lambda: None)

    for rule_id in (23, 30):
        c._dispatch_event(
            {"Code": "StayDetection", "data": {"Class": "Normal", "RuleID": rule_id}},
            "Start",
        )
    assert sensors["23"].is_on and sensors["30"].is_on

    c._dispatch_event({"Code": "StayDetection"}, "Stop")
    assert not sensors["23"].is_on
    assert not sensors["30"].is_on


def test_stop_tracking_is_consumed_and_a_later_cycle_works():
    c = _coordinator()
    sensors = {rule["id"]: _sensor(c, rule) for rule in c.get_ivs_rules()}
    for sensor in sensors.values():
        c.add_dahua_event_listener(sensor._event_name, lambda: None)
    start = {"Code": "CrossLineDetection", "data": {"Class": "Normal", "RuleID": 5}}
    stop = {"Code": "CrossLineDetection"}

    c._dispatch_event(start, "Start")
    c._dispatch_event(stop, "Stop")
    assert not sensors["5"].is_on
    assert c._ivs_active_rules == {}

    # A second Stop with nothing lit changes nothing, and the next Start still lights it.
    c._dispatch_event(stop, "Stop")
    c._dispatch_event(start, "Start")
    assert sensors["5"].is_on
    c._dispatch_event(stop, "Stop")
    assert not sensors["5"].is_on


def test_rule_sensor_keeps_base_attributes_and_event_details():
    """Per-rule attributes sit on top of the base's and the latest event's.

    A fresh dict here once dropped `id` and `integration` from every per-rule
    sensor. The rule's own `rule_id` is the configured string and wins over the
    event's, which the device sends as a number.
    """
    c = _coordinator()
    c.data = {"id": "SERIAL"}
    sensors = {rule["id"]: _sensor(c, rule) for rule in c.get_ivs_rules()}
    for sensor in sensors.values():
        c.add_dahua_event_listener(sensor._event_name, lambda: None)

    c._dispatch_event(
        {
            "Code": "CrossLineDetection",
            "data": {
                "Class": "Normal",
                "RuleID": 5,
                "Name": "Rule3",
                "Direction": "LeftToRight",
                "Object": {"ObjectType": "Human"},
            },
        },
        "Start",
    )

    attributes = sensors["5"].extra_state_attributes
    assert attributes["id"] == "SERIAL"
    assert attributes["integration"] == DOMAIN
    assert attributes["rule_id"] == "5"
    assert attributes["rule_type"] == "CrossLineDetection"
    assert attributes["rule_name"] == "Rule3"
    assert attributes["direction"] == "LeftToRight"
    assert attributes["object_type"] == "Human"

    # A rule no event has named yet still reports the base's keys and its own.
    quiet = sensors["17"].extra_state_attributes
    assert quiet["id"] == "SERIAL"
    assert quiet["integration"] == DOMAIN
    assert quiet["rule_id"] == "17"
    assert "direction" not in quiet


def test_stop_clears_both_tracked_rules_and_rules_known_only_by_type():
    """A reload loses the tracking, so a rule lit before it is found by Type.

    Rule 5 was lit before the reload and is known only from its configured Type.
    Rule 17 was lit after it and is tracked. One Stop has to clear both.
    """
    c = _coordinator()
    sensors = {rule["id"]: _sensor(c, rule) for rule in c.get_ivs_rules()}
    for sensor in sensors.values():
        c.add_dahua_event_listener(sensor._event_name, lambda: None)

    # Lit before the reload: the sensor shows on, but nothing tracked it.
    c._dahua_event_timestamp[c.get_event_key("IVSRule_5")] = 1
    assert sensors["5"].is_on
    assert getattr(c, "_ivs_active_rules", {}) == {}

    c._dispatch_event(
        {"Code": "CrossLineDetection", "data": {"Class": "Normal", "RuleID": 17}},
        "Start",
    )
    assert c._ivs_active_rules == {"CrossLineDetection": {"17"}}
    assert sensors["5"].is_on and sensors["17"].is_on

    c._dispatch_event({"Code": "CrossLineDetection"}, "Stop")
    assert not sensors["5"].is_on
    assert not sensors["17"].is_on


def test_stop_naming_one_rule_does_not_overwrite_the_other_rules_details():
    """A batch Stop clears every rule of the code, but names only one of them.

    Rule3 saw a person and Rule4 saw a vehicle. The Stop carries Rule3's data, so
    it may refresh Rule3's attributes and must leave Rule4 showing its own.
    """
    c = _coordinator()
    c.data = {"id": "SERIAL"}
    sensors = {rule["id"]: _sensor(c, rule) for rule in c.get_ivs_rules()}
    c.add_dahua_event_listener("CrossLineDetection", lambda: None)
    for sensor in sensors.values():
        c.add_dahua_event_listener(sensor._event_name, lambda: None)

    for rule_id, name, direction, kind in (
        (5, "Rule3", "LeftToRight", "Human"),
        (17, "Rule4", "RightToLeft", "Vehicle"),
    ):
        c._dispatch_event(
            {
                "Code": "CrossLineDetection",
                "data": {
                    "Class": "Normal",
                    "RuleID": rule_id,
                    "Name": name,
                    "Direction": direction,
                    "Object": {"ObjectType": kind},
                },
            },
            "Start",
        )

    c._dispatch_event(
        {
            "Code": "CrossLineDetection",
            "data": {
                "Class": "Normal",
                "RuleID": 5,
                "Name": "Rule3",
                "Direction": "LeftToRight",
                "Object": {"ObjectType": "Human"},
            },
        },
        "Stop",
    )

    # Both are cleared, by the one Stop.
    assert not sensors["5"].is_on and not sensors["17"].is_on

    rule4 = sensors["17"].extra_state_attributes
    assert rule4["rule_name"] == "Rule4"
    assert rule4["direction"] == "RightToLeft"
    assert rule4["object_type"] == "Vehicle"
    assert rule4["rule_id"] == "17"

    # The rule the Stop names still takes its details from it.
    rule3 = sensors["5"].extra_state_attributes
    assert rule3["rule_name"] == "Rule3"
    assert rule3["object_type"] == "Human"
