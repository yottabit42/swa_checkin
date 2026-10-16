"""Unit and integration tests for Southwest check-in application."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from logger_setup import read_recent_logs, setup_diagnostic_logging
from models import BoardingPassInfo, CheckInSchedule, CheckInStatus, Passenger, Reservation
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
    # BoardingPassInfo formats
    bp_legacy = BoardingPassInfo(passenger_name="JANE DOE", boarding_group="A", boarding_position="24")
    assert bp_legacy.position_str == "A24"

    bp_modern = BoardingPassInfo(passenger_name="JANE DOE", boarding_group="3", boarding_position="12F")
    assert bp_modern.position_str == "Group 3 (Seat 12F)"

    bp_confirmed = BoardingPassInfo(passenger_name="JANE DOE", boarding_group="Confirmed", boarding_position="")
    assert bp_confirmed.position_str == "Confirmed"

    print("✓ Model and schedule tests passed (0.0s lead, 0.5s interval defaults, modern/legacy boarding info)")


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
    async def _test():
        client = PlaywrightSouthwestClient(headless=True)
        try:
            await client.start()
            assert client.browser is not None
            assert client.context is not None
            print("✓ Playwright stealth browser successfully launched and context created")
        finally:
            await client.close()

    asyncio.run(_test())


def test_parallel_multi_pnr_execution():
    # Test checking in 3 reservations concurrently in parallel
    print("Testing parallel check-in across multiple PNRs...")
    now = datetime.now(ZoneInfo("America/Chicago"))
    target = now + timedelta(seconds=1.2)
    target_str = target.strftime("%Y-%m-%d %H:%M:%S")

    r1 = Reservation(confirmation_number="PARA01", passenger=Passenger(first_name="ALICE", last_name="SMITH"))
    r2 = Reservation(confirmation_number="PARA02", passenger=Passenger(first_name="BOB", last_name="SMITH"))
    r3 = Reservation(confirmation_number="PARA03", passenger=Passenger(first_name="CHARLIE", last_name="BROWN"))

    sched = CheckInSchedule(
        checkin_time_str=target_str,
        timezone="America/Chicago",
        lead_time_seconds=0.0,
        poll_interval_seconds=0.5,
        max_poll_seconds=10.0,
        engine_type="mock",
    )

    completed_attempts = []

    def on_attempt(result, res):
        completed_attempts.append((res.confirmation_number, result.attempt_number))
        print(f"  [Parallel Attempt {result.attempt_number}] {res.confirmation_number}: {result.status.value} - {result.message}")

    t_start = time.perf_counter()
    scheduler = CheckInScheduler(
        reservations=[r1, r2, r3],
        schedule=sched,
        engine_type="mock",
        on_attempt_result_callback=on_attempt,
    )

    results = scheduler.run()
    t_elapsed = time.perf_counter() - t_start

    assert all(r.status == CheckInStatus.SUCCESS for r in results), "All reservations should succeed in parallel"
    assert len(r1.boarding_passes) > 0
    assert len(r2.boarding_passes) > 0
    assert len(r3.boarding_passes) > 0
    print(f"✓ Parallel check-in of 3 PNRs completed successfully in {t_elapsed:.2f}s!")


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
    r2 = Reservation(
        confirmation_number="PAST02",
        passenger=Passenger(first_name="CORBIN", last_name="MCDONALD"),
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
        reservations=[r1, r2],
        schedule=sched,
        engine_type="mock",
    )

    results = scheduler.run()
    elapsed = time.time() - start_exec

    assert all(r.status == CheckInStatus.SUCCESS for r in results)
    assert elapsed < 3.0, f"Immediate checkin took too long: {elapsed}s"
    print(f"✓ Past check-in of multiple PNRs executed immediately in parallel and succeeded in {elapsed:.2f}s!")


def test_dom_boarding_pass_extraction():
    class MockPage:
        def __init__(self, text):
            self._text = text

        async def inner_text(self, selector):
            return self._text

    async def _test():
        client = PlaywrightSouthwestClient(headless=True)
        res = Reservation(
            confirmation_number="BLC5LE",
            passenger=Passenger(first_name="CORBIN", last_name="MC DONALD"),
        )

        modern_html_text = """
        How would you like to receive your boarding documents?
        Companion confirmation # BLC5LE
        Corbin Layne Mc Donald
        Boarding group 3
        Seat 12F Window
        FLIGHT # WN991
        """
        passes = await client._extract_boarding_passes_from_dom_async(MockPage(modern_html_text), res)
        assert len(passes) == 1
        assert passes[0].boarding_group == "3"
        assert passes[0].boarding_position == "12F"
        assert passes[0].position_str == "Group 3 (Seat 12F)"

        legacy_html_text = """
        BOARDING PASS
        Boarding Group: A
        Position: 24
        Flight: WN 452
        """
        passes_legacy = await client._extract_boarding_passes_from_dom_async(MockPage(legacy_html_text), res)
        assert len(passes_legacy) == 1
        assert passes_legacy[0].boarding_group == "A"
        assert passes_legacy[0].boarding_position == "24"
        assert passes_legacy[0].position_str == "A24"
        print("✓ Boarding pass DOM parsing verified for both legacy and modern assigned seating")

    asyncio.run(_test())


if __name__ == "__main__":
    test_models()
    test_ntp()
    test_logging()
    test_browser_client_initialization()
    test_parallel_multi_pnr_execution()
    test_past_checkin_immediate_execution()
    test_dom_boarding_pass_extraction()
    print("\nALL TESTS PASSED SUCCESSFULLY!")
