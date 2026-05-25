# ota.py
# Smart Solar Irrigation System
#
# Over-the-air update system.
# Checks a manifest.json file on GitHub once per day (called from main.py
# during new-day detection). Downloads changed code files and reboots.
#
# Protected files — never overwritten:
#   config.py       (your calibration values and settings)
#   secrets.py      (WiFi and API credentials)
#   webrepl_cfg.py  (WebREPL password hash)
#
# How it works:
#   1.  Fetch manifest.json from OTA_MANIFEST_URL in config.py
#   2.  Compare remote CODE_VERSION against local CODE_VERSION
#   3.  If newer: download each listed file to a .new temp copy
#   4.  If ALL downloads succeed: rename temps → live files, reboot
#   5.  If ANY download fails: delete all temps, leave device unchanged
#
# Setting up:
#   - Create a public GitHub repo and push all your .py files to it
#   - Add manifest.json (template below) to the repo root
#   - Set OTA_MANIFEST_URL in config.py to the raw GitHub URL
#   - Increment CODE_VERSION in config.py each time you push new code

import urequests
import ujson
import os
import machine
import time
import watchdog

from config import OTA_ENABLED, OTA_MANIFEST_URL, CODE_VERSION

# Files that are NEVER replaced by OTA — user data or credentials
_PROTECTED = frozenset({"config.py", "secrets.py", "webrepl_cfg.py", "state.bin"})


def cleanup_temp_files():
    """
    Remove any stale .new temp files left from a previous interrupted update.
    Called once at startup before anything else runs.
    """
    try:
        stale = [f for f in os.listdir("/") if f.endswith(".new")]
        for f in stale:
            os.remove(f)
            print(f"OTA: removed stale temp file: {f}")
    except Exception as e:
        print(f"OTA: cleanup error: {e}")


def check_and_apply(send_alert=None):
    """
    Check GitHub for a newer version. Download and apply if found.

    send_alert is a callable (cloud.send_ntfy_alert) passed in from main.py
    so ota.py doesn't need to import cloud directly.

    Returns True if an update was applied — device will reset() before
    returning so the caller never actually sees True.
    Returns False if no update was needed or the check/download failed.
    """
    if not OTA_ENABLED:
        print("OTA: disabled in config.")
        return False

    # ── Fetch manifest ───────────────────────────────────────────────────────
    try:
        print(f"OTA: checking manifest at {OTA_MANIFEST_URL}")
        r = urequests.get(OTA_MANIFEST_URL, timeout=10)
        watchdog.feed()
        if r.status_code != 200:
            print(f"OTA: manifest fetch failed (HTTP {r.status_code})")
            r.close()
            return False
        manifest = ujson.loads(r.text)
        r.close()
        watchdog.feed()
    except Exception as e:
        print(f"OTA: manifest error: {e}")
        return False

    # ── Version check ────────────────────────────────────────────────────────
    remote_version = manifest.get("version", 0)
    if remote_version <= CODE_VERSION:
        print(f"OTA: up to date (local v{CODE_VERSION}, remote v{remote_version})")
        return False

    print(f"OTA: update available — v{CODE_VERSION} → v{remote_version}")
    if send_alert:
        send_alert(
            f"OTA update starting: v{CODE_VERSION} → v{remote_version}. "
            "Device will reboot when complete."
        )

    # ── Download files ───────────────────────────────────────────────────────
    base_url   = manifest.get("base_url", "")
    file_list  = manifest.get("files", [])
    downloaded = []

    for filename in file_list:
        if filename in _PROTECTED:
            print(f"OTA: skipping protected file: {filename}")
            continue

        tmp_name = filename + ".new"
        url      = base_url + filename

        try:
            print(f"OTA: downloading {filename}...")
            r = urequests.get(url, timeout=20)
            watchdog.feed()

            if r.status_code != 200:
                print(f"OTA: FAILED to download {filename} (HTTP {r.status_code})")
                r.close()
                _abort(downloaded, send_alert, filename)
                return False

            # Write to temp file
            with open(tmp_name, "w") as f:
                f.write(r.text)
            r.close()
            watchdog.feed()

            downloaded.append(filename)
            print(f"OTA: downloaded {filename}")

        except Exception as e:
            print(f"OTA: error downloading {filename}: {e}")
            _abort(downloaded, send_alert, str(e))
            return False

    # ── Apply — rename temps to live files ───────────────────────────────────
    # All downloads succeeded. Rename atomically.
    print("OTA: applying update...")
    failed_apply = []

    for filename in downloaded:
        try:
            os.rename(filename + ".new", filename)
            print(f"OTA: applied {filename}")
        except Exception as e:
            print(f"OTA: could not apply {filename}: {e}")
            failed_apply.append(filename)

    if failed_apply:
        msg = f"OTA: partial update — some files could not be applied: {failed_apply}"
        print(msg)
        if send_alert:
            send_alert(msg + " — rebooting anyway.")
    else:
        print("OTA: all files applied successfully.")

    if send_alert:
        send_alert(
            f"OTA update complete: v{remote_version} applied. Rebooting now."
        )

    time.sleep(2)    # give ntfy time to send before reset
    machine.reset()  # does not return
    return True      # unreachable


def _abort(downloaded_so_far, send_alert, reason):
    """Clean up temp files and notify after a failed download."""
    for filename in downloaded_so_far:
        try:
            os.remove(filename + ".new")
        except Exception:
            pass
    msg = f"OTA update FAILED ({reason}) — device unchanged."
    print(msg)
    if send_alert:
        send_alert(msg)
