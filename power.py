# power.py
# Smart Solar Irrigation System
#
# Responsibilities:
#   - Read battery voltage via ADC
#   - Classify voltage into a power tier (1-4)
#   - Fire critical low and recovery alerts
#   - Own and manage state that survives deep sleep (written to flash filesystem)
#   - Put the Pico into deep sleep at the end of each wake cycle
#
# State file layout — 11 bytes, written to state.bin on the Pico filesystem.
# The Pico 2W (RP2350) does not support RTC.memory() — flash filesystem is
# used instead. state.bin persists across deep sleep and power cycles.
#   Byte 0:    battery tier from last wake (1-4, 0 = first boot)
#   Byte 1:    watered today flag (0 = no, 1 = yes) — written by decisions.py
#   Bytes 2-5: RESERVED (kept for layout compatibility)
#   Bytes 6-9: today's sunrise as unix timestamp uint32 — written by cloud.py
#   Byte 10:   post-water fast monitoring cycles remaining (0 = normal sleep)
#
# All other files that need persistent state import the helper functions from here.

import machine
from machine import ADC, Pin
import ustruct

from config import (
    ADC_PIN, VDIV_RATIO, ADC_REF_V, ADC_RESOLUTION,
    BATTERY_TIER1_V, BATTERY_TIER2_V, BATTERY_TIER3_V,
    SLEEP_TIER1_S, SLEEP_TIER2_S, SLEEP_TIER3_S, SLEEP_TIER4_S,
)

# BATTERY_EMERGENCY_CUTOFF_V is read at call time inside go_to_sleep()
# so it picks up any value set by the Gist remote config during this wake cycle.

# ---------------------------------------------------------------------------
# Persistent state — filesystem helpers
# ---------------------------------------------------------------------------
# The Pico 2W (RP2350) does not support RTC.memory().
# State is stored in a small binary file on the flash filesystem instead.
# Flash survives deep sleep and power cycles — behaviour is identical to RTC memory.
# ---------------------------------------------------------------------------
_STATE_FILE       = "state.bin"
_STATE_SIZE       = 11   # total bytes in state file
_IDX_TIER         = 0    # byte 0:  last battery tier
_IDX_WATERED      = 1    # byte 1:  watered today flag
_IDX_PRESSURE     = 2    # bytes 2-5: reserved
_IDX_SUNRISE      = 6    # bytes 6-9: sunrise unix timestamp (uint32)
_IDX_POST_WATER   = 10   # byte 10: post-water fast cycles remaining


def _read_rtc():
    """Read state file into a bytearray. Initialises to zeros if missing or wrong size."""
    try:
        with open(_STATE_FILE, "rb") as f:
            data = f.read()
        if len(data) == _STATE_SIZE:
            return bytearray(data)
    except OSError:
        pass
    # First boot or corrupted file — start fresh
    mem = bytearray(_STATE_SIZE)
    _write_rtc(mem)
    return mem


def _write_rtc(mem):
    """Write bytearray to state file on flash."""
    with open(_STATE_FILE, "wb") as f:
        f.write(bytes(mem))


# ---------------------------------------------------------------------------
# RTC memory — public getters and setters (used by other files)
# ---------------------------------------------------------------------------
def get_last_tier():
    """Return the battery tier recorded at the end of the previous wake cycle.
    Returns 0 on first boot (RTC memory not yet written)."""
    return _read_rtc()[_IDX_TIER]


def save_tier(tier):
    """Save the current battery tier to RTC memory before sleeping."""
    mem = _read_rtc()
    mem[_IDX_TIER] = tier
    _write_rtc(mem)


def get_watered_today():
    """Return True if watering has already run today."""
    return bool(_read_rtc()[_IDX_WATERED])


def set_watered_today(value):
    """Set the watered-today flag. Call with True after watering, False at midnight."""
    mem = _read_rtc()
    mem[_IDX_WATERED] = 1 if value else 0
    _write_rtc(mem)


def get_sunrise_unix():
    """Return today's sunrise time as a unix timestamp, or 0 on first boot."""
    mem = _read_rtc()
    return ustruct.unpack(">I", bytes(mem[_IDX_SUNRISE:_IDX_SUNRISE + 4]))[0]


def save_sunrise_unix(timestamp):
    """Save today's sunrise unix timestamp to RTC memory."""
    mem = _read_rtc()
    mem[_IDX_SUNRISE:_IDX_SUNRISE + 4] = ustruct.pack(">I", timestamp)
    _write_rtc(mem)


# ---------------------------------------------------------------------------
# Post-watering fast monitoring counter
# ---------------------------------------------------------------------------
def get_post_water_cycles():
    """
    Return the number of fast monitoring cycles remaining after watering.
    0 means normal tier-based sleep should be used.
    """
    return _read_rtc()[_IDX_POST_WATER]


def set_post_water_cycles(count):
    """
    Set the post-watering fast cycle counter.
    Call with POST_WATER_CYCLES after any watering event to start fast monitoring.
    Call with 0 to cancel.
    """
    mem = _read_rtc()
    mem[_IDX_POST_WATER] = max(0, int(count))
    _write_rtc(mem)


# ---------------------------------------------------------------------------
# Battery voltage
# ---------------------------------------------------------------------------
def read_battery_voltage():
    """
    Read battery voltage from the ADC on GP26.
    Takes 10 readings and averages them for stability — the pump motor
    can cause noise on the ADC rail so a single reading is unreliable.
    Returns voltage as a rounded float, e.g. 11.84
    """
    adc = ADC(Pin(ADC_PIN))
    readings = [adc.read_u16() for _ in range(10)]
    avg_raw   = sum(readings) / len(readings)
    adc_v     = (avg_raw / ADC_RESOLUTION) * ADC_REF_V
    battery_v = adc_v / VDIV_RATIO
    return round(battery_v, 2)


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------
def get_tier(voltage):
    """
    Classify battery voltage into a power tier.
      Tier 1  > 3.9V  healthy (~70%+) — 15 min sleep, commands accepted
      Tier 2  > 3.6V  ok      (~40%+) — 1 hr sleep,  commands accepted
      Tier 3  > 3.3V  low     (~10%+) — 4 hr sleep,  commands ignored
      Tier 4  ≤ 3.3V  critical         — 24 hr sleep, commands ignored
    """
    if voltage > BATTERY_TIER1_V: return 1
    if voltage > BATTERY_TIER2_V: return 2
    if voltage > BATTERY_TIER3_V: return 3
    return 4


def get_sleep_seconds(tier):
    """Return sleep duration in seconds for a given tier."""
    return {
        1: SLEEP_TIER1_S,
        2: SLEEP_TIER2_S,
        3: SLEEP_TIER3_S,
        4: SLEEP_TIER4_S,
    }[tier]


def commands_accepted(tier):
    """Return True if remote ntfy commands should be acted on at this tier."""
    return tier <= 2


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
def check_battery_alerts(voltage, tier, previous_tier, send_alert):
    """
    Fire critical low or recovery alerts if tier has changed meaningfully.

    send_alert is a callable passed in from main.py, for example:
        lambda msg: cloud.send_ntfy_alert(msg)
    power.py does not import cloud.py — it just calls whatever it is given.
    This keeps power.py independent of the networking layer.
    """
    # Entering hibernation — only alert on the first cycle at tier 4
    if tier == 4 and previous_tier != 4:
        send_alert(
            f"CRITICAL: Battery at {voltage}V — entering 24hr hibernation. "
            f"Check solar panel and charge controller."
        )

    # Recovering from tier 3 or 4 back into normal operation
    if tier <= 2 and previous_tier >= 3:
        send_alert(
            f"Battery recovered: {voltage}V — resuming normal operation."
        )


# ---------------------------------------------------------------------------
# Deep sleep
# ---------------------------------------------------------------------------
def go_to_sleep(tier, voltage=None):
    """
    Save the current tier and enter deep sleep.
    Sleep duration is normally set by the tier.

    If voltage is passed and is below BATTERY_EMERGENCY_CUTOFF_V (3.0V),
    the device sleeps for 7 days instead of the normal 24 hours.
    This hard floor prevents the 18650 cell being damaged by repeated
    wake cycles when solar cannot keep up with consumption.

    This function does not return — the Pico resets and restarts
    from the top of main.py when the sleep period ends.
    """
    save_tier(tier)

    # Read emergency cutoff from config at call time so it reflects any
    # value applied by the Gist remote config during this wake cycle.
    import config as _cfg
    emergency_v = getattr(_cfg, "BATTERY_EMERGENCY_CUTOFF_V", 3.0)

    if voltage is not None and voltage < emergency_v:
        days = 7
        print(f"EMERGENCY: {voltage}V below {emergency_v}V cutoff "
              f"— sleeping {days} days to protect battery.")
        machine.deepsleep(days * 24 * 60 * 60 * 1000)

    duration_ms = get_sleep_seconds(tier) * 1000
    mins = get_sleep_seconds(tier) // 60
    print(f"Sleeping for {mins} minutes (tier {tier}). Goodbye.")
    machine.deepsleep(duration_ms)


# ---------------------------------------------------------------------------
# Main entry point — called from main.py
# ---------------------------------------------------------------------------
def run(send_alert):
    """
    Read battery, determine tier, log status, check for alerts.

    Returns (voltage, tier) so main.py can decide what to do next —
    for example skipping WiFi and cloud calls at tier 3 or 4.

    Does NOT call go_to_sleep. main.py calls that at the very end of
    the wake cycle once all other work is done.

    Usage in main.py:
        import power
        voltage, tier = power.run(send_alert=lambda msg: cloud.send_ntfy_alert(msg))
    """
    voltage       = read_battery_voltage()
    tier          = get_tier(voltage)
    previous_tier = get_last_tier()

    print(f"Battery:  {voltage}V")
    print(f"Tier:     {tier}  (previous: {previous_tier})")
    print(f"Sleep:    {get_sleep_seconds(tier) // 60} min")
    print(f"Commands: {'accepted' if commands_accepted(tier) else 'ignored'}")

    check_battery_alerts(voltage, tier, previous_tier, send_alert)

    return voltage, tier
