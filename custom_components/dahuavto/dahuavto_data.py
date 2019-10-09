
import sys
from datetime import datetime
import logging

import requests

from homeassistant.const import (EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STOP,
                                 STATE_ON, STATE_OFF)

from homeassistant.helpers.event import track_time_interval
from requests.auth import HTTPBasicAuth
from homeassistant.util import slugify
from .const import *

REQUIREMENTS = ['aiohttp']

_LOGGER = logging.getLogger(__name__)


class DahuaVTOData(object):
    """The Class for handling the data retrieval."""

    def __init__(self, hass, name, host, port, username, password):
        """Initialize the data object."""
        self._name = name
        self._host = host
        self._port = port
        self._username = username
        self._password = password

        self._attributes = {}
        self._is_ringing = False
        self._hass = hass

        self._auth = None
        self._connected = False
        self._updating = False
        self._stopped = False
        self._unavailable = False

        self._base_url = None
        self._last_update = None
        self._data = {}

        def vto_refresh(event_time):
            """Call Wifi Bell to refresh information."""
            _LOGGER.debug(f'Updating Wifi Bell component, at {event_time}')
            self.update()

        def open_gate(event_time):
            """Call Wifi Bell to refresh information."""
            _LOGGER.debug(f'Open WifiBell gate, at {event_time}')
            self.vto_open_gate()

        self.initialize()
        self.initialize_events(vto_refresh, SCAN_INTERVAL)

        self._hass.services.register(DOMAIN, 'open', open_gate)

    def initialize(self):
        self._auth = requests.auth.HTTPDigestAuth(self._username, self._password)

        self._base_url = f"http://{self._host}:{self._port}"

        self.update_system_information()

    def initialize_events(self, vto_refresh, interval):
        track_time_interval(self._hass, vto_refresh, interval)

        self._hass.bus.listen_once(EVENT_HOMEASSISTANT_START, vto_refresh)

    def vto_http_request(self, command):
        try:
            url = f"{self._base_url}{command}"

            response = requests.get(url, timeout=10, auth=self._auth)

            response.raise_for_status()

            self._connected = True
            result = response.text
        except Exception as ex:
            exc_type, exc_obj, tb = sys.exc_info()
            line_number = tb.tb_lineno

            _LOGGER.error(f'Failed to get response from VTO, Error: {ex}, Line: {line_number}')
            self._connected = False
            result = None

        return result

    def update_system_information(self):
        try:
            content = self.vto_http_request(SYSTEM_INFO_URL)
            attributes = {}

            if content is not None:
                lines = content.split('\n')

                for item in lines:
                    data_item_arr = item.split('=')
                    if len(data_item_arr) > 1:
                        data_key = data_item_arr[0]
                        data_value = data_item_arr[1]

                        attributes[data_key] = data_value

            self._attributes = attributes

        except Exception as ex:
            exc_type, exc_obj, tb = sys.exc_info()
            line_number = tb.tb_lineno

            _LOGGER.error(f'Failed to update VTO system information, Error: {ex}, Line: {line_number}')

    def update_video_talk_log(self):
        try:
            content = self.vto_http_request(VIDEO_TALK_LOG_URL)

            if content is not None:
                self.parse(content)

                self._last_update = datetime.now()

                content = self.vto_http_request(VIDEO_TALK_LOG_URL)

                if content is not None:
                    self.parse(content)

                    current_time = datetime.now()

                    for key in self._data:
                        item = self._data[key]

                        create_time = int(item.get("CreateTime", 0))

                        create_date_time = datetime.fromtimestamp(create_time - (3 * 60 * 60))
                        item["CreatedDate"] = create_date_time

                        delta_seconds = (current_time - create_date_time).total_seconds()

                        self._is_ringing = delta_seconds < RING_TIME
                        self._last_update = current_time

                        log_message = f'Current time: {current_time}, Last ring: {create_date_time},' \
                                      f' Delta:{delta_seconds}'

                        if self._is_ringing:
                            _LOGGER.info(f'update - Ringing, {log_message}')
                        else:
                            _LOGGER.debug(f'update - {log_message}')

                        self._attributes[ATTR_LAST_EVENT_TIME] = current_time

        except Exception as ex:
            exc_type, exc_obj, tb = sys.exc_info()
            line_number = tb.tb_lineno

            _LOGGER.error(f'Failed to update VTO talk log, Error: {ex}, Line: {line_number}')

    def vto_open_gate(self):
        try:
            self.vto_http_request(OPEN_GATE_URL)
        except Exception as ex:
            exc_type, exc_obj, tb = sys.exc_info()
            line_number = tb.tb_lineno

            _LOGGER.error(f'Failed to open gate, Error: {ex}, Line: {line_number}')

    def parse(self, content):
        lines = content.split('\n')
        for item in lines:
            call_data = item.split('.')

            if len(call_data) > 1:
                call_key = call_data[0]
                call_item = call_data[1]

                cleaned_call_key = call_key.replace("records[", "").replace("]", "")
                call_data_item = call_item.split('=')
                call_data_property = call_data_item[0]
                call_data_value = call_data_item[1]

                data_item = self._data.get(cleaned_call_key, {})
                data_item[call_data_property] = call_data_value

                self._data[cleaned_call_key] = data_item

        for key in self._data:
            item = self._data[key]

            create_time = int(item.get("CreateTime", 0))

            item["CreateDateTime"] = datetime.fromtimestamp(create_time)

    def update(self):
        if self._updating:
            return

        self._updating = True
        self.update_video_talk_log()

        self.create_vto_available_sensor()
        self.create_vto_ring_sensor()

        self._updating = False

    def get_attributes(self, device_class):
        attributes = {}
        for key in self._attributes:
            attributes[key] = self._attributes[key]

        attributes["device_class"] = SENSOR_TYPES[SENSOR_TYPE_AVAILABLE]

        return attributes

    def create_vto_available_sensor(self):
        entity_id = BINARY_SENSOR_ENTITY_ID.format(slugify(self._name), SENSOR_TYPE_AVAILABLE.lower())
        state = STATE_OFF
        attributes = self.get_attributes(SENSOR_TYPE_AVAILABLE)

        if self._connected:
            state = STATE_ON

        self._hass.states.set(entity_id, state, attributes)

    def create_vto_ring_sensor(self):
        entity_id = BINARY_SENSOR_ENTITY_ID.format(slugify(self._name), SENSOR_TYPE_RING.lower())
        state = STATE_OFF
        attributes = self.get_attributes(SENSOR_TYPE_RING)

        if self._is_ringing:
            state = STATE_ON

        self._hass.states.set(entity_id, state, attributes)
