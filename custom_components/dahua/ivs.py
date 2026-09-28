"""Identify normal IVS rules independently of their current array positions."""

import re


def ivs_rules_for_channel(table: dict, channel: int, name: str = "VideoAnalyseRule") -> list[dict]:
    """Return complete normal rules with unambiguous Dahua IDs on this channel."""
    rules = []
    ids = {}
    for key, value in table.items():
        match = re.fullmatch(rf"table.{name}\[{channel}\]\[(\d+)\]\.Id", key)
        if not match or value is None or str(value).strip() == "":
            continue
        rule_id = str(value)
        ids[rule_id] = ids.get(rule_id, 0) + 1
        prefix = key[:-3]
        if table.get(prefix + ".Class") != "Normal":
            continue
        if table.get(prefix + ".Enable") not in ("true", "false"):
            continue
        rules.append({
            "channel": channel,
            "index": int(match[1]),
            "id": rule_id,
            "name": table.get(prefix + ".Name") or f"IVS Rule {rule_id}",
        })
    return sorted((rule for rule in rules if ids[rule["id"]] == 1), key=lambda rule: rule["index"])


def ivs_rule_index(table: dict, channel: int, rule_id: str, name: str = "VideoAnalyseRule") -> int | None:
    """Resolve an ID in the current table, never falling back to an old index."""
    for rule in ivs_rules_for_channel(table, channel, name):
        if rule["id"] == str(rule_id):
            return rule["index"]
    return None
