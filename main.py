# main.py
# Smart Solar Irrigation System
#
# Wake cycle flow:
#   1.  Read battery → determine power tier
#   2.  Start watchdog timer
#   3.  Connect WiFi
#   4.  Send queued battery alerts
#   5.  Sync time from NTP — check validity
#   6.  Fetch + apply remote config from GitHub Gist
#   7.  Read sensors (BME280, water level, probe)
#   8.  Check ntfy for commands (tier 1–2 only)
#   9.  Fetch weather from Open-Meteo
#  10.  Detect new day → reset watered flag
#  11.  Check environment alerts (frost, overheat)
#  12.  Decide whether to water (skipped if time unknown)
#  13.  Run pump if watering, with mid-run safety checks
#  14.  Log everything to InfluxDB
#  15.  Disconnect WiFi
#  16.  Deep sleep (shorter if post-water monitoring active)

import time
import machine
import watchdog
import ota
import power
import network_manager as net
import cloud
import hardware
import decisions

from config import WATER_PUMP_CUTOFF_PCT, POST_WATER_CYCLES, POST_WATER_SLEEP_S

print("\n" + "=" * 42)
print("  Smart Irrigation — Wake cycle start")
print("=" * 42)


# ── Stage 1: Battery ────────────────────────────────────────────────────────
# Read battery before starting the watchdog — so a first-boot ADC issue
# doesn't cause a watchdog reset loop before we can diagnose it.

_pending_alerts = []
voltage, tier = power.run(send_alert=lambda msg: _pending_alerts.append(msg))


# ── Stage 2: Watchdog ───────────────────────────────────────────────────────
# Start the hardware watchdog now that basic boot succeeded.
# It auto-resets the Pico if any later stage hangs for more than ~8 seconds.
# Deep sleep disables it; it starts fresh on the next wake cycle.

watchdog.init()
watchdog.feed()

# Clean up any .new temp files left by an interrupted OTA update.
ota.cleanup_temp_files()

if tier == 4:
    print("Tier 4: critical hibernation — minimal wake.")
    try:
        wlan, rssi = net.connect()
        watchdog.feed()
        cloud.sync_time()
        watchdog.feed()
        for msg in _pending_alerts:
            cloud.send_ntfy_alert(msg)
        watchdog.feed()
        cloud.log_to_influx({"battery_v": voltage, "wifi_rssi": rssi})
        watchdog.feed()
    except Exception as e:
        print(f"Tier 4 WiFi/cloud failed: {e}")
    finally:
        net.disconnect()
    power.go_to_sleep(tier)   # does not return — 24hr sleep

try:
    wlan, rssi = net.connect()
    watchdog.feed()
except RuntimeError as e:
    print(f"WiFi failed: {e} — going back to sleep.")
    power.go_to_sleep(tier)


# ── Stage 4: Send queued battery alerts ─────────────────────────────────────

for msg in _pending_alerts:
    cloud.send_ntfy_alert(msg)
_pending_alerts.clear()
watchdog.feed()


# ── Stage 5: Time sync and validity check ───────────────────────────────────
# After deep sleep the Pico's RTC continues running, so time is usually
# approximately correct. On first boot or after a total power loss the clock
# resets to 2000-01-01. We detect this and skip time-dependent decisions
# rather than watering at the wrong time.

time_synced = cloud.sync_time()
watchdog.feed()

def _time_is_valid():
    """Return True if the clock year looks plausible (2024 or later)."""
    return time.localtime()[0] >= 2024

time_valid = time_synced or _time_is_valid()

if not time_valid:
    print("WARNING: NTP sync failed and clock is at year 2000 — time is unknown.")
    cloud.send_ntfy_alert(
        "Warning: time sync failed — clock not set. "
        "Watering skipped this cycle. Check WiFi / NTP."
    )


# ── Stage 6: Remote config ──────────────────────────────────────────────────
# Fetch overrides from GitHub Gist and apply them to the config module.
# Falls back to config.py hardcoded defaults if Gist is unreachable.

remote_cfg = cloud.fetch_remote_config()
watchdog.feed()
cloud.apply_remote_config(remote_cfg)
watchdog.feed()


# ── Stage 7: Read sensors ───────────────────────────────────────────────────

water_pct = hardware.read_water_level_pct()
water_mm  = hardware.read_water_level_mm()   # raw mm — for calibration, logged to InfluxDB
watchdog.feed()
probe_ok  = hardware.read_probe_sensor()
watchdog.feed()

sensors = {
    "water_level_pct": water_pct,
    "probe_sensor_ok": probe_ok,
}

print(f"Sensors: {sensors}")


# ── Stage 8: Commands ───────────────────────────────────────────────────────

sleep_s   = power.get_sleep_seconds(tier)
water_now = False
snooze    = False

if power.commands_accepted(tier):
    commands  = cloud.check_ntfy_commands(sleep_s)
    watchdog.feed()
    water_now = "water_now" in commands
    snooze    = "snooze"    in commands
else:
    print(f"Tier {tier}: commands ignored.")

if snooze:
    power.set_watered_today(True)
    cloud.send_ntfy_alert("Snooze received — next scheduled watering skipped.")
    print("Snooze: next watering skipped.")

watchdog.feed()


# ── Stage 9: Weather ────────────────────────────────────────────────────────

weather = cloud.fetch_weather()
watchdog.feed()


# ── Stage 10: New day detection + OTA check ─────────────────────────────────

if weather is not None and time_valid:
    new_sunrise = weather.get("sunrise_unix")
    old_sunrise = power.get_sunrise_unix()
    if new_sunrise and new_sunrise != old_sunrise:
        print("New day — resetting watered flag.")
        power.set_watered_today(False)
        power.save_sunrise_unix(new_sunrise)

        # OTA check — runs once per day when a new sunrise is detected.
        # If an update is available it downloads, applies, and reboots here.
        # If no update or OTA is disabled, execution continues normally.
        watchdog.feed()
        ota.check_and_apply(send_alert=cloud.send_ntfy_alert)
        watchdog.feed()


# ── Stage 11: Frost alert (from Open-Meteo forecast) ────────────────────────

decisions.check_frost_alert(
    weather    = weather,
    send_alert = cloud.send_ntfy_alert,
)
watchdog.feed()


# ── Stage 12: Watering decision ─────────────────────────────────────────────
# Skip if time is unknown — we don't know if it's watering time or not.

if not time_valid:
    should_water = False
    duration_s   = 0
    skip_reason  = "time_unknown"
    print("Watering decision skipped — time not known.")
else:
    should_water, duration_s, skip_reason = decisions.check_watering(
        weather   = weather,
        sensors   = sensors,
        battery_v = voltage,
        tier      = tier,
    )

watchdog.feed()


# ── Stage 13: Run pump ──────────────────────────────────────────────────────

pump_runtime_s     = 0
pump_stopped_early = False

def _pump_safety_ok():
    """
    Called every PUMP_CHECK_INTERVAL_S seconds during pumping.
    Returns False to stop the pump if either safety sensor detects dry conditions.
    Also feeds the watchdog so a long pump run doesn't trigger a reset.
    """
    watchdog.feed()
    probe = hardware.read_probe_sensor()
    if probe is False:
        print("SAFETY: probe sensor dry — stopping pump")
        return False
    level = hardware.read_water_level_pct()
    if level is not None and level <= WATER_PUMP_CUTOFF_PCT:
        print(f"SAFETY: water level {level:.1f}% — stopping pump")
        return False
    return True

if water_now and power.commands_accepted(tier):
    if water_pct is not None and water_pct <= WATER_PUMP_CUTOFF_PCT:
        msg = f"water_now ignored — water level too low ({water_pct:.1f}%)"
        cloud.send_ntfy_alert(msg)
        print(msg)
    elif probe_ok is False:
        msg = "water_now ignored — probe sensor reads dry"
        cloud.send_ntfy_alert(msg)
        print(msg)
    else:
        run_for = duration_s if duration_s > 0 else 600
        print(f"Running pump: water_now command for {run_for}s")
        pump_runtime_s     = hardware.run_pump(run_for, safety_check=_pump_safety_ok)
        pump_stopped_early = pump_runtime_s < run_for
        power.set_watered_today(True)
        power.set_post_water_cycles(POST_WATER_CYCLES)
        msg = f"Manual watering complete: {pump_runtime_s}s"
        if pump_stopped_early:
            msg += " — stopped early by safety sensor"
        cloud.send_ntfy_alert(msg)

elif should_water:
    print(f"Running pump: scheduled watering for {duration_s}s")
    pump_runtime_s     = hardware.run_pump(duration_s, safety_check=_pump_safety_ok)
    pump_stopped_early = pump_runtime_s < duration_s
    power.set_watered_today(True)
    power.set_post_water_cycles(POST_WATER_CYCLES)
    msg = f"Watering complete: {pump_runtime_s}s"
    if pump_stopped_early:
        msg += " — stopped early by safety sensor"
    if water_pct is not None:
        msg += f" | level={water_pct:.1f}%"
    cloud.send_ntfy_alert(msg)

else:
    if skip_reason not in ("not_time", "already_watered"):
        print(f"Not watering: {skip_reason}")

watchdog.feed()


# ── Stage 14: Log to InfluxDB ───────────────────────────────────────────────

log_data = {
    "battery_v":          voltage,
    "wifi_rssi":          rssi,
    "pump_runtime_s":     pump_runtime_s,
    "pump_stopped_early": pump_stopped_early,
    "skip_reason":        "none" if (should_water or water_now) else skip_reason,
    "probe_sensor_ok":    probe_ok if probe_ok is not None else True,
    "time_synced":        time_synced,
}

# Log Open-Meteo forecast values so they appear on Grafana graphs
if weather is not None:
    if weather.get("temp_max") is not None:
        log_data["forecast_temp_max_c"] = weather["temp_max"]
    if weather.get("rain_pct") is not None:
        log_data["forecast_rain_pct"] = weather["rain_pct"]

if water_pct is not None:
    log_data["water_level_pct"] = water_pct
if water_mm is not None:
    log_data["water_level_mm"] = water_mm

cloud.log_to_influx(log_data)
watchdog.feed()


# ── Stage 15 & 16: Disconnect and sleep ─────────────────────────────────────

net.disconnect()
watchdog.feed()

# Post-watering fast monitoring overrides normal tier-based sleep.
# Gives 6 cycles of 5-minute readings to capture the syphon refill curve.
# Only active in tier 1–2 (enough battery to support the extra wake cycles).
post_water = power.get_post_water_cycles()
if post_water > 0 and power.commands_accepted(tier):
    power.set_post_water_cycles(post_water - 1)
    sleep_s = POST_WATER_SLEEP_S
    print(f"Post-water monitoring: {post_water} cycles left — sleeping {sleep_s // 60} min")
else:
    sleep_s = power.get_sleep_seconds(tier)

t = time.localtime()
print(f"Wake cycle complete at {t[3]:02d}:{t[4]:02d} UTC — tier {tier} — sleeping {sleep_s // 60} min")

power.save_tier(tier)
machine.deepsleep(sleep_s * 1000)   # does not return
