"""Playwright-based Stealth Browser Automation Client for Southwest Airlines Check-in."""

from datetime import datetime
import logging
from pathlib import Path
import re
import time
from typing import Dict, List, Optional

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from logger_setup import get_logger
from models import BoardingPassInfo, CheckInAttemptResult, CheckInStatus, Reservation

logger = get_logger("browser")

SWA_CHECKIN_PAGE_URL = "https://www.southwest.com/air/check-in/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class PlaywrightSouthwestClient:
    """Automates the desktop web check-in page using a stealth headless/headed Chromium browser."""

    def __init__(self, headless: bool = True, timeout_ms: int = 25000):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.pages: Dict[str, Page] = {}  # reservation.id -> Page

    def start(self):
        """Initializes the Playwright Chromium instance with stealth flags."""
        if self.browser:
            return

        logger.info(f"Launching Playwright Chromium (headless={self.headless})...")
        self.playwright = sync_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--window-size=1280,800",
        ]

        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=launch_args,
        )

        self.context = self.browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/Chicago",
        )

        logger.info("Playwright browser instance initialized successfully.")

    def prepare_reservations(self, reservations: List[Reservation]) -> bool:
        """Pre-loads check-in page and pre-fills form fields for all reservations in parallel tabs."""
        self.start()
        success = True

        for res in reservations:
            try:
                logger.info(f"Opening tab and navigating to {SWA_CHECKIN_PAGE_URL} for PNR: {res.confirmation_number}")
                page = self.context.new_page()

                # Stealth overrides
                page.add_init_script("""
                    delete Object.getPrototypeOf(navigator).webdriver;
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                """)

                response = page.goto(SWA_CHECKIN_PAGE_URL, timeout=self.timeout_ms)
                status_code = response.status if response else "Unknown"
                logger.info(f"Loaded {page.url} with HTTP status {status_code} for PNR {res.confirmation_number}")

                # Dismiss cookie dialog if present
                try:
                    cookie_btn = page.query_selector("#onetrust-accept-btn-handler, #accept-recommended-btn-handler")
                    if cookie_btn:
                        cookie_btn.click(timeout=1500)
                        logger.debug("Dismissed cookie consent banner.")
                except Exception:
                    pass

                # Wait for form inputs to be ready
                page.wait_for_selector("#confirmationNumber", timeout=self.timeout_ms)

                # Pre-fill form fields
                page.fill("#confirmationNumber", res.confirmation_number)
                page.fill("#passengerFirstName", res.passenger.first_name)
                page.fill("#passengerLastName", res.passenger.last_name)

                logger.info(
                    f"Pre-filled form for {res.passenger.full_name} ({res.confirmation_number}). "
                    f"Armed and ready to submit."
                )

                self.pages[res.id] = page

            except Exception as e:
                logger.error(f"Failed to prepare browser tab for reservation {res.confirmation_number}: {e}")
                self._capture_debug_screenshot(res.confirmation_number, "prepare_failed")
                success = False

        return success

    def attempt_checkin(
        self, reservation: Reservation, attempt_num: int, target_epoch: float, current_epoch: float
    ) -> CheckInAttemptResult:
        """Executes a check-in submit click in the browser for the given reservation."""
        diff_from_t = current_epoch - target_epoch
        timing_str = f"T{diff_from_t:+.2f}s"
        pnr = reservation.confirmation_number

        page = self.pages.get(reservation.id)
        if not page or page.is_closed():
            logger.error(f"[{timing_str}] Browser tab for {pnr} is not available.")
            return CheckInAttemptResult(
                timestamp=current_epoch,
                attempt_number=attempt_num,
                reservation_id=reservation.id,
                status=CheckInStatus.ERROR,
                message=f"[{timing_str}] Browser tab is closed or unavailable.",
            )

        try:
            # Handle name variation with spaces (e.g. MC DONALD vs MCDONALD)
            last_name_input = reservation.passenger.last_name
            if " " in last_name_input:
                if attempt_num % 2 == 0:
                    chosen_last = last_name_input.replace(" ", "")
                else:
                    chosen_last = last_name_input
                try:
                    if page.query_selector("#passengerLastName"):
                        page.fill("#passengerLastName", chosen_last, timeout=1200)
                        logger.debug(f"[{timing_str}] Using surname variant '{chosen_last}' for PNR {pnr}")
                except Exception:
                    pass

            logger.info(f"[{timing_str}] Submitting Check-In form for PNR {pnr} (Attempt {attempt_num})...")

            # Click submit button
            submit_btn = page.query_selector("#form-mixin--submit-button")
            if submit_btn:
                try:
                    submit_btn.click(timeout=1500)
                except Exception:
                    page.keyboard.press("Enter")
            else:
                page.keyboard.press("Enter")

            # Wait for response / DOM update
            page.wait_for_timeout(700)

            current_url = page.url
            logger.debug(f"[{timing_str}] Current URL after submit for {pnr}: {current_url}")

            # 1. Check for Access Denied / 403 Bot Block
            page_text = page.inner_text("body")
            if "Access Denied" in page_text or "You don't have permission to access" in page_text:
                screenshot_path = self._capture_debug_screenshot(pnr, "access_denied")
                logger.warning(f"[{timing_str}] Akamai Access Denied. Screenshot saved: {screenshot_path}")
                return CheckInAttemptResult(
                    timestamp=current_epoch,
                    attempt_number=attempt_num,
                    reservation_id=reservation.id,
                    status=CheckInStatus.BLOCKED,
                    message=f"[{timing_str}] Bot defense detected (Access Denied).",
                    raw_response_code=403,
                )

            # 2. Check for Check-in Confirmation / Success
            is_confirmation_page = (
                "confirmation" in current_url.lower()
                or page.query_selector(".boarding-pass-information, .passenger-boarding-info, [class*='boarding-group']")
                is not None
            )

            if is_confirmation_page:
                boarding_passes = self._extract_boarding_passes_from_dom(page, reservation)
                summary = ", ".join([f"{bp.passenger_name}: {bp.position_str}" for bp in boarding_passes])
                screenshot_path = self._capture_debug_screenshot(pnr, "success_boarding_pass")
                logger.info(
                    f"[{timing_str}] Check-in SUCCESS via Browser! Boarding: {summary or 'Confirmed'}. "
                    f"Screenshot: {screenshot_path}"
                )
                return CheckInAttemptResult(
                    timestamp=current_epoch,
                    attempt_number=attempt_num,
                    reservation_id=reservation.id,
                    status=CheckInStatus.SUCCESS,
                    message=f"[{timing_str}] Check-in SUCCESS! Boarding: {summary or 'Confirmed'}",
                    boarding_passes=boarding_passes,
                    raw_response_code=200,
                )

            # 3. Check for Errors & Banner Messages
            error_elements = page.query_selector_all(
                '[class*="error"], [class*="alert"], [role="alert"], .form-mixin--error-message'
            )
            error_messages = [el.inner_text().strip() for el in error_elements if el.inner_text().strip()]
            full_error_text = " ".join(error_messages)

            logger.info(f"[{timing_str}] SWA Page response for {pnr}: {full_error_text[:250]}")

            # Check if outside 24-hour window
            is_outside_24h = any(
                phrase in full_error_text.lower()
                for phrase in [
                    "check in is only allowed within 24 hours",
                    "within 24 hours of departure",
                    "too early",
                    "outside the check-in window",
                ]
            )

            if is_outside_24h:
                # Save screenshot on first attempt or intermittently
                if attempt_num == 1:
                    self._capture_debug_screenshot(pnr, "outside_24h_window")

                return CheckInAttemptResult(
                    timestamp=current_epoch,
                    attempt_number=attempt_num,
                    reservation_id=reservation.id,
                    status=CheckInStatus.TOO_EARLY,
                    message=f"[{timing_str}] Flight outside 24h window (Too early). Retrying...",
                    raw_response_code=400,
                )

            # Check for general unable to retrieve reservation
            if "unable to retrieve your reservation" in full_error_text.lower():
                self._capture_debug_screenshot(pnr, "unable_to_retrieve")
                return CheckInAttemptResult(
                    timestamp=current_epoch,
                    attempt_number=attempt_num,
                    reservation_id=reservation.id,
                    status=CheckInStatus.FAILED,
                    message=f"[{timing_str}] SWA: Unable to retrieve reservation. Verify PNR & departure date.",
                    raw_response_code=400,
                )

            # Default fallback: retry
            return CheckInAttemptResult(
                timestamp=current_epoch,
                attempt_number=attempt_num,
                reservation_id=reservation.id,
                status=CheckInStatus.TOO_EARLY,
                message=f"[{timing_str}] Awaiting response (Attempt {attempt_num})...",
                raw_response_code=400,
            )

        except Exception as e:
            logger.error(f"[{timing_str}] Error during browser checkin attempt for {pnr}: {e}", exc_info=True)
            return CheckInAttemptResult(
                timestamp=current_epoch,
                attempt_number=attempt_num,
                reservation_id=reservation.id,
                status=CheckInStatus.ERROR,
                message=f"[{timing_str}] Browser execution error: {str(e)[:80]}",
            )

    def _extract_boarding_passes_from_dom(self, page: Page, reservation: Reservation) -> List[BoardingPassInfo]:
        """Extracts boarding group and position from the rendered confirmation page."""
        passes: List[BoardingPassInfo] = []
        try:
            text = page.inner_text("body")
            matches = re.findall(r"\b([ABC])\s*[-–]?\s*([0-5]?[0-9]|60)\b", text)

            if matches:
                for grp, pos in matches:
                    passes.append(
                        BoardingPassInfo(
                            passenger_name=reservation.passenger.full_name,
                            boarding_group=grp,
                            boarding_position=pos,
                        )
                    )
            else:
                passes.append(
                    BoardingPassInfo(
                        passenger_name=reservation.passenger.full_name,
                        boarding_group="A",
                        boarding_position="Confirmed",
                    )
                )
        except Exception as e:
            logger.error(f"Error parsing boarding pass from DOM: {e}")

        return passes

    def _capture_debug_screenshot(self, pnr: str, prefix: str) -> str:
        """Captures a screenshot for diagnostic troubleshooting."""
        page = self.pages.get(pnr) or (list(self.pages.values())[0] if self.pages else None)
        if not page:
            return ""

        filename = f"debug_{prefix}_{pnr}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        try:
            page.screenshot(path=filename, full_page=True)
            logger.info(f"Captured diagnostic screenshot: {filename}")
            return filename
        except Exception as e:
            logger.error(f"Failed to capture screenshot: {e}")
            return ""

    def close(self):
        """Closes all browser pages, context, and the Playwright engine."""
        try:
            for page in self.pages.values():
                if not page.is_closed():
                    page.close()
            self.pages.clear()

            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
            logger.info("Playwright browser session closed cleanly.")
        except Exception as e:
            logger.error(f"Error during browser teardown: {e}")
