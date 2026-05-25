# watchdog.py
# Smart Solar Irrigation System
#
# Hardware watchdog timer — resets the Pico automatically if the code hangs.
# Essential for unattended outdoor use: a stuck HTTP call, a frozen sensor
# read, or any unexpected loop will cause a clean reset and fresh wake cycle.
#
# Usage:
#   import watchdog
#   watchdog.init()        — call once near the start of main.py
#   watchdog.feed()        — call regularly to prevent reset
#
# The WDT cannot be stopped once started. It is automatically disabled
# during deep sleep and restarted fresh on the next wake cycle.
#
# On RP2350, the hardware watchdog maximum timeout is approximately 8.3
# seconds. We use 8000ms to stay safely within that limit. Code must call
# watchdog.feed() at least once every 8 seconds during the wake cycle.

from machine import WDT

_wdt     = None
_timeout = 8000   # milliseconds — do not increase above 8300 on RP2350


def init():
    """
    Start the hardware watchdog. Call once near the beginning of main.py,
    after the battery tier has been read (so a first-boot ADC issue doesn't
    cause an infinite reset loop before we can diagnose it).
    """
    global _wdt
    _wdt = WDT(timeout=_timeout)
    print(f"Watchdog started ({_timeout}ms timeout)")


def feed():
    """
    Reset the watchdog countdown. Call this at every major step in main.py,
    inside any loop that runs longer than ~7 seconds (WiFi connect, pump run),
    and after each cloud call.
    If the watchdog has not been initialised, this is a no-op.
    """
    if _wdt is not None:
        _wdt.feed()
