"""Stove rows keep the connection that used to live in the integration options."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigSubentry

from tests.helpers import load_component_module

stoves = load_component_module("stoves")


def _remember(entry, subentry):
    entry.subentries[subentry.subentry_id] = subentry


def test_legacy_mqtt_devices_move_onto_the_stove_row():
    hass = MagicMock()
    hass.config_entries.async_add_subentry.side_effect = _remember
    entry = SimpleNamespace(
        data={"ble_bootstrap_devices": [{"id_device": "one", "name": "Cadel"}]},
        options={
            "connection_mode": "connection_local_mqtt",
            "mqtt_devices": {
                "one": {
                    "hostname": "127.0.0.1",
                    "port": 1883,
                    "device_mac": "349454591D20",
                    "username": "navel-agua",
                    "password": "test-only-secret",
                    "allow_eeprom_writes": False,
                    "write_confirmation": True,
                }
            },
        },
        subentries={},
    )
    stoves.ensure_stove_subentries(hass, entry)
    created = hass.config_entries.async_add_subentry.call_args.args[1]
    assert created.title == "Cadel"
    assert created.unique_id == "one"
    assert created.data["hostname"] == "127.0.0.1"
    assert created.data["port"] == 1883
    assert created.data["device_mac"] == "349454591D20"
    assert created.data["allow_eeprom_writes"] is False
    assert created.data["write_confirmation"] is True
    assert created.data["password"] == "test-only-secret"
    updated = hass.config_entries.async_update_entry.call_args.kwargs["options"]
    assert updated["connection_mode"] == "connection_local_mqtt"
    assert "mqtt_devices" not in updated
    entry.options = updated
    stoves.ensure_stove_subentries(hass, entry)
    assert hass.config_entries.async_add_subentry.call_count == 1
    assert hass.config_entries.async_update_entry.call_count == 1
    settings = stoves.stove_settings_by_id(entry)
    assert settings["one"]["hostname"] == "127.0.0.1"
    assert "id_device" not in settings["one"]


def test_stove_row_wins_over_a_leftover_options_copy():
    current = ConfigSubentry(
        data={"hostname": "127.0.0.1", "allow_eeprom_writes": False},
        subentry_type="stove",
        title="Cadel",
        unique_id="one",
    )
    entry = SimpleNamespace(
        options={"mqtt_devices": {"one": {"hostname": "old.example"}}},
        subentries={current.subentry_id: current},
    )
    assert stoves.stove_settings_by_id(entry)["one"]["hostname"] == "127.0.0.1"
    assert stoves.stove_settings_by_id(entry)["one"]["allow_eeprom_writes"] is False


def test_device_moves_under_its_stove_row(monkeypatch):
    current = ConfigSubentry(
        data={},
        subentry_type="stove",
        title="Cadel",
        unique_id="one",
    )
    entry = SimpleNamespace(subentries={current.subentry_id: current})
    device = SimpleNamespace(id="dev", config_subentry_id=None)
    registry = MagicMock()
    registry.async_get_device_by_identifier.return_value = device
    monkeypatch.setattr(stoves.dr, "async_get", lambda _hass: registry)
    entry.entry_id = "entry-1"
    stoves.attach_stove_devices(MagicMock(), entry)
    registry.async_get_device_by_identifier.assert_called_with(
        ("aguaiot", "one"), "entry-1"
    )
    registry.async_update_device.assert_called_once_with(
        "dev", new_config_subentry_id=current.subentry_id
    )
    device.config_subentry_id = current.subentry_id
    registry.async_update_device.reset_mock()
    stoves.attach_stove_devices(MagicMock(), entry)
    registry.async_update_device.assert_not_called()
    registry.async_get_device_by_identifier.return_value = None
    stoves.attach_stove_devices(MagicMock(), entry)
    registry.async_update_device.assert_not_called()
