# Review Notes

## Consistency Check

### Issues Found

1. **VTO vs IP camera event key casing**: VTO events use capitalized keys (`Action`, `Data`, `Index`) while IP camera events use lowercase (`action`, `data`, `index`). The coordinator's `on_receive_vto_event` and `on_receive` methods handle this differently. This is documented in data_models.md but could be a source of bugs.

2. **`supports_ptz_position()` method has wrong implementation**: In `__init__.py`, `supports_ptz_position()` checks `table.Lighting_V2` data instead of PTZ-related data — it's a copy-paste of `supports_illuminator()`. The actual PTZ support flag `_supports_ptz_position` is set correctly during initialization but the public method doesn't use it.

3. **`button.py` is a placeholder**: Registered as a platform but `async_setup_entry` does nothing. No button entities are created.

4. **`rpc2.py` appears unused**: `DahuaRpc2Client` is defined but not imported or used by the coordinator or any entity. It may be intended for future use or specific device types.

5. **`models.py` appears unused in main flow**: `CoaxialControlIOStatus` is only imported by `rpc2.py`, which itself appears unused.

## Completeness Check

### Gaps Identified

1. **Test coverage is minimal**: Only `test_api.py` and `conftest.py` exist with minimal content. No tests for coordinator, entities, event parsing, or VTO protocol.

2. **No error handling documentation**: The codebase has extensive try/except patterns for feature detection but no documentation on error recovery strategies or known failure modes beyond the README's "Known Issues" section.

3. **NVR multi-channel behavior**: The code has comments about NVR channel handling (creating one thread per channel, all getting same events) but the workaround (filtering by channel index) and its limitations aren't well documented.

4. **Amcrest/Lorex/IMOU device differences**: The code has many model-specific branches (e.g., `is_amcrest_doorbell()`, `is_flood_light()`, NVR-specific night mode switching) but there's no consolidated guide on which features work with which device families.

5. **Profile mode semantics**: Profile mode values (0=day, 1=night, 2=scene) are scattered across comments but not centrally documented.

6. **SSL handling**: Both `__init__.py` and `config_flow.py` create SSL contexts that disable certificate verification. The security implications aren't documented.

7. **`button.py` platform**: Listed in `PLATFORMS` but has no implementation. Could confuse contributors.

## Recommendations

1. **Fix `supports_ptz_position()`** — It currently returns illuminator support status instead of PTZ support.
2. **Remove or implement `button.py`** — Either add button entities or remove from PLATFORMS to avoid confusion.
3. **Add integration tests** — Especially for event parsing (`parse_event`), VTO protocol parsing, and coordinator initialization.
4. **Consolidate device-specific logic** — Create a device capabilities matrix or registry instead of scattered model string checks.
5. **Document the RPC2 client** — Clarify if it's actively used, planned for future use, or deprecated.
6. **Standardize event key casing** — Consider normalizing VTO events to match IP camera event format in the coordinator.
