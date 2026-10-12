"""Labels chosen once and put on every device this entry creates.

Home Assistant has no `DeviceInfo` field for labels. `suggested_area` is
honoured when a device is created; labels are not, and the only way to set them
is through the device registry. Verified against the installed Home Assistant
rather than assumed:

    selector.LabelSelector / LabelSelectorConfig   exist
    DeviceEntry.labels                             exists
    async_update_device(..., labels=...)           exists
    DeviceInfo has a label field                   no

So they are applied after the devices exist: at the end of setup, and again
whenever the option changes.

Entry wide, unlike the area. A recorder's twelve channels are twelve devices
and the point of the option is to label them together, so it lives in the
entry's own data rather than in each channel's -- which is also why it is not
in CHANNEL_OPTION_KEYS.

The whole design is in one small function. `existing` is what the device has,
including anything somebody added by hand; `previously_chosen` is what the
option said last time. Subtracting `previously_chosen - chosen` removes only
what this option put there and has stopped asking for, so:

* ticking a label puts it on every camera
* unticking it takes it off again, which is what somebody expects of a list
  they just edited
* a label put on a camera by hand survives both, and survives a reload

and at setup, where nothing has changed, `previously_chosen` is `chosen`, so
the subtraction is empty and the pass is add-only. That falls out rather than
being a second rule.
"""

import pytest

from custom_components.dahua.dahua_utils import labels_for_device


def test_the_chosen_labels_are_put_on():
    assert labels_for_device(existing=[], previously_chosen=[], chosen=["a", "b"]) == {
        "a",
        "b",
    }


def test_unticking_one_takes_it_off():
    """The reason this is not a union. Somebody who edits a list expects the
    thing they removed to go."""
    assert labels_for_device(
        existing=["a", "b"], previously_chosen=["a", "b"], chosen=["a"]
    ) == {"a"}


def test_a_label_somebody_added_by_hand_is_left_alone():
    """The reason this is not an assignment. `mine` was never in the option, so
    the option has no business removing it."""
    assert labels_for_device(
        existing=["a", "mine"], previously_chosen=["a"], chosen=["b"]
    ) == {"mine", "b"}


def test_a_hand_added_label_survives_the_option_being_cleared():
    assert labels_for_device(
        existing=["a", "mine"], previously_chosen=["a"], chosen=[]
    ) == {"mine"}


def test_at_setup_nothing_is_removed():
    """Setup passes the same set as both arguments, because nothing has changed
    since last time. So a label the user removed by hand stays removed until
    they change the option, rather than coming back on every reload."""
    assert labels_for_device(
        existing=["mine"], previously_chosen=["a", "b"], chosen=["a", "b"]
    ) == {"mine", "a", "b"}


def test_a_label_removed_by_hand_is_not_re_added_at_setup():
    """The same call, from the point of view of somebody who took one off."""
    before = {"keep"}
    after = labels_for_device(
        existing=before, previously_chosen=["keep"], chosen=["keep"]
    )

    assert after == {"keep"}


def test_choosing_nothing_changes_nothing():
    assert labels_for_device(
        existing=["a", "mine"], previously_chosen=[], chosen=[]
    ) == {"a", "mine"}


@pytest.mark.parametrize(
    "existing, previously, chosen",
    [
        (None, None, None),
        (None, [], ["a"]),
        (["a"], None, None),
    ],
    ids=["all none", "no previous", "nothing chosen"],
)
def test_none_is_the_same_as_empty(existing, previously, chosen):
    """A registry can hand back None, and an option that was never set is None.
    Neither is a reason to raise inside a setup step."""
    result = labels_for_device(existing, previously, chosen)

    assert isinstance(result, set)


def test_the_result_is_a_set_the_registry_will_take():
    """async_update_device wants a set of label ids, and a list of duplicates
    from a form would otherwise reach it."""
    assert labels_for_device(["a", "a"], [], ["b", "b"]) == {"a", "b"}


def test_it_is_not_a_per_channel_setting():
    """CHANNEL_OPTION_KEYS is the set of options that differ per channel and get
    merged into each channel's config. Labels are entry wide, so a channel
    carrying its own copy would be a second source of truth."""
    from custom_components.dahua.const import CHANNEL_OPTION_KEYS, CONF_LABELS

    assert CONF_LABELS not in CHANNEL_OPTION_KEYS
