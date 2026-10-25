"""Offline runtime/options paths with small HA stubs; no login or network."""
import asyncio
from contextlib import suppress
import copy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1] / "custom_components/xiaomi_ble_gateway"
PACKAGE = "_gateway_environment_offline"
MAC = "123456789ABC"


class Store:
    def __init__(self, *args):
        self.saved = []
        self.fail = False
        self.on_save = None

    async def async_load(self):
        return None

    async def async_save(self, value):
        if self.fail:
            raise OSError("synthetic storage failure")
        self.saved.append(copy.deepcopy(value))
        if self.on_save:
            self.on_save()


class Flow:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def async_show_form(self, **kwargs):
        return kwargs

    def async_show_menu(self, **kwargs):
        return kwargs

    def async_create_entry(self, **kwargs):
        return kwargs


class Required:
    def __init__(self, key, default=None):
        self.schema, self.default = key, default


def load_modules():
    """Do not leak HA/provider/schema stubs into other test modules."""
    names = ["homeassistant", "homeassistant.components", "homeassistant.components.bluetooth",
             "homeassistant.core", "homeassistant.helpers", "homeassistant.helpers.dispatcher",
             "homeassistant.helpers.storage", "homeassistant.helpers.selector",
             "homeassistant.config_entries", "homeassistant.const", "homeassistant.exceptions",
             "voluptuous", PACKAGE, PACKAGE + ".providers", PACKAGE + ".protocol",
             PACKAGE + ".recorder_manager"]
    stubs = {name: ModuleType(name) for name in names}
    stubs[PACKAGE].__path__ = [str(ROOT)]
    stubs["homeassistant.core"].callback = lambda fn: fn
    stubs["homeassistant.helpers.dispatcher"].async_dispatcher_send = lambda *args: None
    stubs["homeassistant.helpers.storage"].Store = Store
    stubs["homeassistant.helpers"].selector = stubs["homeassistant.helpers.selector"]
    entries = stubs["homeassistant.config_entries"]
    entries.ConfigFlow, entries.OptionsFlow, entries.ConfigEntry = Flow, Flow, object
    stubs["homeassistant"].config_entries = entries
    stubs["homeassistant.const"].EVENT_HOMEASSISTANT_STOP = "stop"
    stubs["homeassistant.const"].Platform = NS(SENSOR="sensor")
    errors = stubs["homeassistant.exceptions"]
    errors.ConfigEntryError = errors.ConfigEntryNotReady = RuntimeError
    stubs["voluptuous"].Required = Required
    stubs["voluptuous"].Schema = lambda fields: NS(schema=fields)
    stubs[PACKAGE + ".providers"].ProviderError = type("ProviderError", (Exception,), {})
    stubs[PACKAGE + ".providers"].report_environment = AsyncMock()
    stubs[PACKAGE + ".protocol"].decode_advertisement = Mock()
    stubs[PACKAGE + ".recorder_manager"].get_recorder_manager = lambda hass: NS(
        async_refresh=AsyncMock())
    loaded = []
    with patch.dict(sys.modules, stubs):
        for name, filename in [("const", "const.py"), ("state", "state.py"),
                               ("runtime", "runtime.py"), ("config_flow", "config_flow.py"),
                               ("integration", "__init__.py")]:
            spec = importlib.util.spec_from_file_location(PACKAGE + "." + name, ROOT / filename)
            module = importlib.util.module_from_spec(spec)
            module.__package__ = PACKAGE
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded.append(module)
    return loaded


c, state_module, r, flows, integration = load_modules()


class EnvironmentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.report = AsyncMock()
        self.provider_patch = patch.object(r.providers, "report_environment", self.report)
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)
        self.runtime = self.make_runtime()
        self.addAsyncCleanup(self.stop_worker)

    def make_runtime(self, co2=False, th=True, epoch=1, model=c.CO2_MODEL):
        entry = NS(entry_id="offline", data={c.CONF_ACCOUNT_ID: "synthetic",
                   c.CONF_DEVICES: [MAC], c.CONF_UPLOAD: co2,
                   c.CONF_TH_UPLOAD: th, c.CONF_TH_ENABLED_AT: epoch})
        device = NS(mac=MAC, model=model)
        runtime = r.GatewayRuntime(NS(), entry, [device], {MAC: b""}, co2)
        runtime._started_at = asyncio.get_running_loop().time() - 1
        runtime.states[MAC].observe(10, {"temperature": 99.9, "humidity": 99.9})
        return runtime

    def frame(self, readings, counter=11, seen="live", source="synthetic"):
        info = NS(address="12:34:56:78:9A:BC", service_data={c.FE95_UUID: b"synthetic"},
                  source=source)
        if seen != "missing":
            info.time = asyncio.get_running_loop().time() if seen == "live" else seen
        frame = NS(counter=counter, product_id=c.MODEL_PIDS[self.runtime.devices[MAC].model],
                   readings=readings, metadata={})
        with patch.object(r, "decode_advertisement", return_value=frame):
            self.runtime._on_advertisement(info, None)
        return frame

    async def drain(self):
        self.runtime._worker = asyncio.create_task(self.runtime._async_process())
        await asyncio.wait_for(self.runtime._queue.join(), 1)

    async def stop_worker(self):
        worker = self.runtime._worker
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def test_t2_th_without_co2_cache_and_duplicates(self):
        self.runtime = self.make_runtime(model=c.T2_MODEL)
        await self.runtime.async_prepare()
        self.assertEqual(self.runtime.states[MAC].cloud_status, "waiting")
        self.frame({"temperature": 99.0}, seen=self.runtime._started_at - 1)
        for source in ("proxy-a", "proxy-b"):
            self.frame({"temperature": -12.3, "humidity": 56.7, "battery": 87},
                       counter=12, source=source)
        await self.drain()
        self.report.assert_awaited_once()
        self.assertEqual(self.report.call_args.args[-1], {"temperature": -12.3, "humidity": 56.7})
        self.assertEqual(self.runtime.store.saved[-1]["devices"][MAC]["last_attempt"], 12)
        await self.stop_worker()
        self.report.reset_mock()
        self.runtime = self.make_runtime(co2=True, th=False, model=c.T2_MODEL)
        await self.runtime.async_prepare()
        self.assertEqual(self.runtime.states[MAC].cloud_status, "disabled")
        self.frame({"co2": 650, "temperature": 22.3})
        await self.drain()
        self.report.assert_not_awaited()

    async def test_th_only_fresh_batch_and_durable_gate(self):
        await self.runtime.async_prepare()
        self.assertEqual(self.runtime.states[MAC].cloud_status, "waiting")
        self.frame({"co2": 650, "temperature": 23.75, "humidity": 48.25, "battery": 80})

        async def send(hass, account, device, readings):
            self.assertEqual(readings, {"temperature": 23.75, "humidity": 48.25})
            self.assertTrue(all(type(v) is float for v in readings.values()))
            saved = self.runtime.store.saved[-1]["devices"][MAC]
            self.assertEqual(saved["last_attempt"], 11)
            self.assertEqual(saved["cloud_status"], "unknown")
            self.assertNotIn("temperature", str(saved))

        self.report.side_effect = send
        await self.drain()
        self.report.assert_awaited_once()
        self.assertEqual(self.runtime.last_environment_upload[MAC], {
            "counter": 11, "fields": ["temperature", "humidity"],
            "receive_time": self.runtime.last_environment_upload[MAC]["receive_time"],
            "status": "success", "error": None})
        self.assertIn("last_success_at", self.runtime.environment_fields[MAC]["temperature"])

    async def test_same_counter_three_sources_one_complete_batch(self):
        self.runtime = self.make_runtime(co2=True)
        for source in ("proxy-a", "proxy-b", "local"):
            self.frame({"co2": 650.9, "temperature": 23.75, "humidity": 48.25}, source=source)
        self.assertEqual(self.runtime._queue.qsize(), 1)
        await self.drain()
        self.report.assert_awaited_once()
        readings = self.report.call_args.args[-1]
        self.assertEqual(readings, {"co2": 650, "temperature": 23.75, "humidity": 48.25})
        self.assertIs(type(readings["co2"]), int)

    async def test_cache_missing_time_epoch_and_baseline_rejected_for_th(self):
        self.frame({"temperature": 20.5}, seen=self.runtime._started_at - 1)
        self.frame({"temperature": 20.5}, counter=12, seen="missing")
        self.runtime.th_enabled_at = 2**40
        self.frame({"temperature": 20.5}, counter=13)
        self.runtime.th_enabled_at = 1
        self.runtime.states[MAC] = state_module.DeviceState()
        self.frame({"temperature": 20.5}, counter=14)
        self.assertTrue(all(job.kind is None for job in self.runtime._queue._queue))
        await self.drain()
        self.report.assert_not_awaited()

    async def test_opt_out_keeps_co2_only_and_no_historical_fill(self):
        self.runtime = self.make_runtime(co2=True, th=False)
        self.frame({"co2": 650, "temperature": 23.75})
        await self.drain()
        self.assertEqual(self.report.call_args.args[-1], {"co2": 650})
        await self.stop_worker()
        self.report.reset_mock()
        self.runtime = self.make_runtime()
        self.frame({"humidity": 48.25})
        await self.drain()
        self.assertEqual(self.report.call_args.args[-1], {"humidity": 48.25})

    async def test_store_failure_never_calls_provider(self):
        self.frame({"temperature": 23.75})
        self.runtime.store.fail = True
        await self.drain()
        self.report.assert_not_awaited()
        self.assertEqual(self.runtime.states[MAC].last_attempt, 11)
        self.assertEqual(self.runtime.last_environment_upload[MAC]["status"], "storage_error")

    async def test_changed_config_drops_old_batch_before_and_during_save(self):
        for during_save in (False, True):
            with self.subTest(during_save=during_save):
                self.runtime = self.make_runtime(co2=True)
                self.frame({"co2": 650, "temperature": 23.75})
                def disable():
                    self.runtime.entry.data[c.CONF_TH_UPLOAD] = False
                    self.runtime.entry.data[c.CONF_UPLOAD] = False
                if during_save:
                    self.runtime.store.on_save = disable
                else:
                    disable()
                await self.drain()
                self.report.assert_not_awaited()
                await self.stop_worker()

    async def test_partial_or_unknown_not_success_or_retried(self):
        self.frame({"co2": 650, "temperature": 23.75, "humidity": 48.25})
        self.report.side_effect = r.providers.ProviderError("CLOUD_REJECTED: partial ACK")
        await self.drain()
        self.frame({"temperature": 23.75, "humidity": 48.25})
        self.report.assert_awaited_once()
        self.assertEqual(self.runtime.last_environment_upload[MAC]["status"], "unknown")
        for field in self.runtime.environment_fields[MAC].values():
            self.assertIn("last_unknown_at", field)
            self.assertNotIn("last_success_at", field)


class EnvironmentOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_schema_and_enable_epoch_transitions(self):
        flow = flows.GatewayOptionsFlow()
        entry = NS(data={c.CONF_UPLOAD: True, c.CONF_WEIGHT_UPLOAD: True,
                         c.CONF_RECORD_RAW: False})
        def update(target, *, data):
            target.data = data
        flow.config_entry = entry
        flow.hass = NS(config_entries=NS(async_update_entry=update))
        menu = await flow.async_step_init()
        self.assertIn("environment_upload", menu["menu_options"])
        form = await flow.async_step_environment_upload()
        [(key, validator)] = form["data_schema"].schema.items()
        self.assertEqual(form["step_id"], "environment_upload")
        self.assertEqual(key.schema, c.CONF_TH_UPLOAD)
        self.assertIs(key.default, False)
        self.assertIs(validator, bool)
        with patch.object(flows.time, "time", return_value=1234.9):
            await flow.async_step_environment_upload({c.CONF_TH_UPLOAD: True})
        self.assertEqual(entry.data[c.CONF_TH_ENABLED_AT], 1234)
        with patch.object(flows.time, "time", return_value=9999):
            await flow.async_step_environment_upload({c.CONF_TH_UPLOAD: True})
            self.assertEqual(entry.data[c.CONF_TH_ENABLED_AT], 1234)
            await flow.async_step_environment_upload({c.CONF_TH_UPLOAD: False})
            self.assertEqual(entry.data[c.CONF_TH_ENABLED_AT], 1234)
            await flow.async_step_environment_upload({c.CONF_TH_UPLOAD: True})
        self.assertEqual(entry.data[c.CONF_TH_ENABLED_AT], 9999)
        self.assertTrue(entry.data[c.CONF_UPLOAD])
        self.assertTrue(entry.data[c.CONF_WEIGHT_UPLOAD])
        self.assertFalse(entry.data[c.CONF_RECORD_RAW])

    async def test_reload_compares_both_th_keys(self):
        data = {c.CONF_UPLOAD: False, c.CONF_DEVICES: [MAC]}
        runtime = NS(upload=False, environment_config=c.environment_config(data),
                     weight_config=c.weight_config(data), devices={MAC: NS()})
        entry = NS(data=data, runtime_data=runtime, entry_id="offline")
        reload = AsyncMock()
        hass = NS(config_entries=NS(async_reload=reload))
        await integration._async_reload(hass, entry)
        reload.assert_not_awaited()
        for key, value in ((c.CONF_TH_UPLOAD, True), (c.CONF_TH_ENABLED_AT, 1234)):
            entry.data = {**data, key: value}
            await integration._async_reload(hass, entry)
        self.assertEqual(reload.await_count, 2)


if __name__ == "__main__":
    unittest.main()
