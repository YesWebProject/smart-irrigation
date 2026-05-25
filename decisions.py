# decisions.py
# Smart Solar Irrigation System
#
# Responsibilities:
#   - Evaluate all conditions and decide whether to water this wake cycle
#   - Calculate how long to water based on Open-Meteo forecast temperature
#
# This file does not talk to hardware or the network directly.
# All inputs are passed in as parameters from main.py.
# All RTC memory access goes through power.py.

import time
import power

from config import (
    WATERING_BASE_DURATION_S,
    WATERING_SUNRISE_OFFSET_M,
    WATERING_WINDOW_M,
    RAIN_SKIP_THRESHOLD_PCT,
    TEMP_SCALING,
    WATER_PUMP_CUTOFF_PCT,
    FROST_THRESHOLD_C,
)

# Maximum pump runtime — hard cap regardless of temperature multiplier
MAX_DURATION_S = 900


# ---------------------------------------------------------------------------
# Temperature scaling
# ---------------------------------------------------------------------------
def get_temp_multiplier(temp_c):
    """
    Return the watering duration multiplier for a given temperature.
    Uses the TEMP_SCALING table from config.py.
    Example: 18°C → 0.8, meaning 80% of base duration.
    """
    for max_temp, multiplier in TEMP_SCALING:
        if temp_c <= max_temp:
            return multiplier
    return 1.0  # fallback — should never be reached with (999, x) in table


def get_watering_duration(temp_c):
    """
    Calculate actual watering duration in seconds.
    Formula: base_duration × temp_multiplier, capped at MAX_DURATION_S.
    Returns 0 if temperature multiplier is 0 (frost protection).
    """
    multiplier = get_temp_multiplier(temp_c)
    duration   = int(WATERING_BASE_DURATION_S * multiplier)
    return min(duration, MAX_DURATION_S)


# ---------------------------------------------------------------------------
# Watering window check
# ---------------------------------------------------------------------------
def _is_watering_time(sunrise_unix):
    """
    Check whether the current UTC time falls inside the watering window.

    Target time = sunrise + WATERING_SUNRISE_OFFSET_M  (offset is -30, so 30 min before sunrise)
    Window      = target ± WATERING_WINDOW_M minutes

    Both the Pico clock (after NTP sync) and the Open-Meteo sunrise time
    are in UTC, so the comparison is straightforward.

    Returns True if inside window, False otherwise.
    """
    now          = time.localtime()
    current_secs = now[3] * 3600 + now[4] * 60 + now[5]

    sr           = time.localtime(sunrise_unix)
    sunrise_secs = sr[3] * 3600 + sr[4] * 60

    target_secs  = sunrise_secs + (WATERING_SUNRISE_OFFSET_M * 60)
    window_secs  = WATERING_WINDOW_M * 60

    in_window = (target_secs - window_secs) <= current_secs <= (target_secs + window_secs)

    target_h = (target_secs % 86400) // 3600
    target_m = (target_secs % 3600) // 60
    print(
        f"Time check: now={now[3]:02d}:{now[4]:02d} UTC  "
        f"target={target_h:02d}:{target_m:02d} UTC  "
        f"window=±{WATERING_WINDOW_M}min  "
        f"in_window={in_window}"
    )
    return in_window


# ---------------------------------------------------------------------------
# Frost alert (uses Open-Meteo forecast — local sensor removed)
# ---------------------------------------------------------------------------
def check_frost_alert(weather, send_alert):
    """
    Send a frost warning if the forecast min temperature falls below threshold.
    weather is the dict from cloud.fetch_weather().
    send_alert is the cloud.send_ntfy_alert callable passed in from main.py.
    Returns True if frost was detected (used to skip watering).
    """
    if weather is None:
        return False
    temp_max = weather.get("temp_max")
    if temp_max is not None and temp_max <= FROST_THRESHOLD_C:
        send_alert(f"Frost warning: forecast max {temp_max}°C — watering skipped.")
        return True
    return False


# ---------------------------------------------------------------------------
# Main decision function
# ---------------------------------------------------------------------------
def check_watering(weather, sensors, battery_v, tier):
    """
    Evaluate all conditions and decide whether to water this wake cycle.

    Parameters:
        weather   — dict from cloud.fetch_weather(), or None if fetch failed
                    keys: rain_pct, sunrise_unix, temp_max
        sensors   — dict from hardware readings
                    keys: water_level_pct, probe_sensor_ok
        battery_v — float, current battery voltage
        tier      — int 1-4, current power tier from power.py

    Returns (should_water, duration_s, skip_reason):
        should_water — True or False
        duration_s   — seconds to run pump (0 if not watering)
        skip_reason  — string logged to InfluxDB:
                       "none" | "already_watered" | "low_battery" | "empty_butt" |
                       "frost" | "no_weather_data" | "rain" | "not_time"

    Side effects:
        - Reads watered-today flag from RTC memory
    """

    # --- Condition 1: Already watered today? ---
    if power.get_watered_today():
        print("Decision: skip — already watered today.")
        return False, 0, "already_watered"

    # --- Condition 2: Battery too low? ---
    if tier >= 3:
        print(f"Decision: skip — battery tier {tier} ({battery_v}V), too low to water.")
        return False, 0, "low_battery"

    # --- Condition 3: Water level too low? (pump dry-run protection) ---
    # If the ultrasonic sensor gave no reading (None), skip this check and rely
    # on the probe sensor below — a failed sensor should not block watering.
    water_pct = sensors.get("water_level_pct")
    if water_pct is not None and water_pct <= WATER_PUMP_CUTOFF_PCT:
        print(f"Decision: skip — water level {water_pct:.1f}% below cutoff {WATER_PUMP_CUTOFF_PCT}%.")
        return False, 0, "empty_butt"

    # --- Condition 3b: Probe sensor dry? (backup hardware protection) ---
    # probe_sensor_ok is None if the sensor couldn't be read — treated as safe
    # so a faulty sensor doesn't block watering permanently.
    probe_ok = sensors.get("probe_sensor_ok")
    if probe_ok is False:
        print("Decision: skip — probe sensor reads dry.")
        return False, 0, "empty_butt"

    # --- Condition 4: Weather data available? ---
    if weather is None:
        print("Decision: skip — no weather data available.")
        return False, 0, "no_weather_data"

    # --- Condition 5: Frost forecast? ---
    temp_max = weather.get("temp_max")
    if temp_max is not None and temp_max <= FROST_THRESHOLD_C:
        print(f"Decision: skip — frost forecast ({temp_max}°C).")
        return False, 0, "frost"

    # --- Condition 6: Rain forecast? ---
    rain_pct = weather.get("rain_pct", 0)
    if rain_pct >= RAIN_SKIP_THRESHOLD_PCT:
        print(f"Decision: skip — rain forecast {rain_pct}% (threshold {RAIN_SKIP_THRESHOLD_PCT}%).")
        return False, 0, "rain"

    # --- Condition 7: Is it time? ---
    sunrise_unix = weather.get("sunrise_unix")
    if sunrise_unix is None:
        print("Decision: skip — no sunrise time in weather data.")
        return False, 0, "no_weather_data"

    if not _is_watering_time(sunrise_unix):
        print("Decision: not watering — outside time window.")
        return False, 0, "not_time"

    # --- All conditions passed — calculate duration ---
    duration = get_watering_duration(temp_max if temp_max is not None else 18.0)

    if duration == 0:
        # temp_multiplier was 0.0 — catches edge case where temp just above FROST_THRESHOLD_C
        # but below the first scaling tier
        print(f"Decision: skip — temperature multiplier is 0 at {temp_max}°C.")
        return False, 0, "frost"

    print(
        f"Decision: WATER for {duration}s "
        f"(temp={temp_max}°C, rain={rain_pct}%, water={water_pct:.1f}%)"
    )
    return True, duration, "none"
