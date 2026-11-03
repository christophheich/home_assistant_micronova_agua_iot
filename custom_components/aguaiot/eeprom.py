"""Per-stove reservations for EEPROM writes."""

import asyncio
import json
import logging
import math
import time
from pathlib import Path

from homeassistant.helpers.storage import Store

from .aguaiot import AguaIOTError

_LOGGER = logging.getLogger(__name__)

# About 32,000 cycles is the middle of the usual 10,000 to 100,000 range.
# 5 per day lasts about 17.5 years there. 3 per hour stays under that daily cap.
HOURLY_LIMIT = 3
DAILY_LIMIT = 5
# The ceiling stays above the default so a saved higher limit still loads.
HOURLY_CEILING = 15
DAILY_CEILING = 50
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


def whole_count(value):
    """Return a whole number, or None when the value is not one."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        return int(value)
    return None


def checked_limits(hour_raw, day_raw):
    """Return hour and day when both sit inside the ceiling and hour <= day."""
    hour = whole_count(hour_raw)
    day = whole_count(day_raw)
    if (
        hour is None
        or day is None
        or not 1 <= hour <= HOURLY_CEILING
        or not 1 <= day <= DAILY_CEILING
        or hour > day
    ):
        return None
    return hour, day


def limits_for(device_options, device_id):
    """Return the hour and day caps. Missing or invalid values stay at 3 and 5."""
    settings = (device_options or {}).get(str(device_id), {})
    checked = checked_limits(
        settings.get("max_eeprom_writes_per_hour", HOURLY_LIMIT),
        settings.get("max_eeprom_writes_per_day", DAILY_LIMIT),
    )
    if checked is None:
        return HOURLY_LIMIT, DAILY_LIMIT
    return checked


class EepromBudget:
    """Reserve before sending. release() gives one reservation back."""

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

    async def reserve(self, *, hourly=None, daily=None):
        async with self.lock:
            try:
                hourly_limit = HOURLY_LIMIT if hourly is None else hourly
                daily_limit = DAILY_LIMIT if daily is None else daily
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
                    len(self.history) >= daily_limit
                    or sum(now - stamp < 3600 for stamp in self.history) >= hourly_limit
                ):
                    raise AguaIOTError(
                        "EEPROM write limit reached for this stove. "
                        f"The limit is {hourly_limit} commands per hour and "
                        f"{daily_limit} commands per day. Try again later."
                    )
                self.history.append(now)
                await self.store.async_save(self.history)
                await self.hass.async_add_executor_job(self._verify, self.history)
                return now
            except (OSError, ValueError, TypeError) as err:
                raise AguaIOTError(
                    "Cannot verify the EEPROM write budget. No command was sent."
                ) from err

    async def release(self, stamp):
        """Return one reservation after a write that did not land."""
        async with self.lock:
            if not self.history or stamp is None:
                return
            # Drop one object, so two reservations in the same second stay distinct.
            updated = []
            removed = False
            for value in self.history:
                if not removed and value is stamp:
                    removed = True
                    continue
                updated.append(value)
            if not removed:
                return
            await self.store.async_save(updated)
            await self.hass.async_add_executor_job(self._verify, updated)
            self.history = updated


async def reserve_eeprom_write(
    hass, stove_id, offsets, *, allowed=True, legacy_id=None, hourly=None, daily=None
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
    return await budget.reserve(hourly=hourly, daily=daily)


async def release_device_write(hass, device, stamp):
    """Give a cloud or Bluetooth reservation back. Ignore a failed save."""
    if stamp is None or hass is None:
        return
    stove_id = str(getattr(device, "id_device", "") or "")
    budget = hass.data.get("aguaiot", {}).get("eeprom_budgets", {}).get(stove_id)
    if budget is None:
        return
    try:
        await budget.release(stamp)
    except (OSError, ValueError, TypeError, AguaIOTError):
        _LOGGER.debug("Could not return the EEPROM reservation for %s", stove_id)


async def reserve_device_write(
    hass, device, offsets, device_options, explicit_mac=None
):
    """Apply the shared stove budget before a cloud, Bluetooth or MQTT write."""
    stove_id = getattr(device, "id_device", "")
    hourly, daily = limits_for(device_options, stove_id)
    return await reserve_eeprom_write(
        hass,
        str(stove_id or ""),
        offsets,
        allowed=writes_allowed(device_options, stove_id),
        legacy_id=legacy_mac_for(device, device_options, explicit=explicit_mac),
        hourly=hourly,
        daily=daily,
    )
