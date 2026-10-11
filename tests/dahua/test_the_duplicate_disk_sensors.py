"""Removing the disk sensors every channel of a recorder used to create.

`storageDevice.cgi?action=getDeviceAllInfo` takes no channel: the disks belong
to the box. Every channel coordinator asked anyway and created its own sensor
per disk, so a twelve channel recorder with four bays carried forty eight
entities describing four disks. Measured on a DHI-NVR5464-16P-EI:

    binary_sensor...ch1_front_street_disk_dev_sda   BC0A198PAJ779DF_disk_dev_sda
    binary_sensor...ch2_front_verandah_disk_dev_sda BC0A198PAJ779DF_1_disk_dev_sda
    binary_sensor...backyard_disk_dev_sda           BC0A198PAJ779DF_2_disk_dev_sda
    ... and so on for sdb, sdc, sdd on all twelve channels

Channel 0's id carries no channel, so it is the one the sensors keep using and
the one that survives with its history. These tests are about removing the rest
without ever removing that.

This is a destructive migration, in the module whose merge once sent 232
entities on a real install to `deleted_entities` (#891). So the tests are mostly
about what it must refuse to do:

* never remove a row unless the surviving id is actually present
* never remove anything that is not a disk sensor
* never mistake part of a serial for a channel number
* never remove anything at all if the backup could not be written
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import migrate

SERIAL = "BC0A198PAJ779DF"


class _Row:
    def __init__(self, unique_id, entity_id=None):
        self.unique_id = unique_id
        self.entity_id = entity_id or (
            "binary_sensor.%s" % unique_id.replace(".", "_").lower()
        )


class _Registry:
    def __init__(self, rows):
        self.rows = list(rows)
        self.log = []

    def async_remove(self, entity_id):
        self.log.append(("remove", entity_id))
        self.rows = [row for row in self.rows if row.entity_id != entity_id]


class _Hass:
    def __init__(self):
        self.config = SimpleNamespace(path=lambda *parts: "/config")

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def world(monkeypatch):
    """The migration wired to a fake registry, recording what it asks for."""

    def build(rows, backup="/config/dahua-pre-disk-cleanup-backup-x"):
        registry = _Registry(rows)
        monkeypatch.setattr(migrate.er, "async_get", lambda _hass: registry)
        monkeypatch.setattr(
            migrate.er,
            "async_entries_for_config_entry",
            lambda reg, entry_id: list(reg.rows),
        )

        def _backup(_hass, label="pre-merge"):
            registry.log.append(("backup", label))
            return backup

        monkeypatch.setattr(migrate, "_backup", _backup)
        return SimpleNamespace(
            registry=registry,
            entry=SimpleNamespace(entry_id="entry", data={"address": "10.0.0.5"}),
            hass=_Hass(),
        )

    return build


def _removed(registry):
    return [name for kind, name in registry.log if kind == "remove"]


def _disk_rows(channels, slugs=("dev_sda",), serial=SERIAL):
    """One row per channel per disk, in the shape the entities really used."""
    rows = []
    for channel in channels:
        for slug in slugs:
            if channel == 0:
                rows.append(_Row("%s_disk_%s" % (serial, slug)))
            else:
                rows.append(_Row("%s_%d_disk_%s" % (serial, channel, slug)))
    return rows


# --- what it is for ----------------------------------------------------------


async def test_the_per_channel_copies_go(world):
    place = world(_disk_rows(range(12), ("dev_sda", "dev_sdb", "dev_sdc", "dev_sdd")))

    await migrate.async_drop_duplicate_disk_sensors(place.hass, place.entry)

    assert len(_removed(place.registry)) == 44, _removed(place.registry)
    assert len(place.registry.rows) == 4


async def test_channel_zeros_sensors_are_the_ones_that_stay(world):
    place = world(_disk_rows(range(3), ("dev_sda", "dev_sdb")))

    await migrate.async_drop_duplicate_disk_sensors(place.hass, place.entry)

    assert sorted(row.unique_id for row in place.registry.rows) == [
        "%s_disk_dev_sda" % SERIAL,
        "%s_disk_dev_sdb" % SERIAL,
    ]


async def test_it_does_nothing_the_second_time(world):
    place = world(_disk_rows(range(4)))

    await migrate.async_drop_duplicate_disk_sensors(place.hass, place.entry)
    first = len(_removed(place.registry))
    place.registry.log.clear()
    await migrate.async_drop_duplicate_disk_sensors(place.hass, place.entry)

    assert first == 3
    assert place.registry.log == [], "asked for something on a second pass"


# --- what it must refuse to do ----------------------------------------------


async def test_nothing_goes_when_the_survivor_is_not_there(world):
    """The safety property. A recorder whose entry has no channel 0 has no
    `<serial>_disk_<slug>` row yet, and removing the per-channel ones would
    delete the only copy of a disk. Clutter is a better failure than loss."""
    place = world(_disk_rows([1, 2, 3]))

    await migrate.async_drop_duplicate_disk_sensors(place.hass, place.entry)

    assert place.registry.log == []
    assert len(place.registry.rows) == 3


async def test_nothing_that_is_not_a_disk_sensor_is_touched(world):
    place = world(
        _disk_rows(range(3))
        + [
            _Row("%s_1_motion" % SERIAL),
            _Row("%s_2_siren" % SERIAL),
            _Row("%s_1_disk_space_used" % SERIAL),
            _Row("%s_profile" % SERIAL),
        ]
    )

    await migrate.async_drop_duplicate_disk_sensors(place.hass, place.entry)

    survivors = {row.unique_id for row in place.registry.rows}
    assert "%s_1_motion" % SERIAL in survivors
    assert "%s_2_siren" % SERIAL in survivors
    assert "%s_profile" % SERIAL in survivors
    # No `<serial>_disk_space_used` exists, so this one has no survivor and stays
    # even though it is shaped like a disk id.
    assert "%s_1_disk_space_used" % SERIAL in survivors


async def test_a_serial_with_an_underscore_is_not_read_as_a_channel(world):
    """`rpartition` rather than `partition`, so the digits have to be the last
    segment before `_disk_`. A device whose serial ends in `_12` would otherwise
    have its only disk sensor deleted."""
    odd = "AB_12"
    place = world(
        [
            _Row("%s_disk_dev_sda" % odd),
            _Row("%s_1_disk_dev_sda" % odd),
        ]
    )

    await migrate.async_drop_duplicate_disk_sensors(place.hass, place.entry)

    survivors = {row.unique_id for row in place.registry.rows}
    assert "%s_disk_dev_sda" % odd in survivors, "removed the one that stays"
    assert "%s_1_disk_dev_sda" % odd not in survivors


async def test_a_middle_segment_that_is_not_a_channel_is_left_alone(world):
    """A duplicate has the shape `<serial>_<digits>_disk_<slug>`. Anything else
    that merely contains `_disk_` is not one, and the isdigit check is what stops
    it being swept up -- which matters because camera storage sensors are likely
    to want ids of exactly that kind.
    """
    place = world(
        [
            _Row("%s_disk_dev_sda" % SERIAL),
            _Row("%s_sdcard_disk_dev_sda" % SERIAL),
        ]
    )

    await migrate.async_drop_duplicate_disk_sensors(place.hass, place.entry)

    survivors = {row.unique_id for row in place.registry.rows}
    assert "%s_sdcard_disk_dev_sda" % SERIAL in survivors, _removed(place.registry)
    assert place.registry.log == []


async def test_a_camera_is_left_entirely_alone(world):
    """One disk sensor and no duplicates. Not even a backup: nothing is touched."""
    place = world([_Row("%s_disk_dev_sda" % SERIAL)])

    await migrate.async_drop_duplicate_disk_sensors(place.hass, place.entry)

    assert place.registry.log == []


async def test_an_entry_with_no_entities_at_all_is_fine(world):
    place = world([])

    await migrate.async_drop_duplicate_disk_sensors(place.hass, place.entry)

    assert place.registry.log == []


# --- the backup --------------------------------------------------------------


async def test_the_registries_are_copied_before_anything_is_removed(world):
    place = world(_disk_rows(range(3)))

    await migrate.async_drop_duplicate_disk_sensors(place.hass, place.entry)

    kinds = [kind for kind, _ in place.registry.log]
    assert kinds[0] == "backup", kinds
    assert "remove" in kinds
    assert place.registry.log[0] == ("backup", "pre-disk-cleanup"), (
        "the backup directory should say what it was taken for, not claim to be "
        "the merge's"
    )


async def test_a_backup_that_could_not_be_written_removes_nothing(world):
    """Same rule the merge follows: no copy, no mutation."""
    place = world(_disk_rows(range(3)), backup=None)

    await migrate.async_drop_duplicate_disk_sensors(place.hass, place.entry)

    assert _removed(place.registry) == []
    assert len(place.registry.rows) == 3
