"""Smart motion detection is per channel, but the probe for it is not.

The probe fetches the whole host-wide SmartMotionDetect table with no channel
argument, so it succeeds for every channel of an NVR regardless of whether that
channel can do anything. The table is sparse -- only channels that support it
get a row -- so the row is the real signal.

Measured on a DHI-NVR5464-16P-EI: ten channels had cameras, two had rows.
Disabling one left its row in place reading "false", so the row tracks support
and not state. Writing a row that does not exist returned 200 and changed
nothing.
"""

from custom_components.dahua import DahuaDataUpdateCoordinator


def _coordinator(channel, rows, probe_ok=True, model=""):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c._supports_smart_motion_detection = probe_ok
    c.model = model
    c.data = {
        "table.SmartMotionDetect[{0}].Enable".format(idx): value
        for idx, value in rows.items()
    }
    return c


# --- the capability follows the row -----------------------------------------

def test_a_channel_with_a_row_supports_it():
    assert _coordinator(1, {1: "true"}).supports_smart_motion_detection()


def test_a_channel_with_a_row_supports_it_even_when_switched_off():
    """The row persists reading false, which is how we know it means support."""
    assert _coordinator(1, {1: "false"}).supports_smart_motion_detection()


def test_a_channel_with_no_row_does_not_support_it():
    """This is the phantom switch: permanently off, writes silently ignored."""
    assert not _coordinator(2, {1: "true", 11: "true"}).supports_smart_motion_detection()


def test_the_measured_nvr_supports_exactly_the_channels_with_rows():
    rows = {1: "true", 11: "true"}          # what the real NVR reports
    supported = [ch for ch in range(10) if _coordinator(ch, rows).supports_smart_motion_detection()]

    assert supported == [1], "expected only the one configured channel of ten"


# --- the single camera case, which must not regress -------------------------

def test_a_single_camera_reporting_only_row_zero_still_supports_it():
    """A lone camera reports one row and that row is row 0."""
    assert _coordinator(0, {0: "true"}).supports_smart_motion_detection()


def test_a_camera_whose_channel_does_not_match_row_zero_still_supports_it():
    """The pre-existing fallback: entry channel and table row need not agree."""
    assert _coordinator(3, {0: "true"}).supports_smart_motion_detection()


# --- the probe still has a veto ---------------------------------------------

def test_a_device_without_the_api_at_all_supports_nothing():
    assert not _coordinator(0, {0: "true"}, probe_ok=False).supports_smart_motion_detection()


# --- state and capability must agree ----------------------------------------

def test_state_reads_the_same_row_the_capability_used():
    on = _coordinator(1, {1: "true"})
    off = _coordinator(1, {1: "false"})

    assert on.supports_smart_motion_detection() and on.is_smart_motion_detection_enabled()
    assert off.supports_smart_motion_detection() and not off.is_smart_motion_detection_enabled()


def test_an_unsupported_channel_never_reports_itself_enabled():
    c = _coordinator(2, {1: "true"})

    assert not c.supports_smart_motion_detection()
    assert not c.is_smart_motion_detection_enabled(), "it was reading another channel's row"


def test_a_missing_row_does_not_raise():
    """The old reader defaulted to '' -- None must not reach .lower()."""
    assert _coordinator(4, {}).is_smart_motion_detection_enabled() is False


# --- amcrest is a separate path and must be untouched ------------------------

def test_amcrest_state_ignores_the_smart_motion_table():
    c = _coordinator(0, {}, model="AD410")
    c.data["table.VideoAnalyseRule[0][0].Enable"] = "true"

    assert c.is_smart_motion_detection_enabled(), "amcrest reads its own rule table"
