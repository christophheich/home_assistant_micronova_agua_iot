"""Per-stove reservations for EEPROM writes."""

import asyncio
import json
import math
import time
from pathlib import Path

from homeassistant.helpers.storage import Store

from .aguaiot import AguaIOTError

HOURLY_LIMIT = 15
DAILY_LIMIT = 50
EEPROM_OFFSET_THRESHOLD = 32768


def clean_mac(value):
    """Return a 12-digit MAC, or None."""
    if not isinstance(value, str) or not value:
        return None
    mac = value.replace(":", "").replace("-", "").strip().upper()
    if len(mac) == 12 and all(char in "0123456789ABCDEF" for char in mac):
        return mac
    return None


def legacy_mac_for(device, device_options, explicit=None):
    """MAC used by older MQTT-only budget files."""
    settings = (device_options or {}).get(str(getattr(device, "id_device", "")), {})
    return (
        clean_mac(explicit)
        or clean_mac(settings.get("device_mac"))
        or clean_mac(getattr(device, "product_serial", None))
    )


def writes_allowed(device_options, device_id):
    """Missing stove settings stay allowed. Only an explicit false blocks."""
    settings = (device_options or {}).get(str(device_id), {})
    return settings.get("allow_eeprom_writes", True) is True


class EepromBudget:
    """Reserve before sending. Uncertain writes still consume the budget."""

    def __init__(self, hass, stove_id, legacy_id=None):
        self.hass = hass
        self.store = Store(hass, 1, f"aguaiot.{stove_id}.eeprom", atomic_writes=True)
        self.legacy_id = clean_mac(legacy_id) if legacy_id else None
        self.lock = asyncio.Lock()
        self.history = None

    def _path_exists(self, path):
        file_path = Path(path)
        return file_path.exists() or any(
            file_path.parent.glob(f"{file_path.name}.corrupt.*")
        )

    def _verify(self, history):
        with Path(self.store.path).open() as source:
            if json.load(source).get("data") != history:
                raise OSError("EEPROM reservation was not saved")

    async def _load(self, store):
        history = await store.async_load()
        if history is None and await self.hass.async_add_executor_job(
            self._path_exists, store.path
        ):
            raise ValueError("Unreadable EEPROM history")
        return history

    def _check_history(self, history):
        if not isinstance(history, list) or any(
            type(value) not in (float, int) or not math.isfinite(value) or value < 0
            for value in history
        ):
            raise ValueError("Invalid EEPROM history")

    async def reserve(self):
        async with self.lock:
            try:
                if self.history is None:
                    history = await self._load(self.store)
                    if history is None and self.legacy_id:
                        legacy = Store(
                            self.hass,
                            1,
                            f"aguaiot.mqtt.{self.legacy_id}.eeprom",
                            atomic_writes=True,
                        )
                        history = await self._load(legacy)
                    if history is None:
                        history = []
                    self._check_history(history)
                    self.history = history
                now = time.time()
                # Keep future reservations if the system clock moves backwards.
                self.history = [stamp for stamp in self.history if now - stamp < 86400]
                if (
                    len(self.history) >= DAILY_LIMIT
                    or sum(now - stamp < 3600 for stamp in self.history) >= HOURLY_LIMIT
                ):
                    raise AguaIOTError(
                        "EEPROM write limit reached for this stove "
                        "(15 per hour, 50 per day). Try again later."
                    )
                self.history.append(now)
                await self.store.async_save(self.history)
                await self.hass.async_add_executor_job(self._verify, self.history)
            except (OSError, ValueError, TypeError) as err:
                raise AguaIOTError(
                    "Cannot verify the EEPROM write budget. No command was sent."
                ) from err


async def reserve_eeprom_write(
    hass, stove_id, offsets, *, allowed=True, legacy_id=None
):
    """Count one EEPROM command, or return when every offset is RAM."""
    parsed = []
    for offset in offsets:
        value = int(offset)
        if value < 0 or value > 65535:
            raise AguaIOTError("Invalid register offset")
        parsed.append(value)
    if not any(value >= EEPROM_OFFSET_THRESHOLD for value in parsed):
        return
    if allowed is not True:
        raise AguaIOTError("EEPROM writes are blocked for this stove.")
    if hass is None or not stove_id:
        raise AguaIOTError(
            "Cannot verify the EEPROM write budget. No command was sent."
        )
    budgets = hass.data.setdefault("aguaiot", {}).setdefault("eeprom_budgets", {})
    budget = budgets.get(stove_id)
    if budget is None:
        budget = EepromBudget(hass, stove_id, legacy_id=legacy_id)
        budgets[stove_id] = budget
    await budget.reserve()


async def reserve_device_write(
    hass, device, offsets, device_options, explicit_mac=None
):
    """Apply the shared stove budget before a cloud, Bluetooth or MQTT write."""
    await reserve_eeprom_write(
        hass,
        str(getattr(device, "id_device", "") or ""),
        offsets,
        allowed=writes_allowed(device_options, getattr(device, "id_device", "")),
        legacy_id=legacy_mac_for(device, device_options, explicit=explicit_mac),
    )
