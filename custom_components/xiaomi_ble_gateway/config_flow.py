"""Select loaded accounts/devices; never ask for login credentials or keys."""
from __future__ import annotations

import time

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import providers
from .const import (
    CONF_ACCOUNT_ID, CONF_DEVICES, CONF_RECORD_RAW, CONF_RETENTION_DAYS,
    CONF_UPLOAD, CONF_TH_UPLOAD, CONF_TH_ENABLED_AT,
    DEFAULT_RECORD_RAW, DEFAULT_RETENTION_DAYS, DOMAIN,
    CONF_WEIGHT_UPLOAD, CONF_WEIGHT_DEVICE, CONF_WEIGHT_PROFILE,
    CONF_WEIGHT_ENABLED_AT, CONF_WEIGHT_MODE, CONF_WEIGHT_THRESHOLD,
    DEFAULT_WEIGHT_MODE, DEFAULT_WEIGHT_THRESHOLD, WEIGHT_MODE_FIXED,
    WEIGHT_MODE_NEAREST, positive_weight_threshold, SCALE_MODEL,
)
from .recorder_manager import get_recorder_manager


def _recording_fields(record_raw=DEFAULT_RECORD_RAW,
                      retention_days=DEFAULT_RETENTION_DAYS):
    return {
        vol.Required(CONF_RECORD_RAW, default=record_raw): bool,
        vol.Required(CONF_RETENTION_DAYS, default=retention_days): vol.All(
            int, vol.Range(min=1)
        ),
    }


def _devices_schema(devices, selected=(), upload=False, include_recording=False):
    return vol.Schema({
        vol.Required(CONF_DEVICES, default=list(selected)): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[selector.SelectOptionDict(
                    value=device.mac,
                    label=f"{device.name} · {device.model} · {device.mac}",
                ) for device in devices],
                multiple=True,
                mode=selector.SelectSelectorMode.LIST,
            )
        ),
        vol.Required(CONF_UPLOAD, default=upload): bool,
        **(_recording_fields() if include_recording else {}),
    })


async def _load_devices(hass, account_id):
    account = providers.available_accounts(hass).get(account_id)
    if account is None:
        return [], "source_unavailable"
    if not account.can_upload:
        # Home alone cannot supply bindkeys, even for local-only reception.
        return [], "metadata_only"
    try:
        devices = await providers.get_devices(hass, account_id)
    except providers.ProviderError:
        return [], "provider_error"
    return devices, None if devices else "no_devices"


def _selection_error(user_input, devices):
    selected = user_input[CONF_DEVICES]
    if not selected:
        return "select_devices"
    if not set(selected).issubset({device.mac for device in devices}):
        return "devices_unavailable"
    return None


class GatewayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._account_id = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return GatewayOptionsFlow()

    async def async_step_user(self, user_input=None):
        accounts = providers.available_accounts(self.hass)
        if not accounts:
            return self.async_abort(reason="no_accounts")
        errors = {}
        if user_input is not None:
            account_id = user_input[CONF_ACCOUNT_ID]
            if account_id not in accounts:
                errors["base"] = "source_unavailable"
            elif not accounts[account_id].can_upload:
                errors["base"] = "metadata_only"
            else:
                await self.async_set_unique_id(account_id)
                self._abort_if_unique_id_configured()
                self._account_id = account_id
                return await self.async_step_devices()
        return self.async_show_form(
            step_id="user", errors=errors,
            data_schema=vol.Schema({vol.Required(CONF_ACCOUNT_ID): vol.In({
                key: account.label for key, account in accounts.items()
            })}),
        )

    async def async_step_devices(self, user_input=None):
        devices, error = await _load_devices(self.hass, self._account_id)
        if error:
            return self.async_abort(reason=error)
        errors = {}
        if user_input is not None:
            error = _selection_error(user_input, devices)
            if error is None:
                return self.async_create_entry(
                    title="Xiaomi BLE Cloud Gateway",
                    data={CONF_ACCOUNT_ID: self._account_id,
                          CONF_DEVICES: user_input[CONF_DEVICES],
                          CONF_UPLOAD: user_input[CONF_UPLOAD],
                          CONF_TH_UPLOAD: False,
                          CONF_WEIGHT_UPLOAD: False,
                          CONF_RECORD_RAW: user_input[CONF_RECORD_RAW],
                          CONF_RETENTION_DAYS: user_input[CONF_RETENTION_DAYS]},
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="devices",
            data_schema=_devices_schema(devices, include_recording=True), errors=errors
        )


class GatewayOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        return self.async_show_menu(
            step_id="init", menu_options=["devices", "environment_upload", "weight_upload", "recording"]
        )

    async def async_step_environment_upload(self, user_input=None):
        # Independent opt-in; no provider/profile lookup and no counter reset.
        entry = self.config_entry
        if user_input is not None:
            enabled = user_input[CONF_TH_UPLOAD]
            enabled_at = entry.data.get(CONF_TH_ENABLED_AT, 0)
            if enabled and not entry.data.get(CONF_TH_UPLOAD, False):
                enabled_at = int(time.time())
            self.hass.config_entries.async_update_entry(entry, data={
                **entry.data, CONF_TH_UPLOAD: enabled, CONF_TH_ENABLED_AT: enabled_at,
            })
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="environment_upload",
            data_schema=vol.Schema({vol.Required(
                CONF_TH_UPLOAD, default=entry.data.get(CONF_TH_UPLOAD, False)): bool}),
        )

    def _save_weight(self, enabled, device_mac=None, profile_id=None,
                     mode=None, threshold=None):
        entry = self.config_entry
        old = entry.data
        device_mac = device_mac if device_mac is not None else old.get(CONF_WEIGHT_DEVICE)
        profile_id = profile_id if profile_id is not None else old.get(CONF_WEIGHT_PROFILE)
        mode = old.get(CONF_WEIGHT_MODE, DEFAULT_WEIGHT_MODE) if mode is None else mode
        threshold = (old.get(CONF_WEIGHT_THRESHOLD, DEFAULT_WEIGHT_THRESHOLD)
                     if threshold is None else threshold)
        changed_target = (device_mac != old.get(CONF_WEIGHT_DEVICE)
                          or profile_id != old.get(CONF_WEIGHT_PROFILE)
                          or mode != old.get(CONF_WEIGHT_MODE, DEFAULT_WEIGHT_MODE)
                          or threshold != old.get(CONF_WEIGHT_THRESHOLD, DEFAULT_WEIGHT_THRESHOLD))
        enabled_at = old.get(CONF_WEIGHT_ENABLED_AT, 0)
        if changed_target or (enabled and not old.get(CONF_WEIGHT_UPLOAD, False)):
            enabled_at = int(time.time())
        self.hass.config_entries.async_update_entry(entry, data={
            **old, CONF_WEIGHT_UPLOAD: enabled, CONF_WEIGHT_DEVICE: device_mac,
            CONF_WEIGHT_PROFILE: profile_id, CONF_WEIGHT_ENABLED_AT: enabled_at,
            CONF_WEIGHT_MODE: mode, CONF_WEIGHT_THRESHOLD: threshold,
        })
        return self.async_create_entry(title="", data={})

    async def async_step_weight_upload(self, user_input=None):
        entry = self.config_entry
        errors = {}
        if user_input is not None:
            try:
                threshold = positive_weight_threshold(user_input.get(
                    CONF_WEIGHT_THRESHOLD,
                    entry.data.get(CONF_WEIGHT_THRESHOLD, DEFAULT_WEIGHT_THRESHOLD)))
            except (ValueError, TypeError, OverflowError):
                errors["base"] = "invalid_weight_threshold"
            # Disabling valid settings needs no live provider or profile lookup.
            if not errors and not user_input[CONF_WEIGHT_UPLOAD]:
                return self._save_weight(
                    False, user_input.get(CONF_WEIGHT_DEVICE),
                    mode=user_input.get(CONF_WEIGHT_MODE), threshold=threshold,
                )
        devices, error = await _load_devices(self.hass, entry.data[CONF_ACCOUNT_ID])
        if error:
            return self.async_abort(reason=error)
        scales = {device.mac: device for device in devices
                  if device.model == SCALE_MODEL and device.mac in entry.data[CONF_DEVICES]}
        if not scales:
            return self.async_abort(reason="no_scales")
        if user_input is not None and not errors:
            mac = (next(iter(scales)) if len(scales) == 1
                   else user_input.get(CONF_WEIGHT_DEVICE))
            if mac not in scales:
                errors["base"] = "devices_unavailable"
            else:
                self._weight_device = scales[mac]
                self._weight_mode = user_input.get(CONF_WEIGHT_MODE, DEFAULT_WEIGHT_MODE)
                self._weight_threshold = threshold
                if self._weight_mode == WEIGHT_MODE_NEAREST:
                    return self._save_weight(
                        True, mac, mode=self._weight_mode, threshold=self._weight_threshold)
                return await self.async_step_weight_profile()
        fields = {
            vol.Required(CONF_WEIGHT_UPLOAD,
                         default=entry.data.get(CONF_WEIGHT_UPLOAD, False)): bool,
            vol.Required(CONF_WEIGHT_MODE,
                         default=entry.data.get(CONF_WEIGHT_MODE, DEFAULT_WEIGHT_MODE)):
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=WEIGHT_MODE_FIXED,
                                                  label="Fixed profile / 固定档案"),
                        selector.SelectOptionDict(value=WEIGHT_MODE_NEAREST,
                                                  label="Nearest weight / 最近体重"),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )),
            vol.Required(CONF_WEIGHT_THRESHOLD, default=entry.data.get(
                CONF_WEIGHT_THRESHOLD, DEFAULT_WEIGHT_THRESHOLD)):
                # Use serializable built-ins for HA's form API. The step also
                # rejects NaN/infinity with positive_weight_threshold above.
                vol.All(vol.Coerce(float), vol.Range(min=0, min_included=False)),
        }
        if len(scales) > 1:
            device_field = (vol.Required(CONF_WEIGHT_DEVICE,
                                        default=entry.data[CONF_WEIGHT_DEVICE])
                            if entry.data.get(CONF_WEIGHT_DEVICE) in scales
                            else vol.Required(CONF_WEIGHT_DEVICE))
            fields[device_field] = selector.SelectSelector(selector.SelectSelectorConfig(
                options=[selector.SelectOptionDict(value=device.mac,
                         label=f"{device.name} · {device.mac}") for device in scales.values()],
                mode=selector.SelectSelectorMode.DROPDOWN,
            ))
        return self.async_show_form(step_id="weight_upload", errors=errors,
                                    data_schema=vol.Schema(fields))

    async def async_step_weight_profile(self, user_input=None):
        entry = self.config_entry
        try:
            profiles = await providers.get_scale_profiles(
                self.hass, entry.data[CONF_ACCOUNT_ID], self._weight_device)
        except providers.ProviderError:
            return self.async_abort(reason="provider_error")
        if not profiles:
            return self.async_abort(reason="no_scale_profiles")
        # Keep personal profile payloads transient; persist only account_id.
        options = {str(profile.account_id): profile.name for profile in profiles}
        errors = {}
        if user_input is not None:
            profile_id = user_input[CONF_WEIGHT_PROFILE]
            if profile_id not in options:
                errors["base"] = "profile_unavailable"
            else:
                return self._save_weight(
                    True, self._weight_device.mac, profile_id,
                    mode=self._weight_mode, threshold=self._weight_threshold,
                )
        # No first-profile fallback, even if the provider returns just one.
        profile_field = vol.Required(CONF_WEIGHT_PROFILE)
        if (entry.data.get(CONF_WEIGHT_DEVICE) == self._weight_device.mac
                and entry.data.get(CONF_WEIGHT_PROFILE) in options):
            profile_field = vol.Required(CONF_WEIGHT_PROFILE,
                                        default=entry.data[CONF_WEIGHT_PROFILE])
        return self.async_show_form(
            step_id="weight_profile", errors=errors,
            data_schema=vol.Schema({profile_field: selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[selector.SelectOptionDict(value=key, label=f"{name} · {key}")
                             for key, name in options.items()],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                ))}),
        )

    async def async_step_recording(self, user_input=None):
        # No provider access: even failed account setups can disable recording.
        entry = self.config_entry
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data,
                             CONF_RECORD_RAW: user_input[CONF_RECORD_RAW],
                             CONF_RETENTION_DAYS: user_input[CONF_RETENTION_DAYS]},
            )
            await get_recorder_manager(self.hass).async_refresh()
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="recording",
            data_schema=vol.Schema(_recording_fields(
                entry.data.get(CONF_RECORD_RAW, DEFAULT_RECORD_RAW),
                entry.data.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS),
            )),
        )

    async def async_step_devices(self, user_input=None):
        entry = self.config_entry
        devices, error = await _load_devices(self.hass, entry.data[CONF_ACCOUNT_ID])
        if error:
            return self.async_abort(reason=error)
        errors = {}
        if user_input is not None:
            error = _selection_error(user_input, devices)
            if error is None:
                # Preserve recording settings; device/upload edits still reload.
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data,
                                 CONF_DEVICES: user_input[CONF_DEVICES],
                                 CONF_UPLOAD: user_input[CONF_UPLOAD]},
                )
                return self.async_create_entry(title="", data={})
            errors["base"] = error
        available = {device.mac for device in devices}
        return self.async_show_form(
            step_id="devices", errors=errors,
            data_schema=_devices_schema(
                devices, [mac for mac in entry.data[CONF_DEVICES] if mac in available],
                entry.data.get(CONF_UPLOAD, False),
            ),
        )
