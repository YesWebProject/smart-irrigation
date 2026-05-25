# config.py
# Smart Solar Irrigation System
# Hardcoded safe defaults — overridden at runtime by values fetched from GitHub Gist.
# If Gist is unreachable, these values keep the system running safely.
# Do not store secrets here — WiFi credentials and API keys go in secrets.py

CONFIG_VERSION = 1

# ---------------------------------------------------------------------------
# Firmware version — increment this before pushing new code to GitHub.
# The OTA system compares this against the version in manifest.json.
# ---------------------------------------------------------------------------
CODE_VERSION = 1

# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------
LATITUDE  = 50.569903   # TQ12 area, Devon
LONGITUDE = -3.651687

# ---------------------------------------------------------------------------
# WiFi — populated from secrets.py, not here
# ---------------------------------------------------------------------------
WIFI_TIMEOUT_S = 20       # seconds before giving up on connection

# ---------------------------------------------------------------------------
# Pi 1 server — not used with cloud stack, kept for future option
# ---------------------------------------------------------------------------
PI_HOST         = "http://192.168.1.100"
PI_CONFIG_PORT  = 8080
PI_INFLUX_PORT  = 8086
PI_NTFY_PORT    = 80

PI_CONFIG_URL   = f"{PI_HOST}:{PI_CONFIG_PORT}/config.json"
PI_INFLUX_URL   = f"{PI_HOST}:{PI_INFLUX_PORT}/write?db=irrigation"
PI_NTFY_URL     = f"{PI_HOST}:{PI_NTFY_PORT}/irrigation-alerts"
PI_CMD_URL      = f"{PI_HOST}:{PI_NTFY_PORT}/irrigation-commands"

INFLUX_TIMEOUT_S = 5
NTFY_TIMEOUT_S   = 5
CONFIG_TIMEOUT_S = 5

# ---------------------------------------------------------------------------
# Watering
# ---------------------------------------------------------------------------
WATERING_BASE_DURATION_S  = 600      # 10 minutes
WATERING_SUNRISE_OFFSET_M = -30      # minutes before sunrise (negative = before)
WATERING_WINDOW_M         = 10       # ± window around target time to trigger
RAIN_SKIP_THRESHOLD_PCT   = 60       # skip if rain probability >= this

# Temperature scaling — multiplier applied to base duration
# List of (max_temp_c, multiplier) tuples, evaluated top to bottom.
# Last entry is the catch-all for anything above the previous threshold.
TEMP_SCALING = [
    (5,  0.0),   # at or below 5°C — no watering (frost risk)
    (12, 0.5),
    (18, 0.8),
    (24, 1.0),
    (999, 1.3),  # above 24°C
]

# ---------------------------------------------------------------------------
# Water level
# ---------------------------------------------------------------------------
# Sensor mounted in butt lid, measuring downward to water surface.
# Calibrate these two values to your actual butt depth.
SENSOR_DISTANCE_EMPTY_MM = 800   # confirmed — butt empty
SENSOR_DISTANCE_FULL_MM  = 100   # confirmed — butt full
WATER_LOW_ALERT_PCT      = 15    # send ntfy alert below this level
WATER_PUMP_CUTOFF_PCT    = 5     # disable pump below this level (dry-run protection)

# A02YYUW sensor constants
UART_BAUD        = 9600
UART_TX_PIN      = 0     # GP0 — not used by sensor but required by UART init
UART_RX_PIN      = 1     # GP1 — sensor TX → Pico RX
UART_TIMEOUT_MS  = 500   # ms to wait for a valid reading
SENSOR_BLIND_MM  = 30    # 3cm blind spot — readings below this are invalid

# ---------------------------------------------------------------------------
# Battery voltage thresholds
# ---------------------------------------------------------------------------
# Single 18650 cell: 4.2V fully charged, 3.0V minimum (do not discharge below)
# Voltage divider: equal 10kΩ/10kΩ resistors (1:1 ratio)
# At 4.2V fully charged: 4.2 × 0.5 = 2.1V at ADC — safely under 3.3V
# At 3.0V discharged:    3.0 × 0.5 = 1.5V at ADC — good range across battery life
ADC_PIN            = 26
VDIV_RATIO         = 10000 / (10000 + 10000)   # 0.5 — equal 10k/10k divider
ADC_REF_V          = 3.3
ADC_RESOLUTION     = 65535   # 16-bit on RP2350

BATTERY_TIER1_V    = 3.9     # > this: full power, 30-min sleep  (~70%+ charge)
BATTERY_TIER2_V    = 3.6     # > this: 1-hour sleep              (~40%+ charge)
BATTERY_TIER3_V    = 3.3     # > this: 4-hour sleep, ignore manual commands (~10%+ charge)
                              # below TIER3: critical hibernation, 24-hour sleep

BATTERY_ALERT_V        = 3.3     # send critical low alert below this
BATTERY_RECOVERY_V     = 3.6     # send recovery alert when rising above this

# ---------------------------------------------------------------------------
# Sleep intervals (seconds)
# ---------------------------------------------------------------------------
SLEEP_TIER1_S = 30 * 60      # 30 minutes
SLEEP_TIER2_S = 60 * 60      # 1 hour
SLEEP_TIER3_S = 4 * 60 * 60  # 4 hours
SLEEP_TIER4_S = 24 * 60 * 60 # 24 hours (critical hibernation)

# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
# Frost threshold used with Open-Meteo forecast temperature
FROST_THRESHOLD_C    = 2.0    # alert and skip watering below this

# ---------------------------------------------------------------------------
# BME280 — REMOVED from this device
# ---------------------------------------------------------------------------
# Local temperature/humidity/pressure are now handled by Open-Meteo forecast.
# A BME280 inside a sealed enclosure in direct sun heats to 30–40°C above
# ambient, making local readings unusable for plant care decisions.

# ---------------------------------------------------------------------------
# MOSFET pump
# ---------------------------------------------------------------------------
PUMP_PIN = 15   # GP15

# How often to check water level sensors during pumping (seconds).
PUMP_CHECK_INTERVAL_S = 5

# ---------------------------------------------------------------------------
# Probe water sensor (metal prongs from original AnseTo kit)
# ---------------------------------------------------------------------------
# One probe to GP14, the other probe to GND.
# No external resistor needed — uses Pico internal pull-up.
# Water present = pin reads LOW. Dry = pin reads HIGH.
# Position the probes at the same depth as your WATER_PUMP_CUTOFF_PCT level.
PROBE_PIN = 14  # GP14 — pin 19

# ---------------------------------------------------------------------------
# Post-watering monitoring
# ---------------------------------------------------------------------------
# After watering, the system wakes more frequently for a short window to
# capture the syphon refill effect on Grafana graphs.
POST_WATER_CYCLES  = 6        # number of fast wake cycles after watering
POST_WATER_SLEEP_S = 5 * 60   # seconds between each fast cycle (5 minutes)
                               # 6 cycles × 5 min = 30 minutes of fast monitoring

# ---------------------------------------------------------------------------
# OTA updates
# ---------------------------------------------------------------------------
# Set OTA_ENABLED = True and fill in OTA_MANIFEST_URL to enable automatic
# over-the-air code updates.
#
# Setup steps:
#   1. Create a public GitHub repository
#   2. Push all your .py files and manifest.json to it
#   3. Set OTA_MANIFEST_URL to the raw URL of your manifest.json
#      e.g. "https://raw.githubusercontent.com/yourname/repo/main/manifest.json"
#   4. When you want to push an update: edit code, increment CODE_VERSION above,
#      update "version" in manifest.json to the same number, push to GitHub.
#      The Pico will pick it up the next morning during new-day detection.
#
# config.py and secrets.py are NEVER overwritten by OTA.
OTA_ENABLED      = False   # set True once your GitHub repo is set up
OTA_MANIFEST_URL = ""      # paste your raw manifest.json URL here

# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------
OPENMETEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LATITUDE}"
    f"&longitude={LONGITUDE}"
    f"&daily=sunrise,precipitation_probability_max,temperature_2m_max"
    f"&timezone=UTC"
    f"&forecast_days=1"
)
