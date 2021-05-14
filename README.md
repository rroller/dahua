
# Dahua



## Services
```
camera.enable_motion_detection
camera.disable_motion_detection

dahua.set_infrared_mode
    entity_id: camera.cam13_main
    mode: Auto # Auto, On, Off
    brightness: 100 # 0 - 100 inclusive
````

# Local development
If you wish to work on this component, the easiest way is to follow [HACS Dev Container README](https://github.com/custom-components/integration_blueprint/blob/master/.devcontainer/README.md). In short:

* Install Docker
* Install Visual Studio Code
* Install the devcontainer Visual Code plugin
* Clone this repo and open it in Visual Studio Code
* View -> Command Palette. Type `Tasks: Run Task` and select it, then click `Run Home Assistant on port 9123`
* Open Home Assistant at http://localhost:9123