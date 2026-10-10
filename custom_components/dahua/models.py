from dataclasses import dataclass, InitVar
from typing import Any


def _is_on(value) -> bool:
    """Whether a Dahua status field says the output is on.

    Folded, because the coordinator folds. Every CGI reader of these same two
    fields is `get_status_value("Speaker").lower() == "on"`, and this compared
    `== "On"` exactly -- so a firmware answering `on` or `ON` read as off over
    RPC2 and as on over CGI, for the same output on the same device.

    Tolerant of an absent field for the same reason: the CGI path reads a
    missing key as off rather than raising, and this is the only other reader.
    A KeyError here escapes `_async_coaxial_status`, which catches refusals
    rather than shape errors, and would fail the whole poll over a device that
    reported one output and not the other.
    """
    return str(value or "").strip().lower() == "on"


@dataclass(unsafe_hash=True)
class CoaxialControlIOStatus:
    speaker: bool = False
    white_light: bool = False
    api_response: InitVar[Any] = None

    def __post_init__(self, api_response):
        if api_response is None:
            return
        status = (api_response.get("params") or {}).get("status") or {}
        self.speaker = _is_on(status.get("Speaker"))
        self.white_light = _is_on(status.get("WhiteLight"))
