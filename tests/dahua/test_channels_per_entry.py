"""One entry can describe several channels, and setup must see all of them.

This is the piece that makes the merge in #827 safe. The migration moves a
recorder's channels onto one entry with a subentry each; without this, setup would
still build a single coordinator from `entry.data`, bring up channel 0, and leave
every other channel's entities unavailable with nothing in the log to say why.

`channel_configs` deliberately returns the same shape for both cases, so setup has
one path: a single camera and a pre-merge recorder channel look like one channel
described by the entry's own data, and a merged recorder looks like one per
subentry.
"""

from types import SimpleNamespace

from custom_components.dahua import channel_configs, events_for_channel


def _entry(subentries=None, data=None, options=None):
    return SimpleNamespace(
        entry_id="e1",
        data=data or {"address": "1.2.3.4", "channel": 0,
                      "events": ["VideoMotion"]},
        options=options or {},
        subentries=subentries or {},
    )


def _sub(channel, events=None):
    data = {"address": "1.2.3.4", "channel": channel}
    if events is not None:
        data["events"] = events
    return SimpleNamespace(data=data)


# --- the two shapes ---------------------------------------------------------

def test_an_entry_with_no_subentries_is_one_channel():
    """A single camera, and any recorder channel that predates the merge."""
    configs = channel_configs(_entry())

    assert len(configs) == 1
    subentry_id, config = configs[0]
    assert subentry_id is None
    assert config["channel"] == 0


def test_a_merged_recorder_is_one_channel_per_subentry():
    configs = channel_configs(_entry(subentries={
        "s9": _sub(9), "s0": _sub(0), "s1": _sub(1)}))

    assert len(configs) == 3
    assert [c[1]["channel"] for c in configs] == [0, 1, 9]


def test_channels_come_out_in_channel_order_not_storage_order():
    """Subentries are a dict, so their order is insertion order and means
    nothing. Entities would otherwise appear in an arbitrary order on the device
    page, differently on different installs."""
    configs = channel_configs(_entry(subentries={
        "b": _sub(12), "a": _sub(3), "c": _sub(0)}))

    assert [c[0] for c in configs] == ["c", "a", "b"]


def test_a_channel_stored_as_a_string_still_sorts_as_a_number():
    """The add flow writes extra channels as strings, so "10" and 9 have to be
    compared as numbers or channel 10 sorts before channel 9."""
    configs = channel_configs(_entry(subentries={
        "s10": _sub("10"), "s9": _sub(9), "s0": _sub(0)}))

    assert [c[0] for c in configs] == ["s0", "s9", "s10"]


def test_a_channel_with_an_unreadable_number_is_treated_as_zero():
    """Rather than raising and taking the whole recorder's setup down."""
    configs = channel_configs(_entry(subentries={
        "bad": _sub("not a number"), "one": _sub(1)}))

    assert [c[0] for c in configs] == ["bad", "one"]


def test_the_channel_comes_back_as_a_number_whatever_it_was_stored_as():
    """runtime_data is keyed on the channel, so "3" and 3 would be two keys for
    one channel -- and get_channel() is declared to return an int, which
    anything doing arithmetic on it is entitled to believe."""
    configs = channel_configs(_entry(subentries={"s": _sub("3")}))

    assert configs[0][1]["channel"] == 3
    assert isinstance(configs[0][1]["channel"], int)


def test_one_channel_is_described_once():
    """Two subentries claiming one channel would each get a coordinator, and
    only one of them would end up in runtime_data. The other would keep polling
    the device with nothing owning it, and unload -- which walks runtime_data --
    would never stop it. That is a session left open for the life of Home
    Assistant, on a device that locks out a host making too many."""
    configs = channel_configs(_entry(subentries={
        "first": _sub(3), "second": _sub(3), "other": _sub(4)}))

    assert [c[1]["channel"] for c in configs] == [3, 4]
    assert [c[0] for c in configs] == ["first", "other"]


def test_a_string_and_an_int_are_the_same_channel():
    """The two shapes are both in the wild, so the duplicate above is not a
    hypothetical: one entry added through the form and one imported can disagree
    about the type and agree about the channel."""
    configs = channel_configs(_entry(subentries={
        "as_int": _sub(3), "as_string": _sub("3")}))

    assert len(configs) == 1


# --- per channel event lists ------------------------------------------------

def test_each_channel_keeps_its_own_event_list():
    """The whole point of a subentry per channel. One channel's selection
    becoming every channel's would be a silent, confusing regression."""
    entry = _entry(subentries={
        "s0": _sub(0, ["VideoMotion"]),
        "s9": _sub(9, ["AlarmLocal"]),
    })
    configs = dict((c[0], c[1]) for c in channel_configs(entry))

    assert events_for_channel(entry, configs["s0"]) == ["VideoMotion"]
    assert events_for_channel(entry, configs["s9"]) == ["AlarmLocal"]


def test_a_channel_with_no_list_of_its_own_falls_back_to_the_entry():
    """A subentry written before the event list moved onto it, or one added by
    hand. Better the entry's list than no sensors at all."""
    entry = _entry(subentries={"s4": _sub(4)})

    assert events_for_channel(entry, {"channel": 4}) == ["VideoMotion"]


def test_an_empty_list_on_a_channel_is_honoured_rather_than_replaced():
    """Deselecting everything is a choice. Treating it as "unset" and handing
    back the entry's list would resurrect sensors the user removed."""
    entry = _entry(subentries={"s4": _sub(4, [])})
    configs = dict((c[0], c[1]) for c in channel_configs(entry))

    assert events_for_channel(entry, configs["s4"]) == []


def test_a_single_channel_entry_still_lets_options_win():
    """#601 made the event list changeable after setup by preferring options over
    data, and this must not quietly undo that for everybody with one camera."""
    entry = _entry(options={"events": ["VideoLoss"]})

    assert events_for_channel(entry, entry.data) == ["VideoLoss"]
