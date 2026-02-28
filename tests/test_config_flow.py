"""Tests for the Dahua config flow."""

from unittest.mock import patch

from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

# Importing the config_flow module registers the flow handler with HA
from custom_components.dahua import config_flow as _  # noqa: F401
from custom_components.dahua.const import (
    CONF_ADDRESS,
    CONF_CHANNEL,
    CONF_EVENTS,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_RTSP_PORT,
    CONF_USERNAME,
    DOMAIN,
)

MOCK_USER_INPUT = {
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "password123",
    CONF_ADDRESS: "192.168.1.100",
    CONF_PORT: "80",
    CONF_RTSP_PORT: "554",
    CONF_CHANNEL: 0,
    CONF_EVENTS: ["VideoMotion", "CrossLineDetection"],
}

MOCK_DEVICE_DATA = {
    "name": "TestCamera",
    "serialNumber": "ABC123456",
}


async def test_user_flow_success(hass: HomeAssistant):
    """Test a successful config flow from the user step through the name step."""
    with patch(
        "custom_components.dahua.config_flow.DahuaFlowHandler._test_credentials",
        return_value=MOCK_DEVICE_DATA,
    ):
        # Step 1: show user form
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "user"

        # Step 2: submit credentials
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_USER_INPUT,
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "name"

        # Step 3: submit name
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_NAME: "Front Porch"},
        )
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert result["title"] == "Front Porch"
        assert result["data"][CONF_USERNAME] == "admin"
        assert result["data"][CONF_ADDRESS] == "192.168.1.100"


async def test_user_flow_invalid_credentials(hass: HomeAssistant):
    """Test config flow with invalid credentials shows an error."""
    with patch(
        "custom_components.dahua.config_flow.DahuaFlowHandler._test_credentials",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_USER_INPUT,
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["errors"]["base"] == "auth"


async def test_user_flow_duplicate_device(hass: HomeAssistant):
    """Test config flow aborts when the device is already configured."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_USER_INPUT,
        title="Existing Camera",
        unique_id="ABC123456",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.dahua.config_flow.DahuaFlowHandler._test_credentials",
        return_value=MOCK_DEVICE_DATA,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_USER_INPUT,
        )
        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant):
    """Test the options flow allows toggling platforms."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_USER_INPUT,
        title="Test Camera",
        unique_id="ABC123456",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "binary_sensor": True,
            "camera": True,
            "light": False,
            "select": True,
            "switch": True,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"]["light"] is False
    assert result["data"]["camera"] is True


async def test_reauth_flow_success(hass: HomeAssistant):
    """Test reauthentication flow with valid new credentials."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_USER_INPUT,
        title="Test Camera",
        unique_id="ABC123456",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.dahua.config_flow.DahuaFlowHandler._test_credentials",
        return_value=MOCK_DEVICE_DATA,
    ), patch.object(hass.config_entries, "async_reload"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_USERNAME: "admin",
                CONF_PASSWORD: "newpassword",
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"


async def test_reauth_flow_invalid_credentials(hass: HomeAssistant):
    """Test reauthentication flow with invalid credentials shows error."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_USER_INPUT,
        title="Test Camera",
        unique_id="ABC123456",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.dahua.config_flow.DahuaFlowHandler._test_credentials",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_USERNAME: "admin",
                CONF_PASSWORD: "wrongpassword",
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        assert result["errors"]["base"] == "auth"


async def test_user_flow_with_channel_gt_0(hass: HomeAssistant):
    """Test config flow with channel > 0 appends channel to unique_id."""
    input_with_channel = {**MOCK_USER_INPUT, CONF_CHANNEL: 1}

    with patch(
        "custom_components.dahua.config_flow.DahuaFlowHandler._test_credentials",
        return_value=MOCK_DEVICE_DATA,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=input_with_channel,
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "name"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_NAME: "NVR Channel 1"},
        )
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert result["title"] == "NVR Channel 1"


async def test_reconfigure_flow_success(hass: HomeAssistant):
    """Test reconfigure flow updates address and reloads."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_USER_INPUT, CONF_NAME: "TestCamera"},
        title="TestCamera",
        unique_id="ABC123456",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.dahua.config_flow.DahuaFlowHandler._test_credentials",
        return_value=MOCK_DEVICE_DATA,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_ADDRESS: "192.168.1.200",
                CONF_PORT: "8080",
                CONF_RTSP_PORT: "554",
                CONF_CHANNEL: 0,
                CONF_EVENTS: ["VideoMotion"],
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_ADDRESS] == "192.168.1.200"
        assert entry.data[CONF_PORT] == "8080"


async def test_reconfigure_flow_invalid_credentials(hass: HomeAssistant):
    """Test reconfigure flow shows error when credentials fail."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_USER_INPUT, CONF_NAME: "TestCamera"},
        title="TestCamera",
        unique_id="ABC123456",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.dahua.config_flow.DahuaFlowHandler._test_credentials",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_ADDRESS: "192.168.1.200",
                CONF_PORT: "80",
                CONF_RTSP_PORT: "554",
                CONF_CHANNEL: 0,
                CONF_EVENTS: ["VideoMotion"],
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reconfigure"
        assert result["errors"]["base"] == "auth"


async def test_reconfigure_flow_unique_id_mismatch(hass: HomeAssistant):
    """Test reconfigure flow aborts when device serial number changes."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_USER_INPUT, CONF_NAME: "TestCamera"},
        title="TestCamera",
        unique_id="DIFFERENT_SERIAL",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.dahua.config_flow.DahuaFlowHandler._test_credentials",
        return_value=MOCK_DEVICE_DATA,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_ADDRESS: "192.168.1.200",
                CONF_PORT: "80",
                CONF_RTSP_PORT: "554",
                CONF_CHANNEL: 0,
                CONF_EVENTS: ["VideoMotion"],
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "unique_id_mismatch"
