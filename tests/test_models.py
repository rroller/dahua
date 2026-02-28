"""Tests for models module."""

from custom_components.dahua.models import CoaxialControlIOStatus


def test_defaults():
    status = CoaxialControlIOStatus()
    assert status.speaker is False
    assert status.white_light is False


def test_with_api_response_on():
    response = {"params": {"status": {"Speaker": "On", "WhiteLight": "On"}}}
    status = CoaxialControlIOStatus(api_response=response)
    assert status.speaker is True
    assert status.white_light is True


def test_with_api_response_off():
    response = {"params": {"status": {"Speaker": "Off", "WhiteLight": "Off"}}}
    status = CoaxialControlIOStatus(api_response=response)
    assert status.speaker is False
    assert status.white_light is False
