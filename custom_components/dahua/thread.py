""" Dahua Thread """

import asyncio
import sys
import threading
import logging
import time

from homeassistant.core import HomeAssistant
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.vto import DahuaVTOClient

_LOGGER: logging.Logger = logging.getLogger(__package__)


class DahuaEventThread(threading.Thread):
    """Connects to device and subscribes to events. Mainly to capture motion detection events. """

    def __init__(self, hass: HomeAssistant, client: DahuaClient, on_receive, events: list, channel: int):
        """Construct a thread listening for events."""
        threading.Thread.__init__(self)
        self.hass = hass
        self.stopped = threading.Event()
        self.on_receive = on_receive
        self.client = client
        self.events = events
        self.started = False
        self.channel = channel

    def run(self):
        """Fetch events"""
        self.started = True
        _LOGGER.info("Starting DahuaEventThread")

        while True:
            if not self.started:
                _LOGGER.debug("Exiting DahuaEventThread")
                return
            # submit the coroutine to the event loop thread
            coro = self.client.stream_events(self.on_receive, self.events, self.channel)
            future = asyncio.run_coroutine_threadsafe(coro, self.hass.loop)
            start_time = int(time.time())

            try:
                # wait for the coroutine to finish
                future.result()
            except asyncio.TimeoutError as ex:
                _LOGGER.warning("TimeoutError connecting to camera")
                future.cancel()
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.debug("%s", ex)

            if not self.started:
                _LOGGER.debug("Exiting DahuaEventThread")
                return

            end_time = int(time.time())
            if (end_time - start_time) < 10:
                # We are failing fast when trying to connect to the camera. Let's retry slowly
                time.sleep(60)

            _LOGGER.debug("reconnecting to camera's event stream...")

    def stop(self):
        """ Signals to the thread loop that we should stop """
        if self.started:
            _LOGGER.info("Stopping DahuaEventThread")
            self.stopped.set()
            self.started = False


class DahuaVtoEventThread(threading.Thread):
    """Connects to device and subscribes to events. Mainly to capture motion detection events. """

    def __init__(self, hass: HomeAssistant, client: DahuaClient, on_receive_vto_event, host: str,
                 port: int, username: str, password: str):
        """Construct a thread listening for events."""
        threading.Thread.__init__(self)
        self.hass = hass
        self.stopped = threading.Event()
        self.on_receive_vto_event = on_receive_vto_event
        self.client = client
        self.started = False
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._is_ssl = False
        self.vto_client = None

    def run(self):
        """Fetch VTO events"""
        self.started = True
        _LOGGER.info("Starting DahuaVtoEventThread")

        while True:
            try:
                if not self.started:
                    _LOGGER.debug("Exiting DahuaVtoEventThread")
                    return

                _LOGGER.debug("Connecting to VTO event stream")

                # TODO: How do I integrate this in with the HA loop? Does it even matter? I think so because
                # how well do we know when we are shutting down HA?
                loop = asyncio.new_event_loop()

                def vto_client_lambda():
                    # Notice how we set vto_client client here. This is so nasty, I'm embarrassed to put this into the
                    # code, but I'm not a python expert and it works well enough and this is just a spare time project
                    # so here it is. We need to capture an instance of the DahuaVTOClient so we can use it later on
                    # in switches to execute commands on the VTO. We need the client connected to the event loop
                    # which is done through loop.create_connection. This makes it awkward to capture... which is why
                    # I've done this. I'm sure there's a better way :)
                    self.vto_client = DahuaVTOClient(self._host, self._username, self._password, self._is_ssl,
                                                     self.on_receive_vto_event)
                    return self.vto_client

                client = loop.create_connection(vto_client_lambda, host=self._host, port=self._port)

                loop.run_until_complete(client)
                loop.run_forever()
                loop.close()

                _LOGGER.warning("Disconnected from VTO, will try to connect in 5 seconds")

                time.sleep(5)

            except Exception as ex:
                if not self.started:
                    _LOGGER.debug("Exiting DahuaVtoEventThread")
                    return
                exc_type, exc_obj, exc_tb = sys.exc_info()
                line = exc_tb.tb_lineno

                _LOGGER.error(f"Connection to VTO failed will try to connect in 30 seconds, error: {ex}, Line: {line}")

                time.sleep(30)

    def stop(self):
        """ Signals to the thread loop that we should stop """
        if self.started:
            _LOGGER.info("Stopping DahuaVtoEventThread")
            self.stopped.set()
            self.started = False
