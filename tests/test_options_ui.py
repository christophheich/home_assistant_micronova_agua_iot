"""Advanced UI sections must preserve the existing stored safety settings."""

from types import SimpleNamespace
from unittest.mock import MagicMock

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
    validated = options._build_schema()({"advanced_connection": {"http_timeout": 20}})
    stored = flow.flatten_connection_options(validated)
    assert stored["http_timeout"] == 20
    assert "allow_eeprom_writes" not in stored
    assert "connection_mode" not in stored
    assert "device_fixes" not in stored
    assert "local_wifi_advanced" not in stored
    names = [getattr(key, "schema", key) for key in options._build_schema().schema]
    assert "configure_mqtt" not in names
    assert "connection_mode" not in names
    assert "device_fixes" not in names


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
    result = await options.async_step_user(
        {
            "connection_mode": "connection_local_mqtt",
            "device_fixes": {"air_temp_fix": True},
            "air_temp_fix": True,
            "advanced_connection": {"http_timeout": 20},
        }
    )
    assert result["type"] == "create_entry"
    assert "mqtt_devices" not in result["data"]
    assert "configure_mqtt" not in result["data"]
    assert "connection_mode" not in result["data"]
    assert "device_fixes" not in result["data"]
    assert "air_temp_fix" not in result["data"]
    assert result["data"]["language"] == "DEU"
    assert result["data"]["http_timeout"] == 20
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
    assert saved["max_eeprom_writes_per_hour"] == 3
    assert saved["max_eeprom_writes_per_day"] == 5
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
    assert created.data["max_eeprom_writes_per_hour"] == 3
    assert created.data["max_eeprom_writes_per_day"] == 5
    assert created.data["write_confirmation"] is True
    assert "password" not in created.data
    handler.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")
    handler.hass.config_entries.async_update_subentry.assert_not_called()


def _schema_defaults(schema):
    found = {}
    for key in schema.schema:
        value = getattr(key, "default", None)
        if callable(value):
            value = value()
        found[getattr(key, "schema", key)] = value
    return found


@pytest.mark.asyncio
async def test_stove_row_shows_limits_and_rejects_hour_above_day():
    one = _subentry(
        "one",
        {
            "password": "test-only-secret",
            "allow_eeprom_writes": False,
            "hostname": "127.0.0.1",
            "port": 1883,
            "max_eeprom_writes_per_hour": 1,
            "max_eeprom_writes_per_day": 1,
        },
    )
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={"ble_bootstrap_devices": [{"id_device": "one", "name": "Stove"}]},
        options={},
        subentries={one.subentry_id: one},
        update_listeners=[],
    )
    handler = _wire(flow.StoveSubentryFlow(), entry, "reconfigure", one.subentry_id)
    shown = await handler.async_step_reconfigure()
    defaults = _schema_defaults(shown["data_schema"])
    assert defaults["max_eeprom_writes_per_hour"] == 1
    assert defaults["max_eeprom_writes_per_day"] == 1
    assert "test-only-secret" not in str(shown)
    rejected = await handler.async_step_reconfigure(
        {
            "device_mac": "aa:bb:cc:dd:ee:ff",
            "hostname": "127.0.0.1",
            "port": 1883,
            "allow_eeprom_writes": False,
            "write_confirmation": True,
            "max_eeprom_writes_per_hour": 2,
            "max_eeprom_writes_per_day": 1,
        }
    )
    assert rejected["type"] == "form"
    assert rejected["errors"]["base"] == "invalid_limits"
    handler.hass.config_entries.async_update_subentry.assert_not_called()
    above = await handler.async_step_reconfigure(
        {
            "device_mac": "aa:bb:cc:dd:ee:ff",
            "hostname": "127.0.0.1",
            "port": 1883,
            "allow_eeprom_writes": False,
            "write_confirmation": True,
            "max_eeprom_writes_per_hour": 16,
            "max_eeprom_writes_per_day": 50,
        }
    )
    assert above["errors"]["base"] == "invalid_limits"
    kept = await handler.async_step_reconfigure(
        {
            "device_mac": "aa:bb:cc:dd:ee:ff",
            "hostname": "127.0.0.1",
            "port": 1883,
            "allow_eeprom_writes": False,
            "write_confirmation": True,
            "max_eeprom_writes_per_hour": 1,
            "max_eeprom_writes_per_day": 1,
        }
    )
    assert kept["reason"] == "reconfigure_successful"
    saved = handler.hass.config_entries.async_update_subentry.call_args.kwargs["data"]
    assert saved["password"] == "test-only-secret"
    assert saved["allow_eeprom_writes"] is False
    assert saved["max_eeprom_writes_per_hour"] == 1
    assert saved["max_eeprom_writes_per_day"] == 1

    fresh = _wire(
        flow.StoveSubentryFlow(),
        SimpleNamespace(
            entry_id="entry-2",
            data={"ble_bootstrap_devices": [{"id_device": "two", "name": "Other"}]},
            options={},
            subentries={},
            update_listeners=[],
        ),
        "user",
    )
    opened = await fresh.async_step_user()
    opened_defaults = _schema_defaults(opened["data_schema"])
    assert opened_defaults["max_eeprom_writes_per_hour"] == 3
    assert opened_defaults["max_eeprom_writes_per_day"] == 5
    raised = await fresh.async_step_reconfigure(
        {
            "device_mac": "349454591D20",
            "hostname": "127.0.0.1",
            "port": 1883,
            "allow_eeprom_writes": False,
            "write_confirmation": True,
            "max_eeprom_writes_per_hour": 15.0,
            "max_eeprom_writes_per_day": "50",
        }
    )
    assert raised["reason"] == "reconfigure_successful"
    created = fresh.hass.config_entries.async_add_subentry.call_args.args[1]
    assert created.data["allow_eeprom_writes"] is False
    assert created.data["max_eeprom_writes_per_hour"] == 15
    assert created.data["max_eeprom_writes_per_day"] == 50


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


@pytest.mark.asyncio
async def test_each_stove_chooses_its_own_connection_and_fixes():
    one = _subentry(
        "one",
        {
            "password": "test-only-secret",
            "connection_mode": "connection_local_mqtt",
            "hostname": "127.0.0.1",
            "port": 1883,
            "device_mac": "349454591D20",
            "allow_eeprom_writes": False,
        },
    )
    two = _subentry("two", {"device_mac": "112233445566"})
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={"ble_bootstrap_devices": [{"id_device": "one", "name": "Stove"}]},
        options={
            "connection_mode": "connection_cloud",
            "device_fixes": {"air_temp_fix": False, "reading_error_fix": True},
        },
        subentries={one.subentry_id: one, two.subentry_id: two},
        update_listeners=[],
    )
    handler = _wire(flow.StoveSubentryFlow(), entry, "reconfigure", one.subentry_id)
    shown = await handler.async_step_reconfigure()
    names = [getattr(key, "schema", key) for key in shown["data_schema"].schema]
    assert "connection_mode" in names
    assert "device_fixes" in names
    defaults = _schema_defaults(shown["data_schema"])
    assert defaults["connection_mode"] == "connection_local_mqtt"
    empty = await handler.async_step_reconfigure(
        {
            "connection_mode": "connection_local_mqtt",
            "device_mac": "",
            "hostname": "127.0.0.1",
            "port": 1883,
            "allow_eeprom_writes": False,
            "write_confirmation": True,
        }
    )
    assert empty["errors"]["device_mac"] == "invalid_mac"
    handler.hass.config_entries.async_update_subentry.assert_not_called()
    bluetooth = await handler.async_step_reconfigure(
        {
            "connection_mode": "connection_bluetooth",
            "device_mac": "",
            "hostname": "127.0.0.1",
            "port": 1883,
            "allow_eeprom_writes": False,
            "write_confirmation": True,
            "device_fixes": {"air_temp_fix": True, "reading_error_fix": False},
        }
    )
    assert bluetooth["reason"] == "reconfigure_successful"
    saved = handler.hass.config_entries.async_update_subentry.call_args.kwargs["data"]
    assert saved["connection_mode"] == "connection_bluetooth"
    assert saved["device_mac"] == ""
    assert saved["password"] == "test-only-secret"
    assert saved["hostname"] == "127.0.0.1"
    assert saved["allow_eeprom_writes"] is False
    assert saved["air_temp_fix"] is True
    assert saved["reading_error_fix"] is False
    assert two.data["device_mac"] == "112233445566"
    bad = await handler.async_step_reconfigure(
        {
            "connection_mode": "carrier-pigeon",
            "device_mac": "",
            "port": 1883,
            "allow_eeprom_writes": False,
            "max_eeprom_writes_per_hour": 1,
            "max_eeprom_writes_per_day": 1,
        }
    )
    assert bad["errors"]["base"] == "invalid_connection"
