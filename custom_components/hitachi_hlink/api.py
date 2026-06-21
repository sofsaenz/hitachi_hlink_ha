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

# Gateway uses a self-signed TLS cert — skip verification.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# Fields that must appear on a real device control page.
_CONTROL_PAGE_MARKERS = ("OnOff", "OperationMode", "FanSpeed")


class HitachiGatewayError(Exception):
    pass


class HitachiDevice:
    """One indoor AC unit managed by the gateway."""

    def __init__(self, dev_id: int, name: str) -> None:
        self.dev_id = dev_id
        self.name = name
        self.on_off: str = "0"
        self.operation_mode: str = "4"
        self.temperature: int = 22
        self.fan_speed: str = "0"
        self.room_temp: float | None = None

    def __repr__(self) -> str:
        return f"HitachiDevice(id={self.dev_id}, name={self.name!r})"



class HitachiClient:
    """Async HTTP client for the gateway's index.cgi endpoint."""

    def __init__(
        self,
        host: str,
        port: int = 443,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._base     = f"https://{host}:{port}/index.cgi"
        self._username = username
        self._password = password
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
            # unsafe=True: aiohttp rejects cookies from bare IP addresses by default
            self._session = aiohttp.ClientSession(
                connector=connector,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _ensure_logged_in(self) -> None:
        """POST login credentials to establish a session cookie."""
        session = await self._get_session()
        # Post to the exact action URL from the form
        login_url = self._base + "?mod=0&act=1"
        login_data = {
            "mod": "0",
            "act": "1",
            "username": self._username or "",
            "password": self._password or "",
        }
        async with session.post(
            login_url,
            data=login_data,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            html = await resp.text()
            cookies = {k: v.value for k, v in session.cookie_jar.filter_cookies(self._base).items()}
            _LOGGER.error("Login POST status=%d cookies=%s html_title=%s",
                          resp.status, cookies, html[:200])
            if "<title>Login</title>" in html:
                raise HitachiGatewayError("Login rejected — check username and password")

    async def _get(self, params: dict, _retry: bool = True) -> str:
        """GET, logging in first if we hit the login page."""
        session = await self._get_session()
        async with session.get(
            self._base, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            html = await resp.text()
        is_login = "<title>Login</title>" in html
        _LOGGER.error("GET params=%s is_login=%s is_control=%s snippet=%s",
                      params, is_login, self._is_control_page(html), html[:300])
        if is_login:
            if not _retry or not self._username:
                raise HitachiGatewayError("Gateway requires login — provide credentials")
            await self._ensure_logged_in()
            async with session.get(
                self._base, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp2:
                html2 = await resp2.text()
                _LOGGER.error("GET retry is_control=%s snippet=%s",
                              self._is_control_page(html2), html2[:300])
                return html2
        return html

    async def _post(self, data: dict) -> None:
        """POST device command, re-logging in if session expired."""
        session = await self._get_session()
        async with session.post(
            self._base, data=data, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            html = await resp.text()
        if "<title>Login</title>" in html and self._username:
            await self._ensure_logged_in()
            async with session.post(
                self._base, data=data, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp2:
                resp2.raise_for_status()

    # ------------------------------------------------------------------
    # Device discovery
    # ------------------------------------------------------------------

    async def discover_devices(self) -> list[HitachiDevice]:
        """Probe device IDs 1–16, returning those with a valid control page."""
        names = await self._fetch_device_names()
        devices: list[HitachiDevice] = []

        for dev_id in range(1, 17):
            params = {"mod": MOD_AC, "act": ACT_GET_DEVICE, "dev": dev_id, "Temp": 0}
            try:
                html = await self._get(params)
                if not self._is_control_page(html):
                    _LOGGER.debug("dev=%d: not a control page, skipping", dev_id)
                    continue
                name = names.get(dev_id, f"AC Unit {dev_id}")
                device = HitachiDevice(dev_id, name)
                self._parse_control_page(html, device)
                devices.append(device)
                _LOGGER.debug("Found %r", device)
            except Exception as exc:
                _LOGGER.debug("dev=%d error: %s", dev_id, exc)
                continue

        if not devices:
            raise HitachiGatewayError(
                "No AC units found. Check the gateway IP, port, and credentials."
            )
        return devices

    async def _fetch_device_names(self) -> dict[int, str]:
        """Return {dev_id: room_name} from the device list page (best-effort)."""
        params = {"mod": MOD_DEVICE_LIST, "act": ACT_DEVICE_LIST}
        try:
            html = await self._get(params)
        except (aiohttp.ClientError, HitachiGatewayError):
            return {}

        soup = BeautifulSoup(html, "html.parser")
        names: dict[int, str] = {}

        for tag in soup.find_all("a", href=True):
            href: str = tag["href"]
            if "act=31" in href and "dev=" in href:
                dev_id = self._extract_param(href, "dev")
                if dev_id:
                    names[int(dev_id)] = tag.get_text(strip=True) or f"AC Unit {dev_id}"

        if not names:
            for div in soup.find_all("div", class_="myshow"):
                text = div.get_text(strip=True)
                for attr in ("onclick", "data-href", "data-url"):
                    dev_id = self._extract_param(div.get(attr, ""), "dev")
                    if dev_id:
                        names[int(dev_id)] = text or f"AC Unit {dev_id}"
                        break

        _LOGGER.debug("Device name map from list page: %s", names)
        return names

    @staticmethod
    def _is_control_page(html: str) -> bool:
        return any(m in html for m in _CONTROL_PAGE_MARKERS)

    # ------------------------------------------------------------------
    # State reading
    # ------------------------------------------------------------------

    async def fetch_state(self, device: HitachiDevice) -> None:
        params = {"mod": MOD_AC, "act": ACT_GET_DEVICE, "dev": device.dev_id, "Temp": 0}
        try:
            html = await self._get(params)
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
            return opt.get("value") if opt else tag.get("value")

        if (v := selected_value("OnOff")) is not None:
            device.on_off = v
        if (v := selected_value("OperationMode")) is not None:
            device.operation_mode = v
        if (v := selected_value("FanSpeed")) is not None:
            device.fan_speed = v

        for tag in soup.find_all("input", {"name": "Temp"}):
            try:
                device.temperature = int(tag.get("value", device.temperature))
            except (ValueError, TypeError):
                pass

        for candidate in ("RoomTemp", "roomTemp", "room_temp"):
            tag = soup.find(id=candidate)
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
    # State writing
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
        payload: dict[str, Any] = {
            "mod": MOD_AC,
            "act": ACT_SET_DEVICE,
            "dev": device.dev_id,
            "OnOff":         on_off         if on_off         is not None else device.on_off,
            "OperationMode": operation_mode if operation_mode is not None else device.operation_mode,
            "Temp":          temperature    if temperature    is not None else device.temperature,
            "FanSpeed":      fan_speed      if fan_speed      is not None else device.fan_speed,
        }
        try:
            await self._post(payload)
        except aiohttp.ClientError as exc:
            raise HitachiGatewayError(f"Failed writing device {device.dev_id}: {exc}") from exc

        if on_off         is not None: device.on_off         = on_off
        if operation_mode is not None: device.operation_mode = operation_mode
        if temperature    is not None: device.temperature    = temperature
        if fan_speed      is not None: device.fan_speed      = fan_speed

    @staticmethod
    def _extract_param(text: str, param: str) -> str | None:
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
