"""Passive Windows BLE capture for ESPHome native API RawAdvertisement.

Python >= 3.12. Importing this module does not import WinRT (Linux tests work).
WindowsScanner.start()/stop() are synchronous. start validates the default
adapter/radio on a short-lived MTA thread, including when called from an asyncio
loop. It raises on missing/disabled/unsupported hardware or failed watcher start.
Runtime watcher failures clear running and set stats['last_error_code'].

on_advertisement(address: int, rssi: int, data: bytes, address_type: int) runs on
WinRT's received thread. The consumer must be thread-safe and nonblocking, e.g.
    loop.call_soon_threadsafe(server.publish, address, rssi, data, address_type)
Address types retain WinRT values: 0 public, 1 random, 2 unspecified. Do not invent
an address type for unspecified addresses in a downstream API.

Data is OS-visible AD, NOT RF-original packets: Windows can merge/omit fields.
All DataSections are preserved; parsed metadata only supplements absent fields.
No MAC/UUID/PID filters, connections, decryption, per-packet logs, disk capture,
or BLEDevice/platform-handle serialization. RSSI -127 is Windows' synthetic
out-of-range notification and is counted but never published as a real packet.
Long-term recording belongs to the shared HA plugin runtime, not this receiver.
"""

import asyncio
from collections.abc import Callable
import logging
import queue
import threading
import time
from typing import Any

from advertisement import pack_advertisement

_LOGGER = logging.getLogger(__name__)
_COUNTERS = ("received_events", "emitted", "invalid_advertisements", "windows_out_of_range_events")


def _load_winrt() -> tuple[Any, Any, Any, Any]:
    from winrt.windows.devices.bluetooth.advertisement import (
        BluetoothLEAdvertisementWatcher,
        BluetoothLEAdvertisementWatcherStatus,
        BluetoothLEScanningMode,
    )
    from winrt.windows.storage.streams import DataReader

    return (BluetoothLEAdvertisementWatcher, BluetoothLEAdvertisementWatcherStatus,
            BluetoothLEScanningMode, DataReader)


async def get_adapter_info() -> dict:
    """Return {address: int, name: str, enabled: True} for the default adapter.

    Missing adapter, disabled/inaccessible radio and non-LE hardware raise
    RuntimeError with a short code. WinRT failures propagate to the caller.
    Only primitive values leave this function, never Windows handles.
    """
    from winrt.windows.devices.bluetooth import BluetoothAdapter
    from winrt.windows.devices.radios import RadioState

    adapter = await BluetoothAdapter.get_default_async()
    if adapter is None:
        raise RuntimeError("adapter_missing")
    if not adapter.is_low_energy_supported:
        raise RuntimeError("adapter_no_ble")
    radio = await adapter.get_radio_async()
    if radio is None:
        raise RuntimeError("radio_unavailable")
    if radio.state != RadioState.ON:
        raise RuntimeError("radio_disabled")
    address = int(adapter.bluetooth_address)
    if not 0 < address < (1 << 48):
        raise RuntimeError("adapter_invalid_address")
    return {"address": address, "name": str(radio.name), "enabled": True}


def _get_adapter_info_sync() -> dict:
    # A separate MTA avoids asyncio.run() inside the caller's running loop and
    # does not block that loop waiting for a coroutine scheduled on itself.
    result: queue.Queue = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            import ctypes

            combase = ctypes.OleDLL("combase")
            combase.RoInitialize(1)  # RO_INIT_MULTITHREADED; OleDLL checks HRESULT.
            try:
                async def query() -> dict:
                    return await asyncio.wait_for(get_adapter_info(), timeout=10)
                info = asyncio.run(query())
            finally:
                combase.RoUninitialize.restype = None
                combase.RoUninitialize()
            result.put((info, None))
        except Exception as exc:
            result.put((None, exc))

    threading.Thread(target=worker, name="ble-adapter-info", daemon=True).start()
    try:
        info, error = result.get(timeout=15)
    except queue.Empty:
        raise RuntimeError("adapter_query_timeout") from None
    if error is not None:
        raise RuntimeError("adapter_unavailable") from error
    return info


class WindowsScanner:
    def __init__(self, on_advertisement: Callable[[int, int, bytes, int], None], stats: dict):
        if not callable(on_advertisement):
            raise TypeError("on_advertisement must be callable")
        self.on_advertisement = on_advertisement
        self.stats = stats
        for key in _COUNTERS:
            stats.setdefault(key, 0)
        self.adapter_address: int | None = None
        self.adapter_name: str | None = None
        self._lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._reported_errors: set[str] = set()
        self._running = False
        self._watcher = None
        self._received_token = None
        self._stopped_token = None
        self._reader = None
        self._statuses = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def _error(self, code: str) -> None:
        with self._lock:
            self.stats["last_error_code"] = code
            first = code not in self._reported_errors
            self._reported_errors.add(code)
        if first:
            # Once per code per instance, no exception repr or raw payload.
            _LOGGER.warning("BLE capture: %s", code)

    def start(self) -> None:
        with self._lifecycle:
            if self.running:
                return
            self._dispose()
            self.adapter_address = self.adapter_name = None
            try:
                watcher_type, self._statuses, mode, self._reader = _load_winrt()
                info = _get_adapter_info_sync()
                self.adapter_address = info["address"]
                self.adapter_name = info["name"]
                watcher = self._watcher = watcher_type()
                watcher.scanning_mode = mode.PASSIVE
                # No advertisement_filter, signal-strength filter or dedup.
                # WinRT may project sender into a different Python wrapper.
                # Capture our instance rather than comparing sender identity.
                self._received_token = watcher.add_received(lambda sender, event: self._received(watcher, event))
                self._stopped_token = watcher.add_stopped(lambda sender, event: self._stopped(watcher, event))
                with self._lock:
                    self._running = True
                watcher.start()
                # Start() returns before Windows necessarily leaves CREATED.
                # Wait only for that pending transition, not for a failed or
                # stopped watcher. Callbacks remain active during the wait.
                deadline = time.monotonic() + 5
                while True:
                    status = watcher.status
                    self.stats["watcher_status"] = int(status)
                    if status == self._statuses.STARTED and self.running:
                        break
                    if (status != self._statuses.CREATED or not self.running
                            or time.monotonic() >= deadline):
                        raise RuntimeError(f"watcher_start_status_{int(status)}")
                    time.sleep(0.01)
            except Exception:
                with self._lock:
                    self._running = False
                self._dispose()
                self._error("start_failed")
                raise

    def stop(self) -> None:
        """Idempotently stop and unsubscribe. An already executing callback may finish."""
        with self._lifecycle:
            with self._lock:
                self._running = False
            self._dispose()

    def _dispose(self) -> None:
        watcher, self._watcher = self._watcher, None
        received, self._received_token = self._received_token, None
        stopped, self._stopped_token = self._stopped_token, None
        if watcher is None:
            return
        operations = []
        if received is not None:
            operations.append(lambda: watcher.remove_received(received))
        if stopped is not None:
            operations.append(lambda: watcher.remove_stopped(stopped))
        operations.append(watcher.stop)
        for operation in operations:
            try:
                operation()
            except Exception:
                self._error("watcher_cleanup_failed")

    def _stopped(self, sender: Any, event: Any) -> None:
        with self._lock:
            if sender is not self._watcher:
                return
            expected = not self._running
            self._running = False
            self.stats["watcher_status"] = int(sender.status)
            self.stats["watcher_error"] = int(event.error)
        if not expected:
            self._error("watcher_aborted" if sender.status == self._statuses.ABORTED else "watcher_stopped")

    def _buffer_bytes(self, buffer: Any) -> bytes:
        reader = self._reader.from_buffer(buffer)
        try:
            data = bytearray(buffer.length)
            reader.read_bytes(data)
            return bytes(data)
        finally:
            reader.close()

    def _received(self, sender: Any, event: Any) -> None:
        with self._lock:
            if not self._running or sender is not self._watcher:
                return
            self.stats["received_events"] += 1
        try:
            rssi = int(event.raw_signal_strength_in_dbm)
            if rssi == -127:
                with self._lock:
                    self.stats["windows_out_of_range_events"] += 1
                return
            address = int(event.bluetooth_address)
            address_type = int(event.bluetooth_address_type)
            if not 0 <= address < (1 << 48) or not -128 <= rssi <= 127 or address_type not in (0, 1, 2):
                raise ValueError("invalid_event")
            ad = event.advertisement
            sections = [(section.data_type, self._buffer_bytes(section.data)) for section in ad.data_sections]
            manufacturers = [(item.company_id, self._buffer_bytes(item.data)) for item in ad.manufacturer_data]
            flags = getattr(ad, "flags", None)
            tx_power = getattr(event, "transmit_power_level_in_dbm", None)
            data = pack_advertisement(
                sections, manufacturer_data=manufacturers,
                service_uuids=list(ad.service_uuids), local_name=ad.local_name,
                flags=int(flags) if flags is not None else None,
                tx_power=int(tx_power) if tx_power is not None else None,
            )
        except Exception as exc:
            with self._lock:
                self.stats["invalid_advertisements"] += 1
            self._error("invalid_advertisement" if isinstance(exc, ValueError) else "advertisement_read_failed")
            return
        try:
            self.on_advertisement(address, rssi, data, address_type)
        except Exception:
            with self._lock:
                self.stats["callback_errors"] = self.stats.get("callback_errors", 0) + 1
            self._error("callback_failed")
            return
        with self._lock:
            self.stats["emitted"] += 1
