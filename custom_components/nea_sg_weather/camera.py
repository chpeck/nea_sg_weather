"""Support for retrieving weather data from NEA."""
from __future__ import annotations
from PIL import Image, ImageDraw
import io
import asyncio
import logging
from types import MappingProxyType
from typing import Any
from datetime import datetime, timedelta
import pytz
import math
import httpx

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_PREFIX, CONF_SENSORS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.httpx_client import get_async_client

from . import NeaWeatherDataUpdateCoordinator
from .const import (
    DOMAIN,
    RAIN_MAP_HEADERS,
    RAIN_MAP_URL_PREFIX,
    RAIN_MAP_URL_SUFFIX,
)

_LOGGER = logging.getLogger(__name__)
_SG_TZ = pytz.timezone('Asia/Singapore')

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a weather camera entity from a config_entry."""
    coordinator: NeaWeatherDataUpdateCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]

    async_add_entities([NeaRainCamera(hass, coordinator, config_entry.data)])


class NeaRainCamera(Camera):
    """Implementation of a camera entity for rain map overlay."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator,
        config: MappingProxyType[str, Any],
    ) -> None:
        """Initialise area sensor with a data instance and site."""
        super().__init__()
        self.hass = hass
        self.coordinator = coordinator
        self._name = config.get(CONF_NAME) + " Rain Map"
        self._limit_refetch = True
        self._supported_features = 0
        self.content_type = "image/png"
        self.verify_ssl = True
        self._last_query_time = None
        self._last_image_time = None
        self._last_image_time_pretty = None
        self._last_image = None
        self._last_url = None
        self._platform = "camera"
        self._prefix = config[CONF_SENSORS][CONF_PREFIX]
        self.entity_id = (
            (self._platform + "." + self._prefix + "_rain_map")
            .lower()
            .replace(" ", "_")
        )
        self._last_state = None
        self._last_attributes = None
        self._updated_attributes = None

    @property
    def unique_id(self):
        """Return the unique ID."""
        return self._prefix + " Rain Map"

    @property
    def name(self):
        """Return the name of this device."""
        return self._name

    @property
    def supported_features(self):
        """Return supported features for this camera."""
        return self._supported_features

    def camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return bytes of camera image."""
        return asyncio.run_coroutine_threadsafe(
            self.async_camera_image(), self.hass.loop
        ).result()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return an animated image from the camera."""

        def ceil_dt(dt, delta):
            return dt + (datetime.min - dt) % delta

        async def get_image(timestamp: datetime) -> bytes | None:
            # make sure we use sg time for this
            url = RAIN_MAP_URL_PREFIX + timestamp.astimezone(_SG_TZ).strftime('%Y%m%d%H%M') + RAIN_MAP_URL_SUFFIX
            _LOGGER.debug("Getting rain map image from %s", url)
            try:
                async_client = get_async_client(self.hass, verify_ssl=self.verify_ssl)
                response = await async_client.get(url, headers=RAIN_MAP_HEADERS)
                response.raise_for_status()
                return response.content

            except httpx.TimeoutException:
                _LOGGER.warning("Timeout getting camera image for %s from %s", self._name, url)
                return

            except (httpx.HTTPStatusError, httpx.RequestError):
                _LOGGER.warning("Error getting camera image for %s from %s", self._name, url)
                return

            except Exception as e:
                _LOGGER.warning(e)
                return

        td = timedelta(minutes = 5)
        now = ceil_dt(datetime.now(), td)

        if self._last_image_time == now - td:
            if not (self._last_image is None):
                _LOGGER.debug('Return last successfully fetched image')
                return self._last_image

        timestamps = []
        start = now - timedelta(minutes = 50)
        while start < now:
            timestamps.append(start)
            start = start + td

        contents = await asyncio.gather(*[get_image(dt) for dt in timestamps])

        images = []
        for content in contents:
            if not (content is None):
                self._last_image = content
                images.append(Image.open(io.BytesIO(content)))

        if len(images) > 1:
            # draw progress bar
            frames = []
            for idx, frame in enumerate(images):
                w, h = frame.size
                progress = idx / (len(images) - 1)
                draw = ImageDraw.Draw(frame)
                draw.line((0,0,progress * w, 0), fill='gray', width=3) 
                frames.append(frame)
            buff = io.BytesIO()
            frames[0].save(buff, format='GIF', save_all=True, append_images=frames[1:], optimize=False, duration=1000, disposal=2, loop=0)
            self._last_image = buff.getvalue() 

            # succesfully fetched all images
            if len(images) == len(timestamps):
                self._last_image_time = timestamps[-1]
                self._last_image_time_pretty = timestamps[-1].isoformat()
                self._last_url = RAIN_MAP_URL_PREFIX + timestamps[-1].astimezone(_SG_TZ).strftime('%Y%m%d%H%M') + RAIN_MAP_URL_SUFFIX
                # Update timestamp from external coordinator entity
                self._last_state = self.hass.states.get(self.entity_id).state
                self._last_attributes = self.hass.states.get(self.entity_id).attributes
                self._updated_attributes = dict(self._last_attributes)
                self._updated_attributes["Updated at"] = self._last_image_time_pretty
                self._updated_attributes["URL"] = self._last_url
                self.hass.states.async_set(self.entity_id, self._last_state, self._updated_attributes)
                _LOGGER.debug("Rain map image new URL is %s", self._last_url)

        return self._last_image

    async def stream_source(self):
        """Return the source of the stream."""
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Return dict of additional properties to attach to sensors."""
        return {
            "Updated at": self._last_image_time_pretty,
            "URL": self._last_url,
        }

    @property
    def device_info(self) -> DeviceInfo:
        """Device info."""
        return DeviceInfo(
            default_name="Weather forecast coordinator",
            identifiers={(DOMAIN,)},  # type: ignore[arg-type]
            manufacturer="NEA Weather",
            model="data.gov.sg API Polling",
        )
