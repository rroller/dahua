from dataclasses import dataclass, InitVar
from typing import Any


@dataclass(unsafe_hash=True)
class CoaxialControlIOStatus:
    speaker: bool = False
    white_light: bool = False
    api_response: InitVar[Any] = None

    def __post_init__(self, api_response):
        if api_response is not None:
            self.speaker = api_response["params"]["status"]["Speaker"] == "On"
            self.white_light = api_response["params"]["status"]["WhiteLight"] == "On"
