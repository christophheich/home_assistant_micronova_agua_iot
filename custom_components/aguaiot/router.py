"""One transport per stove connection, so Cloud, Bluetooth and MQTT can mix."""

import logging

from .aguaiot import aguaiot
from .const import (
    CONF_BLE_BOOTSTRAP_DEVICES,
    CONF_BLE_CHAR_UUID,
    CONF_BLE_SERVICE_UUID,
    CONNECTION_MODE_BLUETOOTH,
    CONNECTION_MODE_CLOUD,
    CONNECTION_MODE_LOCAL_MQTT,
)
from .local_ble import DEFAULT_CHAR_UUID, DEFAULT_SERVICE_UUID, LocalBleAguaIOT
from .local_mqtt import LocalMqttAguaIOT
from .stoves import stove_connection_mode, stove_settings_by_id

_LOGGER = logging.getLogger(__name__)


class StoveRouter:
    """Poll each connection on its own. One failure leaves the other stoves up."""

    def __init__(self, clients, taken_ids):
        self._clients = clients
        self._taken = {str(device_id) for device_id in taken_ids}
        self.devices = []

    async def connect(self):
        self.devices = []
        errors = []
        for mode, client in self._clients:
            try:
                await client.connect()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Stove connection %s failed: %s", mode, err)
                errors.append(err)
                close = getattr(client, "close", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        _LOGGER.debug(
                            "Closing stove connection %s failed", mode, exc_info=True
                        )
                continue
            if mode == CONNECTION_MODE_CLOUD:
                client.devices = [
                    device
                    for device in client.devices
                    if str(device.id_device) not in self._taken
                ]
            self.devices.extend(client.devices)
        if not self.devices and errors:
            raise errors[0]

    async def update(self):
        errors = []
        polled = 0
        for _mode, client in self._clients:
            if not getattr(client, "devices", None):
                continue
            polled += 1
            try:
                await client.update()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Stove update failed: %s", err)
                errors.append(err)
        if polled and len(errors) == polled:
            raise errors[0]

    async def close(self):
        for _mode, client in self._clients:
            if hasattr(client, "close"):
                await client.close()

    @property
    def cache_dirty(self):
        return any(
            getattr(client, "cache_dirty", False) for _mode, client in self._clients
        )

    def export_bootstrap_cache(self):
        merged = {}
        for _mode, client in self._clients:
            if hasattr(client, "export_bootstrap_cache"):
                items = client.export_bootstrap_cache() or []
            else:
                items = [
                    device.export_cache()
                    for device in getattr(client, "devices", [])
                    if hasattr(device, "export_cache")
                ]
            for item in items:
                if not isinstance(item, dict):
                    continue
                device_id = str(item.get("id_device") or "")
                if device_id and item.get("register_map"):
                    merged[device_id] = item
        return list(merged.values())

    def mark_cache_persisted(self):
        for _mode, client in self._clients:
            if hasattr(client, "mark_cache_persisted"):
                client.mark_cache_persisted()


def _cached_for(cached, device_ids):
    allowed = {str(device_id) for device_id in device_ids}
    return [
        device
        for device in cached
        if isinstance(device, dict) and str(device.get("id_device") or "") in allowed
    ]


def _one_client(hass, entry, mode, settings, cached, common, only_device_ids):
    options = getattr(entry, "options", None) or {}
    if mode == CONNECTION_MODE_BLUETOOTH:
        return LocalBleAguaIOT(
            hass=hass,
            cached_devices=cached,
            service_uuid=options.get(CONF_BLE_SERVICE_UUID, DEFAULT_SERVICE_UUID),
            char_uuid=options.get(CONF_BLE_CHAR_UUID, DEFAULT_CHAR_UUID),
            device_options=settings,
            only_device_ids=only_device_ids,
            **common,
        )
    if mode == CONNECTION_MODE_LOCAL_MQTT:
        return LocalMqttAguaIOT(
            hass=hass,
            mqtt_devices=settings,
            cached_devices=cached,
            only_device_ids=only_device_ids,
            **common,
        )
    return aguaiot(**common, hass=hass, device_options=settings)


def build_transport(hass, entry, client_kwargs):
    """Return one client, or a router when the stoves disagree on the connection."""
    settings = stove_settings_by_id(entry)
    cached = entry.data.get(CONF_BLE_BOOTSTRAP_DEVICES) or []
    if not isinstance(cached, list):
        cached = []
    common = dict(client_kwargs)
    if not settings:
        mode = stove_connection_mode(entry, {})
        return _one_client(hass, entry, mode, {}, cached, common, None)
    # The per-stove flags decide. A shared client flag would hit every stove.
    common["air_temp_fix"] = False
    common["reading_error_fix"] = False

    grouped = {}
    for device_id, stove in settings.items():
        grouped.setdefault(stove_connection_mode(entry, stove), {})[device_id] = stove
    if len(grouped) == 1:
        mode, chosen = next(iter(grouped.items()))
        if mode == CONNECTION_MODE_CLOUD:
            return _one_client(hass, entry, mode, chosen, cached, common, None)
        return _one_client(
            hass,
            entry,
            mode,
            chosen,
            _cached_for(cached, chosen),
            common,
            set(chosen),
        )

    taken = {
        device_id
        for mode, chosen in grouped.items()
        if mode != CONNECTION_MODE_CLOUD
        for device_id in chosen
    }
    clients = []
    for mode, chosen in grouped.items():
        if mode == CONNECTION_MODE_CLOUD:
            selected, only = cached, None
        else:
            selected, only = _cached_for(cached, chosen), set(chosen)
        clients.append(
            (mode, _one_client(hass, entry, mode, chosen, selected, common, only))
        )
    return StoveRouter(clients, taken)
