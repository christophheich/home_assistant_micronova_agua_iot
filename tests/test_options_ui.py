"""MQTT options preserve existing account and stove settings."""

from unittest.mock import MagicMock

import pytest

from tests.helpers import load_component_module

flow = load_component_module(
    "config_flow",
    stubbed_modules={
        "local_ble": {
            "DEFAULT_CHAR_UUID": "0000ffe1-0000-1000-8000-00805f9b34fb",
            "DEFAULT_SERVICE_UUID": "0000ffe5-0000-1000-8000-00805f9b34fb",
            "LocalBleAguaIOT": MagicMock,
        }
    },
)


def test_mqtt_options_are_neutral_and_have_no_write_limits(monkeypatch):
    options = flow.AguaIOTOptionsFlowHandler()
    entry = MagicMock(options={}, runtime_data=MagicMock())
    monkeypatch.setattr(type(options), "config_entry", property(lambda _self: entry))
    monkeypatch.setattr(options, "_languages", lambda: ["ENG"])
    validated = options._build_schema()(
        {
            "connection_mode": "connection_local_mqtt",
            "http_timeout": 20,
            "device_fixes": {},
        }
    )
    stored = validated
    assert stored["http_timeout"] == 20
    assert "allow_eeprom_writes" not in stored
    assert "local_wifi_advanced" not in stored


def test_options_work_before_runtime_setup(monkeypatch):
    from types import SimpleNamespace

    options = flow.AguaIOTOptionsFlowHandler()
    monkeypatch.setattr(
        type(options),
        "config_entry",
        property(lambda _self: SimpleNamespace(options={})),
    )
    assert options._languages() == ["ENG"]
    assert options._build_schema() is not None


@pytest.mark.asyncio
async def test_per_stove_endpoint_preserves_password_and_other_devices(monkeypatch):
    options = flow.AguaIOTOptionsFlowHandler()
    entry = MagicMock(
        data={"ble_bootstrap_devices": [{"id_device": "one", "name": "Stove"}]},
        options={
            "connection_mode": "connection_cloud",
            "language": "DEU",
            "mqtt_devices": {
                "one": {"password": "test-only-secret"},
                "two": {"device_mac": "112233445566"},
            },
        },
    )
    monkeypatch.setattr(type(options), "config_entry", property(lambda _self: entry))
    await options.async_step_mqtt_device({"device": "one"})
    shown = await options.async_step_mqtt_settings()
    assert "test-only-secret" not in str(shown)
    invalid = await options.async_step_mqtt_settings({"device_mac": "112233445566"})
    assert invalid["errors"] == {"device_mac": "invalid_mac"}
    result = await options.async_step_mqtt_settings(
        {
            "device_mac": "aa:bb:cc:dd:ee:ff",
            "hostname": "broker.example",
            "port": 1883,
            "username": "client",
            "write_confirmation": False,
        }
    )
    assert result["data"]["connection_mode"] == "connection_cloud"
    assert result["data"]["language"] == "DEU"
    assert result["data"]["mqtt_devices"]["two"] == entry.options["mqtt_devices"]["two"]
    assert result["data"]["mqtt_devices"]["one"]["password"] == "test-only-secret"
    assert result["data"]["mqtt_devices"]["one"]["device_mac"] == "AABBCCDDEEFF"
