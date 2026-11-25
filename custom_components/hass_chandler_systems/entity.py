"""Base entity for the Chandler Systems integration."""

from __future__ import annotations

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo

from .coordinator import ChandlerSystemsCoordinator


class ChandlerSystemsEntity(
    PassiveBluetoothCoordinatorEntity[ChandlerSystemsCoordinator]
):
    """Base entity for Chandler Systems devices."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for the entity."""
        return self.coordinator.device_info

    @property
    def available(self) -> bool:
        """Return True if the entity is available."""
        return super().available and self.coordinator.last_connection_successful

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
