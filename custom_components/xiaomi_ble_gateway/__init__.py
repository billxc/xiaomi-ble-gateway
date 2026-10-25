"""Xiaomi BLE Cloud Gateway; reuses loaded account providers only."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from . import providers
from .const import (CONF_ACCOUNT_ID, CONF_DEVICES, CONF_UPLOAD, DOMAIN,
                    environment_config, weight_config)
from .runtime import GatewayRuntime
from .recorder_manager import get_recorder_manager

PLATFORMS = [Platform.SENSOR]


async def async_setup(hass, config) -> bool:
    # Recording must not depend on successful Miot/account/bindkey setup.
    await get_recorder_manager(hass).async_start()
    return True


async def async_setup_entry(hass, entry: ConfigEntry) -> bool:
    await get_recorder_manager(hass).async_start()
    account_id = entry.data[CONF_ACCOUNT_ID]
    account = providers.available_accounts(hass).get(account_id)
    if account is None:
        raise ConfigEntryNotReady(translation_domain=DOMAIN,
                                  translation_key="source_unavailable")
    if not account.can_upload:
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="metadata_only")
    keys = {}
    try:
        available = {device.mac: device for device in
                     await providers.get_devices(hass, account_id)}
        selected = entry.data[CONF_DEVICES]
        if not selected or any(mac not in available for mac in selected):
            raise ConfigEntryError(translation_domain=DOMAIN,
                                   translation_key="devices_unavailable")
        devices = [available[mac] for mac in selected]
        for device in devices:
            keys[device.mac] = await providers.get_bindkey(hass, account_id, device)
    except providers.ProviderError:
        keys.clear()
        raise ConfigEntryNotReady(translation_domain=DOMAIN,
                                  translation_key="bindkey_unavailable") from None
    runtime = GatewayRuntime(hass, entry, devices, keys, entry.data.get(CONF_UPLOAD, False))
    try:
        await runtime.async_prepare()
    except Exception:
        keys.clear()
        # Never silently reset corrupt replay state and risk replaying writes.
        raise ConfigEntryError(translation_domain=DOMAIN,
                               translation_key="state_unavailable") from None
    entry.runtime_data = runtime
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        runtime.async_start()
    except Exception:
        await runtime.async_stop()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    entry.async_on_unload(hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP, runtime.async_stop
    ))
    return True


async def _async_reload(hass, entry):
    await get_recorder_manager(hass).async_refresh()
    runtime = getattr(entry, "runtime_data", None)
    if (runtime is not None
            and runtime.upload == entry.data.get(CONF_UPLOAD, False)
            and runtime.environment_config == environment_config(entry.data)
            and runtime.weight_config == weight_config(entry.data)
            and set(runtime.devices) == set(entry.data[CONF_DEVICES])):
        # weight_config includes mode/threshold: changing either must reload.
        # Recording-only edits do not touch Miot, keys, entities or replay state.
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_stop()
    return True


async def async_remove_entry(hass, entry: ConfigEntry) -> None:
    # Explicit removal (not reload/unload) is the only automatic state deletion.
    from homeassistant.helpers.storage import Store

    await get_recorder_manager(hass).async_refresh(exclude_entry_id=entry.entry_id)
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
