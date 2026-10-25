"""One raw recorder for all enabled entries, independent of account setup."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_RECORD_RAW, CONF_RETENTION_DAYS, DATA_RECORDER_MANAGER,
    DEFAULT_RECORD_RAW, DEFAULT_RETENTION_DAYS, DOMAIN,
)
from .recorder import RawRecorder


class RecorderManager:
    """Serialize recorder lifecycle without filtering by account or devices."""

    def __init__(self, hass):
        self.hass = hass
        self.recorder = RawRecorder(hass, DEFAULT_RETENTION_DAYS)
        self._retention_days = DEFAULT_RETENTION_DAYS
        self._lock = asyncio.Lock()
        self._running = False
        self._closing = False
        self._unsub_timer = None
        self._unsub_stop = None

    async def async_start(self):
        if self._closing:
            return
        if self._unsub_timer is None:
            self._unsub_timer = async_track_time_interval(
                self.hass, self._async_tick, timedelta(seconds=60)
            )
            self._unsub_stop = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self.async_stop
            )
        await self.async_refresh()

    async def _async_tick(self, _now):
        await self.async_refresh()

    async def async_refresh(self, exclude_entry_id=None):
        """Use all non-disabled entries, including failed/not-yet-loaded ones."""
        async with self._lock:
            if self._closing:
                return
            entries = [
                entry for entry in self.hass.config_entries.async_entries(DOMAIN)
                if entry.entry_id != exclude_entry_id
                and entry.disabled_by is None
                and entry.data.get(CONF_RECORD_RAW, DEFAULT_RECORD_RAW)
            ]
            if not entries:
                if self._running:
                    await self.recorder.async_stop()
                    self._running = False
                return
            days = max(entry.data.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)
                       for entry in entries)
            if days != self._retention_days:
                self.recorder.set_retention_days(days)
                self._retention_days = days
            if not self._running:
                await self.recorder.async_start()
                self._running = True

    async def async_stop(self, _event=None):
        self._closing = True
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None
        async with self._lock:
            if self._running:
                await self.recorder.async_stop()
                self._running = False

    def status(self):
        return self.recorder.status()


def get_recorder_manager(hass):
    """Return the single HA-wide manager; no provider calls or credentials."""
    data = hass.data.setdefault(DOMAIN, {})
    if DATA_RECORDER_MANAGER not in data:
        data[DATA_RECORDER_MANAGER] = RecorderManager(hass)
    return data[DATA_RECORDER_MANAGER]
