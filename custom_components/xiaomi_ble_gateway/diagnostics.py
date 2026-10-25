"""Read-only runtime diagnostics; never expose account credentials or bindkeys."""
from homeassistant.components import bluetooth

from .const import CO2_MODEL, FE95_UUID, SCALE_MODEL, WEIGHT_MODE_FIXED
from .recorder_manager import get_recorder_manager
from .protocol import decode_advertisement


async def async_get_config_entry_diagnostics(hass, entry):
    recorder_status = get_recorder_manager(hass).status()
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return {'raw_recorder': recorder_status, 'devices': [],
                'runtime_available': False}
    cached = {info.address.replace(':', '').upper(): info
              for info in bluetooth.async_discovered_service_info(hass, connectable=False)}
    devices = []
    for mac, device in runtime.devices.items():
        state = runtime.states[mac]
        item = {'mac': mac, 'model': device.model, 'counter': state.counter,
                'last_attempt': state.last_attempt, 'cloud_status': state.cloud_status,
                'received_packets': runtime.received_packets[mac],
                'decode_error': runtime.decode_errors.get(mac),
                'upload_error': runtime.upload_errors.get(mac),
                'cached_frame': 'not_seen'}
        if device.model == CO2_MODEL:
            item.update(
                last_environment_upload=runtime.last_environment_upload.get(mac),
                environment_fields=runtime.environment_fields.get(mac, {}),
                environment_freshness="ha_live_reception_only_not_device_measurement_time",
            )
        if device.model == SCALE_MODEL:
            item.update(
                weight_upload_enabled=runtime.weight_active(mac),
                scale_upload_policy="gatt_first_single_attempt",
                gatt_listen_seconds=30,
                last_scale_decision=runtime.last_scale_decisions.get(mac),
                target_profile_id=(runtime.weight_profile if mac == runtime.weight_device
                                   and runtime.weight_mode == WEIGHT_MODE_FIXED else None),
                weight_upload_mode=runtime.weight_mode,
                weight_match_threshold=runtime.weight_threshold,
                weight_upload_enabled_at=runtime.weight_enabled_at,
                last_weight_attempt_device_time=state.last_weight_attempt_device_time,
                weight_attempt=state.weight_attempts.get(runtime.weight_attempt_key),
                last_weight_match=runtime.last_weight_matches.get(mac),
                weight_references=runtime.weight_references.get(mac),
                reference_error=runtime.reference_error.get(mac),
                snapshot_at=runtime.snapshot_at.get(mac),
                scale_history=(dict(runtime._history.status)
                               if runtime._history is not None else {"status": "not_started"}),
            )
        info = cached.get(mac)
        if info is not None and FE95_UUID in info.service_data:
            try:
                frame = decode_advertisement(info.service_data[FE95_UUID], info.address,
                                             runtime.keys[mac])
                item['cached_frame'] = 'basic' if frame is None else 'mic_verified'
                if frame is not None:
                    item['cached_counter'] = frame.counter
                    item['cached_reading_fields'] = list(frame.readings)
            except ValueError:
                item['cached_frame'] = 'authentication_or_format_error'
        devices.append(item)
    return {'raw_recorder': recorder_status, 'devices': devices,
            'upload_enabled': runtime.upload,
            'temperature_humidity_upload_enabled': runtime.th_enabled,
            'temperature_humidity_upload_enabled_at': runtime.th_enabled_at,
            'queue_pending': runtime._queue.qsize()}
