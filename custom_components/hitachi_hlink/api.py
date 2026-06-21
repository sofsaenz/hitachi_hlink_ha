"""Local HTTP client for Hitachi HC-IOTGW (Aircloud Pro) gateway — index.cgi interface."""
from __future__ import annotations

import logging
import ssl
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

from .const import (
    ACT_DEVICE_LIST,
    ACT_GET_DEVICE,
    ACT_SET_DEVICE,
    MOD_AC,
    MOD_DEVICE_LIST,
)

_LOGGER = logging.getLogger(__name__)

# Gateway uses a self-signed TLS cert on the local network.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


class HitachiGatewayError(Exception):
    pass


class HitachiDevice:
    """One indoor AC unit managed by the gateway."""

    def __init__(self, dev_id: int, name: str) -> None:
        self.dev_id = dev_id
        self.name = name
        # Mutable state updated by fetch_state()
        self.on_off: str = "0"
        self.operation_mode: str = "4"
        self.temperature: int = 22
        self.fan_speed: str = "0"
        self.room_temp: float | None = None

    def __repr__(self) -> str:
        return f"HitachiDevice(id={self.dev_id}, name={self.name!r})"


class HitachiClient:
    """Async HTTP client for the gateway's index.cgi endpoint."""

    def __init__(self, host: str, port: int = 443) -> None:
        self._base = f"https://{host}:{port}/index.cgi"
        self._session: aiohttp.ClientSession | None = None

    async def _session_(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Device discovery  (mod=1&act=11 → device list page)
    # ------------------------------------------------------------------

    async def discover_devices(self) -> list[HitachiDevice]:
        """Parse the gateway device list to get real device names and IDs."""
        params = {"mod": MOD_DEVICE_LIST, "act": ACT_DEVICE_LIST}
        session = await self._session_()
        try:
            async with session.get(
                self._base, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except aiohttp.ClientError as exc:
            raise HitachiGatewayError(f"Cannot reach gateway: {exc}") from exc

        return self._parse_device_list(html)

    def _parse_device_list(self, html: str) -> list[HitachiDevice]:
        """Extract device IDs and names from the device list table."""
        soup = BeautifulSoup(html, "html.parser")
        devices: list[HitachiDevice] = []

        # Each device row has a link or div that navigates to mod=3&act=31&dev=N
        for tag in soup.find_all("a", href=True):
            href: str = tag["href"]
            if "act=31" in href and "dev=" in href:
                dev_id = self._extract_param(href, "dev")
                if dev_id is None:
                    continue
                name = tag.get_text(strip=True) or f"AC Unit {dev_id}"
                devices.append(HitachiDevice(int(dev_id), name))

        # Fallback: look for div.myshow elements with navigation (as seen in recording)
        if not devices:
            for div in soup.find_all("div", class_="myshow"):
                parent_row = div.find_parent("tr")
                if not parent_row:
                    continue
                onclick = div.get("onclick", "")
                dev_id = self._extract_param(onclick, "dev")
                if dev_id is None:
                    continue
                name = div.get_text(strip=True) or f"AC Unit {dev_id}"
                devices.append(HitachiDevice(int(dev_id), name))

        _LOGGER.debug("Discovered %d device(s): %s", len(devices), devices)
        return devices

    @staticmethod
    def _extract_param(text: str, param: str) -> str | None:
        """Extract a query-string parameter value from a URL or onclick string."""
        key = f"{param}="
        idx = text.find(key)
        if idx == -1:
            return None
        start = idx + len(key)
        end = start
        while end < len(text) and text[end].isdigit():
            end += 1
        value = text[start:end]
        return value if value else None

    # ------------------------------------------------------------------
    # State reading  (mod=3&act=31&dev=N)
    # ------------------------------------------------------------------

    async def fetch_state(self, device: HitachiDevice) -> None:
        """GET the device control page and parse current field values."""
        params = {"mod": MOD_AC, "act": ACT_GET_DEVICE, "dev": device.dev_id, "Temp": 0}
        session = await self._session_()
        try:
            async with session.get(
                self._base, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except aiohttp.ClientError as exc:
            raise HitachiGatewayError(f"Failed reading device {device.dev_id}: {exc}") from exc

        self._parse_control_page(html, device)

    @staticmethod
    def _parse_control_page(html: str, device: HitachiDevice) -> None:
        soup = BeautifulSoup(html, "html.parser")

        def selected_value(field_id: str) -> str | None:
            tag = soup.find(id=field_id)
            if tag is None:
                return None
            opt = tag.find("option", selected=True)
            if opt:
                return opt.get("value")
            return tag.get("value")

        if (v := selected_value("OnOff")) is not None:
            device.on_off = v
        if (v := selected_value("OperationMode")) is not None:
            device.operation_mode = v
        if (v := selected_value("FanSpeed")) is not None:
            device.fan_speed = v

        # Temperature setpoint hidden input or named input
        for tag in soup.find_all("input", {"name": "Temp"}):
            try:
                device.temperature = int(tag.get("value", device.temperature))
            except (ValueError, TypeError):
                pass

        # Room temperature — read-only display element
        for candidate_id in ("RoomTemp", "roomTemp", "room_temp"):
            tag = soup.find(id=candidate_id)
            if tag:
                try:
                    device.room_temp = float(tag.get_text(strip=True))
                except (ValueError, TypeError):
                    pass
                break

        _LOGGER.debug(
            "Device %s: on=%s mode=%s temp=%s fan=%s room=%s",
            device.dev_id, device.on_off, device.operation_mode,
            device.temperature, device.fan_speed, device.room_temp,
        )

    # ------------------------------------------------------------------
    # State writing  (POST mod=3&act=33&dev=N)
    # ------------------------------------------------------------------

    async def set_state(
        self,
        device: HitachiDevice,
        *,
        on_off: str | None = None,
        operation_mode: str | None = None,
        temperature: int | None = None,
        fan_speed: str | None = None,
    ) -> None:
        """POST updated values. Always sends the full form so the gateway gets a complete payload."""
        payload: dict[str, Any] = {
            "mod": MOD_AC,
            "act": ACT_SET_DEVICE,
            "dev": device.dev_id,
            "OnOff":         on_off         if on_off         is not None else device.on_off,
            "OperationMode": operation_mode if operation_mode is not None else device.operation_mode,
            "Temp":          temperature    if temperature    is not None else device.temperature,
            "FanSpeed":      fan_speed      if fan_speed      is not None else device.fan_speed,
        }
        session = await self._session_()
        try:
            async with session.post(
                self._base, data=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
        except aiohttp.ClientError as exc:
            raise HitachiGatewayError(f"Failed writing device {device.dev_id}: {exc}") from exc

        # Optimistic local update
        if on_off         is not None: device.on_off         = on_off
        if operation_mode is not None: device.operation_mode = operation_mode
        if temperature    is not None: device.temperature    = temperature
        if fan_speed      is not None: device.fan_speed      = fan_speed
