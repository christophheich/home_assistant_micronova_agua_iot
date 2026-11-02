"""Per-stove MQTT connection and EEPROM permission."""

import re
from collections.abc import Mapping

from homeassistant.helpers import device_registry as dr

from .const import CONF_BLE_BOOTSTRAP_DEVICES, DOMAIN

SUBENTRY_STOVE = "stove"


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


def ensure_stove_subentries(hass, entry):
    """Copy legacy per-stove options onto stove rows and drop the old copy."""
    from homeassistant.config_entries import ConfigSubentry

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
                data=stored,
                subentry_type=SUBENTRY_STOVE,
                title=str(title),
                unique_id=device_id,
            ),
        )
    if "mqtt_devices" in options:
        updated = dict(options)
        updated.pop("mqtt_devices", None)
        hass.config_entries.async_update_entry(entry, options=updated)


def attach_stove_devices(hass, entry):
    """Show each stove under its own row on the integration page."""
    registry = dr.async_get(hass)
    for subentry in _subentries(entry).values():
        if subentry.subentry_type != SUBENTRY_STOVE or not subentry.unique_id:
            continue
        device = registry.async_get_device_by_identifier(
            (DOMAIN, subentry.unique_id), entry.entry_id
        )
        if device is None or device.config_subentry_id == subentry.subentry_id:
            continue
        registry.async_update_device(
            device.id, new_config_subentry_id=subentry.subentry_id
        )


def normalize_stove_settings(entry, device_id, user_input, stored):
    """Validate one stove form and keep a blank password."""
    values = dict(user_input)
    mac = re.sub(r"[:-]", "", str(values.get("device_mac", ""))).strip().upper()
    errors = {}
    if not re.fullmatch(r"[0-9A-F]{12}", mac):
        errors["device_mac"] = "invalid_mac"
    else:
        for subentry in _subentries(entry).values():
            if subentry.subentry_type != SUBENTRY_STOVE or str(
                subentry.unique_id
            ) == str(device_id):
                continue
            other = (
                re.sub(r"[:-]", "", str(subentry.data.get("device_mac", "")))
                .strip()
                .upper()
            )
            if other == mac:
                errors["device_mac"] = "invalid_mac"
                break
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
    return values, errors
