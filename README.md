# Home Assistant Dahua Integration
The `Dahua` [Home Assistant](https://www.home-assistant.io) integration allows you to integrate your [Dahua](https://www.dahuasecurity.com/) cameras, doorbells, NVRs, DVRs in Home Assistant. It's also confirmed to work with some Lorex cameras and Amcrest devices.

Supports motion events, alarm events (and others), enabling/disabling motion detection, switches for infrared, illuminator (white light), security lights (red/blue flashers), sirens, doorbell button press events, and more.

Also exposes several services to enable/disable motion detection or set the text overlay on the video.

**NOTE**: Using the switch to turn on/off the infrared light will disable the "auto" mode. Use the service to enable auto mode again (or the camera UI).

## Use Cases

- **Security monitoring**: Get real-time motion, intrusion, and alarm events pushed to Home Assistant. Trigger automations to record video, send notifications, or turn on lights when motion is detected.
- **Doorbell automation**: Detect doorbell presses and trigger notifications, unlock doors via VTO, or play announcements. Integrate with other smart home devices for a complete visitor management workflow.
- **Night vision control**: Automate infrared and illuminator lights based on time of day, presence, or ambient light conditions.
- **PTZ preset positions**: Move cameras to preset positions on a schedule or in response to events (e.g., point at the driveway when the garage opens).
- **Video overlay management**: Dynamically update text overlays on camera feeds (e.g., show current temperature, occupancy count, or alarm status).
- **Multi-camera NVR setups**: Monitor multiple cameras connected to a single NVR, each as a separate channel with independent events and controls.

## How Data is Updated

The integration uses two methods to keep entity states current:

- **Polling (every 30 seconds)**: A `DataUpdateCoordinator` polls the camera's HTTP API to fetch motion detection status, lighting configuration, disarming linkage state, profile mode, PTZ position, and other settings. This ensures all entity states stay in sync even if an event is missed.
- **Event streaming (real-time)**: The integration maintains a persistent HTTP connection to the camera's event manager API (`eventManager.cgi?action=attach`) to receive events like motion detection, cross-line detection, and alarms as they happen. For VTO doorbells, a separate TCP connection on port 5000 streams doorbell press, door status, and call events. Events are fired on the Home Assistant event bus as `dahua_event_received` and immediately update binary sensor states.

Why not use the Amcrest integration already provided by Home Assistant? The Amcrest integration is missing features that this integration provides and I want an integration that is branded as Dahua. Amcrest are rebranded Dahua cams. With this integration living outside of HA, it can be developed faster and released more often. HA has release schedules and rigerous review processes which I'm not ready for while developing this integration. Once this integration is mature I'd like to move it into HA directly.

## Installation

If you want live-streaming, make sure to add the following to your config.yaml:
```
ffmpeg:
```
See [ffmpeg](https://www.home-assistant.io/integrations/ffmpeg/) and [stream](https://www.home-assistant.io/integrations/stream/).


### HACS install
To install with [HACS](https://hacs.xyz/):

1. Click on HACS in the Home Assistant menu
2. Click on `Integrations`
3. Click the `EXPLORE & ADD REPOSITORIES` button
4. Search for `Dahua`
5. Click the `INSTALL THIS REPOSITORY IN HACS` button
6. Restart Home Assistant
7. Configure the camera by going to `Configurations` -> `Integrations` -> `ADD INTERATIONS` button, search for `Dahua` and configure the camera.

### Manual install
To manually install:

```bash
# Download a copy of this repository
$ wget https://github.com/rroller/dahua/archive/dahua-main.zip

# Unzip the archive
$ unzip dahua-main.zip

# Move the dahua directory into your custom_components directory in your Home Assistant install
$ mv dahua-main/custom_components/dahua <home-assistant-install-directory>/config/custom_components/
```

> :warning: **After executing one of the above installation methods, restart Home Assistant. Also clear your browser cache before proceeding to the next step, as the integration may not be visible otherwise.**

### Setup
1. Now the integration is added to HACS and available in the normal HA integration installation, so...
2. In the HA left menu, click `Configuration`
3. Click `Integrations`
4. Click `ADD INTEGRATION`
5. Type `Dahua` and select it
6. Enter the details:
    1. **Username**: Your camera's username
    2. **Password**: Your camera's password
    3. **Address**: Your camera's address, typically just the IP address
    4. **Port**: Your camera's HTTP port. Default is `80`
    5. **RTSP Port**: Your camera's RTSP port, default is `554`. Used to live stream your camera in HA
    6. **Channel**: The camera channel index (0-based). Standalone cameras use `0`. For NVRs, each camera is a separate channel. Add a separate integration entry per channel.
    7. **Events**: The integration will keep a connection open to the camera to capture motion events, alarm events, etc.
       You can select which events you want to monitor and report in HA. If no events are selected then the connection will no be created.
       If you want a specific event that's not listed here open an issue and I'll add it.

NOTE: All streams will be added, even if not enabled in the camera. Just remove the ones you don't want.

![Dahua Setup](static/setup1.png)

### Removal
To remove the integration:

1. In the HA left menu, click `Settings`
2. Click `Devices & services`
3. Find the `Dahua` integration and click on it
4. Click the three-dot menu on the device you want to remove and select `Delete`

If you also want to remove the integration files from HACS:

1. Click on HACS in the Home Assistant menu
2. Click on `Integrations`
3. Find `Dahua`, click the three-dot menu, and select `Remove`
4. Restart Home Assistant

### Configuration Options

After adding the integration you can adjust which entity platforms are enabled by clicking
`CONFIGURE` on the integration card. The following platforms can be toggled on or off:

Option | Description | Default
:------------ | :------------ | :-------------
`binary_sensor` | Motion, alarm, and doorbell event sensors | Enabled
`camera` | Camera entities with live streaming and snapshots | Enabled
`light` | Infrared, illuminator, flood light, and security light controls | Enabled
`select` | Preset position and doorbell light mode selectors | Enabled
`switch` | Motion detection, siren, disarming, and smart motion detection toggles | Enabled


# Known supported cameras
This integration should word with most Dahua cameras and doorbells. It has been tested with very old and very new Dahua cameras.

Doorbells will have a binary sensor that captures the doorbell pressed event.

* **Please let me know if you've tested with additional cameras**

These devices are confirmed as working:

## Dahua cameras


## Dahua cameras

Series | 2 Megapixels | 4 Megapixels | 5 Megapixels | 8 Megapixels
:------------ | :------------ | :------------ | :------------- | :-------------
| *Consumer Series* |
| | A26 |  |  |
| *1-Series* |
| | HFW1230S | HFW1435S-W |  |
| | HDBW1230E-S2 | HFW1435S-W-S2  |  |
| | |HDBW1431EP-S-0360B | |
| *2-/3-Series* |
| | HDW2831T-ZS-S2 | HDW2431TP-AS | HDW3549HP-AS-PV | HDW3849HP-AS-PV
| | HDBW2231FP-AS-0280B-S2 | HFW2449S-S-IL
| | | HFW3441E-AS-S2
| *4-/5-Series* |
| | HDW4231EM-ASE | HFW4433F-ZSA |  | HDW5831R-ZE
| | HDBW4231F-AS | HDBW5421E-Z |  |
| | HDW4233C-A | T5442T-ZE |
| | HDBW4239R-ASE | T5442TM-AS |
| | | B5442E-Z4E |
| | | B54IR-ASE |
| | HDBW4239RP-ASE |
| *6-/7-Series* |
| | HDPW7564N-SP |
| *Panoramic Series* |
| |  |  | EW5531-AS | 

## Other brand cameras

Brand | 2 Megapixels | 4 Megapixels | 5 Megapixels | 8 Megapixels
:------------ | :------------ | :------------ | :------------- | :-------------
| *Amcrest* |
| | | | Amcrest IP5M-T1179E | Amcrest IPC-Color4K-T
| *IMOU* |
| | IMOU IPC-A26Z / Ranger Pro Z | | IMOU DB61i
| | IMOU IPC-C26E-V2 <sup>*</sup> |
| | IMOU IPC-K22A / Cube PoE-322A |
| *Lorex* |
| | | | | Lorex E891AB
| | | | | Lorex LNB8005-C
| | | | | Lorex LNE8964AB

<sup>*</sup> partial support

## Doorbell cameras

Brand | 2 Megapixels | 4 Megapixels | 5 Megapixels | 8 Megapixels
:------------ | :------------ | :------------ | :------------- | :-------------
| *Amcrest* |
| | Amcrest AD110 | Amcrest AD410
| *Dahua* |
| | DHI-VTO2202F-P |
| | DHI-VTO2211G-P |
| | DHI-VTO3311Q-WP |
| *IMOU* |
| | IMOU C26EP-V2 | IMOU IPC-K46 | IMOU DB61i

# Known Limitations

* **iMou / cloud-only devices**: Devices that require the iMou cloud service (and don't expose a local HTTP API) are not supported. See [issue #6](https://github.com/rroller/dahua/issues/6) for details.
* **IPC-D2B20-ZS**: This model doesn't work directly. It needs a [wrapper](https://gist.github.com/gxfxyz/48072a72be3a169bc43549e676713201) — see [#7](https://github.com/bp2008/DahuaSunriseSunset/issues/7#issuecomment-829513144), [#8](https://github.com/mcw0/Tools/issues/8#issuecomment-830669237).
* **Firmware quirks**: Some older firmwares use non-standard channel numbering (channel 0 instead of channel 1). The integration auto-detects this, but if entities show incorrect data, try changing the channel setting.
* **Auto mode vs manual control**: Turning infrared or illuminator lights on/off via the switch disables the camera's "auto" mode. Use the `set_infrared_mode` service with `mode: Auto` to restore automatic control.
* **Siren duration**: Camera sirens typically auto-disable after 10-15 seconds. The siren switch will show as "on" briefly before the camera turns it off.
* **Preset position select**: The preset position entity is disabled by default since most cameras don't use PTZ presets. Enable it in the entity settings if needed.
* **Entity detection**: The integration tries to detect which features your device supports, but sometimes adds entities for unsupported features. Simply disable any entities that don't work with your device.
* **Discovery**: Dahua cameras do not advertise via standard Home Assistant discovery protocols (SSDP, Zeroconf, DHCP). Cameras must be added manually via the config flow.

# Troubleshooting

## Camera won't connect
* Verify the camera is reachable by opening `http://<camera-ip>` in a browser.
* Ensure the username and password are correct. Try logging into the camera's web UI with the same credentials.
* Check that the HTTP port (default 80) matches what the camera is configured to use.
* For HTTPS cameras, the integration accepts self-signed certificates automatically.

## Events not firing
* Make sure you selected the events you want when setting up the integration. You can change them by removing and re-adding the integration, or using the reconfigure flow.
* Open **Developer Tools -> Events** in Home Assistant, listen for `dahua_event_received`, and trigger an event (e.g., walk in front of the camera) to verify events are being received.
* Enable debug logging (see below) to see event stream connection status.
* Some events (e.g., CrossLineDetection, SmartMotionHuman) require IVS rules to be configured in the camera's own UI first.

## Entities show unavailable
* This usually means the camera is unreachable or returned an error. Check your network connection to the camera.
* If the camera requires re-authentication, you'll see a notification in Home Assistant. Use the reauth flow to update credentials.
* Restart the integration by going to **Settings -> Devices & services -> Dahua** and clicking **Reload**.

## Streams not loading
* Ensure the RTSP port (default 554) is correct and accessible.
* Make sure `ffmpeg` is configured in your `configuration.yaml` (see Installation section).
* Try accessing the RTSP stream directly: `rtsp://user:pass@camera-ip:554/cam/realmonitor?channel=1&subtype=0`

## NVR setup
* Each NVR channel must be added as a separate integration entry. Use the channel index (0-based) when configuring.
* Channel 0 is the first camera, channel 1 is the second, and so on.

## Debug logging
Add to your `configuration.yaml` and restart:
```yaml
logger:
  default: info
  logs:
    custom_components.dahua: debug
```

# Events
Events are streamed from the device and fired on the Home Assistant event bus.

Here's example event data:

```json
{
    "event_type": "dahua_event_received",
    "data": {
        "name": "Cam13",
        "Code": "VideoMotion",
        "action": "Start",
        "index": "0",
        "data": {
            "Id": [
                0
            ],
            "RegionName": [
                "Region1"
            ],
            "SmartMotionEnable": false
        },
        "DeviceName": "Cam13"
    },
    "origin": "LOCAL",
    "time_fired": "2021-06-30T04:00:28.605290+00:00",
    "context": {
        "id": "199542fe3f404f2a0a81031ee495bdd1",
        "parent_id": null,
        "user_id": null
    }
}
```

And here's how you configure and event trigger in an automation:
```yaml
platform: event
event_type: dahua_event_received
event_data:
  name: Cam13
  Code: VideoMotion
  action: Start
```

And that's it! You can enable debug logging (See at the end of this readme) to print out events to the Home Assisant log
as they fire. That can help you understand the events. Or you can HA and open Developer Tools -> Events -> and under
"Listen to events" enter `dahua_event_received` and then click "Start Listening" and wait for events to fire (you might
need to walk in front of your cam to make motion events fire, or press a button, etc)

## Example Code Events
| Code | Description |
| ----- | ----------- |
| BackKeyLight    | Unit Events, See Below States |
| VideoMotion     | motion detection event |
| VideoLoss  | video loss detection event |
| VideoBlind     | video blind detection event |
| AlarmLocal     | alarm detection event |
| CrossLineDetection     | tripwire event |
| CrossRegionDetection     | intrusion event |
| LeftDetection     | abandoned object detection |
| TakenAwayDetection     | missing object detection |
| VideoAbnormalDetection    | scene change event |
| FaceDetection    | face detect event |
| AudioMutation    | intensity change |
| AudioAnomaly    | input abnormal |
| VideoUnFocus    | defocus detect event |
| WanderDetection    | loitering detection event |
| RioterDetection    | People Gathering event |
| ParkingDetection    | parking detection event |
| MoveDetection    | fast moving event |
| MDResult    | motion detection data reporting event. The motion detect window contains 18 rows and 22 columns. The event info contains motion detect data with mask of every row |
| HeatImagingTemper    | temperature alarm event |

## Example Automations

### Send a notification on motion detection
```yaml
automation:
  - alias: "Camera motion notification"
    trigger:
      - platform: state
        entity_id: binary_sensor.front_porch_motion_alarm
        to: "on"
    action:
      - action: notify.mobile_app_phone
        data:
          title: "Motion Detected"
          message: "Motion detected on Front Porch camera"
          data:
            image: /api/camera_proxy/camera.front_porch_main
```

### Turn on lights when motion is detected at night
```yaml
automation:
  - alias: "Driveway motion lights"
    trigger:
      - platform: state
        entity_id: binary_sensor.driveway_cam_motion_alarm
        to: "on"
    condition:
      - condition: sun
        after: sunset
        before: sunrise
    action:
      - action: light.turn_on
        target:
          entity_id: light.driveway_flood_light
      - delay: "00:05:00"
      - action: light.turn_off
        target:
          entity_id: light.driveway_flood_light
```

### Doorbell press notification with snapshot
```yaml
automation:
  - alias: "Doorbell pressed"
    trigger:
      - platform: state
        entity_id: binary_sensor.front_door_button_pressed
        to: "on"
    action:
      - action: camera.snapshot
        target:
          entity_id: camera.front_door_main
        data:
          filename: /config/www/doorbell_snapshot.jpg
      - action: notify.mobile_app_phone
        data:
          title: "Doorbell"
          message: "Someone is at the front door"
          data:
            image: /local/doorbell_snapshot.jpg
```

### Move PTZ camera to preset on event
```yaml
automation:
  - alias: "Point camera at gate when opened"
    trigger:
      - platform: state
        entity_id: binary_sensor.gate_sensor
        to: "on"
    action:
      - action: dahua.goto_preset_position
        target:
          entity_id: camera.backyard_main
        data:
          position: 2
```

### Listen for raw events on the event bus
```yaml
automation:
  - alias: "Log all Dahua events"
    trigger:
      - platform: event
        event_type: dahua_event_received
        event_data:
          Code: CrossLineDetection
          action: Start
    action:
      - action: notify.persistent_notification
        data:
          title: "Tripwire Alert"
          message: "Cross-line detection triggered on {{ trigger.event.data.name }}"
```

## BackKeyLight States
| State | Description |
| ----- | ----------- |
| 0     | OK, No Call/Ring |
| 1, 2  | Call/Ring |
| 4     | Voice message |
| 5     | Call answered from VTH |
| 6     | Call **not** answered |
| 7     | VTH calling VTO |
| 8     | Unlock |
| 9     | Unlock failed |
| 11    | Device rebooted |

# Supported Functions

The integration creates entities and services based on what your device supports. Not all devices support all features — the integration probes the camera at setup time and adds entities accordingly. You can disable any entities that don't apply to your device.

## Entities

### Camera
Entity | Description | Added when
:--- | :--- | :---
Camera (Main) | Main stream with live view, snapshots, and RTSP streaming | Always
Camera (Sub) | Sub stream(s) with lower resolution | Always (one per sub-stream)

### Binary Sensors
Entity | Description | Device class | Added when
:--- | :--- | :--- | :---
Motion alarm | Motion detection event | `motion` | VideoMotion event selected
Cross line alarm | Tripwire / cross-line event | `motion` | CrossLineDetection event selected
Button pressed | Doorbell button press | `sound` | Doorbell devices
Door status | Door open/close state | `door` | Doorbell devices
Smart Motion Human | Human detected by smart motion | `motion` | SmartMotionHuman event selected
Smart Motion Vehicle | Vehicle detected by smart motion | `motion` | SmartMotionVehicle event selected
Other events | One sensor per selected event type | `motion` (default) | Based on event selection

### Switches
Entity | Description | Category | Added when
:--- | :--- | :--- | :---
Motion detection | Enable/disable motion detection | Config | Always
Disarming | Toggle disarming linkage (Event -> Disarming) | Config | Device supports disarming API
Event notifications | Toggle event notifications when disarmed | Config | Device supports disarming API
Smart motion detection | Enable/disable smart motion detection | Config | Device supports smart motion
Siren | Activate the camera's built-in siren | — | AS-PV models, L46N, W452ASD

### Lights
Entity | Description | Added when
:--- | :--- | :---
Infrared | Infrared LED control with brightness | Device has infrared lighting
Illuminator | White light control with brightness | Device has Lighting_V2 support
Flood light | Flood light on/off | ASH26, L26N, L46N, V261LC, W452ASD models
Security light | Red/blue flashing alarm light | AS-PV models, AD410, DB61i, IP8M-2796E
Ring light | Doorbell ring light on/off | Amcrest doorbells (AD/DB6 models)

### Selects
Entity | Description | Added when
:--- | :--- | :---
Security light | Doorbell light mode (Off/On/Strobe) | Amcrest doorbells with security light
Preset position | PTZ preset position (1-10 or Manual) | Always (disabled by default)

## Services
Service | Parameters | Description
:------------ | :------------ | :-------------
`camera.enable_motion_detection` | | Enables motion detection
`camera.disable_motion_detection` | | Disabled motion detection
`dahua.set_infrared_mode` | `target`: camera.cam13_main <br /> `mode`: Auto, On, Off <br /> `brightness`: 0 - 100 inclusive| Sets the infrared mode. Useful to set the mode back to Auto
`dahua.goto_preset_position` | `target`: camera.cam13_main <br /> `position`: 1 - 10 inclusive| Go to a preset position
`dahua.set_video_profile_mode` | `target`: camera.cam13_main <br /> `mode`: Day, Night| Sets the video profile mode to day or night
`dahua.set_focus_zoom` | `target`: camera.cam13_main <br /> `focus`: The focus level, e.g.: 0.81 0 - 1 inclusive <br /> `zoom`: The zoom level, e.g.: 0.72 0 - 1 inclusive | Sets the focus and zoom level
`dahua.set_channel_title` | `target`: camera.cam13_main <br /> `channel`: The camera channel, e.g.: 0 <br /> `text1`: The text 1<br /> `text2`: The text 2| Sets the channel title
`dahua.set_text_overlay` | `target`: camera.cam13_main <br /> `channel`: The camera channel, e.g.: 0 <br /> `group`: The group, used to apply multiple of text as an overly, e.g.: 1 <br /> `text1`: The text 1<br /> `text3`: The text 3 <br /> `text4`: The text 4 <br /> `text2`: The text 2 | Sets the text overlay on the video
`dahua.set_custom_overlay` | `target`: camera.cam13_main <br /> `channel`: The camera channel, e.g.: 0 <br /> `group`: The group, used to apply multiple of text as an overly, e.g.: 0 <br /> `text1`: The text 1<br /> `text2`: The text 2 | Sets the custom overlay on the video
`dahua.enable_channel_title` | `target`: camera.cam13_main <br /> `channel`: The camera channel, e.g.: 0 <br /> `enabled`: True to enable, False to disable | Enables or disables the channel title overlay on the video
`dahua.enable_time_overlay` | `target`: camera.cam13_main <br /> `channel`: The camera channel, e.g.: 0 <br /> `enabled`: True to enable, False to disable | Enables or disables the time overlay on the video
`dahua.enable_text_overlay` | `target`: camera.cam13_main <br /> `channel`: The camera channel, e.g.: 0 <br /> `group`: The group, used to apply multiple of text as an overly, e.g.: 0 <br /> `enabled`: True to enable, False to disable | Enables or disables the text overlay on the video
`dahua.enable_custom_overlay` | `target`: camera.cam13_main <br /> `channel`: The camera channel, e.g.: 0 <br /> `group`: The group, used to apply multiple of text as an overly, e.g.: 0 <br /> `enabled`: True to enable, False to disable | Enables or disables the custom overlay on the video
`dahua.set_privacy_masking` | `target`: camera.cam13_main <br /> `index`: The mask index, e.g.: 0 <br /> `enabled`: True to enable, False to disable | Enables or disabled a privacy mask on the camera
`dahua.set_record_mode` | `target`: camera.cam13_main <br /> `mode`: Auto, On, Off | Sets the record mode. On is always on recording. Off is always off. Auto based on motion settings, etc.
`dahua.enable_all_ivs_rules` | `target`: camera.cam13_main <br /> `channel`: The camera channel, e.g.: 0 <br /> `enabled`: True to enable all IVS rules, False to disable all IVS rules | Enables or disables all IVS rules
`dahua.enable_ivs_rule` | `target`: camera.cam13_main <br /> `channel`: The camera channel, e.g.: 0 <br /> `index`: The rule index <br /> enabled`: True to enable the IVS rule, False to disable the IVS rule | Enable or disable an IVS rule
`dahua.vto_open_door` | `target`: camera.cam13_main <br /> `door_id`: The door ID to open, e.g.: 1 <br /> Opens a door via a VTO
`dahua.vto_cancel_call` | `target`: camera.cam13_main <br />Cancels a call on a VTO device (Doorbell)
`dahua.set_video_in_day_night_mode` | `target`: camera.cam13_main <br /> `config_type`: The config type: general, day, night <br /> `mode`: The mode: Auto, Color, BlackWhite. Note Auto is also known as Brightness by Dahua|Set the camera's Day/Night Mode. For example, Color, BlackWhite, or Auto
`dahua.reboot` | `target`: camera.cam13_main <br />Reboots the device 


# Local development
If you wish to work on this component, the easiest way is to follow [HACS Dev Container README](https://github.com/custom-components/integration_blueprint/blob/master/.devcontainer/README.md). In short:

* Install Docker
* Install Visual Studio Code
* Install the devcontainer Visual Code plugin
* Clone this repo and open it in Visual Studio Code
* View -> Command Palette. Type `Tasks: Run Task` and select it, then click `Run Home Assistant on port 9123`
* Open Home Assistant at http://localhost:9123

# Curl/HTTP commands

```bash
# Stream events
curl -s --digest -u admin:$DAHUA_PASSWORD  "http://192.168.1.203/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5"

# List IVS rules
http://192.168.1.203/cgi-bin/configManager.cgi?action=getConfig&name=VideoAnalyseRule

# Enable/Disable IVS rules for [0][3] ... 0 is the channel, 3 is the rule index. Use the right index as required
http://192.168.1.203/cgi-bin/configManager.cgi?action=setConfig&VideoAnalyseRule[0][3].Enable=false

# Enable/disable Audio Linkage for an IVS rule
http://192.168.1.203/cgi-bin/configManager.cgi?action=setConfig&VideoAnalyseRule[0][3].EventHandler.VoiceEnable=false
```

# References and thanks
* Thanks to @elad-ba for his work on https://github.com/elad-bar/DahuaVTO2MQTT which was copied and modified and then used here for VTO devices
* Thanks for the DAHUA_HTTP_API_V2.76.pdf API reference found at http://www.ipcamtalk.com
* Thanks to all the people opening issues, reporting bugs, pasting commands, etc
