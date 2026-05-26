# hardware.py
# Smart Solar Irrigation System
#
# Responsibilities:
#   - A02YYUW: read water level distance over UART, convert to percentage
#   - ProbeWaterSensor: GP14 digital probe (backup dry-run protection)
#   - Pump: switch on/off via IRLZ44N MOSFET on GP15
#
# main.py calls the module-level functions at the bottom:
#   hardware.read_water_level_pct() → float or None
#   hardware.read_probe_sensor()    → True/False/None
#   hardware.run_pump(duration_s)   → int (actual seconds run)
#
# Sensors and pump are initialised once on first call and reused.
# Because the Pico restarts from scratch on each deep sleep wake,
# "once per wake cycle" is all that is ever needed.

from machine import Pin, UART
import time
import watchdog

from config import (
    UART_BAUD, UART_RX_PIN, UART_TIMEOUT_MS, SENSOR_BLIND_MM,
    SENSOR_DISTANCE_EMPTY_MM, SENSOR_DISTANCE_FULL_MM,
    PUMP_PIN, PUMP_CHECK_INTERVAL_S, PROBE_PIN,
)

# Maximum pump run time — hard cap applied here in hardware, regardless of
# what decisions.py or a manual command requests
_PUMP_MAX_S = 900

# ---------------------------------------------------------------------------
# A02YYUW — water level
# ---------------------------------------------------------------------------

class WaterLevel:
    """
    Driver for the A02YYUW ultrasonic distance sensor over UART.
    Converts raw distance (mm) to water level percentage using calibration
    values from config.py.
    """

    def __init__(self):
        self._uart    = UART(0, baudrate=UART_BAUD, rx=Pin(UART_RX_PIN))
        self._last_mm = None   # cached raw reading — available after read_percent()

    def _read_distance_mm(self):
        """
        Read one distance frame from the sensor.
        Protocol: 4-byte frame — 0xFF, high_byte, low_byte, checksum
        Checksum = (0xFF + high + low) & 0xFF
        Returns distance in mm, or None on timeout or invalid frame.
        """
        self._uart.read(self._uart.any())   # flush stale data
        buf      = bytearray()
        deadline = time.ticks_add(time.ticks_ms(), UART_TIMEOUT_MS)

        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            if self._uart.any():
                byte = self._uart.read(1)
                if not byte:
                    continue
                buf += byte
                # Discard bytes until we see the start byte 0xFF
                if len(buf) == 1 and buf[0] != 0xFF:
                    buf = bytearray()
                    continue
                if len(buf) == 4:
                    expected = (buf[0] + buf[1] + buf[2]) & 0xFF
                    if expected == buf[3]:
                        distance = buf[1] * 256 + buf[2]
                        return None if distance < SENSOR_BLIND_MM else distance
                    buf = bytearray()   # bad checksum — try again

        return None   # timed out

    def _read_median_mm(self, samples=3, delay_ms=120):
        """
        Take multiple readings and return the median — rejects single bad readings
        caused by condensation on the sensor face inside the water butt.
        """
        readings = []
        for _ in range(samples):
            d = self._read_distance_mm()
            if d is not None:
                readings.append(d)
            time.sleep_ms(delay_ms)
        if not readings:
            return None
        readings.sort()
        return readings[len(readings) // 2]

    def read_percent(self):
        """
        Read water level as a percentage (0-100%).
        Also caches the raw mm reading in self._last_mm for calibration logging.
        Returns None if the sensor gives no valid reading.

        Calibration:
            SENSOR_DISTANCE_FULL_MM  = distance when butt is full  (small number, e.g. 100mm)
            SENSOR_DISTANCE_EMPTY_MM = distance when butt is empty (large number, e.g. 800mm)
        """
        distance      = self._read_median_mm()
        self._last_mm = distance   # cache raw reading -- None if sensor failed

        if distance is None:
            print("Water level: no valid reading.")
            return None

        # Reject readings closer than full-mark minus 20mm — physically impossible,
        # likely condensation on the sensor face causing a spuriously short echo
        if distance < SENSOR_DISTANCE_FULL_MM - 20:
            print(f"Water level: {distance}mm rejected (closer than full mark minus 20mm)")
            self._last_mm = None
            return None

        pct = ((SENSOR_DISTANCE_EMPTY_MM - distance) /
               (SENSOR_DISTANCE_EMPTY_MM - SENSOR_DISTANCE_FULL_MM) * 100.0)
        pct = max(0.0, min(100.0, pct))
        print(f"Water level: {distance}mm -> {pct:.1f}%")
        return round(pct, 1)


# ---------------------------------------------------------------------------
# Probe water sensor (metal prongs from original AnseTo kit)
# ---------------------------------------------------------------------------

class ProbeWaterSensor:
    """
    Simple conductivity sensor using the two metal probes from the original
    AnseTo kit. When water bridges both probes, current flows and the GPIO
    pin reads LOW. Dry = HIGH (held up by the Pico's internal pull-up).

    Wiring:
        One probe → GP14 (PROBE_PIN)
        Other probe → GND
        No external resistor needed — Pico's internal pull-up handles it.

    Position the probes at your WATER_PUMP_CUTOFF_PCT depth so they trigger
    before the pump runs dry.
    """

    def __init__(self):
        self._pin = Pin(PROBE_PIN, Pin.IN, Pin.PULL_UP)

    def water_present(self):
        """
        Returns True if water is touching both probes (pin reads LOW).
        Returns False if the probes are dry (pin reads HIGH).
        """
        return self._pin.value() == 0


# ---------------------------------------------------------------------------
# Pump
# ---------------------------------------------------------------------------

class Pump:
    """
    Controls the pump via the IRLZ44N MOSFET on GP15.
    The MOSFET gate pull-down resistor ensures the pump stays off
    if the Pico resets unexpectedly.
    """

    def __init__(self):
        self._pin = Pin(PUMP_PIN, Pin.OUT)
        self._pin.off()   # ensure pump is off on init

    def run(self, duration_s, safety_check=None):
        """
        Run the pump for duration_s seconds, checking safety sensors mid-run.

        Every PUMP_CHECK_INTERVAL_S seconds, safety_check() is called.
        If it returns False the pump stops immediately.

        safety_check should be a callable returning True (safe) or False (stop).
        Pass None to skip mid-run checks entirely.

        Returns the actual number of seconds the pump ran.
        If shorter than duration_s, a safety trigger fired during the run.
        """
        duration_s = min(int(duration_s), _PUMP_MAX_S)
        if duration_s <= 0:
            print("Pump: duration is 0 — not running.")
            return 0

        print(f"Pump ON — target {duration_s}s")
        self._pin.on()

        start_ms  = time.ticks_ms()
        target_ms = time.ticks_add(start_ms, duration_s * 1000)
        stopped_by = "duration"

        while True:
            remaining_ms = time.ticks_diff(target_ms, time.ticks_ms())
            if remaining_ms <= 0:
                break
            sleep_ms = min(PUMP_CHECK_INTERVAL_S * 1000, remaining_ms)
            time.sleep_ms(sleep_ms)
            watchdog.feed()   # pump can run for up to 900s — must keep watchdog alive
            if safety_check is not None and not safety_check():
                stopped_by = "safety"
                break

        self._pin.off()
        actual_s = time.ticks_diff(time.ticks_ms(), start_ms) // 1000
        print(f"Pump OFF — ran {actual_s}s (stopped by: {stopped_by})")
        return actual_s


# ---------------------------------------------------------------------------
# Module-level singletons
# Initialised on first call, reused for the rest of the wake cycle.
# ---------------------------------------------------------------------------

_water = None
_pump  = None
_probe = None


def read_water_level_pct():
    """
    Read water level as a percentage (0-100).
    Also populates the internal mm cache so read_water_level_mm() can be
    called afterwards without triggering a second sensor read.
    Returns a float or None on failure.
    """
    global _water
    try:
        if _water is None:
            _water = WaterLevel()
        return _water.read_percent()
    except Exception as e:
        print(f"Water level init/read failed: {e}")
        return None


def read_water_level_mm():
    """
    Return the raw sensor distance in mm from the last read_water_level_pct() call.
    Call AFTER read_water_level_pct() -- no second sensor read is needed.
    Returns an int (mm) or None if the sensor gave no valid reading.
    Useful for remote calibration: log this value to InfluxDB and view in Grafana
    to find the correct SENSOR_DISTANCE_EMPTY_MM and SENSOR_DISTANCE_FULL_MM values.
    """
    global _water
    if _water is None:
        return None
    return _water._last_mm


def run_pump(duration_s, safety_check=None):
    """
    Run the pump for duration_s seconds with optional mid-run safety checks.
    safety_check is a callable returning True (safe) or False (stop now).
    Returns the actual number of seconds the pump ran (0 on error).
    """
    global _pump
    try:
        if _pump is None:
            _pump = Pump()
        return _pump.run(duration_s, safety_check=safety_check)
    except Exception as e:
        print(f"Pump init/run failed: {e}")
        return 0


def read_probe_sensor():
    """
    Read the metal probe water sensor.
    Returns True if water is present, False if dry, None on error.
    """
    global _probe
    try:
        if _probe is None:
            _probe = ProbeWaterSensor()
        result = _probe.water_present()
        print(f"Probe sensor: {'water present' if result else 'DRY'}")
        return result
    except Exception as e:
        print(f"Probe sensor read failed: {e}")
        return None  # None = unknown, treated as safe (don't block watering)
