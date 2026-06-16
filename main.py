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
import config

from config import POST_WATER_CYCLES, POST_WATER_SLEEP_S
# The pump dry-run cutoff is read at call time via decisions.pump_cutoff_pct()
# (not imported here) so Gist overrides of the cutoff / pump-level distance apply.

print("\n" + "=" * 42)
print("  Smart Irrigation — Wake cycle start")
print("=" * 42)


# ── Stage 1: Battery ────────────────────────────────────────────────────────
# Read battery before starting the watchdog — so a first-boot ADC issue
# doesn't cause a watchdog reset loop before we can diagnose it.

_pending_alerts = []
voltage, tier = power.run(send_alert=lambda msg: _pending_alerts.append(msg))




# ── Startup mode ────────────────────────────────────────────────────────────
# Triggered only on cold power-on (battery just connected, not deep sleep wake).
# Stays awake for 30 minutes, logging sensor readings every 30 seconds.
# Makes it easy to verify WiFi signal and sensors after outdoor deployment
# without waiting for the normal sleep cycle.

def _run_test_mode(wlan):
    """
    Send dense sensor readings for 5 minutes.
    Triggered by sending "test" via the ntfy commands topic.
    Useful for verifying WiFi signal and sensors after outdoor deployment.
    WiFi is already connected when this is called — no connect/disconnect here.
    Returns when complete; main.py continues to InfluxDB log and sleep as normal.
    """
    DURATION_S = 5 * 60   # 5 minutes
    INTERVAL_S = 15        # reading every 15 seconds = 20 readings total

    print("\n>>> TEST MODE — dense readings for 5 minutes <<<")
    cloud.send_ntfy_alert(
        f"Test mode started — sending readings every {INTERVAL_S}s "
        f"for {DURATION_S // 60}min."
    )

    deadline = time.ticks_add(time.ticks_ms(), DURATION_S * 1000)
    reading  = 0
    probe_wet = probe_dry = probe_err = 0   # probe tally for the completion summary

    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        reading += 1
        watchdog.feed()

        w_pct = hardware.read_water_level_pct()
        w_mm  = hardware.read_water_level_mm()
        probe = hardware.read_probe_sensor()   # forced read — diagnostic, ignores disable flag
        if   probe is True:  probe_wet += 1
        elif probe is False: probe_dry += 1
        else:                probe_err += 1
        v, _  = power.run(send_alert=lambda m: None)
        rssi  = wlan.status("rssi")
        watchdog.feed()

        log = {
            "battery_v":       v,
            "wifi_rssi":       rssi,
            "probe_sensor_ok": probe if probe is not None else True,
        }
        if w_pct is not None:
            log["water_level_pct"] = w_pct
        if w_mm is not None:
            log["water_level_mm"] = w_mm

        cloud.log_to_influx(log)
        watchdog.feed()

        probe_str = "water present" if probe else ("DRY" if probe is False else "unknown")
        level_str = f"{w_pct:.1f}%" if w_pct is not None else "no reading"
        dist_str  = f"{w_mm}mm" if w_mm is not None else ""
        remaining = max(0, time.ticks_diff(deadline, time.ticks_ms()) // 1000)

        print(f"  #{reading:3d}  battery={v:.2f}V  level={level_str} {dist_str}  "
              f"probe={probe_str}  rssi={rssi}dBm  {remaining}s remaining")

        interval_end = time.ticks_add(time.ticks_ms(), INTERVAL_S * 1000)
        while time.ticks_diff(interval_end, time.ticks_ms()) > 0:
            time.sleep(1)
            watchdog.feed()

    probe_total = probe_wet + probe_dry + probe_err
    probe_summary = f"probe wet {probe_wet}/{probe_total}, dry {probe_dry}/{probe_total}"
    if probe_err:
        probe_summary += f", err {probe_err}/{probe_total}"
    cloud.send_ntfy_alert(f"Test mode complete — {probe_summary}.")
    print(f"Test mode complete — {probe_summary}.")


def _run_probe_test():
    """
    Quick one-shot probe diagnostic, triggered by the "probe_test" ntfy command.
    Force-reads the probe a few times (pulse-powered) EVEN WHEN it is disabled via
    the Gist, then replies over ntfy with the wet/dry tally alongside the current
    ultrasonic level so the result can be cross-checked while the probe is installed.
    """
    print("\n>>> PROBE TEST <<<")
    reads = []
    for _ in range(5):
        watchdog.feed()
        reads.append(hardware.read_probe_sensor())
        time.sleep_ms(300)

    wet = sum(1 for r in reads if r is True)
    dry = sum(1 for r in reads if r is False)
    err = sum(1 for r in reads if r is None)

    lvl = hardware.read_water_level_pct()
    lvl_str = f"{lvl:.0f}%" if lvl is not None else "no reading"

    msg = f"Probe test: wet {wet}/5, dry {dry}/5"
    if err:
        msg += f", err {err}/5"
    msg += f" | water level {lvl_str}"
    # If the butt clearly holds water above the probe but it reads dry, flag a likely fault.
    if lvl is not None and lvl > decisions.pump_cutoff_pct() and wet == 0 and dry > 0:
        msg += " — butt has water but probe reads DRY, likely faulty/corroded"

    cloud.send_ntfy_alert(msg)
    print(msg)


def _run_status(voltage, tier, rssi, time_synced,
                water_pct, water_mm, probe_ok,
                sleep_s, commands):
    """
    Send a concise hardware/config status reply via ntfy.
    Triggered by the 'status' ntfy command. Fires during Stage 8 (before
    weather fetch and watering decision) so the reply is fast.

    Example reply:
        Battery: 4.47V  Tier 1  (sleep 30 min)
        WiFi: -66 dBm  NTP: OK
        Water: 67% / 603mm  Probe: disabled (Gist)
        Watered today: no  Post-water: off  Rest: streak 2/3
        Pump cutoff: 600mm (≈29%)  Base: 900s
        Commands this wake: status, water_now
    """
    # Battery / power
    sleep_min = sleep_s // 60
    line1 = f"Battery: {voltage:.2f}V  Tier {tier}  (sleep {sleep_min} min)"

    # WiFi / NTP
    ntp_str = "OK" if time_synced else "FAILED"
    line2 = f"WiFi: {rssi} dBm  NTP: {ntp_str}"

    # Water level and probe
    if water_pct is not None:
        level_str = f"{water_pct:.0f}%"
        if water_mm is not None:
            level_str += f" / {water_mm}mm"
    else:
        level_str = "no reading"
    if not getattr(config, "PROBE_SENSOR_ENABLED", True):
        probe_str = "disabled (Gist)"
    elif probe_ok is True:
        probe_str = "wet"
    elif probe_ok is False:
        probe_str = "DRY"
    else:
        probe_str = "unknown"
    line3 = f"Water: {level_str}  Probe: {probe_str}"

    # Persistent state
    watered_str = "yes" if power.get_watered_today() else "no"
    post = power.get_post_water_cycles()
    post_str = f"{post} cycles left" if post > 0 else "off"
    rest_after = getattr(config, "WATER_REST_AFTER_DAYS", 3)
    rest_left  = power.get_skip_days_remaining()
    if rest_after > 0:
        rest_str = (f"{rest_left} day(s) left" if rest_left > 0
                    else f"streak {power.get_consecutive_water_days()}/{rest_after}")
    else:
        rest_str = "off"
    line4 = f"Watered today: {watered_str}  Post-water: {post_str}  Rest: {rest_str}"

    # Config
    cutoff_mm = getattr(config, "SENSOR_DISTANCE_PUMP_MM", 0)
    if cutoff_mm and cutoff_mm > 0:
        cutoff_str = f"{cutoff_mm}mm (approx {decisions.pump_cutoff_pct():.0f}%)"
    else:
        cutoff_str = f"{decisions.pump_cutoff_pct():.0f}%"
    base_s = getattr(config, "WATERING_BASE_DURATION_S", 600)
    line5 = f"Pump cutoff: {cutoff_str}  Base: {base_s}s"

    # Commands received this cycle
    other_cmds = [c for c in commands if c != "status"]
    cmd_str = ", ".join(other_cmds) if other_cmds else "none"
    line6 = f"Commands this wake: status" + (f", {cmd_str}" if other_cmds else "")

    msg = "\n".join([line1, line2, line3, line4, line5, line6])
    cloud.send_ntfy_alert(msg)
    print(f"Status sent:\n{msg}")


def _run_stay_awake(wlan):
    """
    Keep the Pico awake and WebREPL live for up to 20 minutes so Thonny can
    connect over WiFi.  Called after Stage 14 (InfluxDB log) so all sensor
    and watering work completes first; WiFi stays connected throughout.

    Sends the device IP via ntfy so you know what to type in Thonny:
        Run → Configure interpreter → MicroPython (WebREPL)
        URL: ws://<IP>:8266   Password: WEBREPL_PASS from secrets.py

    Send 'sleep' via ntfy to exit early, or just wait for the 20-min timeout.
    """
    ip = wlan.ifconfig()[0]
    cloud.send_ntfy_alert(
        f"Stay-awake: WebREPL active for 20 min. "
        f"Thonny ws://{ip}:8266  pass=WEBREPL_PASS in secrets.py"
    )
    print(f"\n>>> STAY-AWAKE — WebREPL live at ws://{ip}:8266 <<<")
    print("Send 'sleep' via ntfy to exit early, or wait 20 min.")

    deadline = time.ticks_add(time.ticks_ms(), 20 * 60 * 1000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        watchdog.feed()
        remaining = time.ticks_diff(deadline, time.ticks_ms()) // 1000
        print(f"Stay-awake: {remaining}s remaining — WebREPL active at ws://{ip}:8266")
        # Poll ntfy for a 'sleep' command to exit early
        cmds = cloud.check_ntfy_commands(35)
        if "sleep" in cmds:
            print("Stay-awake: 'sleep' command received — resuming normal sleep.")
            cloud.send_ntfy_alert("Stay-awake ended by 'sleep' command.")
            return
        time.sleep(30)

    print("Stay-awake: 20-minute timeout — resuming normal sleep.")
    cloud.send_ntfy_alert("Stay-awake timeout — resuming normal operation.")


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
    print(f"WiFi failed: {e}")
    # No WiFi — attempt offline cycle using stored sunrise and RTC time.
    # If watering is due and sensors are OK, water now. Then sleep.
    print("WiFi offline — attempting offline sensor read and watering check.")
    try:
        w_pct  = hardware.read_water_level_pct()
        if getattr(config, "PROBE_SENSOR_ENABLED", True):
            probe = hardware.read_probe_sensor()
        else:
            probe = None
        w_mm   = hardware.read_water_level_mm()
        stored_sunrise = power.get_sunrise_unix()
        if (stored_sunrise
                and not power.get_watered_today()
                and tier < 3
                and (probe is not False)
                and (w_pct is None or w_pct > decisions.pump_cutoff_pct())):

            from config import WATERING_BASE_DURATION_S, WATERING_SUNRISE_OFFSET_M, WATERING_WINDOW_M
            import time as _t
            now_unix    = _t.time()
            target_unix = stored_sunrise + (WATERING_SUNRISE_OFFSET_M * 60)
            window_s    = (WATERING_WINDOW_M + 10) * 60
            if abs(now_unix - target_unix) <= window_s:
                print("Offline cycle: watering window reached — running pump.")
                def _offline_safety():
                    watchdog.feed()
                    if getattr(config, "PROBE_SENSOR_ENABLED", True):
                        return hardware.read_probe_sensor() is not False
                    # Probe disabled — fall back to the ultrasonic cutoff for dry-run safety.
                    lvl = hardware.read_water_level_pct()
                    return lvl is None or lvl > decisions.pump_cutoff_pct()
                ran = hardware.run_pump(int(WATERING_BASE_DURATION_S), safety_check=_offline_safety)
                power.set_watered_today(True)
                print(f"Offline cycle: pump ran {ran}s.")
    except Exception as offline_e:
        print(f"Offline cycle error: {offline_e}")
    power.go_to_sleep(tier)


# ── Stage 4: Send queued battery alerts ─────────────────────────────────────

for msg in _pending_alerts:
    cloud.send_ntfy_alert(msg)
_pending_alerts.clear()
watchdog.feed()


# ── Stage 5: Time sync and validity check ───────────────────────────────────
# The RP2350 RTC does not reliably survive deep sleep, so first restore an
# approximate clock from the saved wake time (no-op if the RTC is still valid or
# nothing was saved). Then try NTP, which corrects any drift when it succeeds.
# Only if we end up with no usable clock at all do we skip time-dependent work.

power.restore_clock()
watchdog.feed()

time_synced = cloud.sync_time(feed=watchdog.feed)
watchdog.feed()

def _time_is_valid():
    """Return True if the clock year looks plausible (2024 or later)."""
    return time.localtime()[0] >= 2024

time_valid = time_synced or _time_is_valid()

if not time_valid:
    print("WARNING: time is unknown — NTP failed and no clock to restore.")
    # Alert at most once per outage so a weak connection doesn't spam the phone.
    if not power.get_time_alerted():
        power.set_time_alerted(True)
        cloud.send_ntfy_alert(
            "Warning: time unknown — NTP unreachable and no saved clock. "
            "Watering skipped until time recovers. Check WiFi / NTP."
        )
elif power.get_time_alerted():
    # Time has recovered since the last alert — clear the flag (and say so once).
    power.set_time_alerted(False)
    cloud.send_ntfy_alert("Time recovered — clock is set again.")


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
# Only energise the probe when it's enabled — a disabled probe must not be
# electrically read each wake (the pull-up bias corrodes the electrodes).
# Diagnostics (test / probe_test) call read_probe_sensor() directly to force a read.
if getattr(config, "PROBE_SENSOR_ENABLED", True):
    probe_ok = hardware.read_probe_sensor()
else:
    probe_ok = None
    print("Probe sensor: disabled via Gist — not energised this cycle.")
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
    water_now  = "water_now"  in commands
    snooze     = "snooze"     in commands
    cancel     = "cancel"     in commands
    test       = "test"       in commands
    probe_test = "probe_test" in commands
    stay_awake = "stay_awake" in commands
    status     = "status"     in commands
else:
    print(f"Tier {tier}: commands ignored.")
    cancel     = False
    test       = False
    probe_test = False
    stay_awake = False
    status     = False

if cancel:
    water_now = False
    cloud.send_ntfy_alert("Cancel received — water_now cleared.")
    print("Cancel: water_now command cleared.")

if test:
    _run_test_mode(wlan)

if probe_test:
    _run_probe_test()

if status:
    _run_status(voltage, tier, rssi, time_synced,
                water_pct, water_mm, probe_ok,
                sleep_s, commands)

if snooze:
    power.set_watered_today(True)
    cloud.send_ntfy_alert("Snooze received — next scheduled watering skipped.")
    print("Snooze: next watering skipped.")

watchdog.feed()


# ── Stage 9: Weather ────────────────────────────────────────────────────────

weather = cloud.fetch_weather()
watchdog.feed()


# ── Stage 10: New day detection ─────────────────────────────────────────────

if weather is not None and time_valid:
    new_sunrise = weather.get("sunrise_unix")
    old_sunrise = power.get_sunrise_unix()
    if new_sunrise and new_sunrise != old_sunrise:
        print("New day — resetting watered flag.")

        # ── Rest-day over-watering guard — finalise the day that just ended ──
        # Done once per day at rollover, BEFORE the watered flag is reset, so we can
        # read yesterday's watering. A day "counts" if it was watered OR it actually
        # rained over the rain-skip amount (actual rainfall preferred over forecast).
        # After WATER_REST_AFTER_DAYS counted days in a row, skip WATER_REST_DURATION_DAYS.
        rest_after = int(getattr(config, "WATER_REST_AFTER_DAYS", 3))
        rest_dur   = int(getattr(config, "WATER_REST_DURATION_DAYS", 1))
        if rest_after > 0:
            rain_amt_threshold = float(getattr(config, "RAIN_SKIP_AMOUNT_MM", 5.0))
            yest_rain    = weather.get("actual_rain_mm")
            rain_counted = yest_rain is not None and yest_rain >= rain_amt_threshold
            counted_day  = power.get_watered_today() or rain_counted

            consec = power.get_consecutive_water_days()
            rest   = power.get_skip_days_remaining()
            if rest > 0:
                rest -= 1                       # yesterday was a rest day — consume it
            elif counted_day:
                consec += 1
                if consec >= rest_after:
                    rest   = rest_dur           # streak reached — schedule the rest
                    consec = 0
            else:
                consec = 0                      # dry, un-watered day broke the streak
            power.set_consecutive_water_days(consec)
            power.set_skip_days_remaining(rest)
            print(f"Rest accounting: counted={counted_day} streak={consec}/{rest_after} "
                  f"rest_left={rest}")
        else:
            # Feature disabled — keep counters clean so re-enabling starts fresh.
            if power.get_consecutive_water_days() or power.get_skip_days_remaining():
                power.set_consecutive_water_days(0)
                power.set_skip_days_remaining(0)

        power.set_watered_today(False)
        power.save_sunrise_unix(new_sunrise)

        # Log yesterday's actual weather at yesterday's timestamp so it lands
        # directly beside the forecast that was recorded yesterday — making
        # forecast-vs-actual a visual comparison with no mental offset.
        # Written once per day (only on new-sunrise detection), not every wake.
        actual = {}
        if weather.get("actual_temp_max_c") is not None:
            actual["actual_temp_max_c"] = weather["actual_temp_max_c"]
        if weather.get("actual_rain_mm") is not None:
            actual["actual_rain_mm"] = weather["actual_rain_mm"]
        if actual:
            # time.time() on Pico 2W returns Unix seconds (since 1970) after ntptime.settime().
            # Subtract one day to get yesterday's Unix timestamp in nanoseconds for InfluxDB.
            yesterday_ns = (int(time.time()) - 86400) * 1000000000
            cloud.log_to_influx(actual, timestamp_ns=yesterday_ns)
            watchdog.feed()

# OTA check — runs every wake cycle so updates land within one sleep interval.
# Fetches manifest.json, compares version, and exits immediately if up to date.
# If an update is available it downloads, applies, and reboots — does not return.
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
    decision_ctx = None
    print("Watering decision skipped — time not known.")
else:
    should_water, duration_s, skip_reason, decision_ctx = decisions.check_watering(
        weather   = weather,
        sensors   = sensors,
        battery_v = voltage,
        tier      = tier,
    )

watchdog.feed()


# ── Stage 13: Run pump ──────────────────────────────────────────────────────

pump_runtime_s     = 0
pump_stopped_early = False
pump_health_warning = False

# _safety_state[0] = tick counter (reset before each pump run)
# _safety_state[1] = last known water level pct (seeded from Stage 7 reading)
# The water-level sensor read (3-sample median filter, ~1.7 s) is expensive —
# rate-limited to once every 6 ticks (~30 s). The probe sensor (GPIO, <1 ms)
# still runs every tick so dry-run is caught promptly.
_safety_state = [0, None]

def _build_water_msg(runtime_s, planned_s, stopped_early, ctx, level_before, level_after, health_warn):
    """
    Build a concise human-readable watering-complete ntfy message, e.g.:
        Watered 21 min | 19.7°C ×1.4 | Base 900s | Rain 20%/1.2mm | Level 67%→54%
        Stopped early at 8 min (planned 21 min) | 19.7°C ×1.4 | ...
    ctx is the decision context dict from decisions.check_watering (or None for
    a manual water_now with no scheduled decision behind it).
    """
    if stopped_early:
        parts = [f"Stopped early at {runtime_s // 60} min (planned {planned_s // 60} min)"]
    else:
        parts = [f"Watered {runtime_s // 60} min"]

    if ctx:
        if ctx.get("temp_c") is not None and ctx.get("multiplier") is not None:
            parts.append(f"{ctx['temp_c']}°C ×{ctx['multiplier']}")
        if ctx.get("base_duration_s") is not None:
            parts.append(f"Base {ctx['base_duration_s']}s")
        if ctx.get("rain_pct") is not None and ctx.get("rain_mm") is not None:
            parts.append(f"Rain {ctx['rain_pct']}%/{ctx['rain_mm']}mm")

    if level_before is not None or level_after is not None:
        b = f"{level_before:.0f}%" if level_before is not None else "N/A"
        a = f"{level_after:.0f}%" if level_after is not None else "N/A"
        parts.append(f"Level {b}→{a}")

    msg = " | ".join(parts)
    if health_warn:
        msg += " | PUMP HEALTH WARNING: low flow"
    return msg

def _pump_safety_ok():
    """
    Called every PUMP_CHECK_INTERVAL_S seconds during pumping.
    Returns False to stop the pump if either safety sensor detects dry conditions.
    Also feeds the watchdog so a long pump run doesn't trigger a reset.
    """
    _safety_state[0] += 1
    watchdog.feed()
    # Probe is skipped when disabled via Gist (false DRY readings in low-conductivity
    # rainwater) — the ultrasonic level cutoff below remains the dry-run guard.
    if getattr(config, "PROBE_SENSOR_ENABLED", True):
        probe = hardware.read_probe_sensor()
        if probe is False:
            print("SAFETY: probe sensor dry — stopping pump")
            return False
    # Full median-filter sensor read every 6 ticks (~30 s); use cached level otherwise.
    if _safety_state[0] % 6 == 0:
        _safety_state[1] = hardware.read_water_level_pct()
    level = _safety_state[1]
    if level is not None and level <= decisions.pump_cutoff_pct():
        print(f"SAFETY: water level {level:.1f}% — stopping pump")
        return False
    return True

if water_now and power.commands_accepted(tier):
    if water_pct is not None and water_pct <= decisions.pump_cutoff_pct():
        msg = f"water_now ignored — water level too low ({water_pct:.1f}%)"
        cloud.send_ntfy_alert(msg)
        print(msg)
    elif probe_ok is False and getattr(config, "PROBE_SENSOR_ENABLED", True):
        msg = "water_now ignored — probe sensor reads dry"
        cloud.send_ntfy_alert(msg)
        print(msg)
    else:
        run_for = duration_s if duration_s > 0 else 600
        print(f"Running pump: water_now command for {run_for}s")
        _safety_state[0] = 0
        _safety_state[1] = water_pct
        water_mm_before     = hardware.read_water_level_mm()
        pump_runtime_s      = hardware.run_pump(run_for, safety_check=_pump_safety_ok)
        pump_stopped_early  = pump_runtime_s < run_for
        level_after         = hardware.read_water_level_pct()   # post-pump level (also refreshes cache)
        water_mm_after      = hardware.read_water_level_mm()
        pump_health_warning = decisions.check_pump_health(
            water_mm_before, water_mm_after, pump_runtime_s, pump_stopped_early
        )
        power.set_watered_today(True)
        power.set_post_water_cycles(POST_WATER_CYCLES)
        msg = "Manual: " + _build_water_msg(
            pump_runtime_s, run_for, pump_stopped_early,
            decision_ctx, water_pct, level_after, pump_health_warning,
        )
        cloud.send_ntfy_alert(msg)
        if pump_health_warning:
            cloud.send_ntfy_alert(
                f"Pump health warning: sensor distance only increased "
                f"{water_mm_after - water_mm_before}mm after {pump_runtime_s}s. "
                "Check pump / water supply."
            )

elif should_water:
    print(f"Running pump: scheduled watering for {duration_s}s")
    _safety_state[0] = 0
    _safety_state[1] = water_pct
    water_mm_before     = hardware.read_water_level_mm()
    pump_runtime_s      = hardware.run_pump(duration_s, safety_check=_pump_safety_ok)
    pump_stopped_early  = pump_runtime_s < duration_s
    level_after         = hardware.read_water_level_pct()   # post-pump level (also refreshes cache)
    water_mm_after      = hardware.read_water_level_mm()
    pump_health_warning = decisions.check_pump_health(
        water_mm_before, water_mm_after, pump_runtime_s, pump_stopped_early
    )
    power.set_watered_today(True)
    power.set_post_water_cycles(POST_WATER_CYCLES)
    msg = _build_water_msg(
        pump_runtime_s, duration_s, pump_stopped_early,
        decision_ctx, water_pct, level_after, pump_health_warning,
    )
    cloud.send_ntfy_alert(msg)
    if pump_health_warning:
        cloud.send_ntfy_alert(
            f"Pump health warning: sensor distance only increased "
            f"{water_mm_after - water_mm_before}mm after {pump_runtime_s}s. "
            "Check pump / water supply."
        )

else:
    if skip_reason not in ("not_time", "already_watered"):
        print(f"Not watering: {skip_reason}")

watchdog.feed()


# ── Stage 14: Log to InfluxDB ───────────────────────────────────────────────

log_data = {
    "battery_v":               voltage,
    "wifi_rssi":               rssi,
    "pump_runtime_s":          pump_runtime_s,
    "pump_stopped_early":      pump_stopped_early,
    "pump_health_warning":     pump_health_warning,
    "skip_reason":             "none" if (should_water or water_now) else skip_reason,
    "probe_sensor_ok":         probe_ok if probe_ok is not None else True,
    "probe_sensor_enabled":    getattr(config, "PROBE_SENSOR_ENABLED", True),
    "time_synced":             time_synced,
    # Rest-day over-watering guard counters — for visibility in Grafana / status
    "consecutive_water_days":  power.get_consecutive_water_days(),
    "rest_days_remaining":     power.get_skip_days_remaining(),
    # Calibration reference values (post-Gist override) — used for Grafana reference lines
    "sensor_distance_full_mm":  config.SENSOR_DISTANCE_FULL_MM,
    "sensor_distance_empty_mm": config.SENSOR_DISTANCE_EMPTY_MM,
}

# Pump-intake safety distance — only log when set (>0) so the Grafana reference
# line is absent rather than pinned at 0 when the percentage cutoff is in use.
if getattr(config, "SENSOR_DISTANCE_PUMP_MM", 0):
    log_data["sensor_distance_pump_mm"] = config.SENSOR_DISTANCE_PUMP_MM

# Decision-threshold reference values (post-Gist override) — logged so Grafana can
# draw auto-updating reference lines on the weather panels. All forced to float to
# keep a single InfluxDB field type. Changing these in the Gist moves the lines.
#   - rain skip limits → dashed lines on the Forecast Rain panel (skip needs BOTH)
#   - temp_scaling boundaries → one dashed line per bracket on the Forecast Temp panel
log_data["rain_skip_probability_pct"] = float(getattr(config, "RAIN_SKIP_THRESHOLD_PCT", 60))
log_data["rain_skip_amount_mm"]       = float(getattr(config, "RAIN_SKIP_AMOUNT_MM", 5.0))

# Temp-scaling bracket boundaries — skip the open-ended sentinel (999), ascending,
# capped at 6 indexed fields (temp_bracket_1_c … temp_bracket_6_c).
_temp_bounds = [t for (t, _m) in getattr(config, "TEMP_SCALING", []) if t < 100]
for _bi, _bt in enumerate(_temp_bounds[:6], start=1):
    log_data[f"temp_bracket_{_bi}_c"] = float(_bt)

# Log Open-Meteo forecast values so they appear on Grafana graphs
if weather is not None:
    if weather.get("temp_max") is not None:
        log_data["forecast_temp_max_c"] = weather["temp_max"]
    if weather.get("temp_min") is not None:
        log_data["forecast_temp_min_c"] = weather["temp_min"]
    if weather.get("rain_pct") is not None:
        log_data["forecast_rain_pct"] = weather["rain_pct"]
    log_data["forecast_rain_mm"] = weather.get("forecast_rain_mm", 0.0)

if water_pct is not None:
    log_data["water_level_pct"] = water_pct
if water_mm is not None:
    log_data["water_level_mm"] = water_mm

cloud.log_to_influx(log_data)
watchdog.feed()

# Stay-awake mode — runs after all logging so the cycle is complete first.
# WiFi stays connected throughout so WebREPL remains accessible.
if stay_awake:
    _run_stay_awake(wlan)

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

power.go_to_sleep(tier, voltage=voltage, sleep_s=sleep_s)   # does not return
