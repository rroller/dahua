"""A release nobody is offered is nearly as bad as one that will not install.

HACS does not sort the releases list. It walks it in the order the REST API
returns and keeps the first entry of the kind it wants. That order is not
promised anywhere, and it has already been wrong for this repository:
`2.0.0-rc.10` came back third, behind rc.9 and rc.8, so every beta user was
told there was no update (#1134).

The guard in release.yml judges the list the way HACS reads it, and the two
cases below are the ones that are easy to get wrong:

  * a PRERELEASE has to be the very first live entry, not merely the first
    prerelease, because HACS breaks out of the loop on the first full release
    and never reaches a prerelease sitting behind one
  * a FULL RELEASE only has to be the first full one, since prereleases ahead
    of it set `prerelease` and the loop keeps going
"""

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml


def _guard():
    workflow = Path(__file__).parents[2] / ".github/workflows/release.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["visible"]["steps"]
    return next(step["run"] for step in steps if "Refuse a release" in step["name"])


def _run(tmp_path, tag, releases):
    (tmp_path / "releases.json").write_text(json.dumps(releases))
    return subprocess.run(
        ["bash", "-eu", "-c", _guard()],
        cwd=tmp_path,
        env={**os.environ, "RELEASE_TAG": tag},
        capture_output=True,
        text=True,
    )


def _rel(tag, prerelease=False, draft=False):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft}


# --- the prerelease has to be first outright ---------------------------------


def test_a_prerelease_at_the_top_is_offered(tmp_path):
    result = _run(
        tmp_path,
        "2.0.0-rc.10",
        [_rel("2.0.0-rc.10", True), _rel("2.0.0-rc.9", True), _rel("1.3.1")],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "beta channel" in result.stdout


def test_the_order_that_actually_happened_is_refused(tmp_path):
    """The real list from 2026-10-24: rc.10 came back third."""
    result = _run(
        tmp_path,
        "2.0.0-rc.10",
        [
            _rel("2.0.0-rc.9", True),
            _rel("2.0.0-rc.8", True),
            _rel("2.0.0-rc.10", True),
            _rel("2.0.0-rc.7", True),
        ],
    )

    assert result.returncode != 0
    assert "2.0.0-rc.9" in result.stdout, "the guard has to name what is in the way"
    assert "later UTC day" in result.stdout, "and say what to do about it"


def test_a_prerelease_behind_a_full_release_is_refused(tmp_path):
    """The case a "first prerelease" test would pass by mistake.

    HACS breaks on the first full release, so `prerelease` is never set and a
    beta user is offered the full release instead. The prerelease here IS the
    first prerelease, and it is still invisible.
    """
    result = _run(
        tmp_path,
        "2.0.0-rc.10",
        [_rel("1.3.1"), _rel("2.0.0-rc.10", True)],
    )

    assert result.returncode != 0
    assert "1.3.1" in result.stdout


# --- a full release only has to beat the other full releases -----------------


def test_a_full_release_may_sit_behind_prereleases(tmp_path):
    result = _run(
        tmp_path,
        "2.0.0",
        [_rel("2.0.1-rc.1", True), _rel("2.0.0"), _rel("1.3.1")],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "stable channel" in result.stdout


def test_a_full_release_behind_another_full_release_is_refused(tmp_path):
    result = _run(tmp_path, "1.3.1", [_rel("2.0.0"), _rel("1.3.1")])

    assert result.returncode != 0
    assert "2.0.0" in result.stdout


# --- the things that must not be mistaken for a blocker ----------------------


def test_a_draft_in_front_is_ignored(tmp_path):
    """HACS skips drafts before anything else, so one cannot be in the way."""
    result = _run(
        tmp_path,
        "2.0.0-rc.10",
        [_rel("2.0.1-rc.1", True, draft=True), _rel("2.0.0-rc.10", True)],
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_tag_that_is_not_in_the_page_at_all_is_refused(tmp_path):
    """HACS reads one page of 30. Past that a release does not exist to it."""
    result = _run(tmp_path, "2.0.0-rc.10", [_rel("2.0.0-rc.9", True)])

    assert result.returncode != 0
    assert "cannot see it at all" in result.stdout


@pytest.mark.parametrize("tag", ["2.0.0-rc.10", "2.0.0"])
def test_the_guard_says_which_release_it_judged(tag, tmp_path):
    """A green log that does not name the tag proves nothing on a re-read."""
    result = _run(tmp_path, tag, [_rel("2.0.0-rc.10", True), _rel("2.0.0")])

    assert result.returncode == 0, result.stdout + result.stderr
    assert tag in result.stdout
