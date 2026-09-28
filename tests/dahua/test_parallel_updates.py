"""Every platform says how many commands it will send at once.

`parallel-updates` is a Silver rule on Home Assistant's integration quality
scale: "Number of parallel updates is specified". Nothing here specified it, so
Home Assistant applied its own default.

The value is not a formality on these devices. A Dahua box is measurably
intolerant of concurrent requests -- MAX_CONCURRENT_REQUESTS_PER_HOST is 2 for
that reason, and the login storms behind #577 and #603 are what happens without
it -- so a platform that sends commands serialises them. A coordinator does not
help: it centralises inbound reads and leaves outbound actions uncontrolled.
"""

import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "dahua"

# Platforms whose entities only ever read. Their state arrives through the
# coordinator, so there is nothing to serialise.
READ_ONLY = {"binary_sensor", "sensor", "event"}

# Platforms that send a command to the device: a toggle, a preset, a reboot, a
# stream. These are the ones the limit exists for.
ACTING = {"camera", "switch", "light", "select", "button"}


def _declared(platform):
    body = (PACKAGE / ("%s.py" % platform)).read_text(encoding="utf-8")
    found = re.search(r"^PARALLEL_UPDATES\s*=\s*(\d+)$", body, re.MULTILINE)
    return None if found is None else int(found.group(1))


def test_the_platform_lists_above_are_the_platforms_that_ship():
    """Derived rather than trusted: a platform added without being listed here
    would silently stop being checked, which is how the issue placeholder map
    quietly stopped covering a new issue."""
    from custom_components.dahua.const import PLATFORMS

    assert READ_ONLY | ACTING == set(PLATFORMS), (
        "unlisted: %s; listed but not a platform: %s"
        % (sorted(set(PLATFORMS) - (READ_ONLY | ACTING)),
           sorted((READ_ONLY | ACTING) - set(PLATFORMS))))


@pytest.mark.parametrize("platform", sorted(READ_ONLY | ACTING))
def test_every_platform_declares_a_limit(platform):
    """Absent means Home Assistant picks, which is the thing the rule is about."""
    assert _declared(platform) is not None, (
        "%s.py does not set PARALLEL_UPDATES" % platform)


@pytest.mark.parametrize("platform", sorted(READ_ONLY))
def test_a_read_only_platform_does_not_serialise(platform):
    """Nothing is sent, so a limit would only slow startup down."""
    assert _declared(platform) == 0


@pytest.mark.parametrize("platform", sorted(ACTING))
def test_a_platform_that_sends_commands_sends_one_at_a_time(platform):
    """The measured reason: these devices fall over on concurrent requests, and
    a coordinator does not govern outbound calls."""
    assert _declared(platform) == 1
