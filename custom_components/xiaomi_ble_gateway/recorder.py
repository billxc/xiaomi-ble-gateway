"""Record every delivered HA Bluetooth callback, independently of gateway decoding.

The parent owns ONE shared RawRecorder for all entries, and awaits start/stop.
No address, UUID, model, decryption, or replay filtering is performed here.
HA/scanner upstream deduplication still applies: this is not an RF packet/IQ
capture and does not capture GATT traffic. Only explicit Bluetooth payload
fields are serialized, never BLEDevice.details or advertisement.platform_data.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

FLUSH_SECONDS = 3.0
BATCH_SIZE = 1000
QUEUE_SIZE = 10000
MAINTENANCE_SECONDS = 3600.0
_LOGGER = logging.getLogger(__name__)


def _retention(days: int) -> int:
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("retention_days must be an integer >= 1")
    return days


def serialize_service_info(info: Any, received_at: float | None = None) -> dict:
    """Take an owned snapshot of the public Bluetooth fields (bytes -> hex).

    `time` is HA's scanner timestamp (typically monotonic), not epoch time;
    `received_at` is our wall-clock epoch. Manufacturer IDs are decimal JSON
    keys. Unknown devices and empty/missing service data are valid records.
    """
    payload = {
        "received_at": time.time() if received_at is None else received_at,
        "address": info.address,
        "name": getattr(info, "name", None),
        "rssi": getattr(info, "rssi", None),
        "source": getattr(info, "source", None),
        "connectable": getattr(info, "connectable", None),
        "time": getattr(info, "time", None),
        "service_uuids": list(getattr(info, "service_uuids", ()) or ()),
        "service_data": {str(key): bytes(value).hex() for key, value in
                         (getattr(info, "service_data", {}) or {}).items()},
        "manufacturer_data": {str(key): bytes(value).hex() for key, value in
                              (getattr(info, "manufacturer_data", {}) or {}).items()},
    }
    advertisement = getattr(info, "advertisement", None)
    tx_power = getattr(advertisement, "tx_power", getattr(info, "tx_power", None))
    if tx_power is not None:
        payload["tx_power"] = tx_power
    if advertisement is not None:
        # Preserve advertisement-specific fields too, even if HA's merged
        # service-info fields differ. Never traverse platform_data.
        payload["advertisement"] = {
            "local_name": getattr(advertisement, "local_name", None),
            "rssi": getattr(advertisement, "rssi", None),
            "tx_power": getattr(advertisement, "tx_power", None),
            "service_uuids": list(getattr(advertisement, "service_uuids", ()) or ()),
            "service_data": {str(k): bytes(v).hex() for k, v in
                             (getattr(advertisement, "service_data", {}) or {}).items()},
            "manufacturer_data": {str(k): bytes(v).hex() for k, v in
                                  (getattr(advertisement, "manufacturer_data", {}) or {}).items()},
        }
    return payload


@dataclass(frozen=True)
class RawRecord:
    received_at: float
    source: str | None
    address: str
    payload: dict


class DBWriter:
    """Synchronous SQLite writer: call ONLY in an executor, never concurrently.

    Executor jobs may run on different threads; the recorder serializes every
    call. Deleted pages are reused; hourly incremental vacuum additionally
    returns free pages to disk. No disk cap or non-time eviction is imposed.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        # Establish private permissions BEFORE SQLite can create journal files.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        connection = sqlite3.connect(self.path, check_same_thread=False)
        try:
            connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS advertisements (
                received_at REAL NOT NULL,
                source TEXT,
                address TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS advertisements_time "
                               "ON advertisements(received_at)")
            connection.commit()
        except Exception:
            connection.close()
            raise
        self._connection = connection

    def write_batch(self, records: list[RawRecord]) -> int:
        assert self._connection is not None
        rows = [(r.received_at, r.source, r.address,
                 json.dumps(r.payload, ensure_ascii=False, separators=(",", ":")))
                for r in records]
        with self._connection:
            self._connection.executemany(
                "INSERT INTO advertisements VALUES (?, ?, ?, ?)", rows)
        return len(rows)

    def prune(self, retention_days: int, now: float | None = None) -> int:
        assert self._connection is not None
        cutoff = (time.time() if now is None else now) - _retention(retention_days) * 86400
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM advertisements WHERE received_at < ?", (cutoff,))
        self._connection.execute("PRAGMA incremental_vacuum(1000)")
        return cursor.rowcount

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


class RawRecorder:
    """One HA-wide subscription, bounded queue, and serialized executor writer.

    Write failures are counted (not retried indefinitely in RAM); queue overflow
    and serialization failures are also explicit in status. Disabling capture
    never removes the database. TTL cleanup runs at start, hourly while active,
    on retention changes (within one flush interval), and at normal stop.
    """

    def __init__(self, hass: Any, retention_days: int = 3):
        self.hass = hass
        self.retention_days = _retention(retention_days)
        self.path = hass.config.path("xiaomi_ble_gateway", "raw_bluetooth.sqlite3")
        self._writer = DBWriter(self.path)
        self._queue: asyncio.Queue[RawRecord | None] = asyncio.Queue(QUEUE_SIZE)
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task | None = None
        self._unregister = None
        self._unlisten_stop = None
        self._active = False
        self._maintenance_due = 0.0
        self._counts = dict(received=0, enqueued=0, written=0, dropped=0,
                            serialization_failures=0, failed_records=0,
                            failures=0, deleted=0)
        self._last_error: str | None = None
        self._inflight = 0

    def set_retention_days(self, days: int) -> None:
        """Call on the HA loop; there is intentionally no upper retention cap."""
        self.retention_days = _retention(days)
        self._maintenance_due = 0.0

    def status(self) -> dict:
        return {**self._counts, "active": self._active,
                "retention_days": self.retention_days, "db_path": str(self.path),
                "queue_size": self._queue.qsize(), "queue_capacity": QUEUE_SIZE,
                "inflight": self._inflight, "last_error": self._last_error,
                "capture_scope": "HA-delivered Bluetooth callbacks; not RF/IQ/GATT"}

    def _error(self, operation: str, error: Exception) -> None:
        self._counts["failures"] += 1
        # Exception messages can include payloads, paths or other private data.
        self._last_error = f"{operation}: {type(error).__name__}"

    async def async_start(self) -> None:
        async with self._lock:
            if self._active:
                return
            from homeassistant.components import bluetooth
            from homeassistant.const import EVENT_HOMEASSISTANT_STOP
            from homeassistant.core import callback

            try:
                await self.hass.async_add_executor_job(self._writer.open)
                await self._maintenance()
                @callback
                def on_advertisement(service_info: Any, change: Any) -> None:
                    self._on_advertisement(service_info, change)

                # HA may immediately deliver its cached advertisements here.
                self._active = True
                self._unregister = bluetooth.async_register_callback(
                    self.hass, on_advertisement,
                    {"connectable": False}, bluetooth.BluetoothScanningMode.PASSIVE)
                self._unlisten_stop = self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STOP, self.async_stop)
                self._worker = self.hass.async_create_background_task(
                    self._run(), "xiaomi_ble_gateway raw Bluetooth recorder")
                self._active = True
                _LOGGER.info("Raw recorder enabled: all HA Bluetooth callbacks, retention %s days",
                             self.retention_days)
            except Exception as error:
                self._active = False
                self._error("start", error)
                if self._unregister is not None:
                    self._unregister()
                    self._unregister = None
                if self._unlisten_stop is not None:
                    self._unlisten_stop()
                    self._unlisten_stop = None
                await self.hass.async_add_executor_job(self._writer.close)
                raise

    def _on_advertisement(self, service_info: Any, change: Any) -> None:
        if not self._active:
            return
        self._counts["received"] += 1
        try:
            payload = serialize_service_info(service_info)
            record = RawRecord(payload["received_at"], payload["source"],
                               payload["address"], payload)
        except Exception as error:
            self._counts["serialization_failures"] += 1
            self._error("snapshot", error)
            return
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._counts["dropped"] += 1
        else:
            self._counts["enqueued"] += 1

    async def _flush(self, records: list[RawRecord]) -> None:
        if not records:
            return
        self._inflight = len(records)
        try:
            previously_written = self._counts["written"]
            self._counts["written"] += await self.hass.async_add_executor_job(
                self._writer.write_batch, records)
            if previously_written == 0 and self._counts["written"]:
                _LOGGER.info("Raw recorder committed its first %s callbacks; no device filter",
                             self._counts["written"])
        except Exception as error:
            self._counts["failed_records"] += len(records)
            self._error("write", error)
        finally:
            for _ in records:
                self._queue.task_done()
            records.clear()
            self._inflight = 0

    async def _maintenance(self) -> None:
        try:
            self._counts["deleted"] += await self.hass.async_add_executor_job(
                self._writer.prune, self.retention_days)
        except Exception as error:
            self._error("cleanup", error)
        self._maintenance_due = time.monotonic() + MAINTENANCE_SECONDS

    async def _run(self) -> None:
        records: list[RawRecord] = []
        deadline = time.monotonic() + FLUSH_SECONDS
        try:
            while True:
                try:
                    record = await asyncio.wait_for(
                        self._queue.get(), max(0.001, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    record = ...
                if record is None:
                    self._queue.task_done()
                    break
                if record is not ...:
                    records.append(record)
                if len(records) >= BATCH_SIZE or time.monotonic() >= deadline:
                    await self._flush(records)
                    if time.monotonic() >= self._maintenance_due:
                        await self._maintenance()
                    deadline = time.monotonic() + FLUSH_SECONDS
        finally:
            await self._flush(records)
            await self._maintenance()
            try:
                await self.hass.async_add_executor_job(self._writer.close)
            except Exception as error:
                self._error("close", error)

    async def async_stop(self, event: Any = None) -> None:
        """Unregister first, drain accepted callbacks, commit and close off-loop."""
        async with self._lock:
            self._active = False
            if self._unregister is not None:
                self._unregister()
                self._unregister = None
            if self._unlisten_stop is not None:
                self._unlisten_stop()
                self._unlisten_stop = None
            if self._worker is not None:
                await self._queue.put(None)
                # Never cancel an executor write: SQLite must finish before close.
                await asyncio.shield(self._worker)
                self._worker = None
