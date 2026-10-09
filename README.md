# Southwest Airlines Automated Check-In Application

An automated check-in engine for Southwest Airlines flights featuring **Stealth Headless Browser Automation (Playwright)**, **comprehensive diagnostic logging**, **sub-second NTP clock synchronization**, **exact-time trigger ($T$)**, **0.5-second retry cadence (or immediate)**, and support for **multiple passengers/confirmation numbers**.

---

## Key Features

- **Stealth Headless Browser Automation (Playwright):** Interacts directly with the official desktop web check-in page (`https://www.southwest.com/air/check-in/`) using realistic browser context, anti-automation overrides, and genuine session cookies to circumvent cold HTTP 403 bot blocks.
- **Exact-Time Check-In Trigger ($T$):** Pre-loads tabs and form fields ahead of time, then triggers the first submit click at the **exact second of check-in ($T$)**.
- **0.5-Second Cadence / Immediate Retry:** Retries every **0.5 seconds** (500ms), or **immediately after the current attempt finishes** if network/page latency takes longer than 0.5s.
- **Immediate Execution for Past Check-In Times:** If you start the tool after the check-in time has already passed, it skips the countdown and executes check-in attempts immediately for the duration of your configured polling window.
- **Comprehensive Diagnostic Logging:** Automatically records all network transactions, headers, status codes, DOM error banners, and execution timings to [`checkin_debug.log`](file:///workspace/swa_checkin/checkin_debug.log).
- **Multi-Passenger & Multi-Reservation Support:** Enter multiple confirmation numbers (PNR) and passenger names (First Name + Surname).
- **Unified Check-In Time & Timezone:** Configure a single check-in time in **24-hour format** (`YYYY-MM-DD HH:MM:SS` or `YYYY-MM-DD HH:MM`) with IANA time zones (e.g., `America/Chicago`, `America/New_York`, `America/Los_Angeles`).
- **NTP Atomic Time Synchronization:** Queries public NTP servers (`time.google.com`, `pool.ntp.org`, `time.cloudflare.com`) to eliminate local computer clock drift down to milliseconds.
- **Interactive Terminal UI:** Built with `rich` and `prompt_toolkit`, featuring live countdown timers, status panels, and boarding position tables.
- **Instant Dry-Run Simulator:** Test the exact countdown, trigger, 0.5s retries, and boarding pass assignment immediately.

---

## Quick Start

### 1. Launch the Application
Run the helper script:
```bash
./run.sh
```
Or run directly with Python:
```bash
source .venv/bin/activate
python3 main.py
```

### 2. Main Menu Options
```text
[1] ➕ Add Passenger & Confirmation Number (Supports multiple)
[2] ❌ View / Remove Reservations
[3] ⏰ Set Check-in Date, Time & Timezone (24-Hour Format)
[4] 🚀 Start Live Check-In Monitor (Armed for Target Time)
[5] 🧪 Run Instant Dry-Run / Test Simulation (Starts in 8s)
[6] ⚙️ Engine & Timing Settings (Browser vs. API)
[7] 📜 View Diagnostic Logs (checkin_debug.log)
[0] 🚪 Exit
```

---

## Engine Options (Menu Option [6])

1. **Stealth Headless Browser (Playwright - Default & Recommended):**
   - Navigates to `https://www.southwest.com/air/check-in/` in a headless Chromium instance.
   - Pre-fills confirmation number and passenger names.
   - Clicks "Check In" at exact time $T$ and retries every 0.5s (or immediately) until confirmed.
   - Saves boarding pass screenshots to disk upon completion.
2. **Headed Browser (Visible Window):**
   - Runs the browser with visible UI so you can observe the exact page rendering and interaction.
3. **Direct HTTP API (`curl_cffi`):**
   - Lightweight direct HTTP client for low-overhead setups.

---

## Diagnosing Issues

All actions and errors are logged to [`checkin_debug.log`](file:///workspace/swa_checkin/checkin_debug.log). You can view the last 40 lines directly inside the application via **Menu Option [7]**, or in a terminal with:
```bash
tail -f checkin_debug.log
```
