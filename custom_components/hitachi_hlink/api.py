"""Local HTTP client for Hitachi HC-IOTGW (Aircloud Pro) gateway — index.cgi interface."""
from __future__ import annotations

import json
import logging
import ssl
import time

import aiohttp
from bs4 import BeautifulSoup

from .const import (
    ACT_DEVICE_LIST,
    ACT_GET_DEVICE,
    ACT_POLL_DEVICE,
    ACT_SET_DEVICE,
    MOD_AC,
    MOD_DEVICE_LIST,
)

# act=35 returns JSON with text labels; map back to numeric codes
_OPERATION_TO_ONOFF: dict[str, str] = {}   # anything != "OFF" → "1"
_MODE_TEXT_TO_CODE = {"cool": "4", "heat": "2", "fan": "1", "dry": "64", "auto": "0"}
_FAN_TEXT_TO_CODE  = {"weak wind": "0", "strong wind": "1", "sharp wind": "2"}

_LOGGER = logging.getLogger(__name__)

class _SessionExpired(Exception):
    """Raised internally when the gateway returns the login page."""

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
        self._base      = f"https://{host}:{port}/index.cgi"
        self._username  = username
        self._password  = password
        self._session: aiohttp.ClientSession | None = None
        self._last_login: float = 0.0
        self._LOGIN_TTL = 20 * 60  # re-login every 20 minutes

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
            _LOGGER.debug("Login POST status=%d", resp.status)
            if "<title>Login</title>" in html:
                raise HitachiGatewayError("Login rejected — check username and password")
            self._last_login = time.monotonic()

    async def _get(self, params: dict, _retry: bool = True, referer: str | None = None) -> str:
        """GET, re-logging in on expired session or timeout."""
        session = await self._get_session()
        if self._username and (time.monotonic() - self._last_login) > self._LOGIN_TTL:
            _LOGGER.debug("Proactive re-login (session TTL exceeded)")
            await self._ensure_logged_in()
        extra_headers = {"Referer": referer} if referer else {}
        try:
            async with session.get(
                self._base, params=params, headers=extra_headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                html = await resp.text()
            if "<title>Login</title>" in html:
                raise _SessionExpired
        except (aiohttp.ClientError, TimeoutError, _SessionExpired):
            if not _retry or not self._username:
                raise HitachiGatewayError("Gateway requires login — provide credentials")
            _LOGGER.debug("GET session expired or timed out — re-logging in")
            await self._ensure_logged_in()
            async with session.get(
                self._base, params=params, headers=extra_headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp2:
                return await resp2.text()
        return html

    async def _post(self, url: str, data: dict) -> None:
        """POST device command, re-logging in on expired session or timeout."""
        session = await self._get_session()
        if self._username and (time.monotonic() - self._last_login) > self._LOGIN_TTL:
            _LOGGER.debug("Proactive re-login before POST (session TTL exceeded)")
            await self._ensure_logged_in()
        try:
            async with session.post(
                url, data=data, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                html = await resp.text()
            _LOGGER.error("POST is_login=%s title=%s", "<title>Login</title>" in html,
                          html[html.find("<title>"):html.find("</title>")+8])
            if "<title>Login</title>" in html:
                raise _SessionExpired
        except (aiohttp.ClientError, TimeoutError, _SessionExpired) as exc:
            _LOGGER.debug("POST failed (%s) — re-logging in", exc)
            await self._ensure_logged_in()
            async with session.post(
                url, data=data, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp2:
                html2 = await resp2.text()
                _LOGGER.error("POST retry is_login=%s title=%s", "<title>Login</title>" in html2,
                              html2[html2.find("<title>"):html2.find("</title>")+8])

    # ------------------------------------------------------------------
    # Device discovery
    # ------------------------------------------------------------------

    async def discover_devices(self) -> list[HitachiDevice]:
        """Discover devices using the device list page, falling back to probing 1–32."""
        names = await self._fetch_device_names()

        # Prefer the IDs we found on the device list page; fall back to full probe
        dev_ids = sorted(names.keys()) if names else list(range(1, 33))
        _LOGGER.debug("Probing dev IDs: %s", dev_ids)

        devices: list[HitachiDevice] = []
        for dev_id in dev_ids:
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
    # State reading  (act=35 returns JSON — select options are JS-populated)
    # ------------------------------------------------------------------

    async def fetch_state(self, device: HitachiDevice) -> None:
        params = {"mod": MOD_AC, "act": ACT_POLL_DEVICE, "dev": device.dev_id}
        try:
            html = await self._get(params)
        except aiohttp.ClientError as exc:
            raise HitachiGatewayError(f"Failed reading device {device.dev_id}: {exc}") from exc
        try:
            data = json.loads(html)
        except json.JSONDecodeError:
            _LOGGER.debug("act=35 returned non-JSON for dev %s; keeping cached state", device.dev_id)
            return
        self._apply_json_state(data, device)

    @staticmethod
    def _apply_json_state(data: dict, device: HitachiDevice) -> None:
        """Update device from the act=35 JSON response."""
        op = data.get("Operation", "").strip().upper()
        device.on_off = "0" if op in ("OFF", "STOP", "") else "1"

        mode_text = data.get("Mode", "").strip().lower()
        device.operation_mode = _MODE_TEXT_TO_CODE.get(mode_text, device.operation_mode)

        fan_text = data.get("Real", "").strip().lower()
        device.fan_speed = _FAN_TEXT_TO_CODE.get(fan_text, device.fan_speed)

        tset = data.get("Tset", "")
        try:
            device.temperature = int(float(tset))
        except (ValueError, TypeError):
            pass

        room = data.get("Ti", "") or data.get("Room", "")
        try:
            device.room_temp = float(room)
        except (ValueError, TypeError):
            device.room_temp = None

        _LOGGER.debug(
            "act=35 dev=%s: on=%s mode=%s temp=%s fan=%s room=%s",
            device.dev_id, device.on_off, device.operation_mode,
            device.temperature, device.fan_speed, device.room_temp,
        )

    @staticmethod
    def _parse_control_page(html: str, device: HitachiDevice, _dump: bool = False) -> None:
        """Used only during discovery to confirm a page is a device control page."""
        if _dump:
            _LOGGER.debug("Control page confirmed for dev %s", device.dev_id)

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
        temp_val = temperature if temperature is not None else device.temperature
        payload = {
            "OnOff":         on_off         if on_off         is not None else device.on_off,
            "OperationMode": operation_mode if operation_mode is not None else device.operation_mode,
            "SetTemp":       f"{temp_val}.0",
            "FanSpeed":      fan_speed      if fan_speed      is not None else device.fan_speed,
        }
        full_payload = {"mod": MOD_AC, "act": ACT_SET_DEVICE, "dev": device.dev_id, **payload}
        _LOGGER.error("set_state payload=%s", full_payload)
        try:
            await self._post(self._base, full_payload)
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
