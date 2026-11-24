# Added by andrejem (2026): gradient lights. Lamps that can show several colors at once (Philips Hue
# gradient lamps like Signe, Play gradient or the gradient light strip in Zigbee2MQTT) get the colors
# of the preset as a gradient instead of one color. Home Assistant's light entity knows only one
# color, so the gradient is sent to Zigbee2MQTT directly over MQTT.
# Zigbee2MQTT reads the hex colors of a gradient as linear "Wide RGB D65" values, without the sRGB
# gamma of normal colors: the colors are converted the same way, so the lamp gets exactly the xy of
# the preset (with the usual sRGB conversion they came out pale, almost white).
# Modified by firepinn (2026): WLED segments are gradient lamps too. Their colors go to the WLED JSON
# API as the three colors of the segment with the effect "Palette" and the palette "* Color Gradient",
# which blends them along the strip (the light entity of WLED knows only one color as well).

import asyncio
import json
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.color import color_xy_to_RGB
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

MAX_GRADIENT_COLORS = 5
GRADIENT_STYLE = "linear"

# WLED: a segment has three color slots; effect 65 "Palette" shows the palette along the strip,
# palette 4 "* Color Gradient" is the gradient of the three slots
WLED_MAX_COLORS = 3
WLED_FX_SOLID = 0
WLED_FX_PALETTE = 65
WLED_PAL_COLOR_GRADIENT = 4
WLED_TIMEOUT = 5


def _hex(xy) -> str:
    """xy as the hex color Zigbee2MQTT turns back into the same xy (linear Wide RGB D65, no gamma)."""
    x, y = float(xy[0]), float(xy[1])
    if y <= 0:
        return "#ffffff"
    big_x = x / y
    big_z = (1.0 - x - y) / y
    rgb = (
        big_x * 1.656492 - 0.354851 - big_z * 0.255038,
        -big_x * 0.707196 + 1.655397 + big_z * 0.036152,
        big_x * 0.051713 - 0.121364 + big_z * 1.011530,
    )
    rgb = [max(0.0, c) for c in rgb]
    top = max(rgb) or 1.0
    return "#" + "".join(f"{round(c / top * 255):02x}" for c in rgb)


def gradient_colors(preset_colors, first_color) -> list:
    """Colors for one gradient lamp: the preset colors, starting with the color the lamp would get."""
    colors = list(preset_colors)
    if not colors:
        return []
    start = colors.index(first_color) if first_color in colors else 0
    count = min(len(colors), MAX_GRADIENT_COLORS)
    return [colors[(start + i) % len(colors)] for i in range(count)]


def wled_colors(colors) -> list:
    """Up to three colors for a WLED segment, spread over the colors of the gradient (first and last kept).
    Repeated colors count once: presets often end with their first color again."""
    colors = list(dict.fromkeys(tuple(c) for c in colors))
    if len(colors) <= WLED_MAX_COLORS:
        return colors
    last = len(colors) - 1
    return [colors[round(i * last / (WLED_MAX_COLORS - 1))] for i in range(WLED_MAX_COLORS)]


class GradientLights:
    """Finds gradient lamps (Zigbee2MQTT, WLED segments) and sends gradients to them."""

    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self._devices: dict[str, str] = {}  # ieee address -> MQTT topic of the device
        self._lights: dict[str, str] = {}  # light entity_id -> MQTT topic
        self._unsub = None

    async def async_start(self):
        """Listen to the device lists of Zigbee2MQTT (any base topic). Without MQTT nothing happens."""
        if "mqtt" not in self.hass.config.components:
            return
        try:
            from homeassistant.components import mqtt

            if not await mqtt.async_wait_for_mqtt_client(self.hass):
                return
            self._unsub = await mqtt.async_subscribe(self.hass, "+/bridge/devices", self._on_devices)
        except Exception as err:  # noqa: BLE001 - gradients are optional
            _LOGGER.debug("Gradient lights not available: %s", err)

    @callback
    def async_stop(self):
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _on_devices(self, msg):
        try:
            devices = json.loads(msg.payload)
        except (ValueError, TypeError):
            return
        base = msg.topic.split("/bridge/devices")[0]
        found = {}
        for device in devices or []:
            exposes = json.dumps((device.get("definition") or {}).get("exposes") or [])
            if '"gradient"' in exposes and device.get("ieee_address") and device.get("friendly_name"):
                found[device["ieee_address"].lower()] = f"{base}/{device['friendly_name']}"
        self._devices = found
        self._map_entities()

    @callback
    def _map_entities(self):
        lights = {}
        if self._devices:
            dev_reg = dr.async_get(self.hass)
            ent_reg = er.async_get(self.hass)
            mqtt_devices = [device for mqtt_entry in self.hass.config_entries.async_entries("mqtt")
                            for device in dr.async_entries_for_config_entry(dev_reg, mqtt_entry.entry_id)]
            for device in mqtt_devices:
                topic = None
                for domain, ident in device.identifiers:
                    if domain != "mqtt":
                        continue
                    for ieee, device_topic in self._devices.items():
                        if ieee in str(ident).lower():
                            topic = device_topic
                if topic is None:
                    continue
                for entry in er.async_entries_for_device(ent_reg, device.id):
                    if entry.domain == "light":
                        lights[entry.entity_id] = topic
        self._lights = lights
        if lights:
            _LOGGER.debug("Gradient lights: %s", ", ".join(sorted(lights)))

    @callback
    def _wled_segment(self, entity_id: str):
        """(host, segment id, single segment) of a WLED segment light, None for other lights.

        The WLED integration gives segment lights the unique id <mac>_<segment> and the master light
        (brightness only, exists with two or more segments) the unique id <mac>."""
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None or entry.platform != "wled" or entry.domain != "light" or "_" not in entry.unique_id:
            return None
        mac, _, segment = entry.unique_id.rpartition("_")
        if not segment.isdigit() or entry.config_entry_id is None:
            return None
        config_entry = self.hass.config_entries.async_get_entry(entry.config_entry_id)
        host = config_entry.data.get("host") if config_entry is not None else None
        if not host:
            return None
        master = er.async_get(self.hass).async_get_entity_id("light", "wled", mac)
        master_state = self.hass.states.get(master) if master else None
        single = master_state is None or master_state.state == "unavailable"
        return host, int(segment), single

    def is_gradient(self, entity_id: str) -> bool:
        return entity_id in self._lights or self._wled_segment(entity_id) is not None

    def needs_single_color(self, entity_id: str) -> bool:
        """WLED segments get a one-color preset here too: it switches them to Solid, so the gradient of
        the last scene does not stay around the new color."""
        return entity_id not in self._lights and self._wled_segment(entity_id) is not None

    def register(self, entity_id: str, topic: str):
        """Mainly for tests: treat a light as gradient lamp with this MQTT topic."""
        self._lights[entity_id] = topic

    async def async_apply(self, entity_id: str, colors, brightness, transition) -> bool:
        """Send a gradient to the lamp. False if the lamp is no gradient lamp or MQTT failed."""
        if entity_id not in self._lights and (wled := self._wled_segment(entity_id)) is not None:
            return await self._async_apply_wled(entity_id, *wled, colors, brightness, transition)
        topic = self._lights.get(entity_id)
        if topic is None or not colors:
            return False
        payload = {
            "state": "ON",
            "gradient": [_hex(c) for c in colors],
            "gradient_style": GRADIENT_STYLE,
        }
        if brightness is not None:
            payload["brightness"] = int(brightness)
        if transition is not None:
            payload["transition"] = transition
        try:
            from homeassistant.components import mqtt

            await mqtt.async_publish(self.hass, f"{topic}/set", json.dumps(payload))
        except Exception as err:  # noqa: BLE001 - fall back to one color
            _LOGGER.warning("Gradient for %s could not be sent: %s", entity_id, err)
            return False
        return True

    async def _async_apply_wled(self, entity_id, host, segment, single, colors, brightness, transition) -> bool:
        """Send the gradient to a WLED segment over the JSON API. False if WLED could not be reached."""
        if not colors:
            return False
        colors = wled_colors(colors)
        seg = {"id": segment, "on": True, "col": [list(color_xy_to_RGB(float(x), float(y))) for x, y in colors]}
        if len(colors) > 1:
            # Palette: shift 0, size 128 = the palette exactly once, rotation 0 = along the strip (128 turns
            # it by 90 degrees: one color on a 1D strip), no animated shift or rotation. Set every time,
            # because the segment keeps the slider values of earlier effects.
            seg.update({"fx": WLED_FX_PALETTE, "pal": WLED_PAL_COLOR_GRADIENT,
                        "sx": 0, "ix": 128, "c1": 0, "o1": False, "o2": False})
        else:
            seg["fx"] = WLED_FX_SOLID
        payload = {"on": True, "seg": [seg]}
        if brightness is not None:
            # One segment: the brightness of the light entity is the brightness of the whole strip
            if single:
                payload["bri"] = int(brightness)
            else:
                seg["bri"] = int(brightness)
        if transition is not None:
            payload["tt"] = round(float(transition) * 10)  # tenths of a second, only for this change
        try:
            session = async_get_clientsession(self.hass)
            async with asyncio.timeout(WLED_TIMEOUT):
                response = await session.post(f"http://{host}/json/state", json=payload)
                response.raise_for_status()
        except Exception as err:  # noqa: BLE001 - fall back to one color
            _LOGGER.warning("Gradient for %s could not be sent to WLED: %s", entity_id, err)
            return False
        return True
