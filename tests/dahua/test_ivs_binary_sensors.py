"""Normal IVS rules, including StayDetection, have independent event state."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.dahua.binary_sensor import (
    DahuaIVSRuleBinarySensor,
    async_setup_entry,
)
from custom_components.dahua.const import DOMAIN
from custom_components.dahua.entity import DahuaBaseEntity
from custom_components.dahua.ivs import ivs_rules_for_channel
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


def _coordinator():
    table = _rules()
    c = coordinator(table)
    c._channel = 0
    c._ivs_rules = ivs_rules_for_channel(table, 0)
    c._dahua_event_timestamp = {}
    c._dahua_event_listeners = {}
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

    def init(self, coord, entry):
        self._coordinator = coord
        self.coordinator = coord

    with patch.object(DahuaBaseEntity, "__init__", init):
        await async_setup_entry(
            SimpleNamespace(data={DOMAIN: {"entry": c}}),
            SimpleNamespace(entry_id="entry"),
            added.extend,
        )

    sensors = [s for s in added if isinstance(s, DahuaIVSRuleBinarySensor)]
    assert len(sensors) == 7
    assert len({s.unique_id for s in sensors}) == 7
    stay = next(s for s in sensors if s.extra_state_attributes["rule_id"] == "23")
    assert stay.name == "Garden Stay Test"
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
