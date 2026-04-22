# Interfaces

## Dahua CGI HTTP API

All requests use HTTP GET with Digest Authentication. Responses are `key=value` text parsed into dicts.

### System APIs (`/cgi-bin/magicBox.cgi`)

| Action | Description |
|--------|-------------|
| `getSystemInfo` | Device type, serial number, hardware version |
| `getDeviceType` | Device model |
| `getSoftwareVersion` | Firmware version |
| `getMachineName` | Device name |
| `getVendor` | Manufacturer |
| `getProductDefinition&name=MaxExtraStream` | Max sub-streams |
| `reboot` | Reboot device |

### Configuration APIs (`/cgi-bin/configManager.cgi`)

| Action | Description |
|--------|-------------|
| `getConfig&name=MotionDetect` | Motion detection status |
| `getConfig&name=Lighting[ch][mode]` | IR light config |
| `getConfig&name=Lighting_V2` | White light / illuminator config |
| `getConfig&name=SmartMotionDetect` | Smart motion detection status |
| `getConfig&name=VideoAnalyseRule` | IVS rules |
| `getConfig&name=DisableLinkage` | Disarming linkage status |
| `getConfig&name=DisableEventNotify` | Event notification status |
| `getConfig&name=VideoInMode` | Day/night profile mode |
| `getConfig&name=General.MachineName` | Machine name |
| `getConfig&name=FloodLightMode.Mode` | Floodlight mode |
| `setConfig&MotionDetect[ch].Enable=bool` | Enable/disable motion |
| `setConfig&Lighting[ch][0].Mode=mode` | Set IR light mode |
| `setConfig&Lighting_V2[ch][pm][0].Mode=mode` | Set illuminator mode |
| `setConfig&VideoAnalyseRule[ch][idx].Enable=bool` | Enable/disable IVS rule |
| `setConfig&VideoWidget[ch].CustomTitle[g].Text=text` | Set text overlay |
| `setConfig&RecordMode[ch].Mode=mode` | Set record mode |
| `setConfig&VideoInDayNight[ch][cfg].Mode=mode` | Set day/night color mode |

### Coaxial Control (`/cgi-bin/coaxialControlIO.cgi`)

| Action | Description |
|--------|-------------|
| `getStatus&channel=1` | Speaker and white light status |
| `control&channel=ch&info[0].Type=type&info[0].IO=io` | Control speaker/light/siren |

Type values: `1` = security light, `2` = siren. IO values: `1` = on, `2` = off.

### PTZ Control (`/cgi-bin/ptz.cgi`)

| Action | Description |
|--------|-------------|
| `getStatus` | Current PTZ position and preset ID |
| `start&channel=ch&code=GotoPreset&arg2=pos` | Go to preset position |

### Event Stream (`/cgi-bin/eventManager.cgi`)

| Action | Description |
|--------|-------------|
| `attach&codes=[events]&heartbeat=5` | Long-polling event stream |

Response format: `--myboundary` delimited chunks with `Code=X;action=Y;index=Z;data={json}`.

### Snapshot (`/cgi-bin/snapshot.cgi`)

| Action | Description |
|--------|-------------|
| `channel=N` | Capture JPEG snapshot |

## VTO Binary Protocol (TCP Port 5000)

Binary framing with DHIP header (32 bytes) + JSON payload.

### Methods

| Method | Description |
|--------|-------------|
| `global.login` | Two-phase MD5 challenge-response login |
| `global.keepAlive` | Keep connection alive |
| `eventManager.attach` | Subscribe to all events |
| `configManager.getConfig` | Get configuration |
| `magicBox.getSoftwareVersion` | Get firmware version |
| `magicBox.getDeviceType` | Get device type |
| `console.runCmd` | Run console command (e.g., cancel call) |

### Event Notification

Events arrive via `client.notifyEventStream` method with `eventList` array containing `Code`, `Action`, and `Data` fields.

## RPC2 JSON API (`/RPC2`)

Alternative JSON-RPC API via HTTP POST. Used by `DahuaRpc2Client`.

| Method | Description |
|--------|-------------|
| `global.login` | Two-phase login (same MD5 scheme) |
| `global.logout` | End session |
| `global.getCurrentTime` | Device time |
| `magicBox.getSerialNo` | Serial number |
| `configManager.getConfig` | Get configuration |
| `CoaxialControlIO.getStatus` | Coaxial IO status |

## Home Assistant Services

18 services registered on the camera entity platform. See `services.yaml` for full definitions.

Key services: `set_infrared_mode`, `set_video_profile_mode`, `set_text_overlay`, `enable_all_ivs_rules`, `vto_open_door`, `reboot`, `set_record_mode`, `goto_preset_position`.

## Home Assistant Events

| Event | Description |
|-------|-------------|
| `dahua_event_received` | Fired for every camera/VTO event. Contains `name`, `Code`, `action`, `index`, `data`, `DeviceName` |

## RTSP Streams

URL format: `rtsp://user:pass@host:port/cam/realmonitor?channel=N&subtype=S`

- Subtype 0 = Main stream
- Subtype 1 = Sub stream
- Subtype 3 = Direct RTSP (no path)
