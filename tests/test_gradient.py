"""Gradient lamps get all colors of the preset over MQTT (Zigbee2MQTT), other lamps one color."""

import json

import pytest

from homeassistant.core import HomeAssistant

from .test_custom_presets import setup, userdata, runtime  # noqa: F401  (fixture and helpers)

PRESET = "940fd864-51e1-4761-95d3-75d4d68835ea"  # Arctic aurora, several colors


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def test_gradient_colors_start_with_the_lamps_color():
    from custom_components.scene_studio.gradient import gradient_colors

    colors = [(0.1, 0.1), (0.2, 0.2), (0.3, 0.3), (0.4, 0.4), (0.5, 0.4), (0.6, 0.3)]
    assert gradient_colors(colors, (0.3, 0.3)) == [(0.3, 0.3), (0.4, 0.4), (0.5, 0.4), (0.6, 0.3), (0.1, 0.1)]
    assert gradient_colors([(0.3, 0.3)], (0.9, 0.9)) == [(0.3, 0.3)]


async def _setup(hass, monkeypatch):
    import homeassistant.components.mqtt as mqtt

    published = []

    async def fake_publish(hass, topic, payload, *args, **kwargs):
        published.append((topic, json.loads(payload)))

    monkeypatch.setattr(mqtt, "async_publish", fake_publish)
    turned_on = []

    async def turn_on(call):
        turned_on.append(call.data["entity_id"])

    hass.services.async_register("light", "turn_on", turn_on)
    attrs = {"supported_color_modes": ["xy", "color_temp"], "color_mode": "xy"}
    hass.states.async_set("light.signe", "off", attrs)
    hass.states.async_set("light.bulb", "off", attrs)
    await setup(hass, monkeypatch)
    runtime(hass).gradient.register("light.signe", "zigbee2mqtt/Signe")
    return published, turned_on


async def test_gradient_lamp_gets_all_colors(hass: HomeAssistant, monkeypatch, userdata):
    published, turned_on = await _setup(hass, monkeypatch)
    await hass.services.async_call("scene_studio", "apply_preset", {
        "preset_id": PRESET, "targets": {"entity_id": ["light.signe", "light.bulb"]}}, blocking=True)
    await hass.async_block_till_done()
    assert [t for t, _ in published] == ["zigbee2mqtt/Signe/set"]
    payload = published[0][1]
    assert len(payload["gradient"]) >= 2 and payload["gradient_style"] == "linear" and payload["state"] == "ON"
    assert turned_on == ["light.bulb"]


async def test_gradient_can_be_switched_off(hass: HomeAssistant, monkeypatch, userdata):
    published, turned_on = await _setup(hass, monkeypatch)
    await hass.services.async_call("scene_studio", "apply_preset", {
        "preset_id": PRESET, "targets": {"entity_id": ["light.signe"]}, "gradient": False}, blocking=True)
    await hass.async_block_till_done()
    assert published == [] and turned_on == ["light.signe"]


async def test_saved_scene_keeps_gradient_setting(hass: HomeAssistant, monkeypatch, userdata):
    published, turned_on = await _setup(hass, monkeypatch)
    r = await hass.services.async_call("scene_studio", "save_scene", {
        "name": "Signe one color", "preset_id": PRESET, "targets": {"entity_id": ["light.signe"]},
        "gradient": False}, blocking=True, return_response=True)
    await hass.services.async_call("scene_studio", "activate_saved_scene", {"id": r["scene"]["id"]}, blocking=True)
    await hass.async_block_till_done()
    assert published == [] and turned_on == ["light.signe"]


def _z2m_xy(hex_color):
    """What Zigbee2MQTT makes of a gradient hex color (ColorRGB.fromHex(hex).toXY(), no gamma)."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    big_x = r * 0.664511 + g * 0.154324 + b * 0.162028
    big_y = r * 0.283881 + g * 0.668433 + b * 0.047685
    big_z = r * 0.000088 + g * 0.07231 + b * 0.986039
    total = big_x + big_y + big_z
    return big_x / total, big_y / total


@pytest.mark.parametrize("xy", [(0.17, 0.7), (0.15, 0.06), (0.64, 0.33), (0.3, 0.32), (0.2, 0.45), (0.45, 0.41)])
def test_hex_round_trip(xy):
    """The lamp gets the xy of the preset back, so gradients are as saturated as single colors."""
    from custom_components.scene_studio.gradient import _hex

    x, y = _z2m_xy(_hex(xy))
    assert abs(x - xy[0]) < 0.01 and abs(y - xy[1]) < 0.01


async def test_gradient_lights_found_from_the_z2m_device_list(hass: HomeAssistant, monkeypatch, userdata):
    """The Z2M device list is matched to the lights of the MQTT devices in Home Assistant."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    await setup(hass, monkeypatch)
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    other_entry = MockConfigEntry(domain="hue")
    other_entry.add_to_hass(hass)
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    signe = dev_reg.async_get_or_create(config_entry_id=mqtt_entry.entry_id,
                                        identifiers={("mqtt", "zigbee2mqtt_0x00178801ABCDEF")})
    ent_reg.async_get_or_create("light", "mqtt", "signe", device_id=signe.id, config_entry=mqtt_entry,
                                suggested_object_id="signe_lamp")
    hue = dev_reg.async_get_or_create(config_entry_id=other_entry.entry_id, identifiers={("hue", "0x00178801abcdef")})
    ent_reg.async_get_or_create("light", "hue", "x", device_id=hue.id, config_entry=other_entry,
                                suggested_object_id="hue_lamp")

    gradient = runtime(hass).gradient
    gradient._devices = {"0x00178801abcdef": "zigbee2mqtt/Signe"}
    gradient._map_entities()
    assert gradient.is_gradient("light.signe_lamp")
    assert not gradient.is_gradient("light.hue_lamp")


def test_wled_colors_spread_over_the_gradient():
    from custom_components.scene_studio.gradient import wled_colors

    colors = [(0.1, 0.1), (0.2, 0.2), (0.3, 0.3), (0.4, 0.4), (0.5, 0.4)]
    assert wled_colors(colors) == [(0.1, 0.1), (0.3, 0.3), (0.5, 0.4)]
    # Soho: two nearly equal blues, peach and warm white -> the blue, peach and warm white
    soho = [(0.213, 0.2867), (0.4539, 0.3345), (0.2053, 0.2566), (0.4651, 0.4117), (0.213, 0.2867)]
    assert wled_colors(soho) == [(0.213, 0.2867), (0.4539, 0.3345), (0.4651, 0.4117)]
    assert wled_colors(colors[:2]) == colors[:2]
    # the first color again at the end counts once
    assert wled_colors([(0.1, 0.1), (0.2, 0.2), (0.3, 0.3), (0.1, 0.1)]) == [(0.1, 0.1), (0.2, 0.2), (0.3, 0.3)]


def _wled_lights(hass, segments):
    """A WLED strip at 192.0.2.10 with the given segment ids; the master light exists from 2 segments on."""
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="wled", data={"host": "192.0.2.10"})
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    attrs = {"supported_color_modes": ["rgb"], "color_mode": "rgb"}
    for segment in segments:
        ent_reg.async_get_or_create("light", "wled", f"aabbccddeeff_{segment}", config_entry=entry,
                                    suggested_object_id=f"strip_{segment}")
        hass.states.async_set(f"light.strip_{segment}", "off", attrs)
    if len(segments) > 1:
        ent_reg.async_get_or_create("light", "wled", "aabbccddeeff", config_entry=entry,
                                    suggested_object_id="strip_master")
        hass.states.async_set("light.strip_master", "on", {"supported_color_modes": ["brightness"]})


async def test_wled_segment_gets_a_palette_gradient(hass: HomeAssistant, monkeypatch, userdata, aioclient_mock):
    published, turned_on = await _setup(hass, monkeypatch)
    _wled_lights(hass, [0])
    aioclient_mock.post("http://192.0.2.10/json/state", json={"success": True})
    gradient = runtime(hass).gradient
    assert gradient.is_gradient("light.strip_0")
    assert not gradient.is_gradient("light.bulb")

    await hass.services.async_call("scene_studio", "apply_preset", {
        "preset_id": PRESET, "targets": {"entity_id": ["light.strip_0", "light.bulb"]}, "transition": 2},
        blocking=True)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1
    payload = aioclient_mock.mock_calls[0][2]
    seg = payload["seg"][0]
    assert seg["id"] == 0 and seg["fx"] == 65 and seg["pal"] == 4 and seg["sx"] == 0
    # whole palette once, along the strip, standing still (whatever the sliders were before)
    assert seg["ix"] == 128 and seg["c1"] == 0 and seg["o1"] is False and seg["o2"] is False
    assert len(seg["col"]) == 3 and all(len(c) == 3 for c in seg["col"])
    # one segment: the brightness goes to the whole strip
    assert "bri" in payload and "bri" not in seg and payload["tt"] == 20 and payload["on"] is True
    assert turned_on == ["light.bulb"] and published == []


async def test_wled_segments_get_their_own_brightness(hass: HomeAssistant, monkeypatch, userdata, aioclient_mock):
    await _setup(hass, monkeypatch)
    _wled_lights(hass, [0, 1])
    aioclient_mock.post("http://192.0.2.10/json/state", json={"success": True})
    gradient = runtime(hass).gradient
    assert not gradient.is_gradient("light.strip_master")  # brightness only, no segment

    await hass.services.async_call("scene_studio", "apply_preset", {
        "preset_id": PRESET, "targets": {"entity_id": ["light.strip_0", "light.strip_1"]}}, blocking=True)
    await hass.async_block_till_done()
    segs = sorted((call[2]["seg"][0] for call in aioclient_mock.mock_calls), key=lambda s: s["id"])
    assert [s["id"] for s in segs] == [0, 1]
    assert all("bri" in s for s in segs)
    assert all("bri" not in call[2] for call in aioclient_mock.mock_calls)


async def test_wled_gradient_can_be_switched_off(hass: HomeAssistant, monkeypatch, userdata, aioclient_mock):
    _, turned_on = await _setup(hass, monkeypatch)
    _wled_lights(hass, [0])
    await hass.services.async_call("scene_studio", "apply_preset", {
        "preset_id": PRESET, "targets": {"entity_id": ["light.strip_0"]}, "gradient": False}, blocking=True)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 0 and turned_on == ["light.strip_0"]


async def test_wled_segment_gets_solid_for_one_color(hass: HomeAssistant, monkeypatch, userdata, aioclient_mock):
    await _setup(hass, monkeypatch)
    _wled_lights(hass, [0])
    aioclient_mock.post("http://192.0.2.10/json/state", json={"success": True})
    bright = "84ebc26c-9d61-4d25-830c-41ea66f1c325"  # Bright, one color
    await hass.services.async_call("scene_studio", "apply_preset", {
        "preset_id": bright, "targets": {"entity_id": ["light.strip_0"]}}, blocking=True)
    await hass.async_block_till_done()
    seg = aioclient_mock.mock_calls[0][2]["seg"][0]
    assert seg["fx"] == 0 and len(seg["col"]) == 1 and "pal" not in seg
