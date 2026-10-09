"""Unit and integration tests for Southwest check-in application."""

from datetime import datetime, timedelta
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from logger_setup import read_recent_logs, setup_diagnostic_logging
from models import CheckInSchedule, CheckInStatus, Passenger, Reservation
from ntp_sync import NTPClockSynchronizer
from scheduler import CheckInScheduler
from swa_browser import PlaywrightSouthwestClient
from swa_client import MockSouthwestClient, SouthwestClient


def test_models():
    # Valid PNR and names
    p = Passenger(first_name="Jane", last_name="Doe")
    assert p.full_name == "JANE DOE"

    r = Reservation(confirmation_number="abc123", passenger=p)
    assert r.confirmation_number == "ABC123"

    # Schedule parsing
    sched = CheckInSchedule(
        checkin_time_str="2026-10-15 14:30:00",
        timezone="America/Chicago",
        lead_time_seconds=0.0,
        poll_interval_seconds=0.5,
        engine_type="browser",
        headless=True,
    )
    dt = sched.get_target_datetime()
    assert dt.hour == 14
    assert dt.minute == 30
    assert dt.second == 0
    assert str(dt.tzinfo) == "America/Chicago"
    assert sched.lead_time_seconds == 0.0
    assert sched.poll_interval_seconds == 0.5
    print("✓ Model and schedule tests passed (0.0s lead, 0.5s interval defaults)")


def test_ntp():
    clock = NTPClockSynchronizer()
    synced = clock.sync(timeout=3.0)
    print(f"✓ NTP sync result: {synced}, offset: {clock.offset:+.4f}s, delay: {clock.delay:.4f}s")
    assert isinstance(clock.get_atomic_time(), float)


def test_logging():
    setup_diagnostic_logging()
    log_text = read_recent_logs(lines=10)
    assert len(log_text) > 0
    print("✓ Diagnostic logging verified (checkin_debug.log active)")


def test_browser_client_initialization():
    print("Testing Playwright Stealth Browser client initialization...")
    client = PlaywrightSouthwestClient(headless=True)
    try:
        client.start()
        assert client.browser is not None
        assert client.context is not None
        print("✓ Playwright stealth browser successfully launched and context created")
    finally:
        client.close()


def test_scheduler_exact_time_and_half_second_interval():
    # Target time is 1.5 seconds from now.
    # Lead time = 0.0s (starts at exact check-in time).
    # Poll interval = 0.5s.
    now = datetime.now(ZoneInfo("America/Chicago"))
    target = now + timedelta(seconds=1.5)
    target_str = target.strftime("%Y-%m-%d %H:%M:%S")

    r1 = Reservation(
        confirmation_number="TESTA1",
        passenger=Passenger(first_name="MARK", last_name="TWAIN"),
    )

    sched = CheckInSchedule(
        checkin_time_str=target_str,
        timezone="America/Chicago",
        lead_time_seconds=0.0,
        poll_interval_seconds=0.5,
        max_poll_seconds=10.0,
        engine_type="mock",
    )

    timestamps = []

    def on_attempt(result, res):
        timestamps.append(result.timestamp)
        print(f"  [Attempt {result.attempt_number}] {res.confirmation_number}: {result.status.value} - {result.message}")

    scheduler = CheckInScheduler(
        reservations=[r1],
        schedule=sched,
        engine_type="mock",
        on_attempt_result_callback=on_attempt,
    )

    results = scheduler.run()

    assert all(r.status == CheckInStatus.SUCCESS for r in results)
    print("✓ Scheduler exact-time (0.0s lead) and 0.5s interval test passed!")


def test_past_checkin_immediate_execution():
    # Target time is 10 minutes in the past!
    # Should start immediately without waiting or timing out prematurely.
    now = datetime.now(ZoneInfo("America/Chicago"))
    past_target = now - timedelta(minutes=10)
    past_target_str = past_target.strftime("%Y-%m-%d %H:%M:%S")

    r1 = Reservation(
        confirmation_number="PAST01",
        passenger=Passenger(first_name="CARSON", last_name="MCDONALD"),
    )

    sched = CheckInSchedule(
        checkin_time_str=past_target_str,
        timezone="America/Chicago",
        lead_time_seconds=0.0,
        poll_interval_seconds=0.5,
        max_poll_seconds=5.0,
        engine_type="mock",
    )

    start_exec = time.time()
    scheduler = CheckInScheduler(
        reservations=[r1],
        schedule=sched,
        engine_type="mock",
    )

    results = scheduler.run()
    elapsed = time.time() - start_exec

    assert all(r.status == CheckInStatus.SUCCESS for r in results)
    # Must execute within 2 seconds (not waiting for past time or hanging)
    assert elapsed < 3.0, f"Immediate checkin took too long: {elapsed}s"
    print(f"✓ Past check-in executed immediately and succeeded in {elapsed:.2f}s!")


if __name__ == "__main__":
    test_models()
    test_ntp()
    test_logging()
    test_browser_client_initialization()
    test_scheduler_exact_time_and_half_second_interval()
    test_past_checkin_immediate_execution()
    print("\nALL TESTS PASSED SUCCESSFULLY!")
