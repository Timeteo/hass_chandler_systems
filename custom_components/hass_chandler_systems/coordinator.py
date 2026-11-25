"""Coordinator for the Chandler Systems integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from bleak.backends.device import BLEDevice

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    ChandlerSystemsAPI,
    ChandlerSystemsAuthenticationError,
    ChandlerSystemsConnectionError,
)
from .const import (
    DEVICE_STARTUP_TIMEOUT,
    DOMAIN,
    IDLE_DISCONNECT_TIMEOUT,
    MAX_CONNECTION_TIMEOUT,
    MIN_POLL_DURATION,
    RETRY_BASE,
    RETRY_MAX,
)
from .device_info import format_device_info

_LOGGER = logging.getLogger(__name__)

type ChandlerSystemsConfigEntry = ConfigEntry[ChandlerSystemsCoordinator]


class ChandlerSystemsCoordinator(ActiveBluetoothDataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Chandler Systems devices.

    Receives BLE advertisement events and establishes an authenticated GATT
    connection to receive push data from the device.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        address: str,
        auth_key: str,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=logger,
            address=address,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_update,
            mode=BluetoothScanningMode.ACTIVE,
            connectable=True,
        )
        self.auth_key = auth_key
        self.config_entry = config_entry
        # Base device, available from the start so HA always registers a
        # device; model/serial/firmware are merged in when they arrive.
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer="Chandler Systems",
            name="Chandler Systems",
        )
        self._device_details: DeviceInfo | None = None
        self.ble_device: BLEDevice | None = None
        self._api: ChandlerSystemsAPI | None = None
        self._ready_event = asyncio.Event()
        self._data_received_event = asyncio.Event()
        self.last_connection_successful = True
        self._consecutive_failures = 0

    def _poll_interval(self) -> float:
        """Seconds to wait before the next poll, backing off after failures."""
        if self._consecutive_failures == 0:
            return MIN_POLL_DURATION
        return min(RETRY_BASE * 2 ** (self._consecutive_failures - 1), RETRY_MAX)

    @callback
    def _needs_poll(
        self,
        service_info: BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        """Return True when a poll (BLE connection attempt) is needed."""
        return (
            self.hass.state is CoreState.running
            and self._api is None
            and (
                seconds_since_last_poll is None
                or seconds_since_last_poll >= self._poll_interval()
            )
            and bool(
                bluetooth.async_ble_device_from_address(
                    self.hass, service_info.device.address, connectable=True
                )
            )
        )

    async def _async_update(
        self, service_info: BluetoothServiceInfoBleak
    ) -> dict[str, Any]:
        """Connect, authenticate, and request initial data from the device."""
        self.ble_device = service_info.device

        # Tear down any stale connection before creating a new one
        if self._api is not None:
            old_api = self._api
            self._api = None
            await old_api.disconnect()

        api = ChandlerSystemsAPI(self.hass, self.address)
        api.register_callback(self._handle_push_data)
        self._api = api

        try:
            await api.connect(service_info.device)
            await api.authenticate(self.auth_key)
            # Stay connected while data keeps arriving.
            # Disconnect after IDLE_DISCONNECT_TIMEOUT seconds of silence,
            # or after MAX_CONNECTION_TIMEOUT seconds total (safety cap),
            # or immediately if the BLE connection drops.
            async with asyncio.timeout(MAX_CONNECTION_TIMEOUT):
                while True:
                    self._data_received_event.clear()
                    data_task = self.hass.async_create_task(
                        self._data_received_event.wait(), eager_start=True
                    )
                    disconnect_task = self.hass.async_create_task(
                        api.disconnect_event.wait(), eager_start=True
                    )
                    done, pending = await asyncio.wait(
                        {data_task, disconnect_task},
                        timeout=IDLE_DISCONNECT_TIMEOUT,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()

                    if disconnect_task in done:
                        _LOGGER.info(
                            "BLE connection to %s dropped, exiting wait loop",
                            self.address,
                        )
                        break
                    if not done:
                        # Idle timeout - no data and no disconnect signal.
                        break
                    # data_task completed - loop and wait for more data.

            self.last_connection_successful = True
            self._consecutive_failures = 0
            async_delete_issue(self.hass, DOMAIN, "cannot_connect")
        except ChandlerSystemsAuthenticationError as err:
            raise ConfigEntryAuthFailed(f"Incorrect API key format: {err}") from err
        except ChandlerSystemsConnectionError as err:
            self.last_connection_successful = False
            self._consecutive_failures += 1
            _LOGGER.debug(
                "%s: poll failed (%d in a row), next attempt in %ds",
                self.address,
                self._consecutive_failures,
                self._poll_interval(),
            )
            self.async_update_listeners()
            async_create_issue(
                self.hass,
                DOMAIN,
                "cannot_connect",
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="cannot_connect",
                translation_placeholders={"address": self.address},
            )
            raise UpdateFailed(f"Failed to connect to {self.address}: {err}") from err
        except TimeoutError:
            _LOGGER.debug(
                "Max connection time reached for %s, disconnecting",
                self.address,
            )
        finally:
            # Always disconnect after reading; _needs_poll will reconnect on
            # the next advertisement (rate-limited by _poll_interval).
            self._api = None
            await api.disconnect()

        return self.data or {}

    @callback
    def _handle_push_data(self, data: dict[str, Any]) -> None:
        """Handle a push notification from the device."""
        if self.data is None:
            self.data = {}
        self.data.update(data)
        self._ready_event.set()
        self._data_received_event.set()
        self._set_device_info(self.data)
        self._sync_time(data)
        self.async_update_listeners()

    def _set_device_info(self, data: dict[str, Any]) -> None:
        """Merge device details into the device info and the device registry."""
        details = format_device_info(data)
        if details is None or details == self._device_details:
            return
        self._device_details = details
        self.device_info = DeviceInfo(**{**self.device_info, **details})
        dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.config_entry.entry_id, **self.device_info
        )

    async def _async_sync_time(
        self, api: ChandlerSystemsAPI, payload: dict[str, Any]
    ) -> None:
        """Send a time-sync command, ignoring connection errors."""
        try:
            await api.send_command(payload)
        except ChandlerSystemsConnectionError:
            _LOGGER.debug(
                "Time sync skipped for %s, device not connected", self.address
            )

    def _sync_time(self, data: dict[str, Any]) -> None:
        """Sync the device clock if it differs from local time."""
        if "dh" not in data or "dm" not in data:
            return

        if self._api is None:
            return

        now = dt_util.now()
        # The device only reports hours and minutes, so we consider it "in sync"
        # if it is within one minute of local time (wrapping at midnight).
        drift = abs((data["dh"] * 60 + data["dm"]) - (now.hour * 60 + now.minute))
        if min(drift, 1440 - drift) <= 1:
            _LOGGER.debug("Device time is in sync; no time sync needed")
            return

        _LOGGER.info(
            "Device time %02d:%02d differs from local time %02d:%02d:%02d, syncing",
            data["dh"],
            data["dm"],
            now.hour,
            now.minute,
            now.second,
        )
        # Capture self._api now - by the time the background task runs,
        # the coordinator's finally block may have set self._api to None.
        self.hass.async_create_background_task(
            self._async_sync_time(
                self._api,
                {"dh": now.hour, "dm": now.minute, "ds": now.second},
            ),
            f"{DOMAIN}_sync_time_{self.address}",
        )

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Handle a Bluetooth advertisement event."""
        if (
            not self.ble_device
            or service_info.device.address != self.ble_device.address
        ):
            _LOGGER.info("Chandler Systems device %s discovered", self.address)

        self.ble_device = service_info.device
        super()._async_handle_bluetooth_event(service_info, change)

    @callback
    def _async_handle_unavailable(
        self, service_info: BluetoothServiceInfoBleak
    ) -> None:
        """Handle the device going unavailable."""
        super()._async_handle_unavailable(service_info)
        _LOGGER.info("Chandler Systems device %s is unavailable", self.address)
        if self._api is not None:
            # Schedule disconnect without blocking the event loop
            self.hass.async_create_background_task(
                self._api.disconnect(),
                f"{DOMAIN}_disconnect_{self.address}",
            )
            self._api = None

    async def async_wait_ready(self) -> bool:
        """Wait for the first push data to arrive after connection.

        Returns True if data was received within the startup timeout,
        False if the timeout expired.
        """
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(DEVICE_STARTUP_TIMEOUT):
                await self._ready_event.wait()
                return True
        return False

    async def async_disconnect(self) -> None:
        """Disconnect from the device and clean up the API instance."""
        if self._api is not None:
            await self._api.disconnect()
            self._api = None
