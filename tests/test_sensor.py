"""Minimal offline entity field coverage; no Home Assistant startup."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1] / "custom_components/xiaomi_ble_gateway"
PACKAGE = "_gateway_sensor_offline"


def load_sensor():
    names = [PACKAGE, "homeassistant", "homeassistant.components",
             "homeassistant.components.sensor", "homeassistant.const",
             "homeassistant.core", "homeassistant.helpers",
             "homeassistant.helpers.device_registry", "homeassistant.helpers.dispatcher",
             "homeassistant.helpers.entity"]
    stubs = {name: ModuleType(name) for name in names}
    stubs[PACKAGE].__path__ = [str(ROOT)]
    sensor = stubs["homeassistant.components.sensor"]
    sensor.RestoreSensor = object
    sensor.SensorEntityDescription = lambda **kw: NS(**kw)
    sensor.SensorDeviceClass = NS(CO2="co2", TEMPERATURE="temperature", HUMIDITY="humidity",
                                 BATTERY="battery", WEIGHT="weight")
    sensor.SensorStateClass = NS(MEASUREMENT="measurement")
    const = stubs["homeassistant.const"]
    const.CONCENTRATION_PARTS_PER_MILLION, const.PERCENTAGE = "ppm", "%"
    const.UnitOfMass, const.UnitOfTemperature = NS(KILOGRAMS="kg"), NS(CELSIUS="°C")
    stubs["homeassistant.core"].callback = lambda fn: fn
    registry = stubs["homeassistant.helpers.device_registry"]
    registry.CONNECTION_BLUETOOTH, registry.DeviceInfo = "bluetooth", lambda **kw: kw
    stubs["homeassistant.helpers.dispatcher"].async_dispatcher_connect = lambda *a: None
    stubs["homeassistant.helpers.entity"].EntityCategory = NS(DIAGNOSTIC="diagnostic")
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(PACKAGE + ".sensor", ROOT / "sensor.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


s = load_sensor()


class SensorTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_fields_and_environment_status(self):
        devices = {str(i): NS(mac=f"123456789AB{i}", model=model, name="Synthetic")
                   for i, model in enumerate((s.CO2_MODEL, s.T2_MODEL, s.SCALE_MODEL, "unknown"))}
        runtime = NS(devices=devices, upload=True, th_enabled=True, th_enabled_at=123,
                     states={d.mac: NS(counter=12, cloud_status="success", last_attempt=12,
                                      readings={"humidity": 56.7}) for d in devices.values()},
                     last_environment_upload={}, environment_fields={})
        entities = []
        await s.async_setup_entry(None, NS(runtime_data=runtime), entities.extend)
        by_model = {}
        for entity in entities:
            by_model.setdefault(entity.device.model, []).append(entity.entity_description.key)
        self.assertEqual(by_model, {
            s.CO2_MODEL: ["co2", "temperature", "humidity", "battery", "cloud_status"],
            s.T2_MODEL: ["temperature", "humidity", "battery", "cloud_status"],
            s.SCALE_MODEL: ["weight_kg", "cloud_status"]})
        humidity = next(e for e in entities if e.device.model == s.T2_MODEL
                        and e.entity_description.key == "humidity")
        self.assertEqual(humidity.native_value, 56.7)
        status = next(e for e in entities if e.device.model == s.T2_MODEL
                      and e.entity_description.key == "cloud_status")
        attrs = status.extra_state_attributes
        self.assertFalse(attrs["co2_upload_enabled"])
        self.assertTrue(attrs["temperature_humidity_upload_enabled"])
        self.assertIn("environment_fields", attrs)
        self.assertNotIn("weight_upload_enabled", attrs)


if __name__ == "__main__":
    unittest.main()
