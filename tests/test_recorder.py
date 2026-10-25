"""Offline raw-recorder tests: no HA installation, credentials or cloud calls."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

_PATH = (Path(__file__).resolve().parents[1]
         / "custom_components/xiaomi_ble_gateway/recorder.py")
_SPEC = importlib.util.spec_from_file_location("_gateway_recorder_test", _PATH)
recorder = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = recorder
_SPEC.loader.exec_module(recorder)


def advertisement():
    """Synthetic unknown device, deliberately without FE95 service data."""
    return SimpleNamespace(
        address="02:00:00:00:00:01", name=None, rssi=-71,
        source="offline-proxy", connectable=False, time=123.5,
        service_uuids=["synthetic-service"],
        service_data={"synthetic-service": bytearray([0, 255, 16])},
        manufacturer_data={65535: bytes([128, 0, 254])},
        device=SimpleNamespace(details=object()),
        advertisement=SimpleNamespace(
            local_name="synthetic", tx_power=-12, rssi=-72,
            service_uuids=[], service_data={}, manufacturer_data={},
            platform_data=object()))


def record(at):
    payload = recorder.serialize_service_info(advertisement(), at)
    return recorder.RawRecord(at, payload["source"], payload["address"], payload)


class RecorderTests(unittest.TestCase):
    def test_owned_complete_payload_without_platform_objects(self):
        info = advertisement()
        payload = recorder.serialize_service_info(info, 1000.0)
        info.service_data["synthetic-service"][0] = 99
        info.service_uuids.append("later")
        self.assertEqual(payload["service_data"], {"synthetic-service": "00ff10"})
        self.assertEqual(payload["service_uuids"], ["synthetic-service"])
        self.assertEqual(bytes.fromhex(payload["manufacturer_data"]["65535"]),
                         bytes([128, 0, 254]))
        self.assertEqual(payload["tx_power"], -12)
        self.assertEqual(payload["time"], 123.5)
        self.assertFalse(payload["connectable"])
        encoded = json.dumps(payload)
        self.assertNotIn("details", encoded)
        self.assertNotIn("platform_data", encoded)
        self.assertNotIn("device", payload)

    def test_sqlite_roundtrip_duplicates_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "raw.sqlite3"
            writer = recorder.DBWriter(path)
            writer.open()
            try:
                self.assertEqual(writer.write_batch([record(1000), record(1000)]), 2)
                with sqlite3.connect(path) as connection:
                    rows = connection.execute("SELECT received_at, source, address, "
                                              "payload_json FROM advertisements").fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0], rows[1])
                self.assertEqual(rows[0][2], advertisement().address)
                payload = json.loads(rows[0][3])
                self.assertEqual(bytes.fromhex(payload["manufacturer_data"]["65535"]),
                                 advertisement().manufacturer_data[65535])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            finally:
                writer.close()

    def test_time_only_retention_and_unbounded_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = recorder.DBWriter(Path(directory) / "private" / "raw.sqlite3")
            writer.open()
            try:
                now = 100 * 86400
                writer.write_batch([record(now - 3 * 86400 - 1),
                                    record(now - 3 * 86400), record(now)])
                self.assertEqual(writer.prune(3, now), 1)
                self.assertEqual(writer.prune(365, now), 0)
                self.assertEqual(writer.prune(1, now), 1)
                for invalid in (0, -1, True, 1.5):
                    with self.assertRaises(ValueError):
                        writer.prune(invalid, now)
            finally:
                writer.close()

    def test_queue_overflow_and_snapshot_errors_are_visible(self):
        hass = SimpleNamespace(config=SimpleNamespace(path=lambda *parts: "/unused"))
        capture = recorder.RawRecorder(hass)
        capture._queue = asyncio.Queue(maxsize=1)
        capture._active = True
        capture._on_advertisement(advertisement(), None)
        capture._on_advertisement(advertisement(), None)
        capture._on_advertisement(SimpleNamespace(), None)
        status = capture.status()
        self.assertEqual(status["received"], 3)
        self.assertEqual(status["enqueued"], 1)
        self.assertEqual(status["dropped"], 1)
        self.assertEqual(status["serialization_failures"], 1)
        self.assertEqual(status["last_error"], "snapshot: AttributeError")
        capture.set_retention_days(365)
        self.assertEqual(capture.status()["retention_days"], 365)


class RecorderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_subscription_stop_flush_and_write_failure_accounting(self):
        modules = {name: ModuleType(name) for name in (
            "homeassistant", "homeassistant.components",
            "homeassistant.components.bluetooth", "homeassistant.const", "homeassistant.core")}
        bluetooth = modules["homeassistant.components.bluetooth"]
        bluetooth.BluetoothScanningMode = SimpleNamespace(PASSIVE="passive")
        modules["homeassistant.const"].EVENT_HOMEASSISTANT_STOP = "stop"
        modules["homeassistant.core"].callback = lambda fn: fn
        registrations = []
        unregistered = []
        listeners = {}

        def register(hass, callback, matcher, mode):
            registrations.append((callback, matcher, mode))
            return lambda: unregistered.append(True)

        def listen(event, callback):
            listeners[event] = callback
            return lambda: listeners.pop(event, None)

        bluetooth.async_register_callback = register

        async def executor(function, *args):
            return await asyncio.to_thread(function, *args)

        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, modules):
            hass = SimpleNamespace(
                config=SimpleNamespace(path=lambda *parts: str(Path(directory).joinpath(*parts))),
                bus=SimpleNamespace(async_listen_once=listen),
                async_add_executor_job=executor,
                async_create_background_task=lambda coro, name: asyncio.create_task(coro, name=name))
            capture = recorder.RawRecorder(hass)
            await capture.async_start()
            await capture.async_start()
            self.assertEqual(len(registrations), 1)
            callback, matcher, mode = registrations[0]
            self.assertEqual(matcher, {"connectable": False})
            callback(advertisement(), None)
            callback(advertisement(), None)
            await listeners["stop"](None)
            self.assertEqual(len(unregistered), 1)
            self.assertEqual(capture.status()["written"], 2)
            self.assertFalse(capture.status()["active"])
            self.assertIsNone(capture._writer._connection)
            callback(advertisement(), None)
            self.assertEqual(capture.status()["received"], 2)
            with sqlite3.connect(capture.path) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM advertisements").fetchone()[0], 2)
            await capture.async_start()
            callback(advertisement(), None)
            # A storage error is explicit and its possibly sensitive message is not retained.
            with patch.object(capture._writer, "write_batch",
                              side_effect=OSError("private payload must not escape")):
                await capture.async_stop()
            self.assertEqual(capture.status()["failed_records"], 1)
            self.assertEqual(capture.status()["last_error"], "write: OSError")
            await capture.async_stop()


if __name__ == "__main__":
    unittest.main()
