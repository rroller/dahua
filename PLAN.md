# Integration Quality Scale

## Bronze Tier (19 rules) — All Pass ✓

| Rule | Status | Notes |
|------|--------|-------|
| action-setup | **Pass** | Services registered in `async_setup_entry` |
| appropriate-polling | **Pass** | 30s polling interval via `DataUpdateCoordinator` |
| brands | **Pass** | Brand assets in `custom_components/dahua/brand/` |
| common-modules | **Pass** | Shared code in `client.py`, `entity.py`, `models.py` |
| config-flow-test-coverage | **Pass** | Config flow tests added (PR #27) |
| config-flow | **Pass** | Full UI config flow with credential validation |
| dependency-transparency | **Pass** | No hidden dependencies; manifest lists none |
| docs-actions | **Pass** | 22 services documented in `services.yaml` |
| docs-high-level-description | **Pass** | README has overview |
| docs-installation-instructions | **Pass** | README has HACS install steps |
| docs-removal-instructions | **Pass** | Removal instructions added to README (PR #27) |
| entity-event-setup | **Pass** | Uses `async_added_to_hass()` for listener registration |
| entity-unique-id | **Pass** | Serial number + channel-based unique IDs |
| has-entity-name | **Pass** | `_attr_has_entity_name = True` set on base entity (PR #27) |
| runtime-data | **Pass** | Uses `ConfigEntry.runtime_data` (PR #27) |
| test-before-configure | **Pass** | Config flow tests credentials before saving |
| test-before-setup | **Pass** | `async_setup_entry` raises `ConfigEntryNotReady` on connection failure |
| unique-config-entry | **Pass** | Checks unique ID (serial number) to prevent duplicates |

## Silver Tier — All Pass ✓

| Rule | Status | Notes |
|------|--------|-------|
| action-exceptions | **Pass** | `dahua_command` decorator wraps all action methods with `HomeAssistantError` |
| config-entry-unloading | **Pass** | `async_unload_entry` implemented |
| docs-configuration-parameters | **Pass** | Configuration Options section added to README |
| docs-installation-parameters | **Pass** | All setup fields including Channel documented in README |
| entity-unavailable | **Pass** | Coordinator pattern handles unavailability |
| integration-owner | **Pass** | `@rroller` listed as codeowner |
| log-when-unavailable | **Pass** | Coordinator logs update failures |
| parallel-updates | **Pass** | `PARALLEL_UPDATES` set on all platform files |
| reauthentication-flow | **Pass** | Reauth flow implemented |
| test-coverage | **Pass** | 95% coverage (PR #31) |

## Gold Tier

| Rule | Status | Notes |
|------|--------|-------|
| diagnostics | **Pass** | `diagnostics.py` with credential redaction (PR #32) |
| entity-category | **Pass** | `EntityCategory.CONFIG` on config switches (PR #32) |
| entity-device-class | **Pass** | `BinarySensorDeviceClass` enums (PR #32) |
| entity-disabled-by-default | **Pass** | Preset position select disabled by default (PR #32) |
| devices | **Pass** | `serial_number` in DeviceInfo (PR #32) |
| stale-devices | **Pass** | `async_remove_config_entry_device` (PR #32) |
| reconfiguration-flow | **Pass** | `async_step_reconfigure` (PR #32) |
| entity-translations | **Pass** | `_attr_translation_key` + `strings.json` (PR #32) |
| exception-translations | **Pass** | Translation keys on `HomeAssistantError` raises (PR #32) |
| icon-translations | **Pass** | `icons.json` replaces hardcoded icons (PR #32) |
| discovery | **Exempt** | Dahua cameras don't advertise via standard HA discovery |
| discovery-update-info | **Exempt** | Discovery not implemented |
| dynamic-devices | **Exempt** | Each config entry = 1 device; no dynamic sub-devices |
| repair-issues | **Exempt** | No actionable repair scenarios beyond reauth (already handled) |
| docs-data-update | **Pass** | Polling (30s) + event push documented in README |
| docs-examples | **Pass** | Automation examples in README |
| docs-known-limitations | **Pass** | Known limitations section in README |
| docs-supported-devices | **Pass** | Tested models listed in README |
| docs-supported-functions | **Pass** | All entities/services documented in README |
| docs-troubleshooting | **Pass** | Troubleshooting section in README |
| docs-use-cases | **Pass** | Use cases section in README |

## Platinum Tier (for future reference)

| Rule | Status | Notes |
|------|--------|-------|
| async-dependency | **Partial** | Uses aiohttp but has legacy `thread.py` |
| inject-websession | **Fail** | Creates own `aiohttp.ClientSession` |
| strict-typing | **Fail** | No strict mypy config |
