"""Offline history glue: small isolated HA/Bleak stubs, real private SQLite.

All identifiers/34-field CSV values are fictional, not captures or profiles.
No real integration __init__, credentials, adapters, network, or idle waits.
"""
import asyncio
from contextlib import closing
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1] / "custom_components/xiaomi_ble_gateway"
PACKAGE = "_gateway_history_runtime_offline"
MAC = "020000000001"
ADDRESS = "02:00:00:00:00:01"
TOKEN = b"fictional-token"
FIELDS = ["9876543210123456789", "87654321", "9", "7319", "83", "2",
          "2000000000"] + ["111"] * 27
CSV = ",".join(FIELDS)


class ServiceValidationError(Exception):
    pass


class ESPHomeScanner:
    def __init__(self, source="fictional-active-proxy"):
        self.source = source


class HistoryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        names = ["homeassistant", "homeassistant.components",
                 "homeassistant.components.bluetooth", "homeassistant.core",
                 "homeassistant.exceptions", "homeassistant.helpers",
                 "homeassistant.helpers.dispatcher", "homeassistant.helpers.storage",
                 "voluptuous", "bleak", "bleak_esphome", "bleak_esphome.backend",
                 "bleak_esphome.backend.scanner", "bleak_retry_connector", PACKAGE,
                 PACKAGE + ".providers", PACKAGE + ".protocol", PACKAGE + ".scale_gatt"]
        stubs = {name: ModuleType(name) for name in names}
        stubs[PACKAGE].__path__ = [str(ROOT)]
        self.bluetooth = stubs["homeassistant.components.bluetooth"]
        stubs["homeassistant.components"].bluetooth = self.bluetooth
        stubs["homeassistant.core"].callback = lambda fn: fn
        stubs["homeassistant.core"].SupportsResponse = NS(OPTIONAL="optional")
        stubs["homeassistant.exceptions"].ServiceValidationError = ServiceValidationError
        stubs["homeassistant.helpers.dispatcher"].async_dispatcher_send = Mock()
        stubs["homeassistant.helpers.storage"].Store = Mock()
        stubs["voluptuous"].Schema = lambda fields: fields
        stubs["voluptuous"].Required = lambda key: key
        self.providers = stubs[PACKAGE + ".providers"]
        self.providers.get_scale_ble_token = AsyncMock(return_value=TOKEN)
        stubs[PACKAGE + ".protocol"].decode_advertisement = Mock()
        self.client = NS(disconnect=AsyncMock())
        self.bleak_client = stubs["bleak"].BleakClient = object()
        stubs["bleak_esphome.backend.scanner"].ESPHomeScanner = ESPHomeScanner
        self.connect = stubs["bleak_retry_connector"].establish_connection = AsyncMock(
            return_value=self.client)
        self.collect = stubs[PACKAGE + ".scale_gatt"].collect_history = AsyncMock(
            side_effect=self.collect_records)
        # Keep dynamic imports inside this context too. patch.dict restores even
        # auto-imported submodules; no HA/provider/Bleak stubs leak to old suites.
        modules_patch = patch.dict(sys.modules, stubs)
        modules_patch.start()
        self.addCleanup(modules_patch.stop)
        self.c = self.load("const")
        self.sh = self.load("scale_history")
        self.load("state")
        self.r = self.load("runtime")
        self.hr = self.load("history_runtime")
        self.record = self.sh.parse_native_csv("42," + CSV, offline=True)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tasks = []

        def background(coro, name):
            task = asyncio.create_task(coro, name=name)
            self.tasks.append(task)
            return task

        self.hass = NS(
            config=NS(path=lambda *parts: str(Path(self.temp.name).joinpath(*parts))),
            async_add_executor_job=asyncio.to_thread,
            async_create_background_task=background,
            services=NS(async_register=Mock()))
        self.device = NS(mac=MAC, did="blt.fictional-scale", name="Fictional scale",
                         model=self.c.SCALE_MODEL)
        self.entry = NS(entry_id="offline-history", domain=self.c.DOMAIN,
                        data={self.c.CONF_ACCOUNT_ID: "fictional-account"})
        self.runtime = self.r.GatewayRuntime(
            self.hass, self.entry, [self.device], {MAC: b""}, False)
        self.entry.runtime_data = self.runtime
        self.hass.config_entries = NS(async_get_entry=Mock(return_value=self.entry))
        self.selected = NS(scanner=ESPHomeScanner(), ble_device=object())
        self.bluetooth.async_scanner_devices_by_address = Mock(return_value=[self.selected])
        self.addAsyncCleanup(self.stop_and_assert)

    def load(self, name):
        spec = importlib.util.spec_from_file_location(PACKAGE + "." + name,
                                                     ROOT / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    async def collect_records(self, client, token, on_records, *, timeout):
        self.assertIs(client, self.client)
        self.assertEqual(token, TOKEN)
        self.assertGreater(timeout, 0)  # Never wait out the real collector's idle period.
        await on_records([copy.deepcopy(self.record)], source="gatt_offline")
        return {"status": "complete", "complete": True, "authenticated": True,
                "expected_records": 1, "received_records": 1, "saved_records": 1}

    def assert_private_status(self, value):
        text = json.dumps(value)
        for secret in (TOKEN.decode(), CSV, FIELDS[0], FIELDS[1], "raw_fields",
                       "miid", "duid", "userType", "weight", "token"):
            self.assertNotIn(secret, text)

    async def stop_and_assert(self):
        coordinator = self.runtime._history
        if coordinator is not None:
            await asyncio.wait_for(coordinator.async_stop(), 3)
            self.assertIsNone(coordinator._pull_task)
            self.assertIsNone(coordinator._worker)
            self.assertEqual(coordinator._queue.qsize(), 0)
        self.assertTrue(all(task.done() for task in self.tasks))

    async def test_service_single_active_proxy_persists_replay_without_cloud_state(self):
        # Exercise the actual service -> GatewayRuntime lazy coordinator path.
        self.hr.async_register_services(self.hass)
        args, kwargs = self.hass.services.async_register.call_args
        self.assertEqual(args[:2], (self.c.DOMAIN, "pull_scale_history"))
        self.assertEqual(kwargs["supports_response"], "optional")
        handler = args[2]
        call = NS(data={"config_entry_id": self.entry.entry_id, "address": ADDRESS})
        state = self.runtime.states[MAC]
        state.observe(17, {"weight_kg": 12.34})
        state.mark_weight_attempt("fictional-profile", 1900000000, 1900000001)
        before = copy.deepcopy(state)
        queued = object()
        self.runtime._queue.put_nowait(queued)
        store_calls = list(self.runtime.store.mock_calls)
        self.assertIsNone(self.runtime._history)

        first = await handler(call)
        coordinator = self.runtime._history
        self.assertIsInstance(coordinator.store, self.sh.HistoryStore)
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["source"], self.selected.scanner.source)
        self.assertEqual(first["merge"], {"inserted": 1, "duplicates": 0, "conflicts": 0})
        second = await handler(call)
        self.assertEqual(second["merge"], {"inserted": 0, "duplicates": 1, "conflicts": 0})
        self.assertEqual(second["totals"]["native"], 1)
        self.assertEqual(second["totals"]["broadcast"], 0)
        self.assertFalse(second["cloud_upload"])
        self.assertFalse(second["offline_confirmation"])
        recovered = self.sh.HistoryStore(coordinator.store.path)
        self.assertEqual(recovered.totals(MAC), second["totals"])
        with closing(sqlite3.connect(recovered.path)) as connection:
            self.assertEqual(connection.execute(
                "SELECT source FROM native_observations").fetchall(),
                [("gatt_offline@" + self.selected.scanner.source,)])
            self.assertEqual(connection.execute("SELECT mac, did FROM device_bindings").fetchall(),
                             [(MAC, self.device.did)])
        self.assertEqual(self.runtime.states[MAC], before)
        self.assertIs(self.runtime.states[MAC], state)
        self.assertEqual(self.runtime._queue.qsize(), 1)
        self.assertIs(self.runtime._queue.get_nowait(), queued)
        self.runtime._queue.task_done()
        self.assertEqual(self.runtime.store.mock_calls, store_calls)
        self.bluetooth.async_scanner_devices_by_address.assert_called_with(
            self.hass, ADDRESS, connectable=True)
        self.connect.assert_awaited_with(self.bleak_client, self.selected.ble_device,
                                        self.device.name, max_attempts=1, timeout=20)
        self.assertEqual(self.client.disconnect.await_count, 2)
        self.assert_private_status(first)
        self.assert_private_status(second)
        self.connect.side_effect = OSError("synthetic next-connection failure")
        with self.assertRaises(ServiceValidationError):
            await handler(call)
        self.assertFalse(coordinator.status["complete"])
        self.assertNotIn("totals", coordinator.status)

    async def test_passive_windows_or_multiple_connectable_candidates_rejected(self):
        # A passive Windows scanner is filtered out by HA; even an unexpected
        # non-ESPHome candidate must not be used as a local GATT fallback.
        cases = {"passive_windows": [], "non_esphome": [NS(scanner=NS(source="windows"))],
                 "multiple": [self.selected, NS(scanner=ESPHomeScanner("other-proxy"))]}
        for label, candidates in cases.items():
            with self.subTest(route=label):
                self.bluetooth.async_scanner_devices_by_address.return_value = candidates
                with self.assertRaises(ServiceValidationError):
                    await self.runtime.async_pull_scale_history(MAC)
                self.assertEqual(self.runtime._history.status["status"], "failed")
                self.assertEqual(self.runtime._history.status["error"], "ServiceValidationError")
                self.assertIsNone(self.runtime._history._pull_task)
                self.assert_private_status(self.runtime._history.status)
        self.connect.assert_not_awaited()
        self.collect.assert_not_awaited()
        self.providers.get_scale_ble_token.assert_not_awaited()
        self.client.disconnect.assert_not_awaited()

    async def test_save_or_connect_failure_status_disconnect_and_worker_cleanup(self):
        coordinator = self.runtime._history = self.hr.ScaleHistoryCoordinator(self.runtime)
        await coordinator._prepare(self.device)
        # Match collect_history's contract: callback save errors become save_error,
        # not a successful count and not a leaked storage exception/payload.
        async def save_failure(client, token, on_records, *, timeout):
            try:
                await on_records([self.record], source="gatt_offline")
            except OSError:
                return {"status": "save_error", "complete": False, "saved_records": 0}
            self.fail("failed merge must propagate to collect callback")

        self.collect.side_effect = save_failure
        with patch.object(coordinator.store, "merge_native",
                          side_effect=OSError(TOKEN.decode() + CSV)):
            result = await coordinator.async_pull(MAC)
        self.assertEqual(result["status"], "save_error")
        self.assertFalse(result["complete"])
        self.assertEqual(result["merge"]["inserted"], 0)
        self.assertEqual(result["totals"]["native"], 0)
        self.client.disconnect.assert_awaited_once()
        self.assert_private_status(result)

        self.connect.side_effect = OSError(TOKEN.decode() + CSV)
        with self.assertRaises(ServiceValidationError) as caught:
            await coordinator.async_pull(MAC)
        self.assertEqual(coordinator.status["status"], "failed")
        self.assertEqual(coordinator.status["error"], "OSError")
        self.assert_private_status(coordinator.status)
        self.assertNotIn(TOKEN.decode(), str(caught.exception))
        self.assertNotIn(CSV, str(caught.exception))
        self.assertIsNone(coordinator._pull_task)
        self.client.disconnect.assert_awaited_once()  # No new client on connect failure.
        await self.stop_and_assert()

    async def test_stop_cancels_inflight_pull_disconnects_and_stops_worker(self):
        entered = asyncio.Event()

        async def blocked_collect(client, token, on_records, *, timeout):
            entered.set()
            await asyncio.Event().wait()

        self.collect.side_effect = blocked_collect
        pull = asyncio.create_task(self.runtime.async_pull_scale_history(MAC))
        # Register cleanup before waiting so a failed assertion cannot orphan it.
        self.tasks.append(pull)
        await asyncio.wait_for(entered.wait(), 3)
        coordinator = self.runtime._history
        self.assertIs(coordinator._pull_task, pull)
        self.assertIsNotNone(coordinator._worker)
        await self.stop_and_assert()
        self.assertTrue(pull.cancelled())
        self.assertEqual(coordinator.status["status"], "cancelled")
        self.client.disconnect.assert_awaited_once()
        self.assert_private_status(coordinator.status)
        with self.assertRaises(ServiceValidationError):
            await coordinator.async_pull(MAC)
        self.assertEqual(self.connect.await_count, 1)


if __name__ == "__main__":
    unittest.main()
