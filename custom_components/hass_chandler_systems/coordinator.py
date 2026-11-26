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
    ISSUE_AFTER_FAILURES,
    RECONNECT_DELAY,
    RETRY_BASE,
    RETRY_MAX,
    SESSION_STABLE_AFTER,
    SILENCE_TIMEOUT,
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
        self.last_connection_successful = True
        self._consecutive_failures = 0
        self._session_task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        """Return True while a persistent session to the device is up."""
        return self._api is not None and self._api.connected

    def _poll_interval(self) -> float:
        """Seconds to wait before the next poll, backing off after failures."""
        if self._consecutive_failures == 0:
            return RECONNECT_DELAY
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
        """Open a persistent, authenticated session to the device.

        The valve's BLE stack leaks something on every connect/disconnect
        cycle and wedges after roughly 10-15 of them, so the session is kept
        open indefinitely. The device pushes data and MARCO keep-alives over
        it; a background task holds it and cleans up when it drops.
        _needs_poll only asks for a new session once this one is gone.
        """
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
        except ChandlerSystemsAuthenticationError as err:
            self._api = None
            await api.disconnect()
            raise ConfigEntryAuthFailed(f"Incorrect API key format: {err}") from err
        except ChandlerSystemsConnectionError as err:
            self._api = None
            await api.disconnect()
            self._record_failure()
            raise UpdateFailed(f"Failed to connect to {self.address}: {err}") from err

        self.last_connection_successful = True
        self.async_update_listeners()
        self._session_task = self.hass.async_create_background_task(
            self._async_hold_session(api),
            f"{DOMAIN}_session_{self.address}",
        )
        return self.data or {}

    async def _async_hold_session(self, api: ChandlerSystemsAPI) -> None:
        """Keep the session open until the link drops or goes silent."""
        started = self.hass.loop.time()
        try:
            while True:
                try:
                    async with asyncio.timeout(SILENCE_TIMEOUT / 4):
                        await api.disconnect_event.wait()
                except TimeoutError:
                    silent = self.hass.loop.time() - api.last_rx
                    if silent < SILENCE_TIMEOUT:
                        continue
                    _LOGGER.warning(
                        "%s: no data or keep-alive for %ds, dropping session",
                        self.address,
                        int(silent),
                    )
                else:
                    _LOGGER.info("%s: session dropped by the link", self.address)
                break
        finally:
            if self._api is api:
                self._api = None
            await api.disconnect()

        lasted = self.hass.loop.time() - started
        _LOGGER.debug("%s: session lasted %ds", self.address, int(lasted))
        if lasted >= SESSION_STABLE_AFTER:
            self._consecutive_failures = 0
            async_delete_issue(self.hass, DOMAIN, "cannot_connect")
        else:
            # A session that dies quickly is as bad as one that never opened.
            self._record_failure()
        self.async_update_listeners()

    def _record_failure(self) -> None:
        """Count a failed or short-lived session and raise an issue if stuck."""
        self.last_connection_successful = False
        self._consecutive_failures += 1
        _LOGGER.debug(
            "%s: session failed (%d in a row), next attempt in %ds",
            self.address,
            self._consecutive_failures,
            self._poll_interval(),
        )
        self.async_update_listeners()
        if self._consecutive_failures >= ISSUE_AFTER_FAILURES:
            async_create_issue(
                self.hass,
                DOMAIN,
                "cannot_connect",
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="cannot_connect",
                translation_placeholders={"address": self.address},
            )

    @callback
    def _handle_push_data(self, data: dict[str, Any]) -> None:
        """Handle a push notification from the device."""
        if self.data is None:
            self.data = {}
        self.data.update(data)
        self._ready_event.set()
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
        if self.connected:
            # Many BLE peripherals stop advertising while connected; the
            # session itself is the source of truth.
            _LOGGER.debug("%s: no advertisements, but session is up", self.address)
            return
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
        if self._session_task is not None:
            self._session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._session_task
            self._session_task = None
        if self._api is not None:
            await self._api.disconnect()
            self._api = None
