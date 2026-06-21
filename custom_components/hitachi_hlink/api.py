"""Local HTTP client for Hitachi HC-IOTGW (Aircloud Pro) gateway — index.cgi interface."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import ssl
from typing import Any
from urllib.parse import urlparse  # used in _make_auth_header POST path

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


def _digest_auth_header(
    username: str, password: str, method: str, uri: str, www_auth: str
) -> str:
    """Compute an Authorization: Digest header from a WWW-Authenticate challenge."""
    realm  = re.search(r'realm="([^"]*)"',  www_auth)
    nonce  = re.search(r'nonce="([^"]*)"',  www_auth)
    opaque = re.search(r'opaque="([^"]*)"', www_auth)
    qop    = re.search(r'qop="([^"]*)"',    www_auth)

    realm_val  = realm.group(1)  if realm  else ""
    nonce_val  = nonce.group(1)  if nonce  else ""
    opaque_val = opaque.group(1) if opaque else None

    ha1 = hashlib.md5(f"{username}:{realm_val}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()

    if qop and "auth" in qop.group(1):
        nc     = "00000001"
        cnonce = hashlib.md5(os.urandom(8)).hexdigest()[:8]
        resp   = hashlib.md5(f"{ha1}:{nonce_val}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        header = (
            f'Digest username="{username}", realm="{realm_val}", nonce="{nonce_val}", '
            f'uri="{uri}", qop=auth, nc={nc}, cnonce="{cnonce}", response="{resp}"'
        )
    else:
        resp   = hashlib.md5(f"{ha1}:{nonce_val}:{ha2}".encode()).hexdigest()
        header = (
            f'Digest username="{username}", realm="{realm_val}", nonce="{nonce_val}", '
            f'uri="{uri}", response="{resp}"'
        )

    if opaque_val:
        header += f', opaque="{opaque_val}"'
    return header


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
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _make_auth_header(self, method: str, url: str, params: dict | None, www_auth: str) -> str:
        """Return the correct Authorization header for a given WWW-Authenticate challenge."""
        if "Digest" in www_auth:
            from yarl import URL as YarlURL
            if params:
                uri = YarlURL(url).with_query(params).path_qs
            else:
                uri = urlparse(url).path
            return _digest_auth_header(self._username, self._password, method, uri, www_auth)
        # Basic
        import base64
        token = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
        return f"Basic {token}"

    async def _get(self, params: dict) -> str:
        """GET with auth — waits for 401 challenge to determine auth type."""
        session = await self._get_session()
        url = self._base

        # First attempt without auth to see what the server wants.
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return await resp.text()
            if resp.status == 401 and self._username:
                www_auth = resp.headers.get("WWW-Authenticate", "")
                _LOGGER.debug("GET 401, WWW-Authenticate: %s", www_auth)
                auth_header = self._make_auth_header("GET", url, params, www_auth)
                async with session.get(
                    url, params=params,
                    headers={"Authorization": auth_header},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp2:
                    _LOGGER.debug("GET retry status: %d", resp2.status)
                    resp2.raise_for_status()
                    return await resp2.text()
            resp.raise_for_status()
            return await resp.text()

    async def _post(self, data: dict) -> None:
        """POST with auth — waits for 401 challenge to determine auth type."""
        session = await self._get_session()
        url = self._base

        async with session.post(url, data=data,
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return
            if resp.status == 401 and self._username:
                www_auth = resp.headers.get("WWW-Authenticate", "")
                auth_header = self._make_auth_header("POST", url, None, www_auth)
                async with session.post(
                    url, data=data,
                    headers={"Authorization": auth_header},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp2:
                    resp2.raise_for_status()
                    return
            resp.raise_for_status()

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
            except (aiohttp.ClientError, HitachiGatewayError) as exc:
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
