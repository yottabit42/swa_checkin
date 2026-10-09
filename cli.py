"""Interactive Command-Line Interface for Southwest Airlines Automated Check-in."""

from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
import time
from typing import List, Optional
from zoneinfo import ZoneInfo

from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from logger_setup import get_logger, read_recent_logs, setup_diagnostic_logging
from models import (
    BoardingPassInfo,
    CheckInAttemptResult,
    CheckInSchedule,
    CheckInStatus,
    Passenger,
    Reservation,
)
from ntp_sync import get_clock
from scheduler import CheckInScheduler
from storage import load_config, save_config

console = Console()
logger = get_logger("cli")

COMMON_TIMEZONES = [
    ("America/Chicago", "US Central Time (CT - Chicago, Dallas, Houston)"),
    ("America/New_York", "US Eastern Time (ET - New York, Atlanta, Orlando)"),
    ("America/Denver", "US Mountain Time (MT - Denver, Salt Lake City)"),
    ("America/Phoenix", "US Arizona Time (MST - Phoenix - No DST)"),
    ("America/Los_Angeles", "US Pacific Time (PT - Los Angeles, Las Vegas, SF)"),
    ("Pacific/Honolulu", "Hawaii Time (HST - Honolulu)"),
]


class SwaCheckInApp:
    def __init__(self):
        setup_diagnostic_logging()
        self.reservations: List[Reservation] = []
        self.schedule: Optional[CheckInSchedule] = None
        self.clock = get_clock()
        self._load_saved_data()

    def _load_saved_data(self):
        res, sched = load_config()
        self.reservations = res
        self.schedule = sched

        # Ensure sensible defaults if newly created
        if self.schedule is None:
            self.schedule = CheckInSchedule(
                checkin_time_str=(datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:00"),
                timezone="America/Chicago",
                lead_time_seconds=0.0,
                poll_interval_seconds=0.5,
                max_poll_seconds=30.0,
                engine_type="browser",
                headless=True,
            )

    def _save_data(self):
        save_config(self.reservations, self.schedule)

    def print_banner(self):
        banner_text = Text()
        banner_text.append("✈️  SOUTHWEST AIRLINES AUTO CHECK-IN  ✈️\n", style="bold cyan")
        banner_text.append("Stealth Browser Automation • Exact-Time Trigger (T) • 0.5s Cadence / Immediate\n", style="italic")
        console.print(Panel(Align.center(banner_text), border_style="blue", padding=(1, 2)))

    def display_status_overview(self):
        """Displays summary tables of current reservations, check-in schedule, and clock sync."""
        clk_info = self.clock.get_status_info()
        offset_ms = clk_info["offset_ms"]
        offset_color = "green" if abs(offset_ms) < 500 else "yellow"

        # Schedule Info
        sched_text = Text()
        if self.schedule and self.schedule.checkin_time_str:
            try:
                target_dt = self.schedule.get_target_datetime()
                now_in_tz = datetime.now(ZoneInfo(self.schedule.timezone))
                diff = target_dt - now_in_tz

                sched_text.append(f"Target: {self.schedule.checkin_time_str} ({self.schedule.timezone})\n", style="bold green")
                sched_text.append(f"Current in TZ: {now_in_tz.strftime('%Y-%m-%d %H:%M:%S')}\n", style="dim")
                if diff.total_seconds() > 0:
                    hours, rem = divmod(int(diff.total_seconds()), 3600)
                    minutes, seconds = divmod(rem, 60)
                    sched_text.append(f"Countdown: {hours}h {minutes}m {seconds}s until window opens", style="cyan")
                else:
                    sched_text.append(f"Target time is in the past! ({abs(int(diff.total_seconds()))}s ago)", style="red")
            except Exception as e:
                sched_text.append(f"Config error: {e}", style="red")
        else:
            sched_text.append("No check-in time configured yet.", style="yellow")

        # Reservations Table
        table = Table(title="Configured Passengers & Reservations", border_style="dim")
        table.add_column("#", justify="center", style="bold", width=3)
        table.add_column("Confirmation (PNR)", style="cyan", width=20)
        table.add_column("Passenger Name", style="magenta", width=25)
        table.add_column("Status", justify="center", width=15)
        table.add_column("Assigned Boarding", style="bold green", width=20)

        if not self.reservations:
            table.add_row("-", "No reservations added", "Add using Menu Option [1]", "-", "-")
        else:
            for idx, r in enumerate(self.reservations, 1):
                status_style = {
                    CheckInStatus.IDLE: "dim",
                    CheckInStatus.WAITING: "yellow",
                    CheckInStatus.POLLING: "bold cyan",
                    CheckInStatus.SUCCESS: "bold green",
                    CheckInStatus.FAILED: "bold red",
                    CheckInStatus.BLOCKED: "bold red",
                }.get(r.status, "white")

                passes_str = ", ".join([bp.position_str for bp in r.boarding_passes]) if r.boarding_passes else "Pending"
                table.add_row(
                    str(idx),
                    r.confirmation_number,
                    r.passenger.full_name,
                    Text(r.status.value, style=status_style),
                    passes_str,
                )

        console.print(table)

        # Overview panel
        overview_table = Table.grid(padding=1)
        overview_table.add_column(style="bold")
        overview_table.add_column()
        overview_table.add_row("⏱️ Check-in Schedule:", sched_text)
        overview_table.add_row(
            "🌐 NTP Clock Sync:",
            Text(
                f"{clk_info['server']} | Drift: {offset_ms:+.1f} ms ({'In Sync' if clk_info['synced'] else 'Unsynced'})",
                style=offset_color,
            ),
        )

        engine_name = "Stealth Headless Browser (Playwright - Bypasses 403)" if self.schedule.engine_type == "browser" and self.schedule.headless else (
            "Headed Browser (Visible Window)" if self.schedule.engine_type == "browser" else "Direct HTTP API (curl_cffi)"
        )
        overview_table.add_row("🛡️ Execution Engine:", Text(engine_name, style="bold green" if "Browser" in engine_name else "yellow"))
        lead_desc = "exact time T" if self.schedule.lead_time_seconds == 0 else f"T - {self.schedule.lead_time_seconds}s"
        overview_table.add_row(
            "⚙️ Polling Parameters:",
            Text(
                f"Starts at {lead_desc} • Retries every {self.schedule.poll_interval_seconds}s (or immediately if attempt takes longer) • Max window {self.schedule.max_poll_seconds}s",
                style="dim",
            ),
        )

        console.print(Panel(overview_table, title="System & Timing Overview", border_style="cyan"))

    def add_reservations_menu(self):
        """Allows user to enter multiple names and confirmation numbers."""
        console.rule("[bold cyan]Add Passenger Reservation[/bold cyan]")
        console.print("[dim]Southwest requires a 6-character confirmation number along with the passenger's First and Last Name (Surname).[/dim]\n")

        while True:
            # 1. Confirmation Number
            while True:
                pnr_input = Prompt.ask("[bold yellow]Enter 6-character Confirmation Number (PNR)[/bold yellow]").strip().upper()
                if len(pnr_input) == 6 and pnr_input.isalnum():
                    break
                console.print("[red]Invalid format. Confirmation number must be exactly 6 alphanumeric characters (e.g. ABC123).[/red]")

            # 2. Passenger Names
            while True:
                first_name = Prompt.ask("[bold yellow]Passenger First Name[/bold yellow]").strip().upper()
                if first_name and all(c.isalpha() or c in " -'" for c in first_name):
                    break
                console.print("[red]Please enter a valid first name.[/red]")

            while True:
                last_name = Prompt.ask("[bold yellow]Passenger Last Name / Surname[/bold yellow]").strip().upper()
                if last_name and all(c.isalpha() or c in " -'" for c in last_name):
                    break
                console.print("[red]Please enter a valid surname / last name.[/red]")

            try:
                passenger = Passenger(first_name=first_name, last_name=last_name)
                res = Reservation(confirmation_number=pnr_input, passenger=passenger)
                self.reservations.append(res)
                self._save_data()
                logger.info(f"Added reservation: {pnr_input} for {passenger.full_name}")
                console.print(f"[green]✓ Added: {passenger.full_name} ({pnr_input})[/green]\n")
            except Exception as e:
                console.print(f"[red]Error adding reservation: {e}[/red]")

            more = Confirm.ask("Would you like to add another passenger / confirmation number?", default=False)
            if not more:
                break

    def remove_reservation_menu(self):
        """Allows removing or clearing reservations."""
        if not self.reservations:
            console.print("[yellow]No reservations to remove.[/yellow]")
            return

        console.rule("[bold red]Remove Reservations[/bold red]")
        for idx, r in enumerate(self.reservations, 1):
            console.print(f"[{idx}] {r.passenger.full_name} - PNR: [cyan]{r.confirmation_number}[/cyan]")
        console.print("[A] Clear All Reservations")
        console.print("[0] Cancel")

        choice = Prompt.ask("Select option to remove", default="0").strip().upper()
        if choice == "0":
            return
        elif choice == "A":
            if Confirm.ask("Are you sure you want to clear all reservations?", default=False):
                self.reservations.clear()
                self._save_data()
                console.print("[green]All reservations cleared.[/green]")
        elif choice.isdigit() and 1 <= int(choice) <= len(self.reservations):
            removed = self.reservations.pop(int(choice) - 1)
            self._save_data()
            console.print(f"[green]Removed: {removed.passenger.full_name} ({removed.confirmation_number})[/green]")

    def set_schedule_menu(self):
        """Sets the single check-in time and timezone that applies to all reservations."""
        console.rule("[bold cyan]Configure Check-in Time & Time Zone[/bold cyan]")
        console.print("[dim]Enter the exact check-in time in 24-hour format (normally 24 hours prior to departure).[/dim]")
        console.print("[dim]Format: YYYY-MM-DD HH:MM:SS or YYYY-MM-DD HH:MM[/dim]\n")

        # Timezone Selection
        console.print("[bold]Select Timezone:[/bold]")
        for idx, (tz_id, desc) in enumerate(COMMON_TIMEZONES, 1):
            console.print(f"[{idx}] {tz_id} - [dim]{desc}[/dim]")
        console.print(f"[{len(COMMON_TIMEZONES) + 1}] Custom IANA Timezone (e.g. Europe/London)")

        tz_choice = Prompt.ask(
            "Select timezone",
            choices=[str(i) for i in range(1, len(COMMON_TIMEZONES) + 2)],
            default="1",
        )

        if int(tz_choice) <= len(COMMON_TIMEZONES):
            selected_tz = COMMON_TIMEZONES[int(tz_choice) - 1][0]
        else:
            while True:
                custom_tz = Prompt.ask("Enter IANA Timezone string (e.g. America/Detroit)").strip()
                try:
                    ZoneInfo(custom_tz)
                    selected_tz = custom_tz
                    break
                except Exception:
                    console.print("[red]Invalid IANA timezone. Try again.[/red]")

        # Current time in selected TZ
        now_in_selected = datetime.now(ZoneInfo(selected_tz))
        console.print(f"\n[cyan]Current time in {selected_tz}:[/cyan] [bold]{now_in_selected.strftime('%Y-%m-%d %H:%M:%S')}[/bold]")

        # Time input
        default_time_str = (now_in_selected + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:00")
        while True:
            time_input = Prompt.ask(
                "[bold yellow]Check-in Date & Time (24h format: YYYY-MM-DD HH:MM:SS)[/bold yellow]",
                default=default_time_str,
            ).strip()

            try:
                test_sched = CheckInSchedule(
                    checkin_time_str=time_input,
                    timezone=selected_tz,
                    lead_time_seconds=self.schedule.lead_time_seconds if self.schedule else 0.0,
                    poll_interval_seconds=self.schedule.poll_interval_seconds if self.schedule else 0.5,
                    max_poll_seconds=self.schedule.max_poll_seconds if self.schedule else 30.0,
                    engine_type=self.schedule.engine_type if self.schedule else "browser",
                    headless=self.schedule.headless if self.schedule else True,
                )
                target_dt = test_sched.get_target_datetime()
                diff_sec = (target_dt - now_in_selected).total_seconds()

                if diff_sec <= 0:
                    console.print("[yellow]Notice: This time is in the past! Monitor will execute check-in immediately when started.[/yellow]")

                self.schedule = test_sched
                self._save_data()
                logger.info(f"Updated schedule: {target_dt.isoformat()}")
                console.print(f"[green]✓ Check-in time set to: {target_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}[/green]")
                break
            except Exception as e:
                console.print(f"[red]Invalid date/time: {e}[/red]")

    def run_monitor(self, simulate: bool = False):
        """Runs the live countdown and high-precision T polling scheduler."""
        if not self.reservations:
            console.print("[red]Cannot start: No reservations configured. Please add at least one reservation first.[/red]")
            return

        if not self.schedule:
            console.print("[red]Cannot start: No check-in time configured. Please set the schedule first.[/red]")
            return

        # Reset statuses
        for r in self.reservations:
            r.status = CheckInStatus.WAITING
            r.boarding_passes.clear()
            r.last_message = None

        engine = "mock" if simulate else self.schedule.engine_type
        headless = self.schedule.headless

        console.rule(f"[bold {'magenta' if simulate else 'green'}]Starting Check-In Monitor[/bold {'magenta' if simulate else 'green'}]")

        target_dt = self.schedule.get_target_datetime()
        lead_str = "exact time T" if self.schedule.lead_time_seconds == 0 else f"T - {self.schedule.lead_time_seconds:.1f}s"
        console.print(f"Target: [bold]{target_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}[/bold]")
        console.print(f"Polling Mode: [bold]Starts at {lead_str}, retrying every {self.schedule.poll_interval_seconds:.1f}s (or immediately if attempt takes longer)[/bold]")
        engine_label = "MOCK SIMULATOR" if simulate else ("STEALTH BROWSER (Playwright)" if engine == "browser" else "DIRECT API (curl_cffi)")
        console.print(f"Engine: [bold green]{engine_label}[/bold green] (Logging to checkin_debug.log)\n")

        # Setup Live Display
        log_messages: List[str] = []

        def on_attempt(result: CheckInAttemptResult, res: Reservation):
            log_messages.append(f"[{datetime.fromtimestamp(result.timestamp).strftime('%H:%M:%S')}] PNR {res.confirmation_number} ({res.passenger.last_name}): {result.message}")
            if len(log_messages) > 12:
                log_messages.pop(0)

        scheduler = CheckInScheduler(
            reservations=self.reservations,
            schedule=self.schedule,
            engine_type=engine,
            headless=headless,
            clock=self.clock,
            on_attempt_result_callback=on_attempt,
        )

        with Live(console=console, refresh_per_second=4) as live:
            def on_tick(data: dict):
                phase = data.get("phase")
                now_epoch = data.get("current_atomic")
                tgt_epoch = data.get("target_epoch")
                diff = now_epoch - tgt_epoch

                status_table = Table(title="Flight Check-In Live Status", border_style="cyan")
                status_table.add_column("PNR", style="cyan")
                status_table.add_column("Passenger", style="magenta")
                status_table.add_column("Status", justify="center")
                status_table.add_column("Assigned Boarding", style="bold green")
                status_table.add_column("Latest Event", style="dim")

                for r in self.reservations:
                    status_style = {
                        CheckInStatus.WAITING: "yellow",
                        CheckInStatus.POLLING: "bold cyan",
                        CheckInStatus.TOO_EARLY: "yellow",
                        CheckInStatus.SUCCESS: "bold green",
                        CheckInStatus.FAILED: "bold red",
                        CheckInStatus.BLOCKED: "bold red",
                    }.get(r.status, "white")

                    passes_str = ", ".join([bp.position_str for bp in r.boarding_passes]) if r.boarding_passes else "-"
                    status_table.add_row(
                        r.confirmation_number,
                        r.passenger.full_name,
                        Text(r.status.value, style=status_style),
                        passes_str,
                        r.last_message or "Awaiting start...",
                    )

                timing_text = Text()
                timing_text.append(f"Phase: {phase}  |  Atomic Skew: {self.clock.offset:+.3f}s\n", style="bold")
                if diff < 0:
                    timing_text.append(f"⏱️ Countdown to T: {abs(diff):.2f}s remaining\n", style="bold yellow")
                else:
                    timing_text.append(f"⚡ Time since T: +{diff:.2f}s\n", style="bold green")

                if log_messages:
                    timing_text.append("\nRecent Activity:\n", style="bold")
                    for m in log_messages[-5:]:
                        timing_text.append(f"  {m}\n", style="dim")

                live.update(
                    Panel(
                        Align.center(status_table),
                        title=f"Monitoring Window (Target: {target_dt.strftime('%H:%M:%S')})",
                        subtitle=timing_text,
                        border_style="green" if phase == "POLLING" else "blue",
                    )
                )

            scheduler.on_tick_callback = on_tick
            try:
                scheduler.run()
            except KeyboardInterrupt:
                scheduler.cancel()
                console.print("\n[yellow]Check-in monitor cancelled by user.[/yellow]")

        self._save_data()
        self.print_final_results()

    def print_final_results(self):
        """Displays final summary of all boarding passes."""
        console.rule("[bold green]Check-In Results Summary[/bold green]")
        table = Table(border_style="green")
        table.add_column("Confirmation (PNR)", style="cyan")
        table.add_column("Passenger Name", style="magenta")
        table.add_column("Status", justify="center")
        table.add_column("Boarding Group & Position", style="bold green")
        table.add_column("Flight", style="cyan")

        for r in self.reservations:
            status_style = "bold green" if r.status == CheckInStatus.SUCCESS else "bold red"
            if r.boarding_passes:
                for bp in r.boarding_passes:
                    table.add_row(
                        r.confirmation_number,
                        bp.passenger_name,
                        Text(r.status.value, style=status_style),
                        bp.position_str,
                        f"{bp.flight_number or 'WN'} ({bp.departure_airport or ''} -> {bp.arrival_airport or ''})",
                    )
            else:
                table.add_row(
                    r.confirmation_number,
                    r.passenger.full_name,
                    Text(r.status.value, style=status_style),
                    "None assigned",
                    "-",
                )

        console.print(table)
        Prompt.ask("\n[bold cyan]Press Enter to return to main menu[/bold cyan]", default="")

    def run_dry_run_simulation(self):
        """Quick simulation mode: creates a test schedule 8 seconds in the future to test the exact workflow."""
        console.rule("[bold magenta]🧪 Instant Dry-Run / Test Simulation[/bold magenta]")
        console.print("[dim]This mode schedules a test check-in 8 seconds from now to demonstrate the 3-second lead-in, 1-second retry cadence, simulated 'too early' responses, and boarding pass assignment.[/dim]\n")

        if not self.reservations:
            console.print("[yellow]Adding temporary demo passengers for the simulation...[/yellow]")
            self.reservations = [
                Reservation(
                    confirmation_number="SW77A1",
                    passenger=Passenger(first_name="ALICE", last_name="JOHNSON"),
                ),
                Reservation(
                    confirmation_number="SW77A2",
                    passenger=Passenger(first_name="BOB", last_name="JOHNSON"),
                ),
            ]

        local_tz = "America/Chicago"
        sim_time = (datetime.now(ZoneInfo(local_tz)) + timedelta(seconds=8)).strftime("%Y-%m-%d %H:%M:%S")
        sim_schedule = CheckInSchedule(
            checkin_time_str=sim_time,
            timezone=local_tz,
            lead_time_seconds=0.0,
            poll_interval_seconds=0.5,
            max_poll_seconds=15.0,
            engine_type="mock",
        )

        original_schedule = self.schedule
        self.schedule = sim_schedule

        try:
            self.run_monitor(simulate=True)
        finally:
            self.schedule = original_schedule

    def settings_menu(self):
        """Allows configuring engine selection, polling intervals, and lead times."""
        console.rule("[bold cyan]Engine & Timing Configuration[/bold cyan]")
        lead = self.schedule.lead_time_seconds if self.schedule else 0.0
        interval = self.schedule.poll_interval_seconds if self.schedule else 0.5
        max_poll = self.schedule.max_poll_seconds if self.schedule else 30.0
        curr_engine = self.schedule.engine_type if self.schedule else "browser"
        curr_headless = self.schedule.headless if self.schedule else True

        console.print(f"Current Engine: [bold green]{curr_engine.upper()}[/bold green] (headless={curr_headless})")
        lead_str = "exact time T (0.0s)" if lead == 0 else f"T - {lead}s"
        console.print(f"Current Lead Time: [bold cyan]{lead}s[/bold cyan] (Starts polling at {lead_str})")
        console.print(f"Current Poll Interval: [bold cyan]{interval}s[/bold cyan] (Retries every {interval}s or immediately)")
        console.print(f"Max Polling Window: [bold cyan]{max_poll}s[/bold cyan]\n")

        console.print("[bold]Select Check-In Engine:[/bold]")
        console.print("[1] Stealth Headless Browser (Playwright) - [green]RECOMMENDED: Renders real page, avoids 403 blocks[/green]")
        console.print("[2] Headed Browser (Visible Window) - [yellow]Displays actual browser window for troubleshooting[/yellow]")
        console.print("[3] Direct HTTP API (curl_cffi) - [dim]Fastest latency, but may be blocked by Akamai on some networks[/dim]")

        eng_choice = Prompt.ask("Choose engine option", choices=["1", "2", "3"], default="1")
        if eng_choice == "1":
            new_engine, new_headless = "browser", True
        elif eng_choice == "2":
            new_engine, new_headless = "browser", False
        else:
            new_engine, new_headless = "api", True

        new_lead = Prompt.ask("Enter lead time in seconds (e.g. 0.0 for exact check-in time)", default=str(lead))
        new_interval = Prompt.ask("Enter poll interval in seconds (e.g. 0.5)", default=str(interval))
        new_max = Prompt.ask("Enter maximum poll window in seconds (e.g. 30.0)", default=str(max_poll))

        try:
            if self.schedule:
                self.schedule.engine_type = new_engine
                self.schedule.headless = new_headless
                self.schedule.lead_time_seconds = float(new_lead)
                self.schedule.poll_interval_seconds = float(new_interval)
                self.schedule.max_poll_seconds = float(new_max)
                self._save_data()
            console.print("[green]✓ Settings updated successfully.[/green]")
        except ValueError:
            console.print("[red]Invalid numbers provided. Settings not changed.[/red]")

        Prompt.ask("\n[bold cyan]Press Enter to return[/bold cyan]", default="")

    def view_logs_menu(self):
        """Displays recent diagnostic logs from checkin_debug.log."""
        console.rule("[bold cyan]📜 Diagnostic Logs (checkin_debug.log)[/bold cyan]")
        logs = read_recent_logs(lines=40)
        console.print(Panel(Text(logs, style="dim green"), title="Last 40 Log Entries", border_style="cyan"))
        Prompt.ask("\n[bold cyan]Press Enter to return[/bold cyan]", default="")

    def run_main_loop(self):
        """Interactive main menu loop."""
        while True:
            console.clear()
            self.print_banner()
            self.display_status_overview()

            console.print("\n[bold]Main Menu:[/bold]")
            console.print("[1] ➕ Add Passenger & Confirmation Number (Supports multiple)")
            console.print("[2] ❌ View / Remove Reservations")
            console.print("[3] ⏰ Set Check-in Date, Time & Timezone (24-Hour Format)")
            console.print("[4] 🚀 Start Live Check-In Monitor (Armed for Target Time)")
            console.print("[5] 🧪 Run Instant Dry-Run / Test Simulation (Starts in 8s)")
            console.print("[6] ⚙️ Engine & Timing Settings (Browser vs. API)")
            console.print("[7] 📜 View Diagnostic Logs (checkin_debug.log)")
            console.print("[0] 🚪 Exit")

            choice = Prompt.ask("\nSelect option", choices=["1", "2", "3", "4", "5", "6", "7", "0"], default="1")

            if choice == "1":
                self.add_reservations_menu()
            elif choice == "2":
                self.remove_reservation_menu()
            elif choice == "3":
                self.set_schedule_menu()
            elif choice == "4":
                self.run_monitor(simulate=False)
            elif choice == "5":
                self.run_dry_run_simulation()
            elif choice == "6":
                self.settings_menu()
            elif choice == "7":
                self.view_logs_menu()
            elif choice == "0":
                console.print("\n[cyan]Goodbye! Have a safe flight.[/cyan]")
                sys.exit(0)
