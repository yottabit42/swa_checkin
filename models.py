"""Data models for Southwest Airlines Automated Check-in Application."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator


class CheckInStatus(str, Enum):
    IDLE = "IDLE"
    WAITING = "WAITING"
    POLLING = "POLLING"
    TOO_EARLY = "TOO_EARLY"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ERROR = "ERROR"
    BLOCKED = "BLOCKED"


class Passenger(BaseModel):
    first_name: str = Field(..., description="Passenger first name")
    last_name: str = Field(..., description="Passenger surname / last name")

    @field_validator("first_name", "last_name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        clean = v.strip().upper()
        if not clean:
            raise ValueError("Name cannot be empty.")
        if not re.match(r"^[A-Z\s'\-]+$", clean):
            raise ValueError("Name can only contain alphabetic characters, hyphens, and apostrophes.")
        return clean

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class BoardingPassInfo(BaseModel):
    passenger_name: str
    flight_number: Optional[str] = None
    departure_airport: Optional[str] = None
    arrival_airport: Optional[str] = None
    boarding_group: Optional[str] = None  # e.g., 'A'
    boarding_position: Optional[str] = None  # e.g., '24'

    @property
    def position_str(self) -> str:
        if self.boarding_group and self.boarding_position:
            return f"{self.boarding_group}{self.boarding_position}"
        return "N/A"


class Reservation(BaseModel):
    id: str = Field(default_factory=lambda: datetime.now().strftime("%f")[:6])
    confirmation_number: str = Field(..., description="6-character alphanumeric confirmation number")
    passenger: Passenger
    status: CheckInStatus = CheckInStatus.IDLE
    boarding_passes: List[BoardingPassInfo] = Field(default_factory=list)
    last_message: Optional[str] = None
    checked_in_at: Optional[datetime] = None

    @field_validator("confirmation_number")
    @classmethod
    def validate_pnr(cls, v: str) -> str:
        clean = v.strip().upper()
        if not re.match(r"^[A-Z0-9]{6}$", clean):
            raise ValueError(f"Confirmation number '{clean}' must be exactly 6 alphanumeric characters.")
        return clean


class CheckInSchedule(BaseModel):
    checkin_time_str: str = Field(
        ...,
        description="Check-in time in 24-hour format: YYYY-MM-DD HH:MM:SS or YYYY-MM-DD HH:MM",
    )
    timezone: str = Field(
        default="America/Chicago",
        description="IANA timezone (e.g., America/Chicago, America/New_York, America/Los_Angeles)",
    )
    lead_time_seconds: float = Field(
        default=0.0,
        description="Number of seconds before check-in time to start polling (default 0.0: exact check-in time)",
    )
    poll_interval_seconds: float = Field(
        default=0.5,
        description="Polling frequency in seconds (default 0.5s or immediately after attempt finishes)",
    )
    max_poll_seconds: float = Field(
        default=30.0,
        description="Maximum seconds to keep retrying after check-in time (default 30.0)",
    )
    engine_type: str = Field(
        default="browser",
        description="Check-in engine: 'browser' (stealth headless browser), 'api' (direct HTTP), or 'mock'",
    )
    headless: bool = Field(
        default=True,
        description="Whether to run browser in headless mode (True) or visible mode (False)",
    )

    @field_validator("timezone")
    @classmethod
    def validate_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v.strip())
            return v.strip()
        except Exception:
            raise ValueError(f"Unknown timezone '{v}'. Must be a valid IANA timezone like 'America/Chicago'.")

    def get_target_datetime(self) -> datetime:
        """Parses the 24-hour format string into a timezone-aware datetime."""
        time_str = self.checkin_time_str.strip()
        tz = ZoneInfo(self.timezone)

        # Try with seconds first, then without seconds
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y %H:%M",
        ]
        dt = None
        for fmt in formats:
            try:
                dt = datetime.strptime(time_str, fmt)
                break
            except ValueError:
                continue

        if dt is None:
            raise ValueError(
                f"Invalid date/time format: '{time_str}'. Expected 24-hour format: 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD HH:MM'"
            )

        return dt.replace(tzinfo=tz)

    def get_target_epoch(self) -> float:
        """Returns the target epoch timestamp in UTC."""
        return self.get_target_datetime().timestamp()


class CheckInAttemptResult(BaseModel):
    timestamp: float
    attempt_number: int
    reservation_id: str
    status: CheckInStatus
    message: str
    boarding_passes: List[BoardingPassInfo] = Field(default_factory=list)
    raw_response_code: Optional[int] = None
