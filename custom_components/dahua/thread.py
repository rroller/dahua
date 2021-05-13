""" Dahua Thread """

import asyncio
import threading
import logging
import time

from homeassistant.core import HomeAssistant
from custom_components.dahua.client import DahuaClient

_LOGGER: logging.Logger = logging.getLogger(__package__)


class DahuaEventThread(threading.Thread):
    """Connects to device and subscribes to events. Mainly to capture motion detection events. """

    def __init__(self, hass: HomeAssistant, name: str, client: DahuaClient, on_receive, events: list):
        """Construct a thread listening for events."""
        threading.Thread.__init__(self)
        self.name = name
        self.hass = hass
        self.stopped = threading.Event()
        self.on_receive = on_receive
        self.client = client
        self.events = events
        self.started = False

    def run(self):
        """Fetch events"""
        self.started = True

        while 1:
            # submit the coroutine to the event loop thread
            coro = self.client.stream_events(self.on_receive, self.events)
            future = asyncio.run_coroutine_threadsafe(coro, self.hass.loop)
            start_time = int(time.time())

            try:
                # wait for the coroutine to finish
                future.result()
            except asyncio.TimeoutError as ex:
                _LOGGER.warning("TimeoutError connecting to %s", self.name)
                future.cancel()
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.debug("%s", ex)

            if not self.started:
                return

            end_time = int(time.time())
            if (end_time - start_time) < 10:
                # We are failing fast when trying to connect to the camera. Let's retry slowly
                time.sleep(60)

            _LOGGER.debug("reconnecting to camera's %s event stream...", self.name)

    def stop(self):
        """ Signals to the thread loop that we should stop """
        self.started = False
