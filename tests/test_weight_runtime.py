"""Offline GATT-first orchestration; synthetic payloads, no HA/network/adapter."""
import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1] / "custom_components/xiaomi_ble_gateway"
PACKAGE = "_gateway_weight_runtime_offline"
MAC, ENV_MAC = "020000000001", "020000000002"
NOW = 2000000000


class Store:
    def __init__(self, *args):
        self.saved = []
        self.fail = False
        self.on_save = None
        self.async_load = AsyncMock(return_value=None)

    async def async_save(self, value):
        if self.fail:
            raise OSError("synthetic storage failure")
        self.saved.append(copy.deepcopy(value))
        if self.on_save:
            self.on_save()


def load_modules():
    # Restore even automatically imported submodules; never import integration
    # __init__ or leave HA/provider stubs in another suite's sys.modules.
    names = ["homeassistant", "homeassistant.components",
             "homeassistant.components.bluetooth", "homeassistant.core",
             "homeassistant.helpers", "homeassistant.helpers.dispatcher",
             "homeassistant.helpers.storage", PACKAGE, PACKAGE + ".providers",
             PACKAGE + ".protocol"]
    stubs = {name: ModuleType(name) for name in names}
    stubs[PACKAGE].__path__ = [str(ROOT)]
    stubs["homeassistant.core"].callback = lambda fn: fn
    stubs["homeassistant.helpers.dispatcher"].async_dispatcher_send = Mock()
    stubs["homeassistant.helpers.storage"].Store = Store
    stubs[PACKAGE + ".protocol"].decode_advertisement = Mock()
    providers = stubs[PACKAGE + ".providers"]
    providers.ProviderError = type("ProviderError", (Exception,), {})
    providers.report_scale_measurement = AsyncMock()
    providers.report_environment = AsyncMock()
    loaded = []
    with patch.dict(sys.modules, stubs):
        for name in ("const", "state", "scale_history", "runtime"):
            spec = importlib.util.spec_from_file_location(PACKAGE + "." + name,
                                                         ROOT / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded.append(module)
    return loaded


c, state_module, history_module, r = load_modules()


class FakeHistory:
    def __init__(self, records):
        self.records = records
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.manual_entered, self.manual_release = asyncio.Event(), asyncio.Event()
        self.manual_busy = False
        self.observe = Mock()
        self.async_collect_for_upload = AsyncMock(side_effect=self.collect)
        self.async_pull = AsyncMock(side_effect=self.pull)
        self.async_stop = AsyncMock()

    async def collect(self, mac):
        self.entered.set()
        if self.manual_busy:
            return {"status": "busy", "complete": False}, []
        await self.release.wait()
        return {"status": "complete", "complete": True}, copy.deepcopy(self.records)

    async def pull(self, mac):
        self.manual_busy = True
        self.manual_entered.set()
        try:
            await self.manual_release.wait()
            # Manual history is local only, not another upload job.
            return {"status": "complete", "complete": True}
        finally:
            self.manual_busy = False


class WeightRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = NS(wall=NOW, mono=1000.0)
        # Do not patch time.time or event-loop.time globally: asyncio deadlines
        # and sibling test modules must keep their real clocks.
        async_proxy = NS(**{name: getattr(asyncio, name) for name in dir(asyncio)})
        async_proxy.get_running_loop = lambda: NS(time=lambda: self.clock.mono)
        patches = [patch.object(r, "time", NS(time=lambda: self.clock.wall)),
                   patch.object(r, "asyncio", async_proxy)]
        self.report = AsyncMock(return_value={"status": "verified", "record_kind": "native"})
        self.environment = AsyncMock()
        patches += [patch.object(r.providers, "report_scale_measurement", self.report),
                    patch.object(r.providers, "report_environment", self.environment)]
        for context in patches:
            context.start()
            self.addCleanup(context.stop)
        self.runtimes = []
        self.addAsyncCleanup(self.stop_all)
        self.runtime = self.make_runtime()

    def native(self, timestamp=NOW):
        fields = ["9876543210123456789", "87654321", "9", "7319", "83", "1",
                  str(timestamp)] + ["111"] * 27
        return history_module.parse_native_csv("42," + ",".join(fields), offline=True)

    def make_runtime(self, *, native=True, mode=None):
        data = {c.CONF_ACCOUNT_ID: "fictional-account", c.CONF_DEVICES: [MAC, ENV_MAC],
                c.CONF_UPLOAD: True, c.CONF_TH_UPLOAD: True, c.CONF_TH_ENABLED_AT: NOW - 300,
                c.CONF_WEIGHT_UPLOAD: True, c.CONF_WEIGHT_DEVICE: MAC,
                c.CONF_WEIGHT_PROFILE: "fictional-profile", c.CONF_WEIGHT_ENABLED_AT: NOW - 300,
                c.CONF_WEIGHT_MODE: mode or c.WEIGHT_MODE_FIXED}
        runtime = r.GatewayRuntime(NS(), NS(entry_id="offline-weight", data=data),
                                   [NS(mac=MAC, model=c.SCALE_MODEL),
                                    NS(mac=ENV_MAC, model=c.CO2_MODEL)],
                                   {MAC: b"", ENV_MAC: b""}, True)
        runtime._started_at = self.clock.mono - 1
        runtime._history = FakeHistory([self.native()] if native else [])
        runtime.states[ENV_MAC].observe(1, {})
        self.runtimes.append(runtime)
        return runtime

    def frame(self, counter=11, source="proxy-a", device_time=NOW, *, environment=False):
        mac = ENV_MAC if environment else MAC
        frame = NS(counter=counter,
                   product_id=c.MODEL_PIDS[c.CO2_MODEL if environment else c.SCALE_MODEL],
                   readings={"co2": 650, "temperature": 23.75, "humidity": 48.25}
                   if environment else {"weight_kg": 73.19},
                   metadata={} if environment else {"device_time": device_time,
                                                      "mic_verified": True, "mass_raw": 7319})
        info = NS(address=c.bluetooth_address(mac), source=source, time=self.clock.mono,
                  service_data={c.FE95_UUID: b"synthetic"})
        with patch.object(r, "decode_advertisement", return_value=frame):
            self.runtime._on_advertisement(info, None)

    def start(self, *, serial=True, prepare=True):
        if serial and self.runtime._worker is None:
            self.runtime._worker = asyncio.create_task(self.runtime._async_process())
        if prepare and self.runtime._scale_worker is None:
            self.runtime._scale_worker = asyncio.create_task(
                self.runtime._async_prepare_scale_uploads())

    async def drain(self):
        await asyncio.wait_for(self.runtime._scale_queue.join(), 1)
        await asyncio.wait_for(self.runtime._queue.join(), 1)

    async def stop_all(self):
        for runtime in self.runtimes:
            await asyncio.wait_for(runtime.async_stop(), 1)

    async def test_same_time_sources_counters_one_gatt_one_exclusive_choice(self):
        for native in (True, False):
            with self.subTest(native=native):
                self.runtime = self.make_runtime(native=native)
                history = self.runtime._history
                self.report.reset_mock()
                self.frame()
                self.frame(12, "proxy-b")
                self.assertEqual(self.runtime._scale_queue.qsize(), 1)
                self.assertTrue(all(job.kind is None for job in self.runtime._queue._queue))
                self.start()
                await asyncio.wait_for(history.entered.wait(), 1)
                await asyncio.wait_for(self.runtime._queue.join(), 1)
                self.frame(13, "local")
                self.report.assert_not_awaited()
                self.assertIsNone(self.runtime.states[MAC].last_weight_attempt_device_time)
                self.assertIn((MAC, NOW), self.runtime._scale_pending)

                async def send(*args, **kwargs):
                    saved = self.runtime.store.saved[-1]["devices"][MAC]
                    self.assertEqual(saved["cloud_status"], "unknown")
                    self.assertEqual(saved["weight_attempts"]["fictional-profile"]
                                     ["last_weight_attempt_device_time"], NOW)
                    self.assertEqual(kwargs["measurement_time"], NOW)
                    self.assertEqual(kwargs["mode"], c.WEIGHT_MODE_FIXED)
                    self.assertEqual(kwargs["native_record"], self.native() if native else None)
                    self.assertIn((MAC, NOW), self.runtime._scale_pending)
                    return {"status": "verified"}

                self.report.side_effect = send
                history.release.set()
                await self.drain()
                self.frame(14, "proxy-b")
                await self.drain()
                history.async_collect_for_upload.assert_awaited_once_with(MAC)
                self.report.assert_awaited_once()
                self.assertFalse(self.runtime._scale_pending)
                await self.runtime.async_stop()

    async def test_suspended_gatt_does_not_block_co2_or_th(self):
        self.frame()
        self.start()
        await asyncio.wait_for(self.runtime._history.entered.wait(), 1)
        self.frame(2, environment=True)
        await asyncio.wait_for(self.runtime._queue.join(), 1)
        self.environment.assert_awaited_once()
        self.assertEqual(self.environment.call_args.args[-1],
                         {"co2": 650, "temperature": 23.75, "humidity": 48.25})
        self.report.assert_not_awaited()
        self.assertFalse(self.runtime._history.release.is_set())
        self.runtime._history.release.set()
        await self.drain()
        self.report.assert_awaited_once()

    async def test_storage_failure_and_unknown_replay_restore_across_modes(self):
        for outcome in ("storage_failure", "unknown"):
            with self.subTest(outcome=outcome):
                self.runtime = self.make_runtime()
                self.report.reset_mock()
                self.report.side_effect = r.providers.ProviderError("UNKNOWN: synthetic")
                self.frame()
                self.runtime.store.fail = outcome == "storage_failure"
                self.runtime._history.release.set()
                self.start()
                await self.drain()
                expected_calls = 0 if outcome == "storage_failure" else 1
                self.assertEqual(self.report.await_count, expected_calls)
                self.frame(12)
                await self.drain()
                self.assertEqual(self.report.await_count, expected_calls)
                saved = {"devices": {mac: state.snapshot()
                                      for mac, state in self.runtime.states.items()}}
                if outcome == "unknown":
                    self.assertEqual(saved, self.runtime.store.saved[-1])
                self.runtime.store.fail = False
                await self.runtime.async_stop()
                self.runtime = self.make_runtime(mode=c.WEIGHT_MODE_NEAREST)
                self.runtime.store.async_load.return_value = saved
                await self.runtime.async_prepare()
                self.frame(13, "restored-proxy")
                self.start()
                await self.drain()
                self.runtime._history.async_collect_for_upload.assert_not_awaited()
                self.assertEqual(self.report.await_count, expected_calls)

    async def test_fresh_at_consumption_and_prepared_queue_clock_reset(self):
        # GATT may exceed the serial queue's 60s budget; prepared jobs get a new
        # queued_at, but that must NOT reset the measurement's 120s age limit.
        cases = [(NOW, 70, True), (NOW, 121, False), (NOW - 110, 20, False)]
        for device_time, delay, allowed in cases:
            with self.subTest(device_time=device_time, delay=delay):
                self.clock.wall, self.clock.mono = NOW, 1000.0
                self.runtime = self.make_runtime(native=False)
                self.report.reset_mock()
                self.frame(device_time=device_time)
                self.start(serial=False)
                await asyncio.wait_for(self.runtime._history.entered.wait(), 1)
                self.clock.wall += delay
                self.clock.mono += delay
                self.runtime._history.release.set()
                await asyncio.wait_for(self.runtime._scale_queue.join(), 1)
                prepared = [job for job in self.runtime._queue._queue if job.kind == "weight"]
                self.assertEqual(len(prepared), 1)
                self.assertTrue(prepared[0].scale_prepared)
                self.assertEqual(prepared[0].queued_at, self.clock.mono)
                self.assertIn((MAC, device_time), self.runtime._scale_pending)
                self.frame(12, "prepared-queue-repeat", device_time=device_time)
                self.assertEqual(self.runtime._scale_queue.qsize(), 0)
                self.start(prepare=False)
                await self.drain()
                self.assertEqual(self.report.await_count, int(allowed))
                self.assertFalse(self.runtime._scale_pending)
                await self.runtime.async_stop()

    async def test_config_change_or_stop_never_sends_late_native_or_fallback(self):
        for native, boundary in ((True, "gatt"), (False, "gatt"),
                                 (True, "save"), (False, "save"),
                                 (True, "stop"), (False, "stop")):
            with self.subTest(native=native, boundary=boundary):
                self.runtime = self.make_runtime(native=native)
                self.frame()
                self.start(serial=boundary != "save")
                history = self.runtime._history
                await asyncio.wait_for(history.entered.wait(), 1)
                if boundary == "stop":
                    await self.runtime.async_stop()
                    history.release.set()
                    self.assertTrue(self.runtime._closing)
                    self.assertFalse(self.runtime._scale_pending)
                else:
                    def disable():
                        self.runtime.entry.data[c.CONF_WEIGHT_UPLOAD] = False
                    if boundary == "gatt":
                        disable()
                    else:
                        def disable_after_consumption():
                            if self.runtime.states[MAC].last_weight_attempt_device_time is not None:
                                disable()
                        self.runtime.store.on_save = disable_after_consumption
                    history.release.set()
                    if boundary == "save":
                        await asyncio.wait_for(self.runtime._scale_queue.join(), 1)
                        self.start(prepare=False)
                    await self.drain()
                    self.assertFalse(self.runtime._scale_pending)
                    if boundary == "save":
                        self.assertEqual(self.runtime.states[MAC].last_weight_attempt_device_time, NOW)
                self.report.assert_not_awaited()
                await self.runtime.async_stop()

    async def test_manual_pull_busy_conflict_and_late_result_cannot_upload(self):
        history = self.runtime._history
        manual = asyncio.create_task(self.runtime.async_pull_scale_history(MAC))
        self.addAsyncCleanup(self.cancel_task, manual)
        await asyncio.wait_for(history.manual_entered.wait(), 1)
        self.frame()
        self.start()
        await self.drain()
        self.report.assert_not_awaited()
        self.assertEqual(self.runtime.last_scale_decisions[MAC]["choice"], "conflict")
        self.assertEqual(self.runtime.states[MAC].cloud_status, "gatt_conflict")
        self.assertEqual(self.runtime.states[MAC].last_weight_attempt_device_time, NOW)
        history.manual_release.set()
        self.assertEqual((await asyncio.wait_for(manual, 1))["status"], "complete")
        self.frame(12, "late-current-manual")
        await self.drain()
        history.async_collect_for_upload.assert_awaited_once_with(MAC)
        self.report.assert_not_awaited()
        self.assertFalse(self.runtime._scale_pending)

    async def cancel_task(self, task):
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
