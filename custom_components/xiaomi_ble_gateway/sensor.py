"""Local broadcast values, not cloud-entity state mirroring."""
from homeassistant.components.sensor import (
    SensorDeviceClass, RestoreSensor, SensorEntityDescription, SensorStateClass,
)
from homeassistant.const import CONCENTRATION_PARTS_PER_MILLION, PERCENTAGE, UnitOfMass, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .const import CO2_MODEL, DOMAIN, SCALE_MODEL, WEIGHT_MODE_FIXED, bluetooth_address

_DESCRIPTIONS = {
    "co2": SensorEntityDescription(
        key="co2", name="二氧化碳", translation_key="co2", device_class=SensorDeviceClass.CO2,
        native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "temperature": SensorEntityDescription(
        key="temperature", name="温度", translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "humidity": SensorEntityDescription(
        key="humidity", name="湿度", translation_key="humidity", device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
    ),
    "battery": SensorEntityDescription(
        key="battery", name="电量", translation_key="battery", device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "weight_kg": SensorEntityDescription(
        key="weight_kg", name="广播体重", translation_key="broadcast_weight",
        device_class=SensorDeviceClass.WEIGHT, native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1,
    ),
    "cloud_status": SensorEntityDescription(
        key="cloud_status", name="云上报状态", translation_key="cloud_status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
}


async def async_setup_entry(hass, entry, async_add_entities):
    runtime = entry.runtime_data
    entities = []
    for device in runtime.devices.values():
        keys = ("co2", "temperature", "humidity", "battery") if device.model == CO2_MODEL else ("weight_kg",)
        entities.extend(GatewaySensor(runtime, device, _DESCRIPTIONS[key])
                        for key in (*keys, "cloud_status"))
    async_add_entities(entities)


class GatewaySensor(RestoreSensor):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime, device, description):
        self.runtime = runtime
        self.device = device
        self._restored_value = None
        self.entity_description = description
        self._attr_unique_id = f"{device.mac}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac)},
            connections={(CONNECTION_BLUETOOTH, bluetooth_address(device.mac))},
            name=device.name, manufacturer="Xiaomi", model=device.model,
        )

    @property
    def native_value(self):
        state = self.runtime.states[self.device.mac]
        if self.entity_description.key == "cloud_status":
            return state.cloud_status
        return state.readings.get(self.entity_description.key, self._restored_value)

    @property
    def extra_state_attributes(self):
        state = self.runtime.states[self.device.mac]
        attrs = {"last_counter": state.counter, "cloud_status": state.cloud_status}
        if self.entity_description.key == "cloud_status":
            attrs["last_attempt_counter"] = state.last_attempt
            if self.device.model == CO2_MODEL:
                attrs.update(
                    co2_upload_enabled=self.runtime.upload,
                    temperature_humidity_upload_enabled=self.runtime.th_enabled,
                    temperature_humidity_upload_enabled_at=self.runtime.th_enabled_at,
                    last_environment_upload=self.runtime.last_environment_upload.get(self.device.mac),
                    environment_fields=self.runtime.environment_fields.get(self.device.mac, {}),
                    environment_freshness="ha_live_reception_only_not_device_measurement_time",
                )
        if self.device.model == SCALE_MODEL:
            attrs.update(
                interpretation="community interpretation; not verified against device screen",
                measurement_source="local_ble_broadcast",
                native_health_upload="gatt_first_native_or_weight_only_single_attempt",
                target_profile_id=(self.runtime.weight_profile
                                   if self.device.mac == self.runtime.weight_device
                                   and self.runtime.weight_mode == WEIGHT_MODE_FIXED else None),
                weight_upload_mode=self.runtime.weight_mode,
                weight_match_threshold=self.runtime.weight_threshold,
                weight_upload_enabled=self.runtime.weight_active(self.device.mac),
                weight_upload_enabled_at=self.runtime.weight_enabled_at,
                record_time_source="authenticated_device_time",
                last_weight_attempt_device_time=state.last_weight_attempt_device_time,
                target_profile_last_attempt_device_time=(state.weight_attempts.get(
                    self.runtime.weight_profile, {}).get("last_weight_attempt_device_time")
                    if self.runtime.weight_mode == WEIGHT_MODE_FIXED else None),
                last_weight_record_time=state.weight_attempts.get(
                    self.runtime.weight_attempt_key, {}).get("record_time"),
            )
            if self.entity_description.key == "cloud_status":
                attrs["last_weight_match"] = self.runtime.last_weight_matches.get(self.device.mac)
                attrs["last_scale_decision"] = self.runtime.last_scale_decisions.get(self.device.mac)
        return attrs

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.entity_description.key != "cloud_status":
            restored = await self.async_get_last_sensor_data()
            if restored is not None:
                self._restored_value = restored.native_value
        self.async_on_remove(async_dispatcher_connect(
            self.hass, self.runtime.signal(self.device.mac), self._async_update
        ))

    @callback
    def _async_update(self):
        self.async_write_ha_state()
