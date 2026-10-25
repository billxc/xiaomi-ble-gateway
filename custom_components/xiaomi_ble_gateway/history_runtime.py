"""Explicit local history pulls through HA's managed ESPHome GATT connection.

No polling, cloud writes, registration, clock sync or offline-record confirmation.
Only an explicit HA action starts this branch. Native payloads never enter the
broadcast upload queue or alter its persistent consumption gates.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import time

from homeassistant.components import bluetooth
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
import voluptuous as vol

from .const import DOMAIN, SCALE_MODEL, bluetooth_address
from . import providers
from .scale_history import HistoryStore

SERVICE_PULL = "pull_scale_history"


def async_register_services(hass):
    async def pull(call):
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        runtime = getattr(entry, "runtime_data", None)
        if entry is None or entry.domain != DOMAIN or runtime is None:
            raise ServiceValidationError("Select a loaded Xiaomi BLE Gateway entry")
        mac = call.data["address"].replace(":", "").replace("-", "").upper()
        if mac not in runtime.devices or runtime.devices[mac].model != SCALE_MODEL:
            raise ServiceValidationError("Select a configured supported scale")
        return await runtime.async_pull_scale_history(mac)

    hass.services.async_register(
        DOMAIN, SERVICE_PULL, pull,
        schema=vol.Schema({vol.Required("config_entry_id"): str,
                           vol.Required("address"): str}),
        supports_response=SupportsResponse.OPTIONAL,
    )


class ScaleHistoryCoordinator:
    def __init__(self, runtime):
        self.runtime = runtime
        self.hass = runtime.hass
        self.store = None
        self._pull_task = None
        self._worker = None
        self._queue = asyncio.Queue(maxsize=1024)
        self._closing = False
        self._bound = set()
        self.status = {"status": "idle", "broadcast_dropped": 0,
                       "broadcast_write_errors": 0}

    async def _prepare(self, device):
        if self.store is None:
            path = self.hass.config.path(DOMAIN, "scale_history.sqlite3")
            self.store = await self.hass.async_add_executor_job(HistoryStore, path)
        if device.mac not in self._bound:
            await self.hass.async_add_executor_job(self.store.bind_device, device.mac, device.did)
            self._bound.add(device.mac)
        if self._worker is None:
            self._worker = self.hass.async_create_background_task(
                self._write_broadcasts(), f"{DOMAIN} local scale observations")

    def observe(self, mac, frame, source, received_at):
        if self._closing or mac not in self._bound:
            return
        weight = frame.readings.get("weight_kg")
        device_time = frame.metadata.get("device_time")
        if weight is None or type(device_time) is not int or device_time <= 0:
            return
        try:
            self._queue.put_nowait((mac, device_time, weight, frame.counter,
                                    source, received_at))
        except asyncio.QueueFull:
            self.status["broadcast_dropped"] += 1

    async def _write_broadcasts(self):
        while True:
            item = await self._queue.get()
            try:
                await self.hass.async_add_executor_job(self.store.merge_broadcast, *item)
            except Exception as err:
                self.status["broadcast_write_errors"] += 1
                self.status["broadcast_error"] = type(err).__name__
            finally:
                self._queue.task_done()

    async def async_pull(self, mac):
        if self._closing:
            raise ServiceValidationError("History collector is stopping")
        if self._pull_task is not None:
            raise ServiceValidationError("A scale history pull is already running")
        self._pull_task = asyncio.current_task()
        self.status = {"status": "starting", "complete": False,
                       "authenticated": False, "address": mac,
                       "broadcast_dropped": self.status["broadcast_dropped"],
                       "broadcast_write_errors": self.status["broadcast_write_errors"]}
        client = None
        device = self.runtime.devices[mac]
        try:
            await self._prepare(device)
            # Lazy imports keep optional GATT setup independent from passive BLE.
            from bleak import BleakClient
            from bleak_esphome.backend.scanner import ESPHomeScanner
            from bleak_retry_connector import establish_connection
            from .scale_gatt import collect_history

            candidates = bluetooth.async_scanner_devices_by_address(
                self.hass, bluetooth_address(mac), connectable=True)
            # This minimal version intentionally supports one reachable active
            # proxy. Reject ambiguous routing instead of accidentally selecting
            # a local adapter or another scanner. Windows passive nodes are not
            # in this connectable collection.
            if len(candidates) != 1 or not isinstance(candidates[0].scanner, ESPHomeScanner):
                raise ServiceValidationError(
                    "Scale must be visible through exactly one active ESPHome proxy; wake it and retry")
            selected = candidates[0]
            source = selected.scanner.source
            self.status.pop("error", None)
            self.status.update(status="connecting", source=source, address=mac,
                               started_at=int(time.time()))
            token = await providers.get_scale_ble_token(
                self.hass, self.runtime.source_account_id, device)
            merged = {"inserted": 0, "duplicates": 0, "conflicts": 0}

            proxy_source = source

            async def persist(records, source):
                summary = await self.hass.async_add_executor_job(
                    self.store.merge_native, mac, records,
                    f"{source}@{proxy_source}", int(time.time()))
                for key in merged:
                    merged[key] += summary[key]

            # One connection attempt; no rebind/configuration fallback.
            async with asyncio.timeout(100):
                client = await establish_connection(
                    BleakClient, selected.ble_device, device.name,
                    max_attempts=1, timeout=20)
                result = await collect_history(client, token, persist, timeout=75)
            del token
            await self._queue.join()
            totals = await self.hass.async_add_executor_job(self.store.totals, mac)
            self.status.update(**result, merge=merged, totals=totals,
                               finished_at=int(time.time()),
                               association_semantics="candidate_exact_not_verified_measurement",
                               cloud_upload=False, offline_confirmation=False)
            return dict(self.status)
        except asyncio.CancelledError:
            self.status.update(status="cancelled", finished_at=int(time.time()))
            raise
        except Exception as err:
            # Do not attach protocol payloads, token, CSV or profile data to errors.
            self.status.update(status="failed", error=type(err).__name__,
                               finished_at=int(time.time()))
            if isinstance(err, ServiceValidationError):
                raise
            raise ServiceValidationError(f"Scale history read failed ({type(err).__name__})") from None
        finally:
            try:
                if client is not None:
                    with suppress(Exception):
                        async with asyncio.timeout(10):
                            await client.disconnect()
            finally:
                self._pull_task = None

    async def async_stop(self):
        self._closing = True
        if self._pull_task is not None:
            self._pull_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._pull_task
        if self._worker is not None:
            await self._queue.join()
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
