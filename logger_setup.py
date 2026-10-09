"""Diagnostic Logging configuration for Southwest Airlines Check-In."""

from datetime import datetime
import logging
from pathlib import Path
import sys
from typing import Optional

LOG_FILE = Path("checkin_debug.log")

_is_configured = False


def setup_diagnostic_logging(log_level: int = logging.DEBUG) -> logging.Logger:
    """Configures centralized file and console logging."""
    global _is_configured
    root_logger = logging.getLogger("swa_checkin")

    if _is_configured:
        return root_logger

    root_logger.setLevel(log_level)

    # File Handler - always DEBUG level for comprehensive diagnostic troubleshooting
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8", mode="a")
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # Log initial header
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*70}\nSESSION STARTED AT {datetime.now().isoformat()}\n{'='*70}\n")

    _is_configured = True
    return root_logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Gets a named logger inheriting from the root swa_checkin logger."""
    if not _is_configured:
        setup_diagnostic_logging()
    if name:
        return logging.getLogger(f"swa_checkin.{name}")
    return logging.getLogger("swa_checkin")


def read_recent_logs(lines: int = 50) -> str:
    """Reads the last N lines of the debug log."""
    if not LOG_FILE.exists():
        return "No diagnostic log file found."
    try:
        content = LOG_FILE.read_text(encoding="utf-8").splitlines()
        recent = content[-lines:] if len(content) > lines else content
        return "\n".join(recent)
    except Exception as e:
        return f"Error reading log file: {e}"
