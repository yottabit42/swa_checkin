"""High-precision scheduling and polling engine for Southwest Airlines check-in."""

import concurrent.futures
from datetime import datetime
import logging
import time
from typing import Callable, List, Optional

from logger_setup import get_logger
from models import (
    CheckInAttemptResult,
    CheckInSchedule,
    CheckInStatus,
    Reservation,
)
from ntp_sync import NTPClockSynchronizer, get_clock
from swa_browser import PlaywrightSouthwestClient
from swa_client import MockSouthwestClient, SouthwestClient

logger = get_logger("scheduler")


class CheckInScheduler:
    def __init__(
        self,
        reservations: List[Reservation],
        schedule: CheckInSchedule,
        engine_type: str = "browser",  # "browser", "api", "mock"
        headless: bool = True,
        clock: Optional[NTPClockSynchronizer] = None,
        on_tick_callback: Optional[Callable[[dict], None]] = None,
        on_attempt_result_callback: Optional[Callable[[CheckInAttemptResult, Reservation], None]] = None,
    ):
        self.reservations = reservations
        self.schedule = schedule
        self.engine_type = engine_type
        self.headless = headless
        self.clock = clock or get_clock()
        self.on_tick_callback = on_tick_callback
        self.on_attempt_result_callback = on_attempt_result_callback

        if self.engine_type == "mock":
            self.client = MockSouthwestClient()
        elif self.engine_type == "api":
            self.client = SouthwestClient()
        else:
            self.client = PlaywrightSouthwestClient(headless=self.headless)

        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        if hasattr(self.client, "close"):
            self.client.close()

    def run(self) -> List[Reservation]:
        """Executes the complete scheduling lifecycle: sync -> countdown -> T-3s polling."""
        target_epoch = self.schedule.get_target_epoch()
        lead_time = self.schedule.lead_time_seconds  # e.g., 3.0s
        poll_interval = self.schedule.poll_interval_seconds  # e.g., 1.0s

        # 1. Initial Clock Sync
        self.clock.sync()
        now_atomic = self.clock.get_atomic_time()

        # Check if target time is in the past or immediately ready
        is_immediate = (target_epoch <= now_atomic)

        if is_immediate:
            logger.info(
                f"Target check-in time ({self.schedule.checkin_time_str}) is in the past! "
                f"Starting immediate check-in for up to {self.schedule.max_poll_seconds}s..."
            )
            start_epoch = now_atomic
            stop_epoch = now_atomic + max(30.0, self.schedule.max_poll_seconds)
        else:
            start_epoch = target_epoch - lead_time
            stop_epoch = target_epoch + max(30.0, self.schedule.max_poll_seconds)

        logger.info(
            f"Scheduling check-in run (Engine: {self.engine_type.upper()}) | "
            f"Target: {self.schedule.checkin_time_str} ({self.schedule.timezone}) | "
            f"Mode: {'IMMEDIATE' if is_immediate else 'COUNTDOWN'} | "
            f"Lead Time: {lead_time}s | Interval: {poll_interval}s"
        )

        # 2. Stage 1 & 2: Countdown & Preparation (Only if not immediate)
        if not is_immediate:
            prepare_epoch = target_epoch - 20.0
            while not self._cancelled:
                now_atomic = self.clock.get_atomic_time()
                time_left_to_start = start_epoch - now_atomic

                if now_atomic >= prepare_epoch:
                    break

                if self.on_tick_callback:
                    self.on_tick_callback({
                        "phase": "COUNTDOWN",
                        "seconds_remaining": time_left_to_start,
                        "target_epoch": target_epoch,
                        "current_atomic": now_atomic,
                        "clock_skew": self.clock.offset,
                        "engine": self.engine_type,
                    })

                sleep_time = min(1.0, max(0.1, prepare_epoch - now_atomic))
                time.sleep(sleep_time)

            if self._cancelled:
                return self.reservations

            # Re-sync clock at armed window
            self.clock.sync()

        # Pre-load browser tabs
        if hasattr(self.client, "prepare_reservations"):
            if self.on_tick_callback:
                self.on_tick_callback({
                    "phase": "PREPARING_BROWSER",
                    "seconds_remaining": max(0.0, start_epoch - self.clock.get_atomic_time()),
                    "target_epoch": target_epoch,
                    "current_atomic": self.clock.get_atomic_time(),
                    "clock_skew": self.clock.offset,
                    "engine": self.engine_type,
                })
            prep_ok = self.client.prepare_reservations(self.reservations)
            if not prep_ok:
                logger.warning("Browser preparation encountered warnings; continuing with available tabs.")

        # High-precision approach to start_epoch (if future)
        if not is_immediate:
            while not self._cancelled:
                now_atomic = self.clock.get_atomic_time()
                time_to_start = start_epoch - now_atomic

                if time_to_start <= 0.0:
                    break

                if self.on_tick_callback:
                    self.on_tick_callback({
                        "phase": "ARMED",
                        "seconds_remaining": time_to_start,
                        "target_epoch": target_epoch,
                        "current_atomic": now_atomic,
                        "clock_skew": self.clock.offset,
                        "engine": self.engine_type,
                    })

                if time_to_start > 0.05:
                    time.sleep(min(0.05, time_to_start - 0.02))
                else:
                    time.sleep(0.001)

        if self._cancelled:
            return self.reservations

        # 3. Stage 3: Active Polling Loop
        attempt_number = 0
        pending_reservations = [r for r in self.reservations if r.status != CheckInStatus.SUCCESS]

        for r in pending_reservations:
            r.status = CheckInStatus.POLLING

        logger.info(f"Triggering polling loop for {len(pending_reservations)} reservation(s)...")

        try:
            while pending_reservations and not self._cancelled:
                attempt_number += 1
                tick_start_atomic = self.clock.get_atomic_time()

                if tick_start_atomic > stop_epoch:
                    logger.warning(
                        f"Max polling window ({self.schedule.max_poll_seconds}s) reached without all reservations succeeding."
                    )
                    for r in pending_reservations:
                        r.status = CheckInStatus.FAILED
                        r.last_message = "Max polling duration reached without success."
                    break

                newly_succeeded = []
                for res in list(pending_reservations):
                    try:
                        attempt_res: CheckInAttemptResult = self.client.attempt_checkin(
                            res,
                            attempt_number,
                            target_epoch,
                            tick_start_atomic,
                        )
                        res.last_message = attempt_res.message

                        if attempt_res.status == CheckInStatus.SUCCESS:
                            res.status = CheckInStatus.SUCCESS
                            res.boarding_passes = attempt_res.boarding_passes
                            res.checked_in_at = datetime.fromtimestamp(attempt_res.timestamp)
                            newly_succeeded.append(res)
                        elif attempt_res.status == CheckInStatus.TOO_EARLY:
                            res.status = CheckInStatus.TOO_EARLY
                        elif attempt_res.status == CheckInStatus.BLOCKED:
                            res.status = CheckInStatus.BLOCKED
                        else:
                            res.status = CheckInStatus.FAILED

                        if self.on_attempt_result_callback:
                            self.on_attempt_result_callback(attempt_res, res)

                    except Exception as e:
                        logger.error(f"Error checking in {res.confirmation_number}: {e}", exc_info=True)
                        res.status = CheckInStatus.ERROR
                        res.last_message = f"Execution error: {e}"

                # Remove succeeded reservations
                for succ in newly_succeeded:
                    if succ in pending_reservations:
                        pending_reservations.remove(succ)

                if not pending_reservations:
                    logger.info("All reservations successfully checked in!")
                    break

                # Progress callback
                if self.on_tick_callback:
                    self.on_tick_callback({
                        "phase": "POLLING",
                        "attempt": attempt_number,
                        "time_relative_to_t": tick_start_atomic - target_epoch,
                        "target_epoch": target_epoch,
                        "current_atomic": tick_start_atomic,
                        "pending_count": len(pending_reservations),
                        "engine": self.engine_type,
                    })

                # Calculate sleep time for next 1.0s boundary
                expected_next_tick = tick_start_atomic + poll_interval
                now_after_tick = self.clock.get_atomic_time()
                sleep_needed = expected_next_tick - now_after_tick

                if sleep_needed > 0:
                    time.sleep(sleep_needed)
                else:
                    time.sleep(0.01)

        finally:
            if hasattr(self.client, "close"):
                self.client.close()

        return self.reservations
