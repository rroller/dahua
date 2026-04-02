# Codebase Information

## Project Overview

- **Name**: Dahua
- **Domain**: `dahua`
- **Version**: 0.9.81
- **Type**: Home Assistant Custom Integration (HACS)
- **Language**: Python
- **IoT Class**: Local Polling
- **Repository**: https://github.com/rroller/dahua
- **Maintainer**: @rroller

## Purpose

Custom Home Assistant integration for Dahua IP cameras, doorbells (VTO), NVRs, and DVRs. Also supports rebranded devices from Amcrest, Lorex, IMOU, EmpireTech, and Avaloid Goliath.

## Technology Stack

- **Runtime**: Home Assistant (Python async)
- **HTTP Client**: aiohttp with custom Digest Auth
- **Camera Protocol**: RTSP for streaming, HTTP CGI API for control
- **VTO Protocol**: Custom binary TCP protocol on port 5000 (RPC-style JSON over binary framing)
- **Authentication**: HTTP Digest Auth (cameras), MD5 challenge-response (VTO)
- **Installation**: HACS or manual copy to `custom_components/`

## Directory Structure

```
dahua/
├── custom_components/dahua/    # Main integration code
│   ├── __init__.py             # Entry point, coordinator, event handling
│   ├── client.py               # HTTP API client for Dahua CGI endpoints
│   ├── camera.py               # Camera entity + service registrations
│   ├── binary_sensor.py        # Event-driven binary sensors
│   ├── switch.py               # Motion detection, siren, disarming switches
│   ├── light.py                # Infrared, illuminator, flood, security lights
│   ├── select.py               # Doorbell light mode, PTZ preset selects
│   ├── button.py               # Placeholder (not yet implemented)
│   ├── config_flow.py          # UI-based setup and reauthentication flow
│   ├── const.py                # Constants, domain, platform list, icons
│   ├── entity.py               # Base entity class (device info, unique ID)
│   ├── dahua_utils.py          # Brightness conversion, event stream parsing
│   ├── digest.py               # Custom aiohttp Digest Auth implementation
│   ├── models.py               # Dataclass for coaxial control IO status
│   ├── rpc2.py                 # RPC2 JSON API client (alternative API)
│   ├── vto.py                  # VTO doorbell binary protocol client
│   ├── services.yaml           # HA service definitions
│   ├── manifest.json           # Integration manifest
│   ├── translations/           # UI translations (en, es, fr, it, etc.)
│   └── brand/                  # Brand icons and logos
├── tests/                      # Test directory (minimal)
├── scripts/                    # Dev scripts (develop, lint, setup)
├── config/                     # HA dev config (configuration.yaml)
├── .github/workflows/          # CI: push.yml, pull.yml
├── .devcontainer.json          # VS Code devcontainer config
├── requirements.txt            # Runtime dependencies
├── requirements_dev.txt        # Dev dependencies
├── requirements_test.txt       # Test dependencies
└── setup.cfg                   # Flake8 and isort config
```

## Supported Platforms

| Platform | Entity Type | Description |
|----------|-------------|-------------|
| `camera` | Camera | RTSP streaming, snapshots, service host |
| `binary_sensor` | BinarySensor | Event-driven sensors (motion, tripwire, doorbell, etc.) |
| `switch` | Switch | Motion detection, siren, disarming, smart motion |
| `light` | Light | Infrared, illuminator, flood light, security light, ring light |
| `select` | Select | Doorbell light mode, PTZ preset position |
| `button` | Button | Placeholder (not implemented) |

## CI/CD

- **Push to master/dev**: HACS validation, Hassfest validation, Black formatting, pytest
- **Pull requests**: Same pipeline
- **Linting**: flake8 (via setup.cfg), Black formatter, isort
- **Testing**: pytest with `pytest-homeassistant-custom-component`
