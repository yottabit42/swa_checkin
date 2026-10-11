"""Playwright-based Async Stealth Browser Automation Client for Southwest Airlines Check-in."""

import asyncio
from datetime import datetime
import logging
from pathlib import Path
import re
import time
from typing import Dict, List, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from logger_setup import get_logger
from models import BoardingPassInfo, CheckInAttemptResult, CheckInStatus, Reservation

logger = get_logger("browser")

SWA_CHECKIN_PAGE_URL = "https://www.southwest.com/air/check-in/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class PlaywrightSouthwestClient:
    """Automates desktop web check-in with parallel multi-tab execution using async Playwright."""

    def __init__(self, headless: bool = True, timeout_ms: int = 25000):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.pages: Dict[str, Page] = {}  # reservation.id -> Page

    async def start(self):
        """Initializes the Playwright Chromium instance with stealth flags."""
        if self.browser:
            return

        logger.info(f"Launching Async Playwright Chromium (headless={self.headless})...")
        self.playwright = await async_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--window-size=1280,800",
        ]

        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=launch_args,
        )

        self.context = await self.browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/Chicago",
        )

        logger.info("Playwright browser instance initialized successfully.")

    async def _prepare_single_reservation(self, res: Reservation) -> bool:
        """Opens a tab and pre-fills form fields for a single reservation."""
        pnr = res.confirmation_number
        try:
            logger.info(f"Opening tab and navigating to {SWA_CHECKIN_PAGE_URL} for PNR: {pnr}")
            page = await self.context.new_page()

            # Anti-detection stealth overrides
            await page.add_init_script("""
                delete Object.getPrototypeOf(navigator).webdriver;
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """)

            response = await page.goto(SWA_CHECKIN_PAGE_URL, timeout=self.timeout_ms)
            status_code = response.status if response else "Unknown"
            logger.info(f"Loaded check-in page (HTTP {status_code}) for PNR {pnr}")

            # Dismiss cookie consent dialog if present
            try:
                cookie_btn = await page.query_selector("#onetrust-accept-btn-handler, #accept-recommended-btn-handler")
                if cookie_btn:
                    await cookie_btn.click(timeout=1500)
                    logger.debug(f"Dismissed cookie banner for PNR {pnr}.")
            except Exception:
                pass

            # Wait for form input fields
            await page.wait_for_selector("#confirmationNumber", timeout=self.timeout_ms)

            # Pre-fill form fields
            await page.fill("#confirmationNumber", res.confirmation_number)
            await page.fill("#passengerFirstName", res.passenger.first_name)
            await page.fill("#passengerLastName", res.passenger.last_name)

            logger.info(
                f"Pre-filled form for {res.passenger.full_name} ({pnr}). Armed and ready for parallel submit."
            )
            self.pages[res.id] = page
            return True

        except Exception as e:
            logger.error(f"Failed to prepare browser tab for PNR {pnr}: {e}")
            await self._capture_debug_screenshot_async(res.id, pnr, "prepare_failed")
            return False

    async def prepare_reservations(self, reservations: List[Reservation]) -> bool:
        """Pre-loads check-in pages and pre-fills forms for all reservations IN PARALLEL."""
        await self.start()
        logger.info(f"Pre-loading {len(reservations)} reservation tab(s) in parallel...")
        results = await asyncio.gather(*(self._prepare_single_reservation(r) for r in reservations))
        return all(results)

    async def attempt_checkin_single(
        self, reservation: Reservation, attempt_num: int, target_epoch: float, current_epoch: float
    ) -> CheckInAttemptResult:
        """Executes a check-in submit click for a single reservation tab."""
        diff_from_t = current_epoch - target_epoch
        timing_str = f"T{diff_from_t:+.2f}s"
        pnr = reservation.confirmation_number

        page = self.pages.get(reservation.id)
        if not page or page.is_closed():
            logger.error(f"[{timing_str}] Browser tab for {pnr} is closed or unavailable.")
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
                    if await page.query_selector("#passengerLastName"):
                        await page.fill("#passengerLastName", chosen_last, timeout=1200)
                        logger.debug(f"[{timing_str}] Using surname variant '{chosen_last}' for PNR {pnr}")
                except Exception:
                    pass

            logger.info(f"[{timing_str}] Submitting Check-In form in parallel for PNR {pnr} (Attempt {attempt_num})...")

            # Click submit button
            submit_btn = await page.query_selector("#form-mixin--submit-button")
            if submit_btn:
                try:
                    await submit_btn.click(timeout=1500)
                except Exception:
                    await page.keyboard.press("Enter")
            else:
                await page.keyboard.press("Enter")

            # Wait briefly for DOM reaction / network transition
            await page.wait_for_timeout(600)

            current_url = page.url
            logger.debug(f"[{timing_str}] Post-click URL for {pnr}: {current_url}")

            # 1. Check for Access Denied / 403 Bot Block
            page_text = await page.inner_text("body")
            if "Access Denied" in page_text or "You don't have permission to access" in page_text:
                screenshot_path = await self._capture_debug_screenshot_async(reservation.id, pnr, "access_denied")
                logger.warning(f"[{timing_str}] Akamai Access Denied for {pnr}. Screenshot: {screenshot_path}")
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
                or (await page.query_selector(".boarding-pass-information, .passenger-boarding-info, [class*='boarding-group']"))
                is not None
            )

            if is_confirmation_page:
                boarding_passes = await self._extract_boarding_passes_from_dom_async(page, reservation)
                summary = ", ".join([f"{bp.passenger_name}: {bp.position_str}" for bp in boarding_passes])
                screenshot_path = await self._capture_debug_screenshot_async(reservation.id, pnr, "success_boarding_pass")
                logger.info(
                    f"[{timing_str}] Parallel Check-in SUCCESS for {pnr}! Boarding: {summary or 'Confirmed'}. "
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
            error_elements = await page.query_selector_all(
                '[class*="error"], [class*="alert"], [role="alert"], .form-mixin--error-message'
            )
            error_messages = []
            for el in error_elements:
                txt = (await el.inner_text()).strip()
                if txt:
                    error_messages.append(txt)
            full_error_text = " ".join(error_messages)

            logger.info(f"[{timing_str}] SWA Page response for {pnr}: {full_error_text[:250]}")

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
                if attempt_num == 1:
                    await self._capture_debug_screenshot_async(reservation.id, pnr, "outside_24h_window")

                return CheckInAttemptResult(
                    timestamp=current_epoch,
                    attempt_number=attempt_num,
                    reservation_id=reservation.id,
                    status=CheckInStatus.TOO_EARLY,
                    message=f"[{timing_str}] Flight outside 24h window (Too early). Retrying...",
                    raw_response_code=400,
                )

            if "unable to retrieve your reservation" in full_error_text.lower():
                await self._capture_debug_screenshot_async(reservation.id, pnr, "unable_to_retrieve")
                return CheckInAttemptResult(
                    timestamp=current_epoch,
                    attempt_number=attempt_num,
                    reservation_id=reservation.id,
                    status=CheckInStatus.FAILED,
                    message=f"[{timing_str}] SWA: Unable to retrieve reservation. Verify PNR & departure date.",
                    raw_response_code=400,
                )

            # Fallback retry
            return CheckInAttemptResult(
                timestamp=current_epoch,
                attempt_number=attempt_num,
                reservation_id=reservation.id,
                status=CheckInStatus.TOO_EARLY,
                message=f"[{timing_str}] Awaiting response (Attempt {attempt_num})...",
                raw_response_code=400,
            )

        except Exception as e:
            logger.error(f"[{timing_str}] Error during parallel checkin attempt for {pnr}: {e}", exc_info=True)
            return CheckInAttemptResult(
                timestamp=current_epoch,
                attempt_number=attempt_num,
                reservation_id=reservation.id,
                status=CheckInStatus.ERROR,
                message=f"[{timing_str}] Browser execution error: {str(e)[:80]}",
            )

    async def attempt_checkin_all(
        self, reservations: List[Reservation], attempt_num: int, target_epoch: float, current_epoch: float
    ) -> List[CheckInAttemptResult]:
        """Dispatches check-in submit clicks across ALL reservation tabs concurrently in parallel."""
        return await asyncio.gather(*(
            self.attempt_checkin_single(r, attempt_num, target_epoch, current_epoch)
            for r in reservations
        ))

    async def _extract_boarding_passes_from_dom_async(self, page: Page, reservation: Reservation) -> List[BoardingPassInfo]:
        """Extracts boarding group and position from the rendered confirmation page."""
        passes: List[BoardingPassInfo] = []
        try:
            text = await page.inner_text("body")
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

    async def _capture_debug_screenshot_async(self, res_id: str, pnr: str, prefix: str) -> str:
        """Captures a screenshot asynchronously for diagnostic troubleshooting."""
        page = self.pages.get(res_id)
        if not page or page.is_closed():
            return ""

        filename = f"debug_{prefix}_{pnr}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        try:
            await page.screenshot(path=filename, full_page=True)
            logger.info(f"Captured diagnostic screenshot: {filename}")
            return filename
        except Exception as e:
            logger.error(f"Failed to capture screenshot: {e}")
            return ""

    async def close(self):
        """Closes all browser pages, context, and the Playwright engine."""
        try:
            for page in self.pages.values():
                if not page.is_closed():
                    await page.close()
            self.pages.clear()

            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            logger.info("Playwright browser session closed cleanly.")
        except Exception as e:
            logger.error(f"Error during browser teardown: {e}")
