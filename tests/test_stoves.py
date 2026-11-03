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
    assert created.data["connection_mode"] == "connection_local_mqtt"
    assert created.data["air_temp_fix"] is False
    assert created.data["reading_error_fix"] is False
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


def test_existing_stove_keeps_its_connection_and_gains_missing_fixes():
    current = ConfigSubentry(
        data={"hostname": "127.0.0.1", "connection_mode": "connection_bluetooth"},
        subentry_type="stove",
        title="Ble",
        unique_id="one",
    )
    entry = SimpleNamespace(
        data={},
        options={
            "connection_mode": "connection_local_mqtt",
            "device_fixes": {"air_temp_fix": True, "reading_error_fix": False},
        },
        subentries={current.subentry_id: current},
    )
    hass = MagicMock()
    stoves.ensure_stove_subentries(hass, entry)
    hass.config_entries.async_add_subentry.assert_not_called()
    saved = hass.config_entries.async_update_subentry.call_args.kwargs["data"]
    assert saved["connection_mode"] == "connection_bluetooth"
    assert saved["hostname"] == "127.0.0.1"
    assert saved["air_temp_fix"] is True
    assert saved["reading_error_fix"] is False


def _entity_registry(monkeypatch, entities):
    ereg = MagicMock()
    monkeypatch.setattr(stoves.er, "async_get", lambda _hass: ereg)
    monkeypatch.setattr(
        stoves.er,
        "async_entries_for_device",
        lambda _registry, _device_id, include_disabled_entities=False: entities,
    )
    return ereg


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
    _entity_registry(monkeypatch, [])
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


def test_entities_keep_their_data_when_the_stove_row_changes(monkeypatch):
    current = ConfigSubentry(
        data={},
        subentry_type="stove",
        title="Cadel",
        unique_id="one",
    )
    entry = SimpleNamespace(
        subentries={current.subentry_id: current}, entry_id="entry-1"
    )
    device = SimpleNamespace(id="dev", config_subentry_id=None)
    kept = SimpleNamespace(
        entity_id="climate.pelletofen",
        config_entry_id="entry-1",
        config_subentry_id=None,
        disabled_by="user",
    )
    foreign = SimpleNamespace(
        entity_id="sensor.other",
        config_entry_id="other",
        config_subentry_id=None,
    )
    ready = SimpleNamespace(
        entity_id="sensor.pelletofen_status",
        config_entry_id="entry-1",
        config_subentry_id=current.subentry_id,
    )
    order = []
    registry = MagicMock()
    registry.async_get_device_by_identifier.return_value = device

    def update_device(*_args, **_kwargs):
        order.append("device")
        assert kept.config_subentry_id == current.subentry_id

    registry.async_update_device.side_effect = update_device
    monkeypatch.setattr(stoves.dr, "async_get", lambda _hass: registry)
    ereg = MagicMock()

    def update_entity(entity_id, **kwargs):
        order.append(entity_id)
        if entity_id == kept.entity_id:
            kept.config_subentry_id = kwargs["config_subentry_id"]

    ereg.async_update_entity.side_effect = update_entity
    included = {}

    def entries(_registry, device_id, include_disabled_entities=False):
        included["disabled"] = include_disabled_entities
        included["device_id"] = device_id
        return [kept, foreign, ready]

    monkeypatch.setattr(stoves.er, "async_get", lambda _hass: ereg)
    monkeypatch.setattr(stoves.er, "async_entries_for_device", entries)
    stoves.attach_stove_devices(MagicMock(), entry)
    assert included["disabled"] is True
    assert included["device_id"] == "dev"
    assert order == ["climate.pelletofen", "device"]
    ereg.async_update_entity.assert_called_once_with(
        "climate.pelletofen", config_subentry_id=current.subentry_id
    )
    device.config_subentry_id = current.subentry_id
    kept.config_subentry_id = None
    registry.async_update_device.reset_mock()
    ereg.async_update_entity.reset_mock()
    stoves.attach_stove_devices(MagicMock(), entry)
    registry.async_update_device.assert_not_called()
    ereg.async_update_entity.assert_called_once_with(
        "climate.pelletofen", config_subentry_id=current.subentry_id
    )


def test_entities_join_the_stove_row():
    current = ConfigSubentry(
        data={},
        subentry_type="stove",
        title="Cadel",
        unique_id="one",
    )
    entry = SimpleNamespace(subentries={current.subentry_id: current})
    calls = []

    def add(entities, update, config_subentry_id=None):
        calls.append((list(entities), update, config_subentry_id))

    matched = SimpleNamespace(_device=SimpleNamespace(id_device="one"))
    missing = SimpleNamespace(_device=SimpleNamespace(id_device="other"))
    stoves.add_stove_entities(entry, add, [])
    assert calls == [([], True, None)]
    calls.clear()
    stoves.add_stove_entities(entry, add, [matched, missing])
    assert calls == [
        ([matched], True, current.subentry_id),
        ([missing], True, None),
    ]
