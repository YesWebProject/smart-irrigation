# Smart Solar Irrigation System — Project Context

## What this is
A solar-powered automatic irrigation system for a garden water butt in Devon (TQ12 area).
A Raspberry Pi Pico 2W runs MicroPython, wakes from deep sleep periodically, reads sensors,
fetches weather data, and decides whether to water. All cloud services are fully set up.

## Hardware
- **MCU:** Raspberry Pi Pico 2W (RP2350 chip, MicroPython v1.28.0)
- **Sensor:** A02YYUW ultrasonic water level sensor — UART on GP1 (RX), 9600 baud
- **Probe:** Metal conductivity sensor — GP14, internal pull-up, LOW = water present
- **Pump:** 12V pump via IRLZ44N MOSFET on GP15
- **Battery:** Single 18650 cell via AnseTo solar charging module
- **Voltage divider:** Equal 10kΩ/10kΩ resistors on GP26 (ADC) — ratio 0.5
- **Water butt:** ~50cm wide × 1m tall, sensor mounted in lid pointing straight down

## Cloud stack (all configured and working)
- **InfluxDB Cloud** — bucket: `irrigation`, Flux query language
- **Grafana Cloud** — connected to InfluxDB, dashboard imported
- **ntfy.sh** — alerts topic and commands topic configured (long random names in secrets.py)
- **GitHub** — repo: `YesWebProject/smart-irrigation` (public, OTA source)
- **GitHub Gist** — `irrigation_config.json` — runtime config overrides

## File responsibilities

| File | Where it lives | Purpose |
|---|---|---|
| `main.py` | Pico + GitHub | Wake cycle orchestrator — runs top to bottom on every wake |
| `hardware.py` | Pico + GitHub | A02YYUW sensor (with median filter), probe sensor, pump driver |
| `decisions.py` | Pico + GitHub | Watering logic — all conditions evaluated here |
| `power.py` | Pico + GitHub | Battery voltage, tier classification, state.bin, deep sleep |
| `network_manager.py` | Pico + GitHub | WiFi connect/disconnect |
| `cloud.py` | Pico + GitHub | NTP sync (3 retries), ntfy, InfluxDB, Open-Meteo, Gist fetch |
| `watchdog.py` | Pico + GitHub | Hardware watchdog — 8 second timeout |
| `ota.py` | Pico + GitHub | OTA update system |
| `boot.py` | Pico + GitHub | Starts WebREPL (password from secrets.WEBREPL_PASS); add that key to secrets.py |
| `manifest.json` | GitHub only | OTA version manifest |
| `irrigation_config.json` | GitHub + Gist | Reference copy of the live Gist runtime config — NOT fetched by the Pico (the Pico reads the Gist URL); update the live gist.github.com copy by hand |
| `config.py` | Pico + PC only | Settings and calibration — NEVER in GitHub, NEVER OTA'd |
| `secrets.py` | Pico + PC only | Credentials — NEVER in GitHub, NEVER OTA'd |
| `CLAUDE.md` | PC only | This file |
| `.gitignore` | PC + GitHub | Excludes secrets.py and junk |

## OTA update system
- Pico checks `manifest.json` on GitHub every wake cycle (picks up updates within one sleep interval)
- Version tracked in `_ota_version` file on Pico flash (NOT config.py — that's protected)
- To push an update: edit files, bump `"version"` in manifest.json, git push
- Protected files (NEVER overwritten by OTA): `config.py`, `secrets.py`, `state.bin`, `_ota_version`
- `config.py` changes always need manual Thonny upload — plan accordingly

**Current manifest version: 23**

## Battery power tiers
| Voltage | Tier | Sleep | Commands |
|---|---|---|---|
| > 3.9V | 1 | 30 min | Accepted |
| 3.6–3.9V | 2 | 60 min | Accepted |
| 3.3–3.6V | 3 | 4 hours | Ignored |
| 3.0–3.3V | 4 | 24 hours | Ignored |
| < 3.0V | Emergency | 7 days | Hard cutoff |

## ntfy commands (sent to commands topic)
| Command | Effect |
|---|---|
| `water_now` | Run pump immediately |
| `snooze` | Skip today's scheduled watering |
| `cancel` | Clear a pending water_now |
| `test` | 5-minute dense readings (15s interval) — for signal/sensor verification; reports a probe wet/dry summary |
| `probe_test` | One-shot probe diagnostic — 5 pulse reads (force-reads even when the probe is disabled), replies with wet/dry tally + current water level for cross-check |
| `status` | Immediate ntfy reply with hardware/config summary: battery V/tier/sleep, WiFi dBm, NTP, water level, probe, watered-today flag, pump config, commands received this wake |
| `stay_awake` | Keep Pico awake 20 min with WebREPL live; replies via ntfy with IP address for Thonny connection (`ws://<IP>:8266`) |
| `sleep` | Exit a stay_awake session early and resume normal sleep |

## Persistent state (state.bin on Pico flash)
11-byte binary file — survives deep sleep. Managed entirely by power.py.
- Byte 0: last battery tier
- Byte 1: watered today flag
- Bytes 2-5: reserved
- Bytes 6-9: today's sunrise unix timestamp
- Byte 10: post-water fast monitoring cycles remaining

## Watering logic
- Triggers once per day, 30 minutes before sunrise (configurable via Gist)
- Skips if: already watered today, rain skip (≥60% probability AND ≥5mm predicted), frost forecast, probe dry (unless probe disabled via Gist), water level at/below the pump cutoff (set by `sensor_distance_pump_mm`, else `pump_cutoff_pct` default 5%), battery Tier 3+
- Offline backup: if WiFi fails, uses stored sunrise + RTC time to water at base duration
- All thresholds overridable via GitHub Gist without code changes — and as of v14 the
  overrides are actually applied at runtime (every module reads Gist-overridable constants via
  `getattr(_cfg, ...)` at call time, not `from config import X` at module load)

## Gist remote config
Edit `irrigation_config.json` on gist.github.com to change settings without code changes.
Pico fetches it every wake cycle. Changes take effect within 30 minutes.
A reference copy of the JSON lives in the repo as `irrigation_config.json` — keep it in sync by
hand; the Pico reads the live gist, not the repo file.
Key fields: `watering.base_duration_s`, `watering.sunrise_offset_min`,
`watering.rain_probability_threshold_pct`, `watering.rain_amount_threshold_mm`,
`watering.pump_min_runtime_for_check_s`, `watering.pump_min_drop_mm`,
`water_level.sensor_distance_empty_mm`, `water_level.sensor_distance_full_mm`,
`water_level.sensor_distance_pump_mm` (measured sensor distance at the pump intake — sets the
dry-run safety cutoff directly; takes precedence over `pump_cutoff_pct` when >0, 0 = use the %),
`battery.emergency_cutoff_v`, `sleep.tier1_interval_min`,
`sensors.probe_sensor_enabled` (set false to disable the probe dry check — useful when
low-conductivity rainwater causes false DRY readings)

## Known hardware notes
- Battery voltage reads ~0.3V high when USB is connected (AnseTo charging)
- A02YYUW gets condensation on face (water butt humidity) — median filter in hardware.py rejects single bad readings
- WiFi signal: -76 dBm (marginal but workable via extender)
- NTP sync: retries 3 times with 2s delay — occasional failures normal at this signal level
- Probe (GP14) corrodes from DC bias through water. As of v18 it is pulse-powered (pull-up on ~1ms
  per read, idle high-Z) and is only energised in normal cycles when `PROBE_SENSOR_ENABLED` is true.
  Diagnostics (`test`, `probe_test`) force-read it regardless, to check it while installed.

## Git workflow
```bash
# Standard update
git add <changed files> manifest.json
git commit -m "vN: description"
git push
# Pico OTA's automatically next morning, or delete state.bin to force immediately
```

## What requires Thonny (USB)
- Uploading `config.py` after changes (protected from OTA)
- Direct debugging / running test_sensor.py
- Checking Pico filesystem
- Manual file management (e.g. deleting state.bin to force OTA)
