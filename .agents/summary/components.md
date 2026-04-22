# Components

## DahuaDataUpdateCoordinator (`__init__.py`)

Central coordinator managing all device communication and state.

**Responsibilities:**
- Device initialization and feature detection
- Periodic state polling (30s interval)
- Event stream management (IP camera and VTO)
- Event listener registry and dispatch
- Session lifecycle management

**Key State:**
- `self.data`: Dict of all polled device state (motion detection, lighting, profiles, etc.)
- `self._dahua_event_listeners`: Dict mapping event keys to HA callbacks
- `self._dahua_event_timestamp`: Dict mapping event keys to fire/clear timestamps
- Feature flags: `_supports_coaxial_control`, `_supports_lighting`, `_supports_lighting_v2`, etc.

**Event Translation:**
- `CrossLineDetection`/`CrossRegionDetection` with `ObjectType=human` → also dispatches `SmartMotionHuman`
- `BackKeyLight`/`PhoneCallDetect` → translated to `DoorbellPressed`

## DahuaClient (`client.py`)

HTTP API client wrapping Dahua's CGI-based HTTP API.

**Responsibilities:**
- All HTTP communication with the camera (GET requests with Digest Auth)
- RTSP stream URL generation
- API response parsing (key=value text → dict)
- Event stream consumption (long-polling with `--myboundary` chunked responses)

**Key Endpoints:**
- `/cgi-bin/magicBox.cgi` — System info, device type, reboot, serial number
- `/cgi-bin/configManager.cgi` — Get/set configuration (motion, lighting, IVS rules, etc.)
- `/cgi-bin/coaxialControlIO.cgi` — Coaxial control (speaker, white light, siren)
- `/cgi-bin/eventManager.cgi` — Event stream attachment
- `/cgi-bin/snapshot.cgi` — Camera snapshots
- `/cgi-bin/ptz.cgi` — PTZ control and status

## DahuaVTOClient (`vto.py`)

Binary TCP protocol client for VTO doorbell devices.

**Responsibilities:**
- TCP connection on port 5000
- DHIP binary protocol framing (32-byte header + JSON payload)
- MD5 challenge-response authentication
- Event stream via `eventManager.attach`
- Keep-alive management
- Access control and door operations

**Protocol:** Messages are JSON objects wrapped in a binary header with magic bytes `0x20000000 DHIP`.

## Camera Entity (`camera.py`)

HA camera entity providing streaming and snapshot capabilities, plus hosting most service registrations.

**Responsibilities:**
- RTSP stream source for live viewing
- Snapshot capture
- Motion detection enable/disable
- Host for 18+ registered services (infrared mode, video profile, overlays, IVS rules, PTZ, reboot, etc.)

**Streams:** Creates one entity per stream (1 main + N sub-streams, typically 3 total).

## Binary Sensor Entity (`binary_sensor.py`)

Event-driven binary sensors that turn on/off based on camera events.

**Responsibilities:**
- One sensor per selected event type (VideoMotion, CrossLineDetection, etc.)
- Doorbell-specific sensors auto-added (DoorbellPressed, Invite, DoorStatus, CallNoAnswered)
- Push-based state updates (no polling) via coordinator event listeners

**Name Generation:** Event names are converted from CamelCase to spaced words (e.g., `SmartMotionHuman` → "Smart Motion Human") with overrides for common events.

## Switch Entity (`switch.py`)

Toggle switches for camera features.

**Entities:**
- Motion Detection — enable/disable motion detection
- Siren — trigger camera siren (auto-off after ~10-15s)
- Smart Motion Detection — enable/disable smart motion (Dahua and Amcrest variants)
- Disarming Linkage — enable/disable disarming feature
- Event Notifications — enable/disable event notifications when disarmed

## Light Entity (`light.py`)

Light controls for various camera illumination features.

**Entities:**
- `DahuaInfraredLight` — IR light with brightness control (Lighting V1 API)
- `DahuaIlluminator` — White light with brightness control (Lighting V2 API)
- `FloodLight` — Flood light for Amcrest/Dahua flood cameras (coaxial or V2 API)
- `DahuaSecurityLight` — Red/blue flashing alarm light (coaxial API)
- `AmcrestRingLight` — Blue ring light on Amcrest doorbells (LightGlobal API)

## Select Entity (`select.py`)

Dropdown selectors for multi-option features.

**Entities:**
- `DahuaDoorbellLightSelect` — Doorbell security light mode (Off/On/Strobe)
- `DahuaCameraPresetPositionSelect` — PTZ preset position (Manual/1-10)

## Config Flow (`config_flow.py`)

UI-based setup and configuration flow.

**Steps:**
1. `user` — Credentials, address, port, channel, event selection
2. `name` — Device name (pre-populated from camera)
3. `reauth_confirm` — Re-authentication when credentials expire

**Options Flow:** Toggle individual platforms on/off (binary_sensor, switch, light, camera, select).

## Supporting Modules

| Module | Purpose |
|--------|---------|
| `entity.py` | Base entity with device info and unique ID |
| `const.py` | Constants: domain, platforms, config keys, icons |
| `dahua_utils.py` | Brightness conversion, event stream text parsing |
| `digest.py` | Custom aiohttp Digest Auth (aiohttp lacks native support) |
| `models.py` | `CoaxialControlIOStatus` dataclass |
| `rpc2.py` | Alternative RPC2 JSON API client (used by some devices) |
