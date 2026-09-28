"""Which way the vehicle was going, from whatever field the camera uses.

#757. An ANPR camera reported plate, type, colour and brand, and `direction`
came out empty. The lookup asked for `Direction` and `DirectionName`, and this
camera reports neither usefully.

@synthtex captured the event driving each way past a DHI-ITC413-PW4D-IZ1. Four
fields look like a direction and only three of them move:

    DrivingDirection   ["Approach", ""]  ->  ["Leave", ""]
    JunctionDirection  "Obverse"         ->  "Reverse"
    VehicleDirection   "Head"            ->  "Tail"
    Direction          0                 ->  0            (never changes)

`DrivingDirection` is preferred because Approach and Leave say what happened.
Obverse and Head describe which end of the car was photographed, which needs
Dahua's vocabulary to interpret, and are kept as fallbacks for cameras that
report one without the other.

`Direction` and `DirectionName` stay ahead of all of them, so a camera that
already reports those is unaffected. On this camera `Direction` is the integer
0, which is falsy, so it falls through instead of reporting a meaningless zero.
That is luck rather than design, and the ordering makes it deliberate.
"""
from custom_components.dahua import dahua_utils


def _event(**traffic_car):
    """An event whose TrafficCar block carries the given fields."""
    return {
        "data": {
            "Object": {"ObjectType": "Plate", "Text": "AB12CDE"},
            "TrafficCar": dict(traffic_car),
        }
    }


def _top_level(**fields):
    """An event carrying the fields at the top of data, not inside TrafficCar."""
    data = {"Object": {"ObjectType": "Plate", "Text": "AB12CDE"}}
    data.update(fields)
    return {"data": data}


def _direction(event):
    result = dahua_utils.extract_plate_data(event)
    assert result is not None, "the plate itself must still parse"
    return result["direction"]


# --- the shapes the reporter's camera actually sent ---------------------------

def test_a_vehicle_approaching():
    assert _direction(_event(
        Direction=0,
        DrivingDirection=["Approach", ""],
        JunctionDirection="Obverse",
        VehicleDirection="Head",
    )) == "Approach"


def test_a_vehicle_leaving():
    assert _direction(_event(
        Direction=0,
        DrivingDirection=["Leave", ""],
        JunctionDirection="Reverse",
        VehicleDirection="Tail",
    )) == "Leave"


def test_junction_direction_at_the_top_level():
    """This camera puts JunctionDirection outside TrafficCar."""
    assert _direction(_top_level(JunctionDirection="Obverse")) == "Obverse"


# --- the ordering ------------------------------------------------------------

def test_a_camera_already_reporting_direction_is_unaffected():
    """No regression. Whatever worked before still wins."""
    assert _direction(_event(
        Direction="North",
        DrivingDirection=["Approach", ""],
    )) == "North"


def test_direction_name_still_beats_the_new_fields():
    assert _direction(_event(
        DirectionName="Inbound",
        JunctionDirection="Obverse",
    )) == "Inbound"


def test_driving_direction_is_preferred_over_the_photographic_ones():
    """Approach says what happened; Obverse and Head need a manual."""
    assert _direction(_event(
        DrivingDirection=["Leave", ""],
        JunctionDirection="Reverse",
        VehicleDirection="Tail",
    )) == "Leave"


def test_vehicle_direction_is_the_last_resort():
    assert _direction(_event(VehicleDirection="Tail")) == "Tail"


# --- the awkward shapes -----------------------------------------------------

def test_a_zero_is_not_a_direction():
    """The bug in miniature: Direction 0 must not be reported as a direction."""
    assert _direction(_event(Direction=0, JunctionDirection="Obverse")) == "Obverse"


def test_a_blank_first_entry_is_skipped():
    assert dahua_utils.first_direction(["", "Leave"]) == "Leave"


def test_whitespace_is_trimmed():
    assert dahua_utils.first_direction(["  Approach  "]) == "Approach"


def test_an_empty_list_gives_nothing():
    assert dahua_utils.first_direction([]) is None
    assert dahua_utils.first_direction(["", ""]) is None


def test_a_plain_string_is_taken_as_it_stands():
    assert dahua_utils.first_direction("Obverse") == "Obverse"


def test_a_number_is_not_a_direction():
    """A list of numbers must not become a direction by accident."""
    assert dahua_utils.first_direction([0, 1]) is None
    assert dahua_utils.first_direction(0) is None
    assert dahua_utils.first_direction(None) is None


def test_a_camera_reporting_none_of_them_still_reads_the_plate():
    result = dahua_utils.extract_plate_data(_event(CarType="NormalCar"))

    assert result is not None
    assert result["direction"] is None
    assert result["plate"] == "AB12CDE"
