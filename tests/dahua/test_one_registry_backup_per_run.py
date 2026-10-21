"""Three monitors migrating together should not write three identical backups.

Reported by @kmich, verifying #1108 on three VTH2421F-P:

    The three backup directories are byte-identical. They were written within
    0.1 s of each other and copy the persisted files, which Home Assistant
    saves with a delay, so each one probably holds the state from before any
    monitor moved.

He is right about why. A backup copies the files in `.storage`, and Home
Assistant persists the registries on a delay, so nothing the first migration
did had reached disk before the second and third copied it.

The copy is now shared, but on evidence rather than on a timer: the files are
stat'd, and the existing copy is reused only while their sizes and modification
times are unchanged. A backup's one job is to be the state immediately before
the change, and a window measured in seconds would have traded that for a round
number. An entry reloaded later, after Home Assistant has persisted anything,
gets its own.
"""

import asyncio
import os
import shutil

import pytest

from custom_components.dahua import migrate
from custom_components.dahua.migrate import BACKED_UP, _async_backup_once


def _write_registries(hass, body=b"{}"):
    storage = hass.config.path(".storage")
    os.makedirs(storage, exist_ok=True)
    for name in BACKED_UP:
        with open(os.path.join(storage, name), "wb") as handle:
            handle.write(body)
    return storage


def _backups(hass):
    return {
        name
        for name in os.listdir(hass.config.path())
        if name.startswith("dahua-pre-serial-backup-")
    }


@pytest.fixture
def written(hass):
    """How many backup directories this test created.

    The config directory outlives one test here, so the count has to be a
    difference rather than a total: a stale assertion on the absolute number
    would pass or fail on what ran before it.
    """
    before = _backups(hass)

    def since():
        return len(_backups(hass) - before)

    yield since

    # The config directory is shared, so anything left here is left for every
    # test that runs after this file.
    for name in _backups(hass) - before:
        shutil.rmtree(hass.config.path(name), ignore_errors=True)


# --- the reported case --------------------------------------------------------


async def test_three_entries_in_one_startup_share_one_backup(hass, written):
    _write_registries(hass)

    paths = [await _async_backup_once(hass) for _ in range(3)]

    assert paths[0] is not None
    assert paths == [paths[0]] * 3
    assert written() == 1


async def test_entries_setting_up_concurrently_share_it_too(hass, written, monkeypatch):
    """The case that matters, and the reason there is a lock.

    The interleaving is forced rather than hoped for. Every executor hop is
    replaced by one that yields to the event loop, so all three coroutines
    reach the check before any of them copies: without the lock each sees no
    backup and writes its own, which is the behaviour being fixed. Left to the
    real executor this passed either way, because the pool happened to finish
    the first caller before starting the second, and a test that passes with
    the fix removed is not a test.
    """

    async def yielding(func, *args):
        await asyncio.sleep(0)
        return func(*args)

    monkeypatch.setattr(hass, "async_add_executor_job", yielding)
    _write_registries(hass)

    paths = await asyncio.gather(*(_async_backup_once(hass) for _ in range(3)))

    assert len(set(paths)) == 1
    assert written() == 1


async def test_the_copy_holds_the_registries(hass):
    """The negative control for sharing: one directory is only acceptable if it
    is a real backup. Reusing a path that holds nothing would pass every count
    above and lose the thing the migration refuses to run without."""
    _write_registries(hass, b'{"version": 1}')

    where = await _async_backup_once(hass)

    assert sorted(os.listdir(where)) == sorted(BACKED_UP)
    for name in BACKED_UP:
        with open(os.path.join(where, name), "rb") as handle:
            assert handle.read() == b'{"version": 1}'


# --- and it is not reused once it is out of date ------------------------------


async def test_a_persisted_change_takes_a_new_backup(hass, written):
    """An entry reloaded after Home Assistant has written the registries is not
    described by the copy taken at startup, so it must not be handed it."""
    _write_registries(hass)
    first = await _async_backup_once(hass)

    _write_registries(hass, b'{"changed": true}')
    second = await _async_backup_once(hass)

    assert second != first
    assert written() == 2


async def test_the_new_copy_holds_the_new_state(hass):
    _write_registries(hass, b"one")
    await _async_backup_once(hass)

    _write_registries(hass, b"two")
    second = await _async_backup_once(hass)

    with open(os.path.join(second, BACKED_UP[0]), "rb") as handle:
        assert handle.read() == b"two"


# --- a failure is not something to remember -----------------------------------


async def test_a_failed_backup_is_not_remembered(hass, monkeypatch):
    """A full disk at the moment the first entry set up should not stop the
    second from protecting itself, and the caller still refuses to migrate on
    None either way."""
    _write_registries(hass)
    monkeypatch.setattr(migrate, "_backup", lambda hass, label="x": None)

    assert await _async_backup_once(hass) is None

    monkeypatch.undo()
    assert await _async_backup_once(hass) is not None


async def test_a_failure_does_not_poison_the_one_already_taken(hass, monkeypatch):
    """The other order. A good copy exists and is still accurate, so a later
    failure to make another one is not a reason to withhold it."""
    _write_registries(hass)
    first = await _async_backup_once(hass)
    monkeypatch.setattr(migrate, "_backup", lambda hass, label="x": None)

    assert await _async_backup_once(hass) == first


# --- the fingerprint itself ---------------------------------------------------


def test_the_fingerprint_notices_a_changed_file(hass):
    _write_registries(hass, b"short")
    before = migrate._registry_fingerprint(hass)

    _write_registries(hass, b"rather longer than before")

    assert migrate._registry_fingerprint(hass) != before


def test_the_fingerprint_is_stable_while_nothing_changes(hass):
    _write_registries(hass)

    assert migrate._registry_fingerprint(hass) == migrate._registry_fingerprint(hass)


def test_a_missing_registry_is_recorded_rather_than_raising(hass):
    """`.storage` need not hold all three. The copy refuses in that case, which
    is `_backup`'s job; this must not raise before it gets there."""
    storage = _write_registries(hass)
    os.unlink(os.path.join(storage, BACKED_UP[0]))

    marks = dict(
        (name, size) for name, size, _mtime in migrate._registry_fingerprint(hass)
    )

    assert marks[BACKED_UP[0]] is None
    assert marks[BACKED_UP[1]] is not None


@pytest.mark.parametrize("name", list(BACKED_UP))
def test_every_backed_up_file_is_in_the_fingerprint(hass, name):
    """Derived from BACKED_UP rather than listed again, so a fourth file added
    to the backup cannot be left out of the check that decides whether the
    backup is still current."""
    _write_registries(hass)

    assert name in {entry[0] for entry in migrate._registry_fingerprint(hass)}
