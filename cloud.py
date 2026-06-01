# cloud.py
# Smart Solar Irrigation System
#
# Responsibilities:
#   - Sync time from NTP after WiFi connects
#   - Send alerts to phone via ntfy
#   - Check for incoming commands from phone via ntfy
#   - Log sensor data to InfluxDB Cloud
#   - Fetch today's weather forecast from Open-Meteo
#   - Fetch remote config from GitHub Gist
#
# Every function is wrapped in try/except.
# A failure in any one function is logged to the console but does not
# crash the wake cycle — the Pico carries on with whatever data it has.

import urequests
import time
import json
import ntptime

import secrets
from config import (
    INFLUX_TIMEOUT_S,
    NTFY_TIMEOUT_S,
    CONFIG_TIMEOUT_S,
)

# Latitude/longitude are read at call time in fetch_weather() via _cfg so the
# Open-Meteo URL is built fresh each cycle and rain_sum can be requested without
# a config.py change.
import config as _cfg


# ---------------------------------------------------------------------------
# Time sync
# ---------------------------------------------------------------------------
def sync_time(retries=3, delay_s=0.5):
    """
    Sync the Pico's internal clock from NTP.
    Call this once after WiFi connects, before anything that needs timestamps.
    Returns True on success, False on failure.
    """
    for attempt in range(1, retries + 1):
        try:
            ntptime.settime()
            t = time.localtime()
            print(f"Time synced: {t[0]}-{t[1]:02d}-{t[2]:02d} {t[3]:02d}:{t[4]:02d} UTC")
            return True
        except Exception as e:
            if attempt < retries:
                print(f"NTP sync attempt {attempt} failed — retrying")
                time.sleep(delay_s)
            else:
                print(f"NTP sync failed after {retries} attempts: {e} — timestamps may be inaccurate")
    return False


# ---------------------------------------------------------------------------
# ntfy — outbound alerts
# ---------------------------------------------------------------------------
def send_ntfy_alert(message):
    """
    POST an alert message to the phone via ntfy.
    Used for frost warnings, critical battery, water level, and recovery alerts.

    This function is also passed as a callable to power.run() so that
    power.py can fire battery alerts without importing cloud.py directly.
    """
    try:
        r = urequests.post(
            f"https://ntfy.sh/{secrets.NTFY_ALERTS_TOPIC}",
            data=message,
            headers={"Content-Type": "text/plain"},
            timeout=NTFY_TIMEOUT_S,
        )
        r.close()
        print(f"Alert sent: {message}")
    except Exception as e:
        print(f"ntfy alert failed: {e}")


# ---------------------------------------------------------------------------
# ntfy — inbound commands
# ---------------------------------------------------------------------------
def check_ntfy_commands(sleep_seconds):
    """
    Poll the ntfy commands topic for any messages sent since the last wake.
    Uses the previous sleep duration to set the look-back window.
    Returns a list of command strings, e.g. ["water_now"] or [].

    Recognised commands (handled in main.py):
        water_now  — run pump immediately for configured duration
        snooze     — skip the next scheduled watering
    """
    try:
        since = f"{int(sleep_seconds)}s"
        url   = f"https://ntfy.sh/{secrets.NTFY_COMMANDS_TOPIC}/json?poll=1&since={since}"
        r     = urequests.get(url, timeout=NTFY_TIMEOUT_S)
        text  = r.text.strip()
        r.close()

        commands = []
        if text:
            for line in text.split("\n"):
                line = line.strip()
                if line:
                    try:
                        msg = json.loads(line)
                        command = msg.get("message", "").strip().lower()
                        if command:
                            commands.append(command)
                    except Exception:
                        pass

        if commands:
            print(f"Commands received: {commands}")
        else:
            print("No commands received.")
        return commands

    except Exception as e:
        print(f"ntfy command check failed: {e}")
        return []


# ---------------------------------------------------------------------------
# InfluxDB Cloud — data logging
# ---------------------------------------------------------------------------
def _build_line_protocol(fields, measurement="irrigation", location="garden", timestamp_ns=None):
    """
    Build an InfluxDB line protocol string from a dict of fields.
    Handles floats, ints, strings, and booleans correctly.

    If timestamp_ns is given it is appended (nanoseconds, Unix epoch) so the point
    lands at a specific time; otherwise InfluxDB assigns the server's current time.

    Example output:
        irrigation,location=garden temp_c=18.4,pump_runtime_s=480i,skip_reason="none",frost_alert=false
    """
    field_parts = []
    for key, value in fields.items():
        if isinstance(value, bool):
            # Must check bool before int — bool is a subclass of int in Python
            field_parts.append(f"{key}={str(value).lower()}")
        elif isinstance(value, int):
            field_parts.append(f"{key}={value}i")
        elif isinstance(value, float):
            field_parts.append(f"{key}={value}")
        elif isinstance(value, str):
            field_parts.append(f'{key}="{value}"')

    field_str = ",".join(field_parts)
    line = f"{measurement},location={location} {field_str}"
    if timestamp_ns is not None:
        line += f" {int(timestamp_ns)}"
    return line


def log_to_influx(fields, timestamp_ns=None):
    """
    Write a data point to InfluxDB Cloud using line protocol.

    Call with a dict of field names and values. All fields are optional —
    include whatever is available on this wake cycle.

    timestamp_ns (optional) places the point at a specific Unix time in
    nanoseconds — used to log yesterday's actual weather at yesterday's slot.
    When omitted, InfluxDB assigns the current server time (normal behaviour).

    Example:
        cloud.log_to_influx({
            "water_level_pct":    84.0,
            "battery_v":          3.85,
            "pump_runtime_s":     480,
            "skip_reason":        "none",
            "wifi_rssi":          -62,
            "forecast_temp_max_c": 17.5,
            "forecast_rain_pct":   20.0,
            "probe_sensor_ok":    True,
        })
    """
    try:
        line = _build_line_protocol(fields, timestamp_ns=timestamp_ns)
        url  = (
            f"{secrets.INFLUX_URL}/api/v2/write"
            f"?org={secrets.INFLUX_ORG}"
            f"&bucket={secrets.INFLUX_BUCKET}"
            f"&precision=ns"
        )
        headers = {
            "Authorization": f"Token {secrets.INFLUX_TOKEN}",
            "Content-Type":  "text/plain; charset=utf-8",
        }
        r = urequests.post(url, data=line, headers=headers, timeout=INFLUX_TIMEOUT_S)
        r.close()
        print(f"InfluxDB: logged {list(fields.keys())}")

    except Exception as e:
        print(f"InfluxDB failed: {e}")


# ---------------------------------------------------------------------------
# Open-Meteo — weather forecast
# ---------------------------------------------------------------------------
def fetch_weather():
    """
    Fetch today's weather forecast from Open-Meteo (no API key required).
    Uses the latitude and longitude from config.py.

    Returns a dict:
        {
            "rain_pct":         60,          # max rain probability today (%)
            "forecast_rain_mm": 4.2,         # total predicted rainfall today (mm)
            "sunrise_unix":     1716000000,  # sunrise as unix timestamp
            "temp_max":         18.4,        # max temperature today (°C)
        }

    Returns None on failure — decisions.py will skip watering if None.
    """
    try:
        # Build the URL here (rather than from config.OPENMETEO_URL) so rain_sum
        # is always requested and lat/lon pick up any Gist override — no config.py change needed.
        # past_days=1 + forecast_days=1 returns two daily entries: [yesterday, today].
        # Today is the LAST entry; yesterday (the entry before it) carries the
        # actuals we log back at yesterday's timestamp for forecast-vs-actual.
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={getattr(_cfg, 'LATITUDE', 50.569903)}"
            f"&longitude={getattr(_cfg, 'LONGITUDE', -3.651687)}"
            f"&daily=sunrise,precipitation_probability_max,temperature_2m_max,temperature_2m_min,rain_sum"
            f"&timezone=UTC"
            f"&past_days=1"
            f"&forecast_days=1"
        )
        r    = urequests.get(url, timeout=5)
        data = json.loads(r.text)
        r.close()

        daily = data["daily"]
        ti = len(daily["temperature_2m_max"]) - 1   # today = last daily entry
        yi = ti - 1                                  # yesterday = the entry before it

        rain_pct   = daily["precipitation_probability_max"][ti]
        temp_max   = daily["temperature_2m_max"][ti]
        temp_min   = daily["temperature_2m_min"][ti]
        sunrise_str = daily["sunrise"][ti]   # e.g. "2024-05-15T05:23"

        # Total predicted rainfall (mm) for today — default to 0.0 if missing or null
        rain_sum = daily.get("rain_sum") or []
        forecast_rain_mm = rain_sum[ti] if ti < len(rain_sum) and rain_sum[ti] is not None else 0.0

        # Yesterday's actuals (index yi) — logged at yesterday's timestamp so they
        # land beside the forecast that was recorded yesterday. None if unavailable.
        actual_temp_max_c = None
        actual_rain_mm    = None
        if yi >= 0:
            tmax = daily.get("temperature_2m_max") or []
            if yi < len(tmax) and tmax[yi] is not None:
                actual_temp_max_c = tmax[yi]
            if yi < len(rain_sum) and rain_sum[yi] is not None:
                actual_rain_mm = rain_sum[yi]

        # Parse sunrise string to unix timestamp
        # MicroPython time.mktime takes (year, month, day, hour, min, sec, weekday, yearday)
        date_part, time_part = sunrise_str.split("T")
        year, month, day     = [int(x) for x in date_part.split("-")]
        hour, minute         = [int(x) for x in time_part.split(":")]
        sunrise_unix = time.mktime((year, month, day, hour, minute, 0, 0, 0))

        print(f"Weather: rain={rain_pct}% / {forecast_rain_mm}mm  sunrise={sunrise_str}  "
              f"max={temp_max}°C  min={temp_min}°C  "
              f"(yesterday actual: max={actual_temp_max_c}°C rain={actual_rain_mm}mm)")
        return {
            "rain_pct":          rain_pct,
            "forecast_rain_mm":  forecast_rain_mm,
            "sunrise_unix":      sunrise_unix,
            "temp_max":          temp_max,
            "temp_min":          temp_min,
            "actual_temp_max_c": actual_temp_max_c,
            "actual_rain_mm":    actual_rain_mm,
        }

    except Exception as e:
        print(f"Weather fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Remote config — apply
# ---------------------------------------------------------------------------
def apply_remote_config(remote_cfg):
    """
    Apply values from the fetched remote config dict to the config module.

    Modifies config module attributes in-place so the changes take effect
    immediately for the rest of this wake cycle. Python modules are
    singletons so any file that imports config will see the new values.

    Returns the number of values that were overridden.
    Falls back silently on any parsing error — a bad Gist value won't crash
    the system, it'll just keep the config.py default.

    Gist JSON structure expected:
        {
          "config_version": 1,
          "watering": { "base_duration_s": 600, "sunrise_offset_min": -30,
                        "pump_min_runtime_for_check_s": 120, "pump_min_drop_mm": 30, ... },
          "water_level": { "sensor_distance_empty_mm": 800, ... },
          "battery":  { "tier1_v": 3.9, ... },
          "alerts":   { "frost_threshold_c": 2.0, ... },
          "sleep":    { "tier1_interval_min": 30, ... }
        }
    """
    if remote_cfg is None:
        return 0

    import config as cfg
    changed = 0

    def _set(attr, value):
        nonlocal changed
        if value is not None:
            try:
                setattr(cfg, attr, value)
                changed += 1
            except Exception as e:
                print(f"Remote config: could not set {attr}: {e}")

    # Version check
    remote_v = remote_cfg.get("config_version", 1)
    if remote_v != cfg.CONFIG_VERSION:
        print(f"Remote config: version mismatch (remote={remote_v}, local={cfg.CONFIG_VERSION})")

    # Watering settings
    w = remote_cfg.get("watering", {})
    _set("WATERING_BASE_DURATION_S",       w.get("base_duration_s"))
    _set("WATERING_SUNRISE_OFFSET_M",      w.get("sunrise_offset_min"))
    _set("WATERING_WINDOW_M",              w.get("watering_window_min"))
    _set("RAIN_SKIP_THRESHOLD_PCT",        w.get("rain_probability_threshold_pct"))
    _set("RAIN_SKIP_AMOUNT_MM",            w.get("rain_amount_threshold_mm", 5.0))
    _set("PUMP_MIN_RUNTIME_FOR_CHECK_S",   w.get("pump_min_runtime_for_check_s"))
    _set("PUMP_MIN_DROP_MM",               w.get("pump_min_drop_mm"))

    # Temperature scaling (list of {below_c/above_c, multiplier} dicts → list of tuples)
    ts = w.get("temp_scaling")
    if ts:
        try:
            new_scaling = []
            for entry in ts:
                if "below_c" in entry:
                    new_scaling.append((entry["below_c"], entry["multiplier"]))
                else:
                    new_scaling.append((999, entry["multiplier"]))
            if new_scaling:
                cfg.TEMP_SCALING = new_scaling
                changed += 1
        except Exception as e:
            print(f"Remote config: temp_scaling parse error: {e}")

    # Water level settings
    wl = remote_cfg.get("water_level", {})
    _set("SENSOR_DISTANCE_EMPTY_MM", wl.get("sensor_distance_empty_mm"))
    _set("SENSOR_DISTANCE_FULL_MM",  wl.get("sensor_distance_full_mm"))
    _set("SENSOR_DISTANCE_PUMP_MM",  wl.get("sensor_distance_pump_mm"))
    _set("WATER_LOW_ALERT_PCT",      wl.get("low_level_alert_pct"))
    _set("WATER_PUMP_CUTOFF_PCT",    wl.get("pump_cutoff_pct"))

    # Battery thresholds
    bat = remote_cfg.get("battery", {})
    _set("BATTERY_TIER1_V",    bat.get("tier1_v"))
    _set("BATTERY_TIER2_V",    bat.get("tier2_v"))
    _set("BATTERY_TIER3_V",    bat.get("tier3_v"))
    _set("BATTERY_ALERT_V",              bat.get("alert_critical_v"))
    _set("BATTERY_RECOVERY_V",           bat.get("alert_recovery_v"))
    _set("BATTERY_EMERGENCY_CUTOFF_V",   bat.get("emergency_cutoff_v"))

    # Alert thresholds
    al = remote_cfg.get("alerts", {})
    _set("FROST_THRESHOLD_C", al.get("frost_threshold_c"))

    # Sensors — probe override. Default True so a Gist without a sensors section
    # (or an unreachable Gist) keeps the probe dry-run protection enabled.
    s = remote_cfg.get("sensors", {})
    _set("PROBE_SENSOR_ENABLED", s.get("probe_sensor_enabled", True))

    # Sleep intervals (Gist stores minutes, config stores seconds)
    sl = remote_cfg.get("sleep", {})
    if sl.get("tier1_interval_min"):
        cfg.SLEEP_TIER1_S = int(sl["tier1_interval_min"]) * 60
        changed += 1
    if sl.get("tier2_interval_min"):
        cfg.SLEEP_TIER2_S = int(sl["tier2_interval_min"]) * 60
        changed += 1
    if sl.get("tier3_interval_min"):
        cfg.SLEEP_TIER3_S = int(sl["tier3_interval_min"]) * 60
        changed += 1
    if sl.get("tier4_interval_min"):
        cfg.SLEEP_TIER4_S = int(sl["tier4_interval_min"]) * 60
        changed += 1

    print(f"Remote config: {changed} values overridden from Gist")
    return changed


# ---------------------------------------------------------------------------
# GitHub Gist — fetch raw config JSON
# ---------------------------------------------------------------------------
def fetch_remote_config():
    """
    Fetch the remote config JSON from a GitHub Gist.
    The Pico checks this on every wake and uses it to override config.py defaults.
    This allows settings to be changed from a phone browser without touching the Pico.

    Returns a dict of config values, or None if unavailable.
    main.py falls back to config.py hardcoded defaults when None is returned.

    Setup:
        1. Create a GitHub account if you don't have one
        2. Go to gist.github.com and create a new public gist
        3. Paste the config JSON (see config/config.json in the project repo)
        4. Click the Raw button and copy the URL
        5. Add it to secrets.py as GIST_URL

    Leave GIST_URL as an empty string ("") to always use config.py defaults.
    """
    if not getattr(secrets, "GIST_URL", ""):
        print("Remote config: GIST_URL not set — using config.py defaults.")
        return None

    try:
        r      = urequests.get(secrets.GIST_URL, timeout=CONFIG_TIMEOUT_S)
        config = json.loads(r.text)
        r.close()
        version = config.get("config_version", "?")
        print(f"Remote config: fetched OK (version {version})")
        return config

    except Exception as e:
        print(f"Remote config fetch failed: {e} — using config.py defaults.")
        return None
