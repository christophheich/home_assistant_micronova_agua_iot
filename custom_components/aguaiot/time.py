"""Support for Agua IOT schedule timers."""

from __future__ import annotations

from datetime import time
import logging

from homeassistant.components.time import TimeEntity
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .aguaiot import AguaIOTError
from .const import DOMAIN, TIME_ENTITIES, AguaIOTTimeEntityDescription

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up Agua IOT schedule time entities."""
    coordinator = config_entry.runtime_data
    agua = coordinator.agua

    entities: list[AguaIOTTimeEntity] = []
    for device in agua.devices:
        for description in TIME_ENTITIES:
            if description.key in device.registers and device.get_register_enabled(
                description.key
            ):
                entities.append(AguaIOTTimeEntity(coordinator, device, description))

    async_add_entities(entities, True)


class AguaIOTTimeEntity(CoordinatorEntity, TimeEntity):
    """Time entity representing an Agua IOT schedule start or stop setpoint."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, device, description: AguaIOTTimeEntityDescription):
        """Initialize schedule time entity."""
        super().__init__(coordinator)
        self._device = device
        self.entity_description = description

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"{self._device.id_device}_{self.entity_description.key}"

    @property
    def name(self) -> str:
        """Return the entity name."""
        return f"{self._device.name} {self.entity_description.name}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device metadata."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.id_device)},
            name=self._device.name,
            manufacturer="Micronova",
            model=self._device.name_product,
        )

    @property
    def native_value(self) -> time | None:
        """Return current setpoint as time."""
        val = self._device.get_register_value(self.entity_description.key)
        if val is None:
            return None
        total_minutes = int(val) * 10
        if total_minutes >= 1440:
            return time(23, 59)
        return time(total_minutes // 60, total_minutes % 60)

    async def async_set_value(self, value: time) -> None:
        """Write schedule time to stove."""
        total_minutes = value.hour * 60 + value.minute
        units = min(144, max(0, round(total_minutes / 10)))
        try:
            await self._device.set_register_value(self.entity_description.key, units)
            await self.coordinator.async_request_refresh()
        except (ValueError, AguaIOTError) as err:
            _LOGGER.error(
                "Failed to set '%s' to %s: %s",
                self.name,
                value,
                err,
            )
