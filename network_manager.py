# network.py
# Smart Solar Irrigation System
#
# Responsibilities:
#   - Connect to WiFi using credentials from secrets.py
#   - Disconnect and shut down the CYW43 WiFi chip cleanly before deep sleep
#
# Always call disconnect() before power.go_to_sleep().
# Failing to do so can cause the CYW43 chip to misbehave on the next wake cycle.
#
# Usage in main.py:
#   import network_manager as net   (avoid naming clash with MicroPython's network module)
#   wlan, rssi = net.connect()
#   ... do work ...
#   net.disconnect()
#   power.go_to_sleep(tier)

import network
import time
import watchdog
import secrets
from config import WIFI_TIMEOUT_S


def connect():
    """
    Activate WiFi and connect using credentials from secrets.py.

    Returns (wlan, rssi):
        wlan — the WLAN object, needed for status checks elsewhere
        rssi — signal strength in dBm (e.g. -62), logged to InfluxDB

    Raises RuntimeError if the connection times out.
    main.py should catch this and go back to sleep rather than hanging.
    """
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    # Already connected (e.g. called twice by accident) — return immediately
    if wlan.isconnected():
        rssi = wlan.status("rssi")
        print(f"WiFi already connected. RSSI: {rssi} dBm")
        return wlan, rssi

    print(f"Connecting to {secrets.WIFI_SSID}...")
    wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)

    # Wait up to WIFI_TIMEOUT_S seconds for connection
    # Feed the watchdog on each iteration — the loop can run for 20 seconds
    # which would exceed the 8-second watchdog timeout without feeding.
    for elapsed in range(WIFI_TIMEOUT_S):
        if wlan.isconnected():
            break
        time.sleep(1)
        watchdog.feed()
    else:
        # Timed out — shut down cleanly before raising
        wlan.active(False)
        raise RuntimeError(
            f"WiFi failed to connect after {WIFI_TIMEOUT_S}s. "
            f"Check SSID and password in secrets.py."
        )

    rssi = wlan.status("rssi")
    ip   = wlan.ifconfig()[0]
    print(f"WiFi connected. IP: {ip}   RSSI: {rssi} dBm ({_rssi_label(rssi)})")
    return wlan, rssi


def disconnect():
    """
    Disconnect from WiFi and shut down the CYW43 chip.

    Call this at the end of every wake cycle, before power.go_to_sleep().
    The short delay gives the chip time to power down fully before the
    Pico enters deep sleep.
    """
    wlan = network.WLAN(network.STA_IF)
    if wlan.active():
        wlan.disconnect()
        wlan.active(False)
        time.sleep_ms(200)   # allow CYW43 to power down before deep sleep
        print("WiFi disconnected.")


def _rssi_label(rssi):
    """Plain English label for WiFi signal strength. Used in status prints only."""
    if rssi >= -50: return "Excellent"
    if rssi >= -60: return "Good"
    if rssi >= -70: return "Fair"
    if rssi >= -80: return "Weak"
    return "Poor"
