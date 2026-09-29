"""Only the main stream is enabled when a camera is first added.

`entity-disabled-by-default` on the quality scale: disable the less popular entities.
Nothing in this integration was disabled by default, and the camera streams were the
one place it plainly mattered. The README said so in as many words:

    NOTE: All streams will be added, even if not enabled in the camera. Just remove
    the ones you don't want.

Every stream the device can serve gets an entity, because which ones are actually
enabled cannot be known without asking and asking costs a request per channel. Most
people watch one stream per camera, so an eleven channel recorder serving three
streams each produced thirty three camera entities, twenty two of which were there to
be deleted by hand.

Created but not enabled is the difference. The entity is still listed and anyone
pointing a card at a sub stream turns it on once, which is a great deal less work than
deleting twenty two.

**Nothing that already exists changes.** `entity_registry_enabled_default` is read only
when an entity is first registered, so an existing sub stream camera stays exactly as
its owner left it. That is the property worth being sure of, and it is why this was safe
to change at all.
"""

from custom_components.dahua.camera import DahuaCamera


def _camera(stream_index):
    """A camera with only what this property reads.

    Built with object.__new__ because the real __init__ needs the whole Home Assistant
    entity machinery, and because the value is derived from the stream index rather than
    stored, that is all this needs. Reading it off an instance also avoids the trap
    where an `_attr_` on the class is a property object and every assertion passes.
    """
    camera = object.__new__(DahuaCamera)
    camera._stream_index = stream_index
    return camera


def test_the_main_stream_is_enabled():
    assert _camera(0).entity_registry_enabled_default is True


def test_the_sub_stream_is_not():
    assert _camera(1).entity_registry_enabled_default is False


def test_the_third_stream_is_not_either():
    assert _camera(2).entity_registry_enabled_default is False


def test_a_recorder_serving_three_streams_enables_one_of_them():
    """The arithmetic the rule is about, for one channel."""
    enabled = [i for i in range(3) if _camera(i).entity_registry_enabled_default]

    assert enabled == [0], "expected only the main stream, got %s" % enabled


def test_eleven_channels_of_three_streams_leave_eleven_enabled():
    """And for the recorder that made this worth doing."""
    cameras = [_camera(index) for _ in range(11) for index in range(3)]

    enabled = [c for c in cameras if c.entity_registry_enabled_default]

    assert len(cameras) == 33
    assert len(enabled) == 11


def test_the_two_sensor_model_keeps_a_main_stream_for_each_sensor():
    """The SDT4E425 is one config entry carrying two physical sensors, and each gets
    its own set of streams with its own index 0. Both of those have to stay enabled, or
    half the camera arrives switched off."""
    panorama_main, panorama_sub = _camera(0), _camera(1)
    ptz_main, ptz_sub = _camera(0), _camera(1)

    assert panorama_main.entity_registry_enabled_default is True
    assert ptz_main.entity_registry_enabled_default is True
    assert panorama_sub.entity_registry_enabled_default is False
    assert ptz_sub.entity_registry_enabled_default is False


def test_the_value_is_derived_rather_than_stored():
    """Derived on purpose. Moving it into `_attr_entity_registry_enabled_default` set in
    __init__ would keep the tests above passing but put the value in two places and make
    it unreadable off the class, which is the trap that produced two vacuous passes on
    an earlier change here.

    Asserted as "a descriptor on DahuaCamera" rather than specifically `property`,
    because Home Assistant's metaclass is entitled to wrap it. What this catches is the
    name not being defined on the class at all, which is what an `_attr_` would mean.
    """
    declared = DahuaCamera.__dict__.get("entity_registry_enabled_default")

    assert declared is not None, (
        "entity_registry_enabled_default is no longer declared on DahuaCamera; if it "
        "moved to an _attr_ then read it off an instance and delete this test")
    assert hasattr(declared, "__get__"), declared
