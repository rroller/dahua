
# Home Assistant Dahua Integration
The `Dahua` integration allows you to integrate your [Dahua](https://www.dahuasecurity.com/) cameras in Home Assistant.

## Installation

### Manual install
To manually install:

```bash
# Download a copy of this repository
$ wget https://github.com/rroller/dahua/archive/main.zip

# Unzip the archive
$ unzip main.zip

# Move the dahua directory into your custom_components directory in your Home Assistant install
$ mv dahua/custom_components/dahua <home-assistant-install-directory>/config/custom_components/
```

### HACS install
To install with [HACS](https://hacs.xyz/):

1. Click on HACS in the Home Assistant menu
2. Click on `Integrations`
3. Click the top right menu (the three dots)
4. Select `Custom repositories`
5. Paste the repository URL (`https://github.com/rroller/dahua`) in the dialog box
6. Select category `Integration`
7. Click `Add`
8. Click `Install`
9. Add integration?

> :warning: **After executing one of the above installation methods, restart Home Assistant. Also clear your browser cache before proceeding to the next step, as the integration may not be visible otherwise.**


# Known supported cameras
This integration should word with most Dahua cameras. It has been tested with very old and very new Dahua cameras.
The following are confirmed to work:

* xxx
* yyy
* IPC-HDlkjsdf

# Services and Entities
## Services
```
camera.enable_motion_detection
camera.disable_motion_detection

dahua.set_infrared_mode
    entity_id: camera.cam13_main
    mode: Auto # Auto, On, Off
    brightness: 100 # 0 - 100 inclusive
````

## Camera

## Switches

## Lights

## Binary Sensors

# Local development
If you wish to work on this component, the easiest way is to follow [HACS Dev Container README](https://github.com/custom-components/integration_blueprint/blob/master/.devcontainer/README.md). In short:

* Install Docker
* Install Visual Studio Code
* Install the devcontainer Visual Code plugin
* Clone this repo and open it in Visual Studio Code
* View -> Command Palette. Type `Tasks: Run Task` and select it, then click `Run Home Assistant on port 9123`
* Open Home Assistant at http://localhost:9123