# Dependencies

## Runtime Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `homeassistant` | ~=2025.1.2 | Home Assistant core framework |
| `ha-ffmpeg` | 3.2.2 | FFmpeg integration for video streaming |
| `colorlog` | 6.8.2 | Colored logging output |
| `pip` | >=24.3.1 | Package installer |
| `ruff` | 0.7.3 | Linter (listed in requirements but used for dev) |

## Home Assistant Dependencies (from manifest.json)

| Dependency | Relationship | Purpose |
|------------|-------------|---------|
| `tag` | `after_dependencies` | NFC tag scanning for VTO access control cards |

## Key HA Framework Components Used

| Component | Usage |
|-----------|-------|
| `homeassistant.helpers.update_coordinator.DataUpdateCoordinator` | Polling coordinator pattern |
| `homeassistant.components.camera.Camera` | Camera entity base |
| `homeassistant.components.binary_sensor.BinarySensorEntity` | Binary sensor base |
| `homeassistant.components.switch.SwitchEntity` | Switch entity base |
| `homeassistant.components.light.LightEntity` | Light entity base |
| `homeassistant.components.select.SelectEntity` | Select entity base |
| `homeassistant.components.tag.async_scan_tag` | NFC tag scanning |
| `homeassistant.config_entries.ConfigFlow` | UI config flow |
| `homeassistant.helpers.entity_platform` | Service registration |
| `aiohttp.ClientSession` | Async HTTP client |
| `voluptuous` | Service parameter validation |

## Third-Party Libraries

| Library | Usage |
|---------|-------|
| `aiohttp` | HTTP client for camera API communication |
| `async_timeout` | Request timeout management |
| `requests` | `HTTPDigestAuth` imported in VTO client (type hint only) |
| `yarl` | URL parsing in digest auth |

## Development Dependencies

| Package | Purpose |
|---------|---------|
| `homeassistant` | Dev environment |

## Test Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `pytest-homeassistant-custom-component` | 0.13.286 | HA custom component test framework |
| `pycares` | >=4.0.0,<5.0.0 | Async DNS resolver (test dependency) |

## External Services

| Service | Protocol | Port | Purpose |
|---------|----------|------|---------|
| Dahua CGI API | HTTP/HTTPS | 80/443 | Device control and configuration |
| RTSP Stream | RTSP | 554 | Live video streaming |
| VTO Protocol | TCP | 5000 | Doorbell event streaming and control |
| RPC2 API | HTTP/HTTPS | 80/443 | Alternative JSON-RPC API |

## HACS Requirements

| Field | Value |
|-------|-------|
| HACS version | >=1.6.0 |
| Home Assistant | >=2025.1.2 |
