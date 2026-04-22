# Workflows

## Integration Setup Flow

```mermaid
sequenceDiagram
    participant User
    participant ConfigFlow
    participant DahuaClient
    participant Camera

    User->>ConfigFlow: Enter credentials + address
    ConfigFlow->>DahuaClient: get_machine_name()
    DahuaClient->>Camera: GET /magicBox.cgi?action=getMachineName
    Camera-->>DahuaClient: name=FrontDoor
    DahuaClient->>Camera: GET /magicBox.cgi?action=getSystemInfo
    Camera-->>DahuaClient: serialNumber=ABC123
    ConfigFlow-->>User: Show name confirmation step
    User->>ConfigFlow: Confirm name
    ConfigFlow->>ConfigFlow: Create config entry
    ConfigFlow->>ConfigFlow: async_setup_entry()
```

## Device Initialization

```mermaid
flowchart TD
    A[async_setup_entry] --> B[Create DahuaDataUpdateCoordinator]
    B --> C[First data refresh]
    C --> D{Initialized?}
    D -->|No| E[Probe device capabilities]
    E --> F[Get machine name, system info, version]
    F --> G[Detect model and device type]
    G --> H[Check channel numbering]
    H --> I[Probe: coaxial control]
    I --> J[Probe: disarming linkage]
    J --> K[Probe: smart motion]
    K --> L[Probe: PTZ]
    L --> M[Probe: lighting V1 + V2]
    M --> N{Is doorbell?}
    N -->|Yes| O[Start VTO event listener<br/>TCP port 5000]
    N -->|No| P[Start HTTP event listener<br/>eventManager.cgi]
    P --> Q[Probe: profile mode support]
    O --> R[Mark initialized]
    Q --> R
    D -->|Yes| S[Poll: motion, lighting, disarming, etc.]
    R --> S
```

## Event Processing (IP Camera)

```mermaid
sequenceDiagram
    participant Camera
    participant Client
    participant Coordinator
    participant EventBus
    participant BinarySensor

    Camera->>Client: HTTP chunked response<br/>Code=VideoMotion;action=Start;index=0;data={...}
    Client->>Coordinator: on_receive(data_bytes, channel)
    Coordinator->>Coordinator: parse_event(data)
    Coordinator->>Coordinator: Filter by channel
    Coordinator->>Coordinator: translate_event_code()
    Coordinator->>EventBus: fire("dahua_event_received", event)
    Coordinator->>Coordinator: Update _dahua_event_timestamp
    Coordinator->>BinarySensor: Call registered listener
    BinarySensor->>BinarySensor: schedule_update_ha_state()
```

## Event Processing (VTO Doorbell)

```mermaid
sequenceDiagram
    participant Doorbell
    participant VTOClient
    participant Coordinator
    participant EventBus

    Doorbell->>VTOClient: Binary frame + JSON event
    VTOClient->>VTOClient: parse_response()
    VTOClient->>VTOClient: handle_notify_event_stream()
    VTOClient->>Coordinator: on_receive_vto_event(event)
    Coordinator->>EventBus: fire("dahua_event_received", event)
    Coordinator->>Coordinator: translate_event_code()<br/>BackKeyLight → DoorbellPressed
    Coordinator->>Coordinator: Handle Start/Stop/Pulse actions
    Coordinator->>Coordinator: Update timestamps + call listeners
```

## Periodic State Polling

```mermaid
flowchart TD
    A[Every 30 seconds] --> B{Profile mode supported?}
    B -->|Yes| C[Get VideoInMode]
    B -->|No| D[Skip]
    C --> E{PTZ supported?}
    D --> E
    E -->|Yes| F[Get PTZ status]
    E -->|No| G[Skip]
    F --> H[Fan out parallel API calls]
    G --> H
    H --> I[Get motion detection config]
    H --> J[Get lighting config<br/>if IR supported]
    H --> K[Get disarming linkage<br/>if supported]
    H --> L[Get coaxial status<br/>if supported]
    H --> M[Get smart motion<br/>if supported]
    H --> N[Get Lighting_V2<br/>if supported]
    I & J & K & L & M & N --> O[Gather results]
    O --> P[Update coordinator.data]
    P --> Q[Entities read updated state]
```

## Event Stream Reconnection

```mermaid
flowchart TD
    A[Start event stream] --> B[Connect to camera]
    B --> C[Stream events]
    C --> D{Connection lost?}
    D -->|No| C
    D -->|Yes| E{Failed quickly<br/>< 10 seconds?}
    E -->|Yes| F[Wait 60 seconds]
    E -->|No| G[Reconnect immediately]
    F --> B
    G --> B
```

VTO reconnection uses a simpler 5s delay on disconnect, 30s on connection failure.

## Service Call Flow

```mermaid
sequenceDiagram
    participant User
    participant HA
    participant CameraEntity
    participant Coordinator
    participant Client
    participant Device

    User->>HA: Call dahua.set_infrared_mode
    HA->>CameraEntity: async_set_infrared_mode(mode, brightness)
    CameraEntity->>Coordinator: get_channel()
    CameraEntity->>Client: async_set_lighting_v1_mode(channel, mode, brightness)
    Client->>Device: GET /configManager.cgi?action=setConfig&Lighting[ch][0].Mode=mode
    Device-->>Client: OK
    CameraEntity->>Coordinator: async_refresh()
    Coordinator->>Coordinator: _async_update_data()
```
