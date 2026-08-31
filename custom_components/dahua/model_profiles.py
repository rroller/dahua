"""Small model-specific helpers for upstream Dahua compatibility."""
from __future__ import annotations

import re

_SDT4E425_RE = re.compile(
    r"^(?:DH-)?SDT4E425-4F-GB-A-PV1(?:-.+)?$",
    re.IGNORECASE,
)


def is_sdt4e425(model: str | None) -> bool:
    """Return True for the hardware-validated Dahua SDT4E425 model family."""
    return bool(_SDT4E425_RE.match((model or "").strip()))
