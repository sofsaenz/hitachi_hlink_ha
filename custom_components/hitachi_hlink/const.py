DOMAIN = "hitachi_hlink"
DEFAULT_PORT = 443
DEFAULT_SCAN_INTERVAL = 30  # seconds

# CGI module / action IDs
MOD_AC = 3
MOD_DEVICE_LIST = 1
ACT_DEVICE_LIST = 11   # GET  mod=1&act=11          → device list page (names + ids)
ACT_GET_DEVICE  = 31   # GET mod=3&act=31&dev=N              → read device control page
ACT_SET_DEVICE  = 31   # GET mod=3&act=31&dev=N&OnOff=...   → same URL, gateway saves on param presence

# OnOff field values
ONOFF_ON  = "1"
ONOFF_OFF = "0"

# OperationMode field values  (confirmed from browser recordings)
MODE_FAN  = "1"   # Fan only
MODE_HEAT = "2"   # Heat
MODE_COOL = "4"   # Cool
MODE_DRY  = "64"  # Dry
# Auto not observed in recordings — included speculatively; remove if unit doesn't support it
MODE_AUTO = "0"

# FanSpeed field values  (confirmed from browser recordings)
FAN_WEAK   = "0"   # Weak Wind  → maps to HA FAN_LOW
FAN_STRONG = "1"   # Strong Wind → maps to HA FAN_HIGH
FAN_SHARP  = "2"   # Sharp Wind  → maps to HA FAN_MEDIUM

# Temperature range (°C)
TEMP_MIN  = 16
TEMP_MAX  = 30
TEMP_STEP = 1
