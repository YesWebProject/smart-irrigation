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

# Watering constants are all Gist-overridable, so they are read at call time
# via getattr(_cfg, ...) inside each function. A plain `from config import X`
# would bind the import-time value and ignore overrides applied later this
# wake cycle by cloud.apply_remote_config().
import config as _cfg

# Maximum pump runtime — hard cap regardless of temperature multiplier.
# Raised to 1800s (30 min) so the Gist temp_scaling multipliers above 1.0 are
# not flattened: with base_duration_s=900, the table's max 2.0× = 1800s.
# (hardware._PUMP_MAX_S is raised to match so the hardware layer doesn't re-clamp.)
MAX_DURATION_S = 1800


# ---------------------------------------------------------------------------
# Temperature scaling
# ---------------------------------------------------------------------------
def get_temp_multiplier(temp_c):
    """
    Return the watering duration multiplier for a given temperature.
    Uses the TEMP_SCALING table from config.py.
    Example: 18°C → 0.8, meaning 80% of base duration.
    """
    temp_scaling = getattr(_cfg, "TEMP_SCALING", [(5, 0.0), (12, 0.5), (18, 0.8), (24, 1.0), (999, 1.3)])
    for max_temp, multiplier in temp_scaling:
        if temp_c <= max_temp:
            return multiplier
    return 1.0  # fallback — should never be reached with (999, x) in table


def get_watering_duration(temp_c):
    """
    Calculate actual watering duration in seconds.
    Formula: base_duration × temp_multiplier, capped at MAX_DURATION_S.
    Returns 0 if temperature multiplier is 0 (frost protection).

    Verification (current Gist scaling table + base_duration_s=900):
        19.7°C falls in the 18 < t <= 20 bucket → multiplier 1.4
        duration = int(900 × 1.4) = 1260s, under the 1800s cap → 1260s.
    """
    base_duration = getattr(_cfg, "WATERING_BASE_DURATION_S", 600)
    multiplier = get_temp_multiplier(temp_c)
    duration   = int(base_duration * multiplier)
    return min(duration, MAX_DURATION_S)


# ---------------------------------------------------------------------------
# Pump dry-run cutoff
# ---------------------------------------------------------------------------
def pump_cutoff_pct():
    """
    Effective dry-run safety cutoff as a water-level percentage.

    If SENSOR_DISTANCE_PUMP_MM is set (> 0) it is the sensor-to-water distance
    at the pump intake — the safety limit, measured directly. Convert it to a
    percentage using the same empty/full calibration the live reading uses.
    Comparing live_pct <= cutoff_pct is then algebraically identical to
    comparing raw_distance >= pump_level_mm (the empty/full span cancels), so
    the existing percentage-based safety checks work unchanged.

    Falls back to WATER_PUMP_CUTOFF_PCT when the pump distance is unset (0).
    """
    pump_mm = getattr(_cfg, "SENSOR_DISTANCE_PUMP_MM", 0)
    if pump_mm and pump_mm > 0:
        empty_mm = getattr(_cfg, "SENSOR_DISTANCE_EMPTY_MM", 800)
        full_mm  = getattr(_cfg, "SENSOR_DISTANCE_FULL_MM", 100)
        if empty_mm != full_mm:
            pct = (empty_mm - pump_mm) / (empty_mm - full_mm) * 100.0
            return max(0.0, min(100.0, pct))
    return getattr(_cfg, "WATER_PUMP_CUTOFF_PCT", 5)


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
    sunrise_offset_m = getattr(_cfg, "WATERING_SUNRISE_OFFSET_M", -30)
    window_m         = getattr(_cfg, "WATERING_WINDOW_M", 10)

    now          = time.localtime()
    current_secs = now[3] * 3600 + now[4] * 60 + now[5]

    sr           = time.localtime(sunrise_unix)
    sunrise_secs = sr[3] * 3600 + sr[4] * 60 + sr[5]

    target_secs  = sunrise_secs + (sunrise_offset_m * 60)
    window_secs  = window_m * 60

    in_window = (target_secs - window_secs) <= current_secs <= (target_secs + window_secs)

    target_h = (target_secs % 86400) // 3600
    target_m = (target_secs % 3600) // 60
    print(
        f"Time check: now={now[3]:02d}:{now[4]:02d} UTC  "
        f"target={target_h:02d}:{target_m:02d} UTC  "
        f"window=±{window_m}min  "
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
    frost_threshold = getattr(_cfg, "FROST_THRESHOLD_C", 2.0)
    temp_min = weather.get("temp_min")
    if temp_min is not None and temp_min <= frost_threshold:
        send_alert(f"Frost warning: forecast min {temp_min}°C — watering skipped.")
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

    Returns (should_water, duration_s, skip_reason, decision_ctx):
        should_water — True or False
        duration_s   — seconds to run pump (0 if not watering)
        skip_reason  — string logged to InfluxDB:
                       "none" | "already_watered" | "rest_day" | "low_battery" |
                       "empty_butt" | "frost" | "no_weather_data" | "rain" | "not_time"
        decision_ctx — dict of the factors behind a WATER decision (for the ntfy
                       notification), or None when not watering. Keys:
                       temp_c, multiplier, base_duration_s, duration_s,
                       rain_pct, rain_mm, water_pct.

    Side effects:
        - Reads watered-today flag from RTC memory
    """

    # Gist-overridable thresholds — read at call time so Gist overrides apply.
    pump_cutoff           = pump_cutoff_pct()
    probe_enabled         = getattr(_cfg, "PROBE_SENSOR_ENABLED", True)
    frost_threshold       = getattr(_cfg, "FROST_THRESHOLD_C", 2.0)
    rain_pct_threshold    = getattr(_cfg, "RAIN_SKIP_THRESHOLD_PCT", 60)
    rain_amount_threshold = getattr(_cfg, "RAIN_SKIP_AMOUNT_MM", 5.0)

    # --- Condition 1: Already watered today? ---
    if power.get_watered_today():
        print("Decision: skip — already watered today.")
        return False, 0, "already_watered", None

    # --- Condition 1b: Forced rest day? (over-watering guard) ---
    # After a run of consecutive watering-or-rain days the rollover in main.py sets
    # a rest counter; skip watering while it's positive. Feature off when AFTER_DAYS <= 0.
    rest_after = getattr(_cfg, "WATER_REST_AFTER_DAYS", 3)
    if rest_after > 0 and power.get_skip_days_remaining() > 0:
        print(f"Decision: skip — rest day ({power.get_skip_days_remaining()} left after "
              f"{rest_after} consecutive watering days).")
        return False, 0, "rest_day", None

    # --- Condition 2: Battery too low? ---
    if tier >= 3:
        print(f"Decision: skip — battery tier {tier} ({battery_v}V), too low to water.")
        return False, 0, "low_battery", None

    # --- Condition 3: Water level too low? (pump dry-run protection) ---
    # If the ultrasonic sensor gave no reading (None), skip this check and rely
    # on the probe sensor below — a failed sensor should not block watering.
    water_pct = sensors.get("water_level_pct")
    if water_pct is not None and water_pct <= pump_cutoff:
        print(f"Decision: skip — water level {water_pct:.1f}% below cutoff {pump_cutoff}%.")
        return False, 0, "empty_butt", None

    # --- Condition 3b: Probe sensor dry? (backup hardware protection) ---
    # probe_sensor_ok is None if the sensor couldn't be read — treated as safe
    # so a faulty sensor doesn't block watering permanently.
    # Skipped entirely when PROBE_SENSOR_ENABLED is False (Gist override) — the
    # probe gives false DRY readings in low-conductivity rainwater, so the
    # ultrasonic level cutoff (Condition 3) becomes the sole dry-run guard.
    if probe_enabled:
        probe_ok = sensors.get("probe_sensor_ok")
        if probe_ok is False:
            print("Decision: skip — probe sensor reads dry.")
            return False, 0, "empty_butt", None
    else:
        print("Decision: probe sensor check disabled via Gist — relying on water level %.")

    # --- Condition 4: Weather data available? ---
    if weather is None:
        # WiFi or weather fetch failed this wake.
        # Attempt offline backup: use stored sunrise + RTC time to decide.
        # No temperature scaling — water for base duration only.
        # Probe sensor still checked as safety.
        stored_sunrise = power.get_sunrise_unix()
        if stored_sunrise and not power.get_watered_today():
            sunrise_offset_m = getattr(_cfg, "WATERING_SUNRISE_OFFSET_M", -30)
            window_m         = getattr(_cfg, "WATERING_WINDOW_M", 10)
            base_duration    = getattr(_cfg, "WATERING_BASE_DURATION_S", 600)
            now_unix    = time.time()
            target_unix = stored_sunrise + (sunrise_offset_m * 60)
            window_s    = (window_m + 10) * 60   # slightly wider window offline
            if abs(now_unix - target_unix) <= window_s:
                print("Decision: OFFLINE BACKUP — no weather data, using stored sunrise.")
                offline_ctx = {
                    "temp_c": None, "multiplier": 1.0,
                    "base_duration_s": base_duration, "duration_s": int(base_duration),
                    "rain_pct": None, "rain_mm": None, "water_pct": water_pct,
                }
                return True, int(base_duration), "none", offline_ctx
        print("Decision: skip — no weather data available.")
        return False, 0, "no_weather_data", None

    # --- Condition 5: Frost forecast? ---
    temp_min = weather.get("temp_min")
    if temp_min is not None and temp_min <= frost_threshold:
        print(f"Decision: skip — frost forecast (min {temp_min}°C).")
        return False, 0, "frost", None

    # --- Condition 6: Rain forecast? ---
    # Skip only when BOTH the probability and the predicted amount are high —
    # a high chance of 0.5mm drizzle should not skip watering, but a high
    # chance of a real downpour should.
    rain_pct = weather.get("rain_pct", 0)
    forecast_rain_mm = weather.get("forecast_rain_mm", 0.0)
    if rain_pct >= rain_pct_threshold and forecast_rain_mm >= rain_amount_threshold:
        print(f"Decision: skip — rain forecast {rain_pct}% / {forecast_rain_mm}mm "
              f"(thresholds {rain_pct_threshold}% / {rain_amount_threshold}mm).")
        return False, 0, "rain", None

    # --- Condition 7: Is it time? ---
    sunrise_unix = weather.get("sunrise_unix")
    if sunrise_unix is None:
        print("Decision: skip — no sunrise time in weather data.")
        return False, 0, "no_weather_data", None

    if not _is_watering_time(sunrise_unix):
        print("Decision: not watering — outside time window.")
        return False, 0, "not_time", None

    # --- All conditions passed — calculate duration ---
    temp_max = weather.get("temp_max")
    temp_for_calc = temp_max if temp_max is not None else 18.0
    duration = get_watering_duration(temp_for_calc)

    if duration == 0:
        # temp_multiplier was 0.0 — catches edge case where temp just above FROST_THRESHOLD_C
        # but below the first scaling tier
        print(f"Decision: skip — temperature multiplier is 0 at {temp_max}°C.")
        return False, 0, "frost", None

    water_str = f"{water_pct:.1f}%" if water_pct is not None else "N/A"
    print(
        f"Decision: WATER for {duration}s "
        f"(temp={temp_max}°C, rain={rain_pct}%, water={water_str})"
    )
    decision_ctx = {
        "temp_c":          temp_max,
        "multiplier":      get_temp_multiplier(temp_for_calc),
        "base_duration_s": getattr(_cfg, "WATERING_BASE_DURATION_S", 600),
        "duration_s":      duration,
        "rain_pct":        rain_pct,
        "rain_mm":         forecast_rain_mm,
        "water_pct":       water_pct,
    }
    return True, duration, "none", decision_ctx


# ---------------------------------------------------------------------------
# Pump health check
# ---------------------------------------------------------------------------
def check_pump_health(water_mm_before, water_mm_after, pump_runtime_s, pump_stopped_early):
    """
    Compare pre- and post-pump sensor distance readings to detect a blocked
    or failed pump.

    The A02YYUW measures distance from sensor to water surface.  When the pump
    is running, water is removed from the butt so the surface drops and the
    distance *increases*.  If the distance barely changed the pump probably
    didn't move any water.

    Returns True (warning) if:
      - both readings are available
      - pump ran for at least PUMP_MIN_RUNTIME_FOR_CHECK_S seconds
      - pump was NOT stopped early by a safety sensor
      - distance increased by less than PUMP_MIN_DROP_MM mm

    Returns False (healthy / not checked) otherwise.

    Uses getattr() for the new constants so a Pico still running the old
    config.py (before manual Thonny upload) gets the built-in defaults.
    """
    import config as _cfg
    min_runtime = getattr(_cfg, "PUMP_MIN_RUNTIME_FOR_CHECK_S", 120)
    min_drop    = getattr(_cfg, "PUMP_MIN_DROP_MM", 30)

    if water_mm_before is None or water_mm_after is None:
        print("Pump health: skipped — sensor reading unavailable.")
        return False
    if pump_stopped_early:
        print("Pump health: skipped — pump stopped early by safety sensor.")
        return False
    if pump_runtime_s < min_runtime:
        print(f"Pump health: skipped — runtime {pump_runtime_s}s < minimum {min_runtime}s.")
        return False

    drop_mm = water_mm_after - water_mm_before   # positive = distance grew = water level fell
    if drop_mm < min_drop:
        print(f"Pump health: WARNING — distance increased by only {drop_mm}mm (expected >{min_drop}mm).")
        return True

    print(f"Pump health: OK — distance increased by {drop_mm}mm.")
    return False
