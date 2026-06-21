# Hitachi HLink Aircloud Pro

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Local-only Home Assistant integration for the **Hitachi HC-IOTGW** (Aircloud Pro) gateway.
Controls all Hitachi H-Link indoor units directly over your LAN — no cloud, no subscription.

## Confirmed API (reverse-engineered from HC-IOTGW web UI)

| URL | Purpose |
|-----|---------|
| `GET  /index.cgi?mod=1&act=11` | Device list — names and IDs |
| `GET  /index.cgi?mod=3&act=31&dev=N` | Read device N state |
| `POST /index.cgi` body `mod=3&act=33&dev=N&...` | Write device N state |

### Field values

| Field | Value | Meaning |
|-------|-------|---------|
| `OnOff` | `1` | ON |
| `OnOff` | `0` | OFF |
| `OperationMode` | `1` | Fan only |
| `OperationMode` | `2` | Heat |
| `OperationMode` | `4` | Cool |
| `OperationMode` | `64` | Dry |
| `FanSpeed` | `0` | Weak Wind (Low) |
| `FanSpeed` | `1` | Strong Wind (High) |
| `FanSpeed` | `2` | Sharp Wind (Medium) |

## Installation via HACS

1. In HACS → **Integrations** → ⋮ menu → **Custom repositories**
2. Add URL: `https://github.com/sofsaenz/hitachi_hlink_ha` — Category: **Integration**
3. Install **Hitachi HLink Aircloud Pro** and restart Home Assistant
4. **Settings → Devices & Services → Add Integration** → search **Hitachi HLink**
5. Enter your gateway IP (default `192.168.xxx.xxx`, port `443`)

## Features

- Auto-discovers all indoor units from the gateway device list (real room names)
- On / Off
- Modes: Auto / Fan Only / Heat / Cool / Dry
- Target temperature: 16–30 °C
- Fan speed: Low (Weak) / Medium (Sharp) / High (Strong)
- Current room temperature (read from gateway)
- Polls every 30 seconds — fully local, no internet required
