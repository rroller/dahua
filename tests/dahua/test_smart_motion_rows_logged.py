"""Which SmartMotionDetect rows a device reported, so it can be said out loud.

#635 made the presence of this channel's row the whole capability decision for
the smart motion switch. Nothing logged which rows the device actually returned,
and the integration never logs a response body at debug -- so when the answer
surprises someone there is no way to see why.

#669 is what that costs. Five channels lost their switch, the reporter had the
feature configured on every one of them, and two rounds went into inferring the
shape of a table that could simply have been printed.

The helper is deliberately total: an empty tuple for anything it cannot read,
never an exception, because a diagnostic that can take setup down is worse than
no diagnostic at all.
"""

from custom_components.dahua import smart_motion_row_indices


# --- the tables that have been measured --------------------------------------

def test_the_measured_nvr_reports_two_rows():
    """DHI-NVR5464-16P-EI: rows for channels 1 and 11 only, and no row 0."""
    table = {
        "table.SmartMotionDetect[1].Enable": "true",
        "table.SmartMotionDetect[1].Sensitivity": "Middle",
        "table.SmartMotionDetect[11].Enable": "true",
        "table.SmartMotionDetect[11].ObjectTypes.Human": "true",
    }

    assert smart_motion_row_indices(table) == (1, 11)


def test_a_single_camera_reports_row_zero():
    assert smart_motion_row_indices(
        {"table.SmartMotionDetect[0].Enable": "true"}) == (0,)


def test_every_row_is_reported_once_however_many_fields_it_has():
    table = {"table.SmartMotionDetect[4].Enable": "true",
             "table.SmartMotionDetect[4].Sensitivity": "Middle",
             "table.SmartMotionDetect[4].ObjectTypes.Human": "true",
             "table.SmartMotionDetect[5].Enable": "false"}

    assert smart_motion_row_indices(table) == (4, 5)


def test_the_indices_come_back_in_order():
    table = {"table.SmartMotionDetect[%d].Enable" % n: "true"
             for n in (11, 2, 7, 0)}

    assert smart_motion_row_indices(table) == (0, 2, 7, 11)


# --- it must never be the thing that breaks setup ----------------------------

def test_an_empty_table_is_no_rows():
    assert smart_motion_row_indices({}) == ()


def test_something_that_is_not_a_table_is_no_rows():
    assert smart_motion_row_indices(None) == ()
    assert smart_motion_row_indices("table.SmartMotionDetect[0].Enable=true") == ()
    assert smart_motion_row_indices(["table.SmartMotionDetect[0].Enable"]) == ()


def test_keys_from_other_tables_are_ignored():
    table = {"table.MotionDetect[3].Enable": "true",
             "table.SmartMotionDetect[3].Enable": "true",
             "table.Lighting_V2[3][0][1].Mode": "Manual"}

    assert smart_motion_row_indices(table) == (3,)


def test_a_malformed_row_index_is_skipped_rather_than_raising():
    table = {"table.SmartMotionDetect[].Enable": "true",
             "table.SmartMotionDetect[x].Enable": "true",
             "table.SmartMotionDetect[2].Enable": "true"}

    assert smart_motion_row_indices(table) == (2,)
