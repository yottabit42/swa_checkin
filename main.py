"""Main entry point for Southwest Airlines Automated Check-in Application."""

import argparse
import sys

from cli import SwaCheckInApp


def main():
    parser = argparse.ArgumentParser(
        description="Southwest Airlines Automated Check-In Application (T-3s Lead Polling, 1s Cadence, NTP Sync)"
    )
    parser.add_argument(
        "--simulate",
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Run an instant dry-run simulation (mock check-in starting in 8 seconds) to verify timing and workflow.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Start the live monitor immediately using saved reservations and schedule without opening the interactive menu.",
    )

    args = parser.parse_args()

    app = SwaCheckInApp()

    if args.dry_run:
        app.run_dry_run_simulation()
    elif args.live:
        app.run_monitor(simulate=False)
    else:
        app.run_main_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nProcess interrupted by user. Exiting.")
        sys.exit(0)
