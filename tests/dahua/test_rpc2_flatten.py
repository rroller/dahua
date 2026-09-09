"""RPC2 answers in nested JSON; every accessor in this integration reads CGI's
flat keys. The translation has to be exact or nothing downstream works.

Verified against a live DHI-NVR5464-16P-EI across seven configs and 4,647 keys:
same keys, same values, no differences.
"""

from custom_components.dahua.client import flatten_rpc2_config


def test_nested_objects_become_dotted_keys():
    out = flatten_rpc2_config("General", {"MachineName": "Cam", "LocalNo": 8})

    assert out == {"table.General.MachineName": "Cam", "table.General.LocalNo": "8"}


def test_lists_become_indexed_keys():
    out = flatten_rpc2_config("DisableLinkage", [{"Enable": True}, {"Enable": False}])

    assert out == {
        "table.DisableLinkage[0].Enable": "true",
        "table.DisableLinkage[1].Enable": "false",
    }


def test_nested_lists_keep_every_index():
    """Lighting_V2 is three deep and the profile index carries the meaning."""
    out = flatten_rpc2_config("Lighting_V2", [[[{"Mode": "Manual"}]]])

    assert out == {"table.Lighting_V2[0][0][0].Mode": "Manual"}


def test_booleans_use_the_wire_spelling():
    """Accessors compare against the string the CGI API returns."""
    out = flatten_rpc2_config("X", {"On": True, "Off": False})

    assert out == {"table.X.On": "true", "table.X.Off": "false"}


def test_numbers_become_strings():
    """Everything downstream does string comparisons on these."""
    assert flatten_rpc2_config("X", {"N": 3}) == {"table.X.N": "3"}


# --- nulls, which is where this could quietly break something ---------------

def test_nulls_are_dropped_not_rendered():
    out = flatten_rpc2_config("X", {"Present": "yes", "Absent": None})

    assert out == {"table.X.Present": "yes"}
    assert "table.X.Absent" not in out


def test_a_sparse_table_stays_sparse():
    """CGI omits an absent row; RPC2 sends it as null.

    Keeping those would give every channel a SmartMotionDetect row -- the
    phantom switch that #635 removed, straight back again.
    """
    out = flatten_rpc2_config("SmartMotionDetect", [None, {"Enable": True}, None])

    assert out == {"table.SmartMotionDetect[1].Enable": "true"}
    assert not any(k.startswith("table.SmartMotionDetect[0]") for k in out)
    assert not any(k.startswith("table.SmartMotionDetect[2]") for k in out)


def test_an_empty_table_gives_nothing():
    assert flatten_rpc2_config("X", None) == {}
    assert flatten_rpc2_config("X", {}) == {}


def test_a_real_shape_round_trips():
    """The shape actually returned for a channel's motion detection."""
    out = flatten_rpc2_config("MotionDetect", [
        {"Enable": True, "EventHandler": {"Dejitter": 5, "PtzLink": None}},
    ])

    assert out == {
        "table.MotionDetect[0].Enable": "true",
        "table.MotionDetect[0].EventHandler.Dejitter": "5",
    }
