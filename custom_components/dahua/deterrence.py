"""Parse direct-camera deterrence ProductDefinition blocks."""


def product_definition_supports_security_light(definition: dict | None) -> bool:
    """Recognize event-linked warning lights, rather than normal illumination."""
    if not isinstance(definition, dict):
        return False
    linking = definition.get("LinkingDetail")
    if not isinstance(linking, dict):
        return False
    # Dahua firmware commonly uses the misspelled FilckerLighting key.
    for name in ("FilckerLighting", "FlickerLighting"):
        flicker = linking.get(name)
        if not isinstance(flicker, dict) or flicker.get("Support") is not True:
            continue
        light_types = flicker.get("LightType")
        if isinstance(light_types, list) and bool(light_types):
            return True
    return False


def product_definition_supports_siren(definition: dict | None) -> bool:
    """Recognize dedicated sirens or selectable event-linked warning audio."""
    if not isinstance(definition, dict):
        return False
    siren = definition.get("SirenFileManager")
    if isinstance(siren, dict) and siren.get("Support") is True:
        return True
    events = definition.get("SupportEventLinkList")
    supports_audio_format = any(
        isinstance(definition.get(key), list)
        and any(
            isinstance(value, str) and value.lower() in {"wav", "pcm", "aac", "mp3"}
            for value in definition[key]
        )
        for key in ("PlayFormat", "PlayFormet")
    )
    return (
        isinstance(events, list)
        and bool(events)
        and all(isinstance(event, str) and bool(event) for event in events)
        and supports_audio_format
    )


def siren_definition_failure_reason(definition) -> str:
    """Explain a negative parser result without exporting device data."""
    if not isinstance(definition, dict):
        return "definition unavailable or not an object"
    reasons = ["SirenFileManager.Support is missing or not true"]
    events = definition.get("SupportEventLinkList")
    if not (
        isinstance(events, list)
        and events
        and all(isinstance(event, str) and bool(event) for event in events)
    ):
        reasons.append("SupportEventLinkList is missing, empty or invalid")
    formats = any(
        isinstance(definition.get(key), list)
        and any(
            isinstance(value, str) and value.lower() in {"wav", "pcm", "aac", "mp3"}
            for value in definition[key]
        )
        for key in ("PlayFormat", "PlayFormet")
    )
    if not formats:
        reasons.append(
            "PlayFormat/PlayFormet contains no supported wav/pcm/aac/mp3 format"
        )
    return "; ".join(reasons)


def security_light_definition_failure_reason(definition) -> str:
    """Explain a negative lighting result without exporting device data."""
    if not isinstance(definition, dict):
        return "definition unavailable or not an object"
    linking = definition.get("LinkingDetail")
    if not isinstance(linking, dict):
        return "LinkingDetail is missing or not an object"
    reasons = []
    for key in ("FilckerLighting", "FlickerLighting"):
        flicker = linking.get(key)
        if not isinstance(flicker, dict):
            reasons.append(f"{key} is missing or not an object")
            continue
        if flicker.get("Support") is not True:
            reasons.append(f"{key}.Support is missing or not true")
        light_types = flicker.get("LightType")
        if not isinstance(light_types, list) or not light_types:
            reasons.append(f"{key}.LightType is missing, empty or not a list")
    return "; ".join(reasons)
