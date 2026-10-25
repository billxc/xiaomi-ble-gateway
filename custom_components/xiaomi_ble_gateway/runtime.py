"""HA live Bluetooth -> authenticated state -> serial, at-most-once writes."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import logging
import math
import time

from homeassistant.components import bluetooth
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from . import providers
from .const import (CO2_MODEL, DOMAIN, FE95_UUID, MODEL_PIDS, SCALE_MODEL,
                    DEFAULT_WEIGHT_MODE, DEFAULT_WEIGHT_THRESHOLD,
                    WEIGHT_MODE_FIXED, WEIGHT_MODE_NEAREST, WEIGHT_NEAREST_AUDIT_KEY,
                    bluetooth_address, environment_config,
                    positive_weight_threshold, weight_config)
from .protocol import decode_advertisement
from .state import DeviceState, Observation, weight_time_is_fresh

_LOGGER = logging.getLogger(__name__)


def _reference_summary(references):
    """Allow only the provider's minimal reference contract into HA diagnostics."""
    if not isinstance(references, list):
        return []
    return [{key: ref[key] for key in (
        "profile_id", "name", "weight", "reference_time", "source"
    ) if key in ref} for ref in references if isinstance(ref, dict)]


@dataclass(frozen=True)
class UploadJob:
    mac: str
    counter: int
    value: float | None
    kind: str | None
    queued_at: float
    received_at: int
    profile_id: str | None = None
    enabled_at: int = 0
    device_time: int | None = None
    mode: str = DEFAULT_WEIGHT_MODE
    threshold: float = DEFAULT_WEIGHT_THRESHOLD
    environment_readings: dict = field(default_factory=dict)
    environment_config: tuple = ()

    @property
    def weight_attempt_key(self):
        return WEIGHT_NEAREST_AUDIT_KEY if self.mode == WEIGHT_MODE_NEAREST else self.profile_id


class GatewayRuntime:
    def __init__(self, hass, entry, devices, keys, upload):
        self.hass = hass
        self.entry = entry
        self.source_account_id = entry.data["account_id"]
        self.devices = {device.mac: device for device in devices}
        self.keys = keys
        self.upload = upload
        self.environment_config = environment_config(entry.data)
        _, self.th_enabled, self.th_enabled_at = self.environment_config
        self.weight_config = weight_config(entry.data)
        (self.weight_enabled, self.weight_device, self.weight_profile,
         self.weight_enabled_at, self.weight_mode, self.weight_threshold) = self.weight_config
        self.states = {mac: DeviceState() for mac in self.devices}
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._queue = asyncio.Queue(maxsize=128)
        self._unsubs = []
        self._worker = None
        self._reference_task = None
        self._history = None
        self._closing = False
        self._started_at = None
        self.received_packets = {mac: 0 for mac in self.devices}
        self.decode_errors = {}
        self.upload_errors = {}
        # RAM-only allowlisted provider results, never raw users or credentials.
        self.last_weight_matches = {}
        self.last_environment_upload = {}
        self.environment_fields = {}
        self.weight_references = {}
        self.reference_error = {}
        self.snapshot_at = {}

    async def async_pull_scale_history(self, mac):
        if self._history is None:
            from .history_runtime import ScaleHistoryCoordinator
            self._history = ScaleHistoryCoordinator(self)
        return await self._history.async_pull(mac)

    def signal(self, mac):
        return f"{DOMAIN}_{self.entry.entry_id}_{mac}"

    @property
    def weight_attempt_key(self):
        return (WEIGHT_NEAREST_AUDIT_KEY if self.weight_mode == WEIGHT_MODE_NEAREST
                else self.weight_profile)

    def weight_active(self, mac):
        try:
            positive_weight_threshold(self.weight_threshold)
        except (ValueError, TypeError, OverflowError):
            return False
        profile_ok = (self.weight_mode == WEIGHT_MODE_NEAREST
                      or (self.weight_mode == WEIGHT_MODE_FIXED
                          and isinstance(self.weight_profile, str)
                          and bool(self.weight_profile)
                          and self.weight_profile != WEIGHT_NEAREST_AUDIT_KEY))
        return (self.weight_enabled is True and mac == self.weight_device
                and profile_ok
                and type(self.weight_enabled_at) is int and self.weight_enabled_at > 0)

    @callback
    def _notify(self, mac):
        async_dispatcher_send(self.hass, self.signal(mac))

    async def async_prepare(self):
        saved = await self.store.async_load()
        if saved is not None:
            if not isinstance(saved, dict) or not isinstance(saved.get("devices"), dict):
                raise ValueError("Invalid replay state")
            # Keep deselected devices and every profile's replay high-water mark.
            for mac, data in saved["devices"].items():
                self.states[mac] = DeviceState.restore(data)
        for mac, device in self.devices.items():
            if device.model == SCALE_MODEL:
                self.states[mac].cloud_status = (
                    "waiting_weight" if self.weight_active(mac) else "weight_upload_disabled")
            elif not (self.upload or self.th_enabled):
                self.states[mac].cloud_status = "disabled"
            elif self.states[mac].cloud_status == "disabled":
                self.states[mac].cloud_status = "waiting"

    @callback
    def async_start(self):
        self._started_at = asyncio.get_running_loop().time()
        self._worker = self.hass.async_create_background_task(
            self._async_process(), f"{DOMAIN} serial upload {self.entry.entry_id}"
        )
        for mac in self.devices:
            self._unsubs.append(bluetooth.async_register_callback(
                self.hass, self._on_advertisement,
                {"address": bluetooth_address(mac), "connectable": False},
                bluetooth.BluetoothScanningMode.PASSIVE,
            ))
        # Register live reception first. This read-only preview never delays
        # local parsing/CO2 and is NEVER used to decide a weighing's target.
        if (self.weight_mode == WEIGHT_MODE_NEAREST
                and self.weight_device in self.devices
                and self.devices[self.weight_device].model == SCALE_MODEL
                and self.weight_active(self.weight_device)):
            self._reference_task = self.hass.async_create_background_task(
                self._async_load_weight_references(self.weight_device),
                f"{DOMAIN} scale reference preview {self.entry.entry_id}",
            )

    async def _async_load_weight_references(self, mac):
        try:
            async with asyncio.timeout(180):
                references = await providers.get_scale_weight_references(
                    self.hass, self.source_account_id, self.devices[mac])
            if not isinstance(references, list):
                raise ValueError("Invalid reference summary")
            self.weight_references[mac] = _reference_summary(references)
            self.snapshot_at[mac] = int(time.time())
            self.reference_error.pop(mac, None)
        except providers.ProviderError as err:
            # Provider messages contain only fixed reasons/status codes, no payloads.
            self.reference_error[mac] = str(err)
        except Exception as err:
            self.reference_error[mac] = f"Reference read failed ({type(err).__name__})"
        self._notify(mac)

    @callback
    def _on_advertisement(self, service_info, change):
        if self._closing:
            return
        # Only this callback's readings/time can create jobs, never restored values.
        received_at = int(time.time())
        mac = service_info.address.replace(":", "").upper()
        device = self.devices.get(mac)
        payload = service_info.service_data.get(FE95_UUID)
        if device is None or payload is None:
            return
        self.received_packets[mac] += 1
        try:
            frame = decode_advertisement(payload, service_info.address, self.keys[mac])
        except ValueError as err:
            self.decode_errors[mac] = str(err)
            return
        if frame is None or frame.product_id != MODEL_PIDS[device.model]:
            return
        if self._history is not None and device.model == SCALE_MODEL:
            # Separate local observations, including authenticated repeats from
            # different receivers. Never create an upload from a GATT record.
            self._history.observe(mac, frame, service_info.source, received_at)
        state = self.states[mac]
        observation = state.observe(frame.counter, frame.readings)
        if observation is Observation.OLD:
            return
        kind, value = None, None
        environment_readings = {}
        device_time = frame.metadata.get("device_time")
        if device.model == CO2_MODEL:
            if observation is Observation.NEW:
                # One complete FE95 payload, never merged with state.readings.
                if self.upload and "co2" in frame.readings:
                    environment_readings["co2"] = int(frame.readings["co2"])
                seen_at = getattr(service_info, "time", None)
                # TH has no device RTC: this verifies reception freshness only,
                # not measurement age. Discovery cache/missing time fail closed.
                live = (self._started_at is not None
                        and type(seen_at) in (int, float)
                        and self._started_at <= seen_at <= asyncio.get_running_loop().time())
                if (self.th_enabled is True
                        and type(self.th_enabled_at) is int and self.th_enabled_at > 0
                        and received_at >= self.th_enabled_at and live):
                    for key in ("temperature", "humidity"):
                        if key in frame.readings:
                            environment_readings[key] = float(frame.readings[key])
                if environment_readings:
                    kind = "environment"
            elif observation is Observation.BASELINE and (self.upload or self.th_enabled):
                state.cloud_status = "baseline"
        elif device.model == SCALE_MODEL and self.weight_active(mac):
            # HA may replay discovery cache while registering a callback. A
            # monotonic reception timestamp after registration is required for
            # upload; absent timestamps fail closed (local display still works).
            seen_at = getattr(service_info, "time", None)
            live = (self._started_at is not None
                    and isinstance(seen_at, (int, float))
                    and self._started_at <= seen_at <= asyncio.get_running_loop().time())
            weight = frame.readings.get("weight_kg")
            if (frame.metadata.get("mic_verified") is True
                    and frame.metadata.get("mass_raw") not in (None, 0, 0x7FF)
                    and isinstance(weight, (float, int)) and math.isfinite(weight) and weight > 0):
                if not live or not weight_time_is_fresh(device_time, self.weight_enabled_at, received_at):
                    state.cloud_status = "time_unverified"
                elif state.can_attempt_weight(self.weight_attempt_key, device_time):
                    # A scale baseline is eligible only with verified freshness.
                    kind, value = "weight", weight
        job = UploadJob(mac, frame.counter, value, kind,
                        asyncio.get_running_loop().time(), received_at,
                        self.weight_profile, self.weight_enabled_at, device_time,
                        self.weight_mode, self.weight_threshold,
                        dict(environment_readings), self.environment_config)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            if kind is not None:
                state.cloud_status = "skipped_overload"
        self._notify(mac)

    def _environment_result(self, job, status, error=None):
        """RAM-only batch/field metadata, not a replay gate or value cache."""
        now = int(time.time())
        summary = {
            "counter": job.counter, "fields": list(job.environment_readings),
            "receive_time": job.received_at, "status": status, "error": error,
        }
        self.last_environment_upload[job.mac] = summary
        fields = self.environment_fields.setdefault(job.mac, {})
        for key in job.environment_readings:
            previous = fields.get(key, {})
            fields[key] = {**previous, "counter": job.counter,
                           "receive_time": job.received_at,
                           "status": status, "error": error}
            if status in {"success", "unknown"}:
                fields[key][f"last_{status}_at"] = now

    async def _async_save(self):
        await self.store.async_save({"devices": {
            mac: state.snapshot() for mac, state in self.states.items()
        }})

    async def _async_process(self):
        while not self._closing:
            job = await self._queue.get()
            state = self.states[job.mac]
            try:
                kind = job.kind
                if kind and asyncio.get_running_loop().time() - job.queued_at > 60:
                    kind = None
                    state.cloud_status = "skipped_stale"
                if kind == "weight":
                    # Recheck in serial worker: multiple counters may have queued
                    # the same physical measurement before the first was marked.
                    if (weight_config(self.entry.data) != self.weight_config
                            or not weight_time_is_fresh(job.device_time, job.enabled_at, job.received_at)
                            or not state.can_attempt_weight(job.weight_attempt_key, job.device_time)):
                        kind = None
                    else:
                        # __nearest__ consumes the measurement locally before the
                        # provider chooses ONE real profile; it is never sent.
                        state.mark_weight_attempt(job.weight_attempt_key, job.device_time, job.received_at)
                elif kind == "environment":
                    if environment_config(self.entry.data) != job.environment_config:
                        kind = None
                    else:
                        state.mark_attempt(job.counter)
                        self._environment_result(job, "pending")
                # UNKNOWN/attempt must be durable before any provider call.
                try:
                    await self._async_save()
                except Exception as err:
                    if kind is not None:
                        state.cloud_status = "storage_error"
                        if kind == "environment":
                            self._environment_result(job, "storage_error")
                    _LOGGER.error("Replay state save failed (%s); cloud write skipped",
                                  type(err).__name__)
                    continue
                # Entry updates may have arrived while awaiting the Store save.
                # Never start an obsolete target's write before reload cancels us.
                if kind == "weight" and weight_config(self.entry.data) != self.weight_config:
                    kind = None
                if (kind == "environment"
                        and environment_config(self.entry.data) != job.environment_config):
                    self._environment_result(job, "skipped_config_changed")
                    kind = None
                if kind is not None and not self._closing:
                    self.upload_errors.pop(job.mac, None)
                    if kind == "weight":
                        self.last_weight_matches.pop(job.mac, None)
                    try:
                        # Weight provider performs profile/precheck/add plus
                        # bounded read-only confirmations (25s/request).
                        async with asyncio.timeout(180 if kind == "weight" else 35):
                            if kind == "weight":
                                if job.mode == WEIGHT_MODE_NEAREST:
                                    result = await providers.report_scale_weight_nearest(
                                        self.hass, self.source_account_id, self.devices[job.mac],
                                        float(job.value), job.received_at,
                                        max_difference=job.threshold,
                                    )
                                else:
                                    result = await providers.report_scale_weight(
                                        self.hass, self.source_account_id, self.devices[job.mac],
                                        job.profile_id, float(job.value), job.received_at,
                                    )
                                status = result.get("status")
                                state.cloud_status = status if status in {
                                    "verified", "existing_verified", "existing_conflict", "existing",
                                    "conflict", "no_match", "ambiguous", "no_reference",
                                } else "unknown"
                                references = result.get("references", [])
                                self.last_weight_matches[job.mac] = {
                                    "status": state.cloud_status,
                                    "selected_id": result.get("matched_profile_id"),
                                    "selected_name": result.get("matched_profile_name"),
                                    "difference": result.get("difference"),
                                    "references": _reference_summary(references),
                                }
                            else:
                                await providers.report_environment(
                                    self.hass, self.source_account_id,
                                    self.devices[job.mac], dict(job.environment_readings),
                                )
                                # Provider returns only when EVERY property ACKs 0.
                                state.cloud_status = "success"
                    except asyncio.CancelledError:
                        if kind == "environment":
                            self._environment_result(job, "unknown", "UNKNOWN: operation cancelled")
                        raise
                    except providers.ProviderError as err:
                        state.cloud_status = "unknown"
                        self.upload_errors[job.mac] = str(err)
                        _LOGGER.warning("%s upload UNKNOWN (%s); no automatic retry", kind, err)
                    except TimeoutError:
                        state.cloud_status = "unknown"
                        self.upload_errors[job.mac] = "UNKNOWN: overall operation timed out"
                        _LOGGER.warning("%s upload UNKNOWN; no automatic retry", kind)
                    except Exception as err:
                        state.cloud_status = "unknown"
                        self.upload_errors[job.mac] = f"UNKNOWN: {type(err).__name__}"
                        _LOGGER.error("%s upload UNKNOWN (%s); no automatic retry",
                                      kind, type(err).__name__)
                    if kind == "environment":
                        self._environment_result(job, state.cloud_status,
                                                 self.upload_errors.get(job.mac))
                    try:
                        await self._async_save()
                    except Exception as err:
                        _LOGGER.error("Cloud status save failed (%s)", type(err).__name__)
            finally:
                self._queue.task_done()
                self._notify(job.mac)

    async def async_stop(self, event=None):
        self._closing = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        # Cancel both before awaiting either; preview cleanup must not delay
        # stopping an in-flight upload worker.
        if self._reference_task is not None:
            self._reference_task.cancel()
        if self._worker is not None:
            self._worker.cancel()
        if self._history is not None:
            await self._history.async_stop()
        if self._reference_task is not None:
            with suppress(asyncio.CancelledError):
                await self._reference_task
            self._reference_task = None
        if self._worker is not None:
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        try:
            await self._async_save()
        except Exception as err:
            _LOGGER.error("Final replay state save failed (%s)", type(err).__name__)
        self.keys.clear()
