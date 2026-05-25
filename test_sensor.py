# test_sensor.py
# Smart Solar Irrigation System — Hardware Test Script
# Tests: A02YYUW water level sensor, WiFi signal strength, ntfy alerts, InfluxDB logging
#
# HOW TO USE:
#   1. Copy this file and secrets.py to the Pico using Thonny
#   2. Run this file from Thonny
#   3. Subscribe to your ntfy topic on your phone to see readings remotely
#   4. Press Stop in Thonny to end the test
#
# WHAT TO NOTE DOWN:
#   - Distance reading (mm) when water butt is EMPTY → SENSOR_DISTANCE_EMPTY_MM in config.py
#   - Distance reading (mm) when water butt is FULL  → SENSOR_DISTANCE_FULL_MM in config.py

import network
import urequests
import time
from machine import UART, Pin
import secrets

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
READ_INTERVAL_S = 30    # seconds between each reading
UART_BAUD       = 9600  # A02YYUW baud rate
UART_RX_PIN     = 1     # GP1 — sensor TX → Pico RX
SENSOR_BLIND_MM = 30    # readings below 30mm are inside the sensor blind spot

# ---------------------------------------------------------------------------
# WiFi
# ---------------------------------------------------------------------------
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    print("Connecting to WiFi...")
    wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)

    for _ in range(20):         # try for up to 20 seconds
        if wlan.isconnected():
            rssi = wlan.status("rssi")
            print(f"Connected. IP: {wlan.ifconfig()[0]}   RSSI: {rssi} dBm ({rssi_label(rssi)})")
            return wlan
        time.sleep(1)
        print("  waiting...")

    raise RuntimeError("WiFi connection failed — check SSID and password in secrets.py")


def rssi_label(rssi):
    """Plain English label for WiFi signal strength."""
    if rssi >= -50: return "Excellent"
    if rssi >= -60: return "Good"
    if rssi >= -70: return "Fair"
    if rssi >= -80: return "Weak"
    return "Poor"


# ---------------------------------------------------------------------------
# A02YYUW sensor
# ---------------------------------------------------------------------------
def read_sensor(uart):
    """
    Read one distance measurement from the A02YYUW sensor.
    The sensor sends a 4-byte frame: 0xFF, high_byte, low_byte, checksum
    Distance in mm = high_byte * 256 + low_byte
    Returns distance in mm, or None if reading is invalid or timed out.
    """
    # Flush any data already sitting in the buffer
    uart.read(uart.any())

    buf = bytearray()
    deadline = time.ticks_add(time.ticks_ms(), 1000)   # 1 second timeout

    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if uart.any():
            byte = uart.read(1)
            if byte:
                buf += byte

                # Wait for the start byte (0xFF) before collecting data
                if len(buf) == 1 and buf[0] != 0xFF:
                    buf = bytearray()
                    continue

                # Once we have all 4 bytes, check the checksum
                if len(buf) == 4:
                    expected_checksum = (buf[0] + buf[1] + buf[2]) & 0xFF
                    if expected_checksum == buf[3]:
                        distance = buf[1] * 256 + buf[2]
                        if distance < SENSOR_BLIND_MM:
                            return None     # too close — inside blind spot
                        return distance
                    else:
                        buf = bytearray()   # bad checksum, try again

    return None     # timed out with no valid reading


# ---------------------------------------------------------------------------
# ntfy
# ---------------------------------------------------------------------------
def post_ntfy(message):
    """Send a message to your ntfy topic."""
    try:
        url = f"https://ntfy.sh/{secrets.NTFY_TOPIC}"
        r = urequests.post(url, data=message, headers={"Content-Type": "text/plain"})
        r.close()
        print(f"ntfy sent: {message}")
    except Exception as e:
        print(f"ntfy failed: {e}")


# ---------------------------------------------------------------------------
# InfluxDB Cloud
# ---------------------------------------------------------------------------
def post_influx(distance_mm, rssi):
    """Write a test data point to InfluxDB Cloud."""
    try:
        # Build line protocol — distance is optional if sensor gave no reading
        if distance_mm is not None:
            line = f"test_sensor distance_mm={distance_mm}i,rssi={rssi}i"
        else:
            line = f"test_sensor rssi={rssi}i"

        url = (
            f"{secrets.INFLUX_URL}/api/v2/write"
            f"?org={secrets.INFLUX_ORG}"
            f"&bucket={secrets.INFLUX_BUCKET}"
            f"&precision=ns"
        )
        headers = {
            "Authorization": f"Token {secrets.INFLUX_TOKEN}",
            "Content-Type": "text/plain; charset=utf-8"
        }
        r = urequests.post(url, data=line, headers=headers)
        r.close()
        print("InfluxDB: posted OK")
    except Exception as e:
        print(f"InfluxDB failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
print("=== Smart Irrigation — Sensor Test ===")
print(f"Readings every {READ_INTERVAL_S} seconds. Press Stop to end.")
print()

# Initialise the UART for the A02YYUW sensor
uart = UART(0, baudrate=UART_BAUD, rx=Pin(UART_RX_PIN))

# Connect to WiFi
wlan = connect_wifi()

# Send a startup message to phone so you know it is running
startup_rssi = wlan.status("rssi")
post_ntfy(f"Test started | WiFi: {startup_rssi} dBm ({rssi_label(startup_rssi)})")

# Post startup point to InfluxDB too
post_influx(None, startup_rssi)

# Main test loop
reading_count = 0

while True:
    reading_count += 1
    print(f"\n--- Reading #{reading_count} ---")

    # Read sensor
    distance = read_sensor(uart)

    # Get current WiFi signal strength
    rssi = wlan.status("rssi")

    # Build message strings
    if distance is not None:
        sensor_str = f"Distance: {distance}mm"
        print(f"Sensor: {distance}mm")
    else:
        sensor_str = "Sensor: no reading"
        print("Sensor: no valid reading (check wiring if this persists)")

    wifi_str = f"WiFi: {rssi}dBm ({rssi_label(rssi)})"
    print(f"WiFi signal: {rssi} dBm ({rssi_label(rssi)})")

    # Post to phone and InfluxDB
    message = f"#{reading_count} | {sensor_str} | {wifi_str}"
    post_ntfy(message)
    post_influx(distance, rssi)

    # Wait before next reading
    print(f"Next reading in {READ_INTERVAL_S}s...")
    time.sleep(READ_INTERVAL_S)
