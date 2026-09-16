"""A poll that failed must not turn the next event into `unavailable`.

The event sensors are pushed to from the event stream and never read the poll.
They did inherit `available` from `CoordinatorEntity`, which reports the last
poll's result, and Home Assistant writes `unavailable` in place of whatever
state an entity reports whenever that property is False. So a `configManager`
read that timed out could turn the doorbell press that arrived a moment later
into `unavailable` instead of `on`.

Seen on a live install: a VTO2000A whose config reads occasionally take longer
than the 20s request timeout. Its motion sensor wrote `unavailable` in place of
a real event seven times in two days, once sitting there for eighteen minutes.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import (
    UNREACHABLE_AFTER_FAILURES,
    _HOST_FAILURES,
    async_host_is_unreachable,
)
from custom_components.dahua.binary_sensor import (
    DahuaAuthorizedVehicleBinarySensor,
    DahuaEventSensor,
)
from custom_components.dahua.entity import DahuaBaseEntity, DahuaEventDrivenEntity
from custom_components.dahua.sensor import DahuaLicensePlateSensor
from custom_components.dahua.switch import DahuaMotionDetectionBinarySwitch

ADDRESS = "192.168.0.232"

EVENT_DRIVEN = [
    DahuaEventSensor,
    DahuaAuthorizedVehicleBinarySensor,
    DahuaLicensePlateSensor,
]


@pytest.fixture(autouse=True)
def _clean_host_failures():
    _HOST_FAILURES.clear()
    yield
    _HOST_FAILURES.clear()


def _fail(times):
    """Put the host on the same counter a run of failed polls would."""
    _HOST_FAILURES[ADDRESS] = {
        "consecutive": times,
        "since": 0.0,
        "entry_ids": set(),
        "last_probe": 0,
    }


def _entity(cls):
    entity = object.__new__(cls)
    # The poll has failed. An event sensor should not care.
    entity._coordinator = SimpleNamespace(
        get_address=lambda: ADDRESS, last_update_success=False
    )
    return entity


@pytest.mark.parametrize("cls", EVENT_DRIVEN, ids=lambda c: c.__name__)
def test_one_slow_read_does_not_take_the_event_entities_away(cls):
    _fail(1)

    assert _entity(cls).available is True


@pytest.mark.parametrize("cls", EVENT_DRIVEN, ids=lambda c: c.__name__)
def test_a_device_that_answers_nothing_still_marks_them_unavailable(cls):
    """The signal is not thrown away, only moved off the wrong transport."""
    _fail(UNREACHABLE_AFTER_FAILURES)

    assert _entity(cls).available is False


@pytest.mark.parametrize("cls", EVENT_DRIVEN, ids=lambda c: c.__name__)
def test_a_host_that_has_never_failed_is_available(cls):
    assert _entity(cls).available is True


def test_the_threshold_is_the_one_the_repair_card_uses():
    _fail(UNREACHABLE_AFTER_FAILURES - 1)
    assert async_host_is_unreachable(ADDRESS) is False

    _fail(UNREACHABLE_AFTER_FAILURES)
    assert async_host_is_unreachable(ADDRESS) is True


def test_the_polled_entities_are_left_alone():
    """Their state does come from the poll, so a failed poll is the truth.

    Only the entities that are pushed to were moved off it.
    """
    assert not issubclass(DahuaMotionDetectionBinarySwitch, DahuaEventDrivenEntity)
    assert issubclass(DahuaMotionDetectionBinarySwitch, DahuaBaseEntity)
    assert "available" not in DahuaMotionDetectionBinarySwitch.__dict__
