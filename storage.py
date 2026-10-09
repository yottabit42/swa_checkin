"""Configuration and reservation persistence storage."""

import json
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from models import CheckInSchedule, Passenger, Reservation

logger = logging.getLogger(__name__)
CONFIG_FILE = Path("reservations.json")


def load_config() -> Tuple[List[Reservation], Optional[CheckInSchedule]]:
    """Loads saved reservations and schedule from disk."""
    if not CONFIG_FILE.exists():
        return [], None

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        reservations_data = data.get("reservations", [])
        schedule_data = data.get("schedule")

        reservations: List[Reservation] = []
        for r_dict in reservations_data:
            try:
                pass_dict = r_dict.get("passenger", {})
                passenger = Passenger(
                    first_name=pass_dict.get("first_name", ""),
                    last_name=pass_dict.get("last_name", ""),
                )
                res = Reservation(
                    id=r_dict.get("id"),
                    confirmation_number=r_dict.get("confirmation_number", ""),
                    passenger=passenger,
                )
                reservations.append(res)
            except Exception as e:
                logger.warning(f"Error parsing saved reservation: {e}")

        schedule: Optional[CheckInSchedule] = None
        if schedule_data:
            try:
                schedule = CheckInSchedule(**schedule_data)
            except Exception as e:
                logger.warning(f"Error parsing saved schedule: {e}")

        return reservations, schedule

    except Exception as e:
        logger.error(f"Failed to read {CONFIG_FILE}: {e}")
        return [], None


def save_config(reservations: List[Reservation], schedule: Optional[CheckInSchedule] = None):
    """Saves reservations and schedule to disk."""
    try:
        data = {
            "reservations": [
                {
                    "id": r.id,
                    "confirmation_number": r.confirmation_number,
                    "passenger": {
                        "first_name": r.passenger.first_name,
                        "last_name": r.passenger.last_name,
                    },
                }
                for r in reservations
            ],
            "schedule": schedule.model_dump() if schedule else None,
        }
        CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to save {CONFIG_FILE}: {e}")
