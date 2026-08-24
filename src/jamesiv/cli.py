"""Command line interface."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import __version__
from .config import Config, Secrets, load_config, load_secrets
from .hunter import Hunter
from .matching import candidate_days, describe_target, drop_target_day
from .models import ResyError
from .notify import Notifier
from .resy import DEFAULT_API_KEY, ResyClient
from .state import Store
from .timeutil import humanize_delta, now_nyc, nyc_at, today_nyc

app = typer.Typer(
    add_completion=False,
    help="James IV -- hunt hard-to-get NYC restaurant reservations on Resy.",
    no_args_is_help=True,
)
console = Console()

DEFAULT_CONFIG = "config.yaml"


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _load(config_path: str) -> tuple[Config, Secrets]:
    config = load_config(config_path)
    secrets = load_secrets()
    _setup_logging(config.settings.log_level)
    return config, secrets


def _make_client(config: Config, secrets: Secrets) -> ResyClient:
    return ResyClient(
        api_key=secrets.resy_api_key or DEFAULT_API_KEY,
        rate=config.settings.request_rate,
        burst=config.settings.request_burst,
    )


async def _with_hunter(config: Config, secrets: Secrets, fn):
    client = _make_client(config, secrets)
    notifier = Notifier(secrets)
    store = Store(config.settings.state_path)
    hunter = Hunter(
        config=config, secrets=secrets, client=client, store=store, notifier=notifier
    )
    try:
        return await fn(hunter)
    finally:
        await client.aclose()
        await notifier.aclose()
        store.close()


# ------------------------------------------------------------------- commands


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"James IV {__version__}")


@app.command()
def run(
    config_path: str = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Find and rank, but never book."),
) -> None:
    """Run every engine for every enabled target. This is the main command."""
    config, secrets = _load(config_path)
    if dry_run:
        config.settings.dry_run = True

    if not secrets.has_credentials:
        console.print("[red]No Resy credentials.[/] Set RESY_EMAIL and RESY_PASSWORD in .env")
        raise typer.Exit(1)
    if not secrets.has_notifier:
        console.print("[yellow]No notification channel configured -- alerts go to the log only.[/]")

    async def _run(hunter: Hunter):
        await hunter.run()

    try:
        asyncio.run(_with_hunter(config, secrets, _run))
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/]")


@app.command()
def check(
    target_name: str | None = typer.Argument(None, help="Target name or slug; omit for all."),
    config_path: str = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
) -> None:
    """One-shot availability check. Never books, never alerts."""
    config, secrets = _load(config_path)
    targets = config.active_targets
    if target_name:
        found = config.target(target_name)
        if found is None:
            console.print(f"[red]No target named {target_name!r}[/]")
            raise typer.Exit(1)
        targets = [found]

    async def _check(hunter: Hunter):
        await hunter.login()
        for target in targets:
            days = candidate_days(target, today_nyc())
            console.print(f"\n[bold]{target.name}[/]  [dim]{describe_target(target)}[/]")
            console.print(f"[dim]checking {len(days)} date(s)[/]")
            slots = await hunter.search(target, days)
            if not slots:
                console.print("  [dim]nothing available[/]")
                continue
            table = Table(show_header=True, header_style="bold")
            table.add_column("Date")
            table.add_column("Time")
            table.add_column("Seating")
            table.add_column("Party")
            for slot in slots[:20]:
                table.add_row(
                    slot.day.strftime("%a %b %-d"),
                    slot.clock,
                    slot.seating_type,
                    str(slot.party_size),
                )
            console.print(table)
            if len(slots) > 20:
                console.print(f"  [dim]... and {len(slots) - 20} more[/]")

    asyncio.run(_with_hunter(config, secrets, _check))


@app.command()
def snipe(
    target_name: str = typer.Argument(..., help="Target name or slug."),
    config_path: str = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    now: bool = typer.Option(False, "--now", help="Fire immediately instead of waiting."),
) -> None:
    """Arm a single drop-time snipe and exit after it fires."""
    config, secrets = _load(config_path)
    target = config.target(target_name)
    if target is None:
        console.print(f"[red]No target named {target_name!r}[/]")
        raise typer.Exit(1)
    if target.drop is None:
        console.print(f"[red]{target.name} has no `drop:` block configured.[/]")
        raise typer.Exit(1)

    async def _snipe(hunter: Hunter):
        await hunter.login()
        if now:
            target.drop.at = now_nyc().time()
        booking = await hunter.snipe(target)
        if booking:
            console.print(f"[green bold]Booked:[/] {booking.slot}")
        else:
            console.print("[yellow]No booking.[/]")

    try:
        asyncio.run(_with_hunter(config, secrets, _snipe))
    except KeyboardInterrupt:
        console.print("\n[dim]Disarmed.[/]")


@app.command()
def venue(
    slug: str = typer.Argument(
        ..., help="Resy URL slug, e.g. 'tatiana' from resy.com/cities/ny/tatiana"
    ),
    location: str = typer.Option("ny", "--location", "-l"),
    config_path: str = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
) -> None:
    """Resolve a Resy URL slug to a venue id."""
    config, secrets = _load(config_path)

    async def _venue(hunter: Hunter):
        await hunter.login()
        try:
            found = await hunter.client.venue_by_slug(slug, location=location)
        except ResyError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        console.print(f"[green]{found.name}[/] -> venue_id: [bold]{found.id}[/]")

    asyncio.run(_with_hunter(config, secrets, _venue))


@app.command()
def status(
    config_path: str = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    events: int = typer.Option(15, "--events", "-e", help="How many recent events to show."),
) -> None:
    """Show current bookings, target schedule, and recent activity."""
    config, _ = _load(config_path)
    store = Store(config.settings.state_path)
    try:
        rows = store.active_bookings()
        if rows:
            table = Table(title="Confirmed reservations", header_style="bold green")
            table.add_column("Target")
            table.add_column("Date")
            table.add_column("Time")
            table.add_column("Seating")
            table.add_column("Party")
            table.add_column("Token", style="dim")
            for row in rows:
                start = row["start_time"][11:16]
                table.add_row(
                    row["target_name"], row["day"], start, row["seating_type"],
                    str(row["party_size"]), row["resy_token"][:16],
                )
            console.print(table)
        else:
            console.print("[dim]No confirmed reservations yet.[/]")

        schedule = Table(title="Targets", header_style="bold")
        schedule.add_column("Target")
        schedule.add_column("Action")
        schedule.add_column("Booked")
        schedule.add_column("Next drop")
        now = now_nyc()
        for target in config.active_targets:
            if target.drop and target.drop.enabled:
                next_drop = nyc_at(now.date(), target.drop.at)
                if next_drop <= now:
                    next_drop = nyc_at(now.date() + timedelta(days=1), target.drop.at)
                day = drop_target_day(target, today_nyc())
                drop_text = (
                    f"{humanize_delta((next_drop - now).total_seconds())} "
                    f"({day.isoformat() if day else 'no matching date'})"
                )
            else:
                drop_text = "[dim]poll only[/]"
            schedule.add_row(
                target.name,
                target.action,
                f"{store.booking_count(target.name)}/{target.max_bookings}",
                drop_text,
            )
        console.print(schedule)

        recent = store.recent_events(events)
        if recent:
            console.print("\n[bold]Recent activity[/]")
            for row in recent:
                console.print(
                    f"  [dim]{row['at'][:19]}[/] [{row['level']}] "
                    f"{row['target'] or '-'}: {row['message']}"
                )
    finally:
        store.close()


@app.command()
def cancel(
    resy_token: str = typer.Argument(..., help="Token from `james status`."),
    config_path: str = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Cancel a reservation the bot booked."""
    config, secrets = _load(config_path)
    if not yes:
        typer.confirm(f"Cancel reservation {resy_token}?", abort=True)

    async def _cancel(hunter: Hunter):
        await hunter.login()
        ok = await hunter.client.cancel(resy_token)
        if ok:
            hunter.store.mark_cancelled(resy_token)
            console.print("[green]Cancelled.[/]")
        else:
            console.print("[red]Cancel failed -- check resy.com directly.[/]")

    asyncio.run(_with_hunter(config, secrets, _cancel))


@app.command()
def doctor(
    config_path: str = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
) -> None:
    """Check credentials, notifications, and every target's venue slug."""
    config, secrets = _load(config_path)
    problems = 0

    console.print("[bold]Credentials[/]")
    if secrets.resy_auth_token:
        console.print("  [green]ok[/] using RESY_AUTH_TOKEN")
    elif secrets.resy_email and secrets.resy_password:
        console.print(f"  [green]ok[/] email/password for {secrets.resy_email}")
    else:
        console.print("  [red]missing[/] set RESY_EMAIL and RESY_PASSWORD in .env")
        problems += 1

    console.print("\n[bold]Notifications[/]")
    if secrets.ntfy_topic:
        console.print(f"  [green]ok[/] ntfy topic {secrets.ntfy_topic}")
    if secrets.pushover_token and secrets.pushover_user:
        console.print("  [green]ok[/] pushover configured")
    if not secrets.has_notifier:
        console.print("  [yellow]none[/] you will only see alerts in the log")

    async def _probe(hunter: Hunter):
        nonlocal problems
        console.print("\n[bold]Resy session[/]")
        try:
            await hunter.login()
            pm = hunter.client.payment_method_id
            console.print("  [green]ok[/] authenticated")
            if pm:
                console.print(f"  [green]ok[/] payment method {pm}")
            else:
                console.print("  [yellow]warn[/] no payment method -- booking will likely fail")
        except Exception as exc:
            console.print(f"  [red]fail[/] {exc}")
            problems += 1
            return

        console.print("\n[bold]Targets[/]")
        for target in config.targets:
            mark = "" if target.enabled else " [dim](disabled)[/]"
            try:
                venue_id = await hunter.resolve_venue(target)
                days = candidate_days(target, today_nyc())
                console.print(
                    f"  [green]ok[/] {target.name}{mark} -> venue {venue_id}, "
                    f"{len(days)} date(s), {describe_target(target)}"
                )
                if target.action == "book" and not hunter.client.payment_method_id:
                    console.print(
                        "      [yellow]warn[/] action=book but no payment method on file"
                    )
                if target.drop and target.drop.enabled:
                    day = drop_target_day(target, today_nyc())
                    if day is None:
                        console.print(
                            "      [yellow]warn[/] today's drop date fails this target's "
                            "weekday/date filters -- nothing will be sniped today"
                        )
            except Exception as exc:
                console.print(f"  [red]fail[/] {target.name}: {exc}")
                problems += 1

    asyncio.run(_with_hunter(config, secrets, _probe))

    console.print()
    if problems:
        console.print(f"[red]{problems} problem(s) found.[/]")
        raise typer.Exit(1)
    console.print("[green]All checks passed.[/]")


@app.command("test-notify")
def test_notify(
    config_path: str = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
) -> None:
    """Send a test push so you know alerts actually reach your phone."""
    config, secrets = _load(config_path)

    async def _send():
        notifier = Notifier(secrets)
        try:
            await notifier.send(
                "James IV test",
                "If you can read this, your alerts are working.",
                tags=["white_check_mark"],
            )
        finally:
            await notifier.aclose()

    asyncio.run(_send())
    console.print("[green]Sent.[/] Check your phone.")


@app.command("init")
def init_config(
    config_path: str = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
) -> None:
    """Copy the example config and env template into place."""
    for src, dst in (("config.example.yaml", config_path), (".env.example", ".env")):
        source, dest = Path(src), Path(dst)
        if not source.exists():
            console.print(f"[red]{src} is missing from the repo.[/]")
            continue
        if dest.exists():
            console.print(f"[yellow]skip[/] {dst} already exists")
            continue
        dest.write_text(source.read_text())
        console.print(f"[green]created[/] {dst}")
    console.print("\nNow edit [bold].env[/] with your Resy login, then run [bold]james doctor[/].")


if __name__ == "__main__":
    app()
