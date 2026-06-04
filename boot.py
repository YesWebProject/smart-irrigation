# boot.py
# Smart Solar Irrigation System
#
# Runs on every boot before main.py.
# Starts WebREPL so Thonny can connect over WiFi during a stay_awake cycle.
# Password is read from secrets.py (WEBREPL_PASS) — never hardcoded.
#
# To connect with Thonny:
#   1. Send 'stay_awake' via ntfy — the device will reply with its IP.
#   2. Thonny → Run → Configure interpreter → MicroPython (WebREPL)
#   3. URL: ws://<IP>:8266   Password: WEBREPL_PASS from secrets.py

import webrepl
try:
    import secrets
    webrepl.start(password=secrets.WEBREPL_PASS)
except Exception:
    # If secrets.py has no WEBREPL_PASS, start with a default so boot doesn't crash.
    # Change this by adding WEBREPL_PASS to secrets.py and rebooting.
    webrepl.start(password="irrigation")
