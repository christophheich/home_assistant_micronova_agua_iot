"""Per-stove reservations for local MQTT EEPROM writes."""

import asyncio
import json
import math
import time
from pathlib import Path

from homeassistant.helpers.storage import Store

from .aguaiot import AguaIOTError

HOURLY_LIMIT = 15
DAILY_LIMIT = 50


class EepromBudget:
    """Reserve before sending; uncertain writes still consume the budget."""

    def __init__(self, hass, mac):
        self.hass = hass
        self.store = Store(hass, 1, f"aguaiot.mqtt.{mac}.eeprom", atomic_writes=True)
        self.lock = asyncio.Lock()
        self.history = None

    def _history_exists(self):
        path = Path(self.store.path)
        return path.exists() or any(path.parent.glob(f"{path.name}.corrupt.*"))

    def _verify(self, history):
        with Path(self.store.path).open() as source:
            if json.load(source).get("data") != history:
                raise OSError("EEPROM reservation was not saved")

    async def reserve(self):
        async with self.lock:
            try:
                if self.history is None:
                    history = await self.store.async_load()
                    if history is None:
                        if await self.hass.async_add_executor_job(self._history_exists):
                            raise ValueError("Unreadable EEPROM history")
                        history = []
                    if not isinstance(history, list) or any(
                        type(value) not in (float, int)
                        or not math.isfinite(value)
                        or value < 0
                        for value in history
                    ):
                        raise ValueError("Invalid EEPROM history")
                    self.history = history
                now = time.time()
                # Keep future reservations if the system clock moves backwards.
                self.history = [t for t in self.history if now - t < 86400]
                if (
                    len(self.history) >= DAILY_LIMIT
                    or sum(now - t < 3600 for t in self.history) >= HOURLY_LIMIT
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
