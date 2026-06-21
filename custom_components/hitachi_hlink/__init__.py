"""Hitachi HLink Aircloud Pro — Home Assistant integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HitachiClient, HitachiDevice, HitachiGatewayError
from .const import DEFAULT_PORT, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    client = HitachiClient(host, port)

    try:
        found = await client.discover_devices()
    except HitachiGatewayError as exc:
        _LOGGER.error("Cannot connect to Hitachi gateway at %s: %s", host, exc)
        await client.close()
        return False

    if not found:
        _LOGGER.error("No AC units found on gateway at %s", host)
        await client.close()
        return False

    devices: dict[int, HitachiDevice] = {d.dev_id: d for d in found}

    async def _async_update() -> dict[int, HitachiDevice]:
        try:
            for device in devices.values():
                await client.fetch_state(device)
        except HitachiGatewayError as exc:
            raise UpdateFailed(str(exc)) from exc
        return devices

    coordinator: DataUpdateCoordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=_async_update,
        update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "devices": devices,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["client"].close()
    return unloaded
