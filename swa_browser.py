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
        """Executes a check-in submit click for a single reservation tab, handling the 2-step SWA flow."""
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
            current_url = page.url

            # 1. Quick check: Is this tab ALREADY on the confirmation page?
            if "confirmation" in current_url.lower():
                boarding_passes = await self._extract_boarding_passes_from_dom_async(page, reservation)
                summary = ", ".join([f"{bp.passenger_name}: {bp.position_str}" for bp in boarding_passes])
                screenshot_path = await self._capture_debug_screenshot_async(reservation.id, pnr, "success_boarding_pass")
                logger.info(
                    f"[{timing_str}] Check-in ALREADY CONFIRMED for {pnr}! Boarding: {summary or 'Confirmed'}. "
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

            # 2. Check if page is already on the Review Step (Step 2 of Southwest flow)
            review_btn = await page.query_selector(
                ".air-check-in-review-results--check-in-button, button.submit-button, button:has-text('Check in')"
            )
            is_on_review = "review" in current_url.lower() or (review_btn and "index" not in current_url.lower())

            if is_on_review and review_btn:
                logger.info(f"[{timing_str}] Tab for PNR {pnr} is on Review page. Clicking Step 2 'Check in' button...")
                try:
                    await review_btn.click(timeout=3000)
                except Exception:
                    await page.keyboard.press("Enter")

                try:
                    await page.wait_for_url("**/confirmation.html*", timeout=6000)
                except Exception:
                    await page.wait_for_timeout(1000)

                current_url = page.url

            else:
                # 3. Retrieve Step (Step 1 of Southwest flow) on index.html
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

                submit_btn = await page.query_selector("#form-mixin--submit-button")
                if submit_btn:
                    try:
                        await submit_btn.click(timeout=1500)
                    except Exception:
                        await page.keyboard.press("Enter")
                else:
                    await page.keyboard.press("Enter")

                # Wait for URL transition to review.html or confirmation.html
                try:
                    await page.wait_for_url(
                        lambda u: "review" in u.lower() or "confirmation" in u.lower(),
                        timeout=3500,
                    )
                except Exception:
                    await page.wait_for_timeout(600)

                current_url = page.url
                logger.debug(f"[{timing_str}] Post-retrieve URL for {pnr}: {current_url}")

                # If retrieved into Review page, immediately click the Step 2 "Check in" button
                if "review" in current_url.lower():
                    logger.info(f"[{timing_str}] Reached Review page for {pnr}! Clicking Step 2 'Check in' button...")
                    try:
                        step2_btn = await page.wait_for_selector(
                            ".air-check-in-review-results--check-in-button, button.submit-button, button:has-text('Check in')",
                            timeout=3000,
                        )
                        if step2_btn:
                            await step2_btn.click(timeout=3000)
                            try:
                                await page.wait_for_url("**/confirmation.html*", timeout=6000)
                            except Exception:
                                await page.wait_for_timeout(1000)
                    except Exception as e:
                        logger.warning(f"[{timing_str}] Step 2 review click encountered: {e}")

                    current_url = page.url

            # 4. Check for Access Denied / 403 Bot Block
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

            # 5. Check for Check-in Confirmation / Success
            is_confirmation_page = (
                "confirmation" in current_url.lower()
                or "you're checked in" in page_text.lower()
                or (await page.query_selector(".air-check-in-confirmation-results, .boarding-pass-information, .passenger-boarding-info"))
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

            # If still on Review page (e.g. click still processing), retry on next poll tick
            if "review" in current_url.lower():
                return CheckInAttemptResult(
                    timestamp=current_epoch,
                    attempt_number=attempt_num,
                    reservation_id=reservation.id,
                    status=CheckInStatus.TOO_EARLY,
                    message=f"[{timing_str}] On Review page, submitting final check-in...",
                    raw_response_code=200,
                )

            # 6. Check for Errors & Banner Messages on retrieval page
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

            # 1. Traditional Southwest format (e.g. A24, B15, C05)
            trad_matches = re.findall(r"\b([ABC])\s*[-–]?\s*([0-5]?[0-9]|60)\b", text)
            if trad_matches:
                for grp, pos in trad_matches:
                    passes.append(
                        BoardingPassInfo(
                            passenger_name=reservation.passenger.full_name,
                            boarding_group=grp,
                            boarding_position=pos,
                        )
                    )
                return passes

            # 2. Modern numeric boarding group & assigned seat format (e.g. Boarding group 3, Seat 12F)
            group_match = re.search(r"boarding\s+group\s*[:\n]?\s*([0-9]+|[ABC])", text, re.IGNORECASE)
            seat_match = re.search(r"(?:seat|position)\s*[:\n]?\s*([0-9]{1,2}[A-F]?|[0-5]?[0-9]|60)", text, re.IGNORECASE)

            if not group_match or not seat_match:
                # Columnar format: BOARDING GROUP ... SEAT ... 3 ... 12F
                col_match = re.search(r"BOARDING GROUP\s+SEAT[\s\S]*?\n\s*([0-9]+|[ABC])\s+([0-9]{1,2}[A-F])", text)
                if col_match:
                    if not group_match:
                        group_match = col_match
                    if not seat_match:
                        seat_match = col_match

            grp = group_match.group(1) if group_match else None
            pos = seat_match.group(1) if seat_match else None

            if grp or pos:
                passes.append(
                    BoardingPassInfo(
                        passenger_name=reservation.passenger.full_name,
                        boarding_group=grp or "Confirmed",
                        boarding_position=pos or "",
                    )
                )
            else:
                passes.append(
                    BoardingPassInfo(
                        passenger_name=reservation.passenger.full_name,
                        boarding_group="Confirmed",
                        boarding_position="",
                    )
                )
        except Exception as e:
            logger.error(f"Error parsing boarding pass from DOM: {e}")
            passes.append(
                BoardingPassInfo(
                    passenger_name=reservation.passenger.full_name,
                    boarding_group="Confirmed",
                    boarding_position="",
                )
            )

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
