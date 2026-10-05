"""No test inherits another test's failing hosts.

`_HOST_FAILURES` is module state keyed by address. A successful poll withdraws the
host's repair issues when its address is in there, through the coordinator's `hass`.
The poll doubles in test_poll_skips_unused.py and test_ivs_rules.py use 10.0.0.5 and a
SimpleNamespace for `hass`, so an entry left behind by any other test on the same
worker made their polls fail with "unhashable type: 'types.SimpleNamespace'". Under
`-n auto` that depended on which tests shared a worker, and
test_ivs_rules::test_poll_reads_current_ivs_table_only_when_switches_enabled failed now
and then for no reason in its own code.

conftest's `_clear_host_failures` empties it around every test.
"""

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components import dahua as dahua_module

from .test_poll_skips_unused import _coordinator


async def test_a_failing_host_left_behind_breaks_the_poll_doubles():
    """The hazard, reproduced on purpose, so the fixture's reason stays visible."""
    dahua_module._HOST_FAILURES["10.0.0.5"] = {"consecutive": 3}

    with pytest.raises(UpdateFailed, match="unhashable"):
        await _coordinator()._async_update_data()


def test_a_test_that_leaves_one_behind():
    """Deliberately not cleaned up: the fixture has to."""
    dahua_module._HOST_FAILURES["10.0.0.5"] = {"consecutive": 7}


def test_the_next_test_starts_with_none():
    """Run in one process (or on the same worker as the test above) this fails
    without the fixture. Split across workers it simply passes."""
    assert dahua_module._HOST_FAILURES == {}


async def test_and_the_poll_double_works():
    await _coordinator()._async_update_data()
