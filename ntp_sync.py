"""NTP Clock Synchronization Module for sub-second precision check-in timing."""

import logging
import time
from typing import Dict, List, Optional

import ntplib

logger = logging.getLogger(__name__)

DEFAULT_NTP_SERVERS = [
    "time.google.com",
    "pool.ntp.org",
    "time.cloudflare.com",
    "time.apple.com",
    "time.windows.com",
]


class NTPClockSynchronizer:
    def __init__(self, servers: Optional[List[str]] = None):
        self.servers = servers or DEFAULT_NTP_SERVERS
        self.offset: float = 0.0  # seconds: atomic_time = local_time + offset
        self.delay: float = 0.0
        self.last_sync_time: float = 0.0
        self.active_server: Optional[str] = None
        self.is_synced: bool = False

    def sync(self, timeout: float = 3.0) -> bool:
        """Synchronizes with an external NTP server and calculates clock drift."""
        client = ntplib.NTPClient()
        for server in self.servers:
            try:
                response = client.request(server, version=3, timeout=timeout)
                self.offset = response.offset
                self.delay = response.delay
                self.last_sync_time = time.time()
                self.active_server = server
                self.is_synced = True
                logger.info(
                    f"Successfully synchronized with {server}. Offset: {self.offset:+.4f}s, Delay: {self.delay:.4f}s"
                )
                return True
            except Exception as e:
                logger.debug(f"Failed to query NTP server {server}: {e}")
                continue

        logger.warning("Could not contact any NTP server. Defaulting to local system clock.")
        self.is_synced = False
        return False

    def get_atomic_time(self) -> float:
        """Returns the current estimated atomic epoch timestamp in seconds."""
        if not self.is_synced:
            # Attempt a quick sync if not synced yet
            self.sync(timeout=1.5)
        return time.time() + self.offset

    def get_status_info(self) -> Dict[str, any]:
        """Returns details about the clock synchronization status."""
        return {
            "synced": self.is_synced,
            "server": self.active_server or "None (Local Clock)",
            "offset_seconds": self.offset,
            "delay_seconds": self.delay,
            "offset_ms": self.offset * 1000.0,
            "last_sync_age_seconds": time.time() - self.last_sync_time if self.last_sync_time else None,
        }


# Global singleton instance
_global_clock = NTPClockSynchronizer()


def get_atomic_time() -> float:
    return _global_clock.get_atomic_time()


def get_clock() -> NTPClockSynchronizer:
    return _global_clock
