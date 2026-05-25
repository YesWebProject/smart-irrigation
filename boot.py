# boot.py
# Smart Solar Irrigation System
#
# Runs on every boot before main.py.
# Enables WebREPL so you can connect Thonny (or a browser) to the Pico
# over WiFi without a USB cable — useful for editing files remotely.
#
# First-time WebREPL setup (do this once via USB):
#   1. Open Thonny, connect via USB
#   2. In the REPL type:  import webrepl; webrepl.configure()
#   3. Enter a password when prompted (remember it — you'll need it to connect)
#   4. This creates webrepl_cfg.py on the Pico with your hashed password
#   5. After that, WebREPL starts automatically on every boot
#
# Connecting to WebREPL:
#   - Thonny:   Tools → Options → Interpreter → MicroPython (WebREPL)
#               Enter ws://192.168.x.x (your Pico's IP) and password
#   - Browser:  http://micropython.org/webrepl/  (enter same IP and password)
#   - mpremote: mpremote connect ws:192.168.x.x
#
# WebREPL is only accessible while the Pico is awake and on WiFi.
# During deep sleep it is not reachable — that's expected.
#
# To find the Pico's IP address: check your router's device list, or
# run this in Thonny REPL (via USB) after WiFi connects:
#   import network; print(network.WLAN(network.STA_IF).ifconfig())
#
# Security note: WebREPL password is hashed in webrepl_cfg.py.
# Do not share that file. Use a strong password if your WiFi is accessible
# outside your home.

try:
    import webrepl
    webrepl.start()
    # webrepl.start() is silent if webrepl_cfg.py doesn't exist yet.
    # Once you've run webrepl.configure() it will activate on every boot.
except Exception as e:
    # Never let a WebREPL error stop the device from booting
    print(f"boot.py: WebREPL start error (non-fatal): {e}")
