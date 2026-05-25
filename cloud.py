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
    OPENMETEO_URL,
    INFLUX_TIMEOUT_S,
    NTFY_TIMEOUT_S,
    CONFIG_TIMEOUT_S,
)


# ---------------------------------------------------------------------------
# Time sync
# ---------------------------------------------------------------------------
def sync_time():
    """
    Sync the Pico's internal clock from NTP.
    Call this once after WiFi connects, before anything that needs timestamps.
    Returns True on success, False on failure.
    """
    try:
        ntptime.settime()
        t = time.localtime()
        print(f"Time synced: {t[0]}-{t[1]:02d}-{t[2]:02d} {t[3]:02d}:{t[4]:02d} UTC")
        return True
    except Exception as e:
        print(f"NTP sync failed: {e} — timestamps may be inaccurate")
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
        r     = urequests.get(url)
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
def _build_line_protocol(fields, measurement="irrigation", location="garden"):
    """
    Build an InfluxDB line protocol string from a dict of fields.
    Handles floats, ints, strings, and booleans correctly.

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
    return f"{measurement},location={location} {field_str}"


def log_to_influx(fields):
    """
    Write a data point to InfluxDB Cloud using line protocol.

    Call with a dict of field names and values. All fields are optional —
    include whatever is available on this wake cycle.

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
        line = _build_line_protocol(fields)
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
        r = urequests.post(url, data=line, headers=headers)
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
            "rain_pct":     60,          # max rain probability today (%)
            "sunrise_unix": 1716000000,  # sunrise as unix timestamp
            "temp_max":     18.4,        # max temperature today (°C)
        }

    Returns None on failure — decisions.py will skip watering if None.
    """
    try:
        r    = urequests.get(OPENMETEO_URL)
        data = json.loads(r.text)
        r.close()

        daily      = data["daily"]
        rain_pct   = daily["precipitation_probability_max"][0]
        temp_max   = daily["temperature_2m_max"][0]
        sunrise_str = daily["sunrise"][0]   # e.g. "2024-05-15T05:23"

        # Parse sunrise string to unix timestamp
        # MicroPython time.mktime takes (year, month, day, hour, min, sec, weekday, yearday)
        date_part, time_part = sunrise_str.split("T")
        year, month, day     = [int(x) for x in date_part.split("-")]
        hour, minute         = [int(x) for x in time_part.split(":")]
        sunrise_unix = time.mktime((year, month, day, hour, minute, 0, 0, 0))

        print(f"Weather: rain={rain_pct}%  sunrise={sunrise_str}  max_temp={temp_max}°C")
        return {
            "rain_pct":     rain_pct,
            "sunrise_unix": sunrise_unix,
            "temp_max":     temp_max,
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
          "watering": { "base_duration_s": 600, "sunrise_offset_min": -30, ... },
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
    _set("WATERING_BASE_DURATION_S",  w.get("base_duration_s"))
    _set("WATERING_SUNRISE_OFFSET_M", w.get("sunrise_offset_min"))
    _set("WATERING_WINDOW_M",         w.get("watering_window_min"))
    _set("RAIN_SKIP_THRESHOLD_PCT",   w.get("rain_probability_threshold_pct"))

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
        r      = urequests.get(secrets.GIST_URL)
        config = json.loads(r.text)
        r.close()
        version = config.get("config_version", "?")
        print(f"Remote config: fetched OK (version {version})")
        return config

    except Exception as e:
        print(f"Remote config fetch failed: {e} — using config.py defaults.")
        return None
