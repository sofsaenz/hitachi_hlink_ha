"""Climate entity for Hitachi HLink Aircloud Pro."""
from __future__ import annotations

import logging

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components.climate.const import FAN_HIGH, FAN_LOW, FAN_MEDIUM
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .api import HitachiClient, HitachiDevice
from .const import (
    DOMAIN,
    FAN_SHARP,
    FAN_STRONG,
    FAN_WEAK,
    MODE_AUTO,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN,
    MODE_HEAT,
    ONOFF_OFF,
    ONOFF_ON,
    TEMP_MAX,
    TEMP_MIN,
    TEMP_STEP,
)

_LOGGER = logging.getLogger(__name__)

# Confirmed from browser recording
_HA_TO_GW_MODE: dict[HVACMode, str] = {
    HVACMode.AUTO:     MODE_AUTO,
    HVACMode.FAN_ONLY: MODE_FAN,   # value "1"
    HVACMode.HEAT:     MODE_HEAT,  # value "2"
    HVACMode.COOL:     MODE_COOL,  # value "4"
    HVACMode.DRY:      MODE_DRY,   # value "64"
}
_GW_TO_HA_MODE: dict[str, HVACMode] = {v: k for k, v in _HA_TO_GW_MODE.items()}

# FanSpeed values confirmed from browser recording
# Gateway label  →  HA fan mode string
_HA_TO_GW_FAN: dict[str, str] = {
    FAN_LOW:    FAN_WEAK,    # "0" = Weak Wind
    FAN_HIGH:   FAN_STRONG,  # "1" = Strong Wind
    FAN_MEDIUM: FAN_SHARP,   # "2" = Sharp Wind
}
_GW_TO_HA_FAN: dict[str, str] = {v: k for k, v in _HA_TO_GW_FAN.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DataUpdateCoordinator = data["coordinator"]
    client: HitachiClient = data["client"]
    devices: dict[int, HitachiDevice] = data["devices"]

    async_add_entities(
        [HitachiClimate(coordinator, client, device) for device in devices.values()],
        update_before_add=True,
    )


class HitachiClimate(CoordinatorEntity, ClimateEntity):
    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = TEMP_MIN
    _attr_max_temp = TEMP_MAX
    _attr_target_temperature_step = TEMP_STEP
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.AUTO,
        HVACMode.FAN_ONLY,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.DRY,
    ]
    _attr_fan_modes = [FAN_LOW, FAN_MEDIUM, FAN_HIGH]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE
    )

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        client: HitachiClient,
        device: HitachiDevice,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._device = device
        self._attr_unique_id = f"hitachi_hlink_{device.dev_id}"
        self._attr_name = device.name

    @property
    def _dev(self) -> HitachiDevice:
        return self.coordinator.data[self._device.dev_id]

    @property
    def hvac_mode(self) -> HVACMode:
        if self._dev.on_off == ONOFF_OFF:
            return HVACMode.OFF
        return _GW_TO_HA_MODE.get(self._dev.operation_mode, HVACMode.COOL)

    @property
    def target_temperature(self) -> float:
        return float(self._dev.temperature)

    @property
    def current_temperature(self) -> float | None:
        return self._dev.room_temp

    @property
    def fan_mode(self) -> str:
        return _GW_TO_HA_FAN.get(self._dev.fan_speed, FAN_LOW)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self._client.set_state(self._dev, on_off=ONOFF_OFF)
        else:
            await self._client.set_state(
                self._dev,
                on_off=ONOFF_ON,
                operation_mode=_HA_TO_GW_MODE.get(hvac_mode, MODE_COOL),
            )
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            await self._client.set_state(self._dev, temperature=int(temp))
            await self.coordinator.async_request_refresh()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._client.set_state(
            self._dev, fan_speed=_HA_TO_GW_FAN.get(fan_mode, FAN_WEAK)
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        await self._client.set_state(self._dev, on_off=ONOFF_ON)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self) -> None:
        await self._client.set_state(self._dev, on_off=ONOFF_OFF)
        await self.coordinator.async_request_refresh()
