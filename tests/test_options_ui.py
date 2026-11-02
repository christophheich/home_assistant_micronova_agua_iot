"""Advanced UI sections must preserve the existing stored safety settings."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigSubentry

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
            "advanced_connection": {"http_timeout": 20},
            "device_fixes": {},
        }
    )
    stored = flow.flatten_connection_options(validated)
    assert stored["http_timeout"] == 20
    assert "allow_eeprom_writes" not in stored
    assert "local_wifi_advanced" not in stored
    names = [getattr(key, "schema", key) for key in options._build_schema().schema]
    assert "configure_mqtt" not in names


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


def _subentry(unique_id, data, title=None):
    return ConfigSubentry(
        data=data,
        subentry_type="stove",
        title=title or unique_id,
        unique_id=unique_id,
    )


def _wire(handler, entry, source, subentry_id=None):
    handler.hass = MagicMock()
    handler.handler = (entry.entry_id, "stove")
    handler.context = {"source": source}
    if subentry_id is not None:
        handler.context["subentry_id"] = subentry_id
    handler.hass.config_entries.async_get_known_entry.return_value = entry
    return handler


@pytest.mark.asyncio
async def test_account_save_drops_the_legacy_stove_copy(monkeypatch):
    options = flow.AguaIOTOptionsFlowHandler()
    entry = MagicMock(
        options={
            "connection_mode": "connection_cloud",
            "language": "DEU",
            "configure_mqtt": True,
            "mqtt_devices": {"one": {"password": "test-only-secret"}},
        }
    )
    monkeypatch.setattr(type(options), "config_entry", property(lambda _self: entry))
    agua = MagicMock()
    agua.connect = AsyncMock()
    agua.close = AsyncMock()
    agua.validate_local_connection = AsyncMock()
    monkeypatch.setattr(options, "_build_client", lambda _mode: agua)
    result = await options.async_step_user(
        {
            "connection_mode": "connection_local_mqtt",
            "device_fixes": {},
            "advanced_connection": {"http_timeout": 20},
        }
    )
    assert result["type"] == "create_entry"
    assert "mqtt_devices" not in result["data"]
    assert "configure_mqtt" not in result["data"]
    assert result["data"]["language"] == "DEU"
    assert result["data"]["http_timeout"] == 20
    assert result["data"]["connection_mode"] == "connection_local_mqtt"
    assert "test-only-secret" not in str(result["data"])


@pytest.mark.asyncio
async def test_stove_row_keeps_password_and_leaves_the_other_stove():
    one = _subentry(
        "one",
        {
            "password": "test-only-secret",
            "allow_eeprom_writes": False,
            "write_confirmation": True,
            "hostname": "127.0.0.1",
            "port": 1883,
        },
    )
    two = _subentry("two", {"device_mac": "112233445566"})
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={
            "ble_bootstrap_devices": [
                {"id_device": "one", "name": "Stove"},
                {"id_device": "two", "name": "Other"},
            ]
        },
        options={"connection_mode": "connection_cloud", "language": "DEU"},
        subentries={one.subentry_id: one, two.subentry_id: two},
        update_listeners=[],
    )
    handler = _wire(flow.StoveSubentryFlow(), entry, "reconfigure", one.subentry_id)
    shown = await handler.async_step_reconfigure()
    assert shown["step_id"] == "reconfigure"
    assert "test-only-secret" not in str(shown)
    invalid = await handler.async_step_reconfigure({"device_mac": "112233445566"})
    assert invalid["errors"] == {"device_mac": "invalid_mac"}
    result = await handler.async_step_reconfigure(
        {
            "device_mac": "aa:bb:cc:dd:ee:ff",
            "hostname": "broker.example",
            "port": 1883,
            "username": "client",
            "allow_eeprom_writes": False,
            "write_confirmation": False,
        }
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    saved = handler.hass.config_entries.async_update_subentry.call_args.kwargs["data"]
    assert saved["password"] == "test-only-secret"
    assert saved["device_mac"] == "AABBCCDDEEFF"
    assert saved["allow_eeprom_writes"] is False
    assert saved["hostname"] == "broker.example"
    assert two.data["device_mac"] == "112233445566"
    handler.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")


@pytest.mark.asyncio
async def test_one_cached_stove_opens_its_form_and_reloads():
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={"ble_bootstrap_devices": [{"id_device": "one", "name": "Cadel"}]},
        options={},
        subentries={},
        update_listeners=[],
    )
    handler = _wire(flow.StoveSubentryFlow(), entry, "user")
    shown = await handler.async_step_user()
    assert shown["step_id"] == "reconfigure"
    result = await handler.async_step_reconfigure(
        {
            "device_mac": "34:94:54:59:1d:20",
            "hostname": "127.0.0.1",
            "port": 1883,
            "allow_eeprom_writes": False,
            "write_confirmation": True,
        }
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    created = handler.hass.config_entries.async_add_subentry.call_args.args[1]
    assert created.unique_id == "one"
    assert created.title == "Cadel"
    assert created.data["device_mac"] == "349454591D20"
    assert created.data["hostname"] == "127.0.0.1"
    assert created.data["port"] == 1883
    assert created.data["allow_eeprom_writes"] is False
    assert created.data["write_confirmation"] is True
    assert "password" not in created.data
    handler.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")
    handler.hass.config_entries.async_update_subentry.assert_not_called()


@pytest.mark.asyncio
async def test_stove_add_stops_when_nothing_is_left():
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={},
        subentries={},
        update_listeners=[],
    )
    empty = _wire(flow.StoveSubentryFlow(), entry, "user")
    assert (await empty.async_step_user())["reason"] == "no_cached_devices"
    existing = _subentry("one", {})
    full = SimpleNamespace(
        entry_id="entry-1",
        data={"ble_bootstrap_devices": [{"id_device": "one", "name": "Cadel"}]},
        options={},
        subentries={existing.subentry_id: existing},
        update_listeners=[],
    )
    done = _wire(flow.StoveSubentryFlow(), full, "user")
    assert (await done.async_step_user())["reason"] == "already_configured"
