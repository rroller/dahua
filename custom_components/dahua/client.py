"""Dahua API client.

The implementation now lives in the standalone :mod:`aiodahua` package rather
than being vendored here. Extracting it means the protocol knowledge -- and the
firmware quirks that go with it -- can be shared with other consumers instead
of being re-derived, and it is versioned and tested independently.

This module stays as a thin compatibility layer so the rest of the integration
is unchanged: it keeps the original positional constructor signature and
re-exports the names other modules import from here.

``aiodahua`` is a superset of what used to be in this file. Every method that
was here is still present with the same name, signature and return shape, so
there is no behavioural diff. It additionally provides white-label brand
identification (Amcrest, Lorex, EmpireTech), storage and recording queries, and
a typed exception hierarchy.
"""

from __future__ import annotations

import aiohttp
from aiodahua import DahuaClient as _AioDahuaClient
from aiodahua.client import SECURITY_LIGHT_TYPE
from aiodahua.client import SIREN_TYPE
from aiodahua.client import TIMEOUT_SECONDS
from aiodahua.client import _parse_adts_frames

__all__ = [
    "SECURITY_LIGHT_TYPE",
    "SIREN_TYPE",
    "TIMEOUT_SECONDS",
    "DahuaClient",
    "_parse_adts_frames",
]


class DahuaClient(_AioDahuaClient):
    """Dahua client using the integration's original argument order.

    ``aiodahua.DahuaClient`` takes ``(host, username, password, *, port, ...)``.
    This subclass preserves the ``(username, password, address, port,
    rtsp_port, session)`` signature the integration has always used, so no call
    site has to change.
    """

    def __init__(
        self,
        username: str,
        password: str,
        address: str,
        port: int,
        rtsp_port: int,
        session: aiohttp.ClientSession,
    ) -> None:
        super().__init__(
            address,
            username,
            password,
            port=int(port),
            rtsp_port=int(rtsp_port),
            session=session,
        )
