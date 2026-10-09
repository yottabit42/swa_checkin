"""Southwest Airlines API Client, Simulation Engine, and Diagnostic Logging."""

import json
import logging
import random
import time
from typing import Dict, List, Optional, Tuple

from curl_cffi import requests

from logger_setup import get_logger
from models import BoardingPassInfo, CheckInAttemptResult, CheckInStatus, Reservation

logger = get_logger("api")

# Southwest Mobile API constants
SWA_MOBILE_CHECKIN_URL = (
    "https://mobile.southwest.com/api/mobile-air-operations/v1/mobile-air-operations/page/check-in"
)
SWA_WEB_CHECKIN_REVIEW_URL = (
    "https://www.southwest.com/api/air-checkin/v1/air-checkin/page/air/check-in/review"
)

DEFAULT_MOBILE_API_KEY = "l7xx12ebcbc825eb4172b5d44446b020054d"


class SouthwestClient:
    """Production API client that sends requests directly to Southwest Airlines endpoints."""

    def __init__(self, impersonate: str = "chrome124", timeout: float = 4.0):
        self.impersonate = impersonate
        self.timeout = timeout
        self.session = requests.Session(impersonate=impersonate)

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json; charset=UTF-8",
            "X-API-Key": DEFAULT_MOBILE_API_KEY,
            "X-Channel-ID": "MWEB",
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1"
            ),
        }

    def attempt_checkin(
        self, reservation: Reservation, attempt_num: int, target_epoch: float, current_epoch: float
    ) -> CheckInAttemptResult:
        """Attempts to check in for the given reservation via direct HTTP POST."""
        pnr = reservation.confirmation_number
        first_name = reservation.passenger.first_name
        last_name = reservation.passenger.last_name

        payload = {
            "confirmationNumber": pnr,
            "passengerFirstName": first_name,
            "passengerLastName": last_name,
        }

        # Calculate time delta relative to T
        diff_from_t = current_epoch - target_epoch
        timing_str = f"T{diff_from_t:+.2f}s"

        headers = self._get_headers()
        logger.debug(
            f"[{timing_str}] Sending Check-In POST to {SWA_MOBILE_CHECKIN_URL} (Attempt {attempt_num}) | "
            f"PNR: {pnr}, Name: {first_name} {last_name}"
        )
        logger.debug(f"[{timing_str}] Outgoing Headers: {json.dumps(headers, indent=2)}")
        logger.debug(f"[{timing_str}] Outgoing Payload: {json.dumps(payload, indent=2)}")

        try:
            req_start = time.perf_counter()
            resp = self.session.post(
                SWA_MOBILE_CHECKIN_URL,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            elapsed_ms = (time.perf_counter() - req_start) * 1000.0

            status_code = resp.status_code
            raw_text = resp.text

            # Comprehensive diagnostic logging of response
            resp_headers = dict(resp.headers)
            logger.info(
                f"[{timing_str}] Response received in {elapsed_ms:.1f}ms: HTTP {status_code} | "
                f"Content-Length: {len(raw_text)}"
            )
            logger.debug(f"[{timing_str}] Response Headers: {json.dumps(resp_headers, indent=2)}")
            logger.debug(f"[{timing_str}] Response Body Preview (first 500 chars): {raw_text[:500]}")

            try:
                data = resp.json()
            except Exception:
                data = {}

            # Parse response
            return self._parse_response(
                reservation_id=reservation.id,
                attempt_num=attempt_num,
                current_epoch=current_epoch,
                status_code=status_code,
                data=data,
                raw_text=raw_text,
                timing_str=timing_str,
            )

        except requests.errors.Timeout:
            logger.error(f"[{timing_str}] HTTP request timed out after {self.timeout}s for PNR {pnr}")
            return CheckInAttemptResult(
                timestamp=current_epoch,
                attempt_number=attempt_num,
                reservation_id=reservation.id,
                status=CheckInStatus.ERROR,
                message=f"[{timing_str}] Request timed out after {self.timeout}s",
                raw_response_code=None,
            )
        except Exception as e:
            logger.error(f"[{timing_str}] Network/TLS error for PNR {pnr}: {e}", exc_info=True)
            return CheckInAttemptResult(
                timestamp=current_epoch,
                attempt_number=attempt_num,
                reservation_id=reservation.id,
                status=CheckInStatus.ERROR,
                message=f"[{timing_str}] Network/TLS error: {str(e)[:80]}",
                raw_response_code=None,
            )

    def _parse_response(
        self,
        reservation_id: str,
        attempt_num: int,
        current_epoch: float,
        status_code: int,
        data: dict,
        raw_text: str,
        timing_str: str,
    ) -> CheckInAttemptResult:
        # 1. Check for success (200 OK)
        if status_code == 200:
            boarding_passes = self._extract_boarding_passes(data)
            pos_summary = ", ".join([f"{bp.passenger_name}: {bp.position_str}" for bp in boarding_passes])
            msg = f"[{timing_str}] Check-in SUCCESS! Boarding: {pos_summary or 'Confirmed'}"
            logger.info(f"[{timing_str}] {msg}")
            return CheckInAttemptResult(
                timestamp=current_epoch,
                attempt_number=attempt_num,
                reservation_id=reservation_id,
                status=CheckInStatus.SUCCESS,
                message=msg,
                boarding_passes=boarding_passes,
                raw_response_code=status_code,
            )

        # 2. Check for "Too Early"
        message_body = (data.get("message") or "") + raw_text
        is_too_early = (
            status_code == 400
            and any(
                phrase in message_body.lower()
                for phrase in [
                    "too early",
                    "not time to check in",
                    "400305041",
                    "outside the check-in window",
                    "check-in is not available",
                ]
            )
        )

        if is_too_early:
            msg = f"[{timing_str}] Flight window not open yet (HTTP 400 Too Early). Retrying..."
            logger.debug(f"[{timing_str}] {msg}")
            return CheckInAttemptResult(
                timestamp=current_epoch,
                attempt_number=attempt_num,
                reservation_id=reservation_id,
                status=CheckInStatus.TOO_EARLY,
                message=msg,
                raw_response_code=status_code,
            )

        # 3. Check for Anti-bot / Rate limit / 403 Forbidden
        if status_code in (403, 429):
            err_details = f"HTTP {status_code} Forbidden/Blocked. Akamai Bot Manager or IP reputation filter triggered."
            logger.warning(
                f"[{timing_str}] {err_details} Body: {raw_text[:300]}. "
                "Recommendation: Switch check-in engine to Stealth Browser Mode in menu [6]."
            )
            return CheckInAttemptResult(
                timestamp=current_epoch,
                attempt_number=attempt_num,
                reservation_id=reservation_id,
                status=CheckInStatus.BLOCKED,
                message=f"[{timing_str}] Bot defense blocked request (HTTP {status_code}). Switch to Browser engine.",
                raw_response_code=status_code,
            )

        # 4. Reservation not found or invalid
        error_msg = data.get("message") or f"HTTP {status_code}"
        logger.warning(f"[{timing_str}] Southwest error response: {error_msg}")
        return CheckInAttemptResult(
            timestamp=current_epoch,
            attempt_number=attempt_num,
            reservation_id=reservation_id,
            status=CheckInStatus.FAILED,
            message=f"[{timing_str}] SWA Error: {error_msg[:100]}",
            raw_response_code=status_code,
        )

    def _extract_boarding_passes(self, data: dict) -> List[BoardingPassInfo]:
        """Extracts boarding group and position from Southwest response."""
        passes: List[BoardingPassInfo] = []
        checkin_page = data.get("checkInConfirmationPage", {})
        flights = checkin_page.get("flights", [])

        for fl in flights:
            fl_num = fl.get("flightNumber")
            dep = fl.get("departureAirportCode")
            arr = fl.get("arrivalAirportCode")
            passengers = fl.get("passengers", [])

            for p in passengers:
                name = p.get("name") or "Passenger"
                group = p.get("boardingGroup")
                pos = p.get("boardingPosition")
                passes.append(
                    BoardingPassInfo(
                        passenger_name=name,
                        flight_number=fl_num,
                        departure_airport=dep,
                        arrival_airport=arr,
                        boarding_group=group,
                        boarding_position=pos,
                    )
                )

        if not passes:
            passengers = data.get("passengers", [])
            for p in passengers:
                passes.append(
                    BoardingPassInfo(
                        passenger_name=p.get("name", "Passenger"),
                        boarding_group=p.get("boardingGroup", "A"),
                        boarding_position=p.get("boardingPosition", "01"),
                    )
                )

        return passes


class MockSouthwestClient:
    """Mock/Simulation client for testing scheduling, T-3s trigger, 1s retries, and boarding extraction."""

    def __init__(self, simulate_success_at_t: bool = True):
        self.simulate_success_at_t = simulate_success_at_t

    def attempt_checkin(
        self, reservation: Reservation, attempt_num: int, target_epoch: float, current_epoch: float
    ) -> CheckInAttemptResult:
        diff_from_t = current_epoch - target_epoch
        timing_str = f"T{diff_from_t:+.2f}s"

        if diff_from_t < 0.0:
            time.sleep(0.08)
            return CheckInAttemptResult(
                timestamp=current_epoch,
                attempt_number=attempt_num,
                reservation_id=reservation.id,
                status=CheckInStatus.TOO_EARLY,
                message=f"[{timing_str}] SWA (Simulated): Window not open yet (HTTP 400 Too Early). Retrying in 1s...",
                raw_response_code=400,
            )

        time.sleep(0.12)
        group = "A"
        pos_num = max(16, int(16 + (diff_from_t * 2) + random.randint(0, 3)))
        pos_str = f"{pos_num:02d}"

        boarding_passes = [
            BoardingPassInfo(
                passenger_name=reservation.passenger.full_name,
                flight_number="WN1422",
                departure_airport="DAL",
                arrival_airport="DEN",
                boarding_group=group,
                boarding_position=pos_str,
            )
        ]

        pos_summary = f"{reservation.passenger.full_name}: {group}{pos_str}"
        return CheckInAttemptResult(
            timestamp=current_epoch,
            attempt_number=attempt_num,
            reservation_id=reservation.id,
            status=CheckInStatus.SUCCESS,
            message=f"[{timing_str}] SWA (Simulated): Check-in SUCCESS! Assigned Boarding: {pos_summary}",
            boarding_passes=boarding_passes,
            raw_response_code=200,
        )
