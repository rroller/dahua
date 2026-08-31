"""ptz.cgi takes the channel number, not the 0-based channel index."""

from types import SimpleNamespace

from custom_components.dahua.camera import DahuaCamera
from custom_components.dahua.select import DahuaCameraPresetPositionSelect


class _CapturingClient:
    """Records the channel handed to the PTZ CGI call."""

    def __init__(self):
        self.channel = None
        self.position = None

    async def async_goto_preset_position(self, channel, position):
        self.channel = channel
        self.position = position
        return {}


class _FakeCoordinator:
    def __init__(self, channel_index, channel_number, model="OTHER"):
        self.client = _CapturingClient()
        self._channel_index = channel_index
        self._channel_number = channel_number
        self._model = model
        self.data = {}

    def get_channel(self):
        return self._channel_index

    def get_channel_number(self):
        return self._channel_number

    def get_model(self):
        return self._model

    async def async_refresh(self):
        return None


def _select_for(coordinator):
    """Build the entity without running Home Assistant's entity plumbing."""
    entity = object.__new__(DahuaCameraPresetPositionSelect)
    entity._coordinator = coordinator
    entity._rpc2_channel = None
    return entity


def _camera_for(coordinator):
    entity = object.__new__(DahuaCamera)
    entity._coordinator = coordinator
    entity._logical_channel = coordinator.get_channel()
    entity._channel_number = coordinator.get_channel_number()
    return entity


async def test_select_sends_the_channel_number_not_the_index():
    """On an NVR the index and the number differ; ptz.cgi wants the number."""
    coordinator = _FakeCoordinator(channel_index=2, channel_number=3)

    await _select_for(coordinator).async_select_option("4")

    assert coordinator.client.channel == 3, "sent the 0-based index to ptz.cgi"
    assert coordinator.client.position == 4


async def test_goto_preset_service_sends_the_channel_number_not_the_index():
    """The goto_preset_position service had the same defect as the select."""
    coordinator = _FakeCoordinator(channel_index=2, channel_number=3)

    await _camera_for(coordinator).async_goto_preset_position(4)

    assert coordinator.client.channel == 3, "sent the 0-based index to ptz.cgi"
    assert coordinator.client.position == 4


async def test_channel_number_equal_to_index_is_respected():
    """Older firmware reports number == index; the coordinator detects that.

    Reading get_channel_number() keeps that correction. Deriving the channel as
    index + 1 would silently undo it.
    """
    coordinator = _FakeCoordinator(channel_index=2, channel_number=2)

    await _select_for(coordinator).async_select_option("1")
    assert coordinator.client.channel == 2

    await _camera_for(coordinator).async_goto_preset_position(1)
    assert coordinator.client.channel == 2


async def test_select_manual_is_a_no_op():
    """Manual is a readback placeholder, not a position to command."""
    coordinator = _FakeCoordinator(channel_index=2, channel_number=3)

    await _select_for(coordinator).async_select_option("Manual")

    assert coordinator.client.channel is None
