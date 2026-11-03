"""Per-stove connection, device fixes and EEPROM permission."""

import re
from collections.abc import Mapping

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_AIR_TEMP_FIX,
    CONF_BLE_BOOTSTRAP_DEVICES,
    CONF_CONNECTION_MODE,
    CONF_DEVICE_FIXES,
    CONF_READING_ERROR_FIX,
    CONNECTION_MODE_BLUETOOTH,
    CONNECTION_MODE_CLOUD,
    CONNECTION_MODE_LOCAL_MQTT,
    DOMAIN,
)
from .eeprom import DAILY_LIMIT, HOURLY_LIMIT, checked_limits

SUBENTRY_STOVE = "stove"
_CONNECTION_MODES = (
    CONNECTION_MODE_CLOUD,
    CONNECTION_MODE_BLUETOOTH,
    CONNECTION_MODE_LOCAL_MQTT,
)


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _subentries(entry):
    return _mapping(getattr(entry, "subentries", None))


def stove_settings_by_id(entry):
    """Return MQTT settings keyed by stove id.

    A stove subentry wins over a legacy options copy.
    """
    settings = {}
    legacy = _mapping(_mapping(getattr(entry, "options", None)).get("mqtt_devices"))
    for device_id, value in legacy.items():
        if isinstance(value, dict):
            settings[str(device_id)] = dict(value)
    for subentry in _subentries(entry).values():
        if subentry.subentry_type != SUBENTRY_STOVE or not subentry.unique_id:
            continue
        current = dict(subentry.data)
        current.pop("id_device", None)
        settings[str(subentry.unique_id)] = current
    return settings


def account_stove_defaults(entry):
    """Return the connection and fixes a stove inherits until it stores its own."""
    options = _mapping(getattr(entry, "options", None))
    fixes = _mapping(options.get(CONF_DEVICE_FIXES))
    mode = options.get(CONF_CONNECTION_MODE, CONNECTION_MODE_CLOUD)
    if mode not in _CONNECTION_MODES:
        mode = CONNECTION_MODE_CLOUD
    return {
        CONF_CONNECTION_MODE: mode,
        CONF_AIR_TEMP_FIX: fixes.get(
            CONF_AIR_TEMP_FIX, options.get(CONF_AIR_TEMP_FIX, False)
        )
        is True,
        CONF_READING_ERROR_FIX: fixes.get(
            CONF_READING_ERROR_FIX, options.get(CONF_READING_ERROR_FIX, False)
        )
        is True,
    }


def stove_connection_mode(entry, settings):
    """Return the connection stored on the stove, or the account default."""
    mode = (settings or {}).get(CONF_CONNECTION_MODE)
    if mode in _CONNECTION_MODES:
        return mode
    return account_stove_defaults(entry)[CONF_CONNECTION_MODE]


def ensure_stove_subentries(hass, entry):
    """Copy legacy per-stove options onto stove rows and drop the old copy."""
    from homeassistant.config_entries import ConfigSubentry

    defaults = account_stove_defaults(entry)
    names = {}
    devices = _mapping(getattr(entry, "data", None)).get(CONF_BLE_BOOTSTRAP_DEVICES)
    devices = devices or []
    if not isinstance(devices, list):
        devices = []
    for device in devices:
        if not isinstance(device, Mapping):
            continue
        device_id = str(device.get("id_device") or "")
        if device_id:
            names[device_id] = device.get("name") or device_id
    options = _mapping(getattr(entry, "options", None))
    legacy = _mapping(options.get("mqtt_devices"))
    known = set(names) | {str(device_id) for device_id in legacy}
    existing = {
        str(subentry.unique_id)
        for subentry in _subentries(entry).values()
        if subentry.subentry_type == SUBENTRY_STOVE and subentry.unique_id
    }
    for device_id in sorted(known):
        if device_id in existing:
            continue
        stored_raw = legacy.get(device_id) or {}
        stored = dict(stored_raw) if isinstance(stored_raw, Mapping) else {}
        title = names.get(device_id) or stored.get("device_mac") or device_id
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data={**defaults, **stored},
                subentry_type=SUBENTRY_STOVE,
                title=str(title),
                unique_id=device_id,
            ),
        )
    for subentry in list(_subentries(entry).values()):
        if subentry.subentry_type != SUBENTRY_STOVE or not subentry.unique_id:
            continue
        current = dict(subentry.data)
        missing = {key: value for key, value in defaults.items() if key not in current}
        if not missing:
            continue
        hass.config_entries.async_update_subentry(
            entry, subentry, data={**current, **missing}
        )
    if "mqtt_devices" in options:
        updated = dict(options)
        updated.pop("mqtt_devices", None)
        hass.config_entries.async_update_entry(entry, options=updated)


def stove_subentry_id(entry, device_id):
    """Return the stove row id for one device, or None."""
    if not device_id:
        return None
    for subentry in _subentries(entry).values():
        if subentry.subentry_type == SUBENTRY_STOVE and str(subentry.unique_id) == str(
            device_id
        ):
            return subentry.subentry_id
    return None


def add_stove_entities(config_entry, async_add_entities, entities):
    """Register entities on the same stove row as their device."""
    if not entities:
        async_add_entities([], True)
        return
    grouped = {}
    for entity in entities:
        device = getattr(entity, "_device", None)
        subentry_id = stove_subentry_id(
            config_entry, getattr(device, "id_device", None)
        )
        grouped.setdefault(subentry_id, []).append(entity)
    for subentry_id, group in grouped.items():
        if subentry_id:
            async_add_entities(group, True, config_subentry_id=subentry_id)
        else:
            async_add_entities(group, True)


def attach_stove_devices(hass, entry):
    """Move a stove onto its row without dropping the entities that belong to it.

    Home Assistant removes entities that still carry the old row when the
    device row changes. Point the entities at the new row first.
    """
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for subentry in _subentries(entry).values():
        if subentry.subentry_type != SUBENTRY_STOVE or not subentry.unique_id:
            continue
        device = devices.async_get_device_by_identifier(
            (DOMAIN, subentry.unique_id), entry.entry_id
        )
        if device is None:
            continue
        for entity in er.async_entries_for_device(
            entities, device.id, include_disabled_entities=True
        ):
            if entity.config_entry_id != entry.entry_id:
                continue
            if entity.config_subentry_id == subentry.subentry_id:
                continue
            entities.async_update_entity(
                entity.entity_id, config_subentry_id=subentry.subentry_id
            )
        if device.config_subentry_id != subentry.subentry_id:
            devices.async_update_device(
                device.id, new_config_subentry_id=subentry.subentry_id
            )


def _mac_taken(entry, device_id, mac):
    for subentry in _subentries(entry).values():
        if subentry.subentry_type != SUBENTRY_STOVE or str(subentry.unique_id) == str(
            device_id
        ):
            continue
        other = (
            re.sub(r"[:-]", "", str(subentry.data.get("device_mac", "")))
            .strip()
            .upper()
        )
        if other and other == mac:
            return True
    return False


def normalize_stove_settings(entry, device_id, user_input, stored):
    """Validate one stove form and keep a blank password."""
    values = dict(user_input)
    stored = stored or {}
    defaults = account_stove_defaults(entry)
    errors = {}
    if CONF_CONNECTION_MODE in values:
        mode = values[CONF_CONNECTION_MODE]
        if mode not in _CONNECTION_MODES:
            errors["base"] = "invalid_connection"
            mode = stove_connection_mode(entry, stored)
    else:
        mode = stove_connection_mode(entry, stored)
    values[CONF_CONNECTION_MODE] = mode
    for key in (CONF_AIR_TEMP_FIX, CONF_READING_ERROR_FIX):
        if key in values:
            values[key] = values[key] is True
        elif key in stored:
            values[key] = stored[key] is True
        else:
            values[key] = defaults[key]
    mac = re.sub(r"[:-]", "", str(values.get("device_mac", "") or "")).strip().upper()
    mac_invalid = (mode == CONNECTION_MODE_LOCAL_MQTT and not mac) or (
        bool(mac) and not re.fullmatch(r"[0-9A-F]{12}", mac)
    )
    if mac_invalid or (mac and _mac_taken(entry, device_id, mac)):
        errors["device_mac"] = "invalid_mac"
    if not values.get("password") and stored.get("password"):
        values["password"] = stored["password"]
    elif not values.get("password"):
        values.pop("password", None)
    values["device_mac"] = mac
    values["port"] = int(values.get("port", stored.get("port", 1883)))
    values["hostname"] = values.get("hostname", stored.get("hostname", "")) or ""
    values["username"] = values.get("username", stored.get("username", "")) or ""
    if "allow_eeprom_writes" not in values:
        values["allow_eeprom_writes"] = stored.get("allow_eeprom_writes", True)
    if "write_confirmation" not in values:
        values["write_confirmation"] = stored.get("write_confirmation", False)
    checked = checked_limits(
        values.get(
            "max_eeprom_writes_per_hour",
            stored.get("max_eeprom_writes_per_hour", HOURLY_LIMIT),
        ),
        values.get(
            "max_eeprom_writes_per_day",
            stored.get("max_eeprom_writes_per_day", DAILY_LIMIT),
        ),
    )
    if checked is None:
        errors.setdefault("base", "invalid_limits")
    else:
        values["max_eeprom_writes_per_hour"] = checked[0]
        values["max_eeprom_writes_per_day"] = checked[1]
    return values, errors
