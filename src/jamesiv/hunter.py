"""The hunt: polling for cancellations, sniping drops, and booking.

Two engines run against the same target list.

**Poll** is the patient one. Hard NYC rooms leak inventory constantly as people
cancel inside the penalty window, and a 45-second poll catches a startling
amount of it. This is where most tables actually come from.

**Snipe** is the sharp one. It wakes up shortly before a venue's release time,
syncs the clock against Resy's servers, warms the connection, and fires a short
burst the instant inventory lands. This is the only way to get the genuinely
impossible rooms, where the entire month sells out in under two seconds.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, timedelta

from .config import Config, Secrets, Target
from .matching import best_slots, candidate_days, drop_target_day
from .models import AuthError, Booking, RateLimited, ResyError, Slot, SlotTaken
from .notify import PRIORITY_HIGH, PRIORITY_URGENT, Notifier, resy_url
from .resy import ResyClient
from .state import Store
from .timeutil import (
    ZERO_OFFSET,
    humanize_delta,
    measure_clock_offset,
    now_nyc,
    now_utc,
    nyc_at,
    sleep_until,
    today_nyc,
)

log = logging.getLogger("jamesiv.hunter")


class BookingBudgetExhausted(Exception):
    """The global per-run booking ceiling has been hit. Stop booking, keep alerting."""


class Hunter:
    def __init__(
        self,
        *,
        config: Config,
        secrets: Secrets,
        client: ResyClient,
        store: Store,
        notifier: Notifier,
    ):
        self.config = config
        self.secrets = secrets
        self.client = client
        self.store = store
        self.notifier = notifier
        self.bookings_this_run = 0
        self._venue_ids: dict[str, int] = {}
        self._book_lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup

    async def login(self) -> None:
        if self.secrets.resy_auth_token:
            self.client.set_token(
                self.secrets.resy_auth_token, self.secrets.resy_payment_method_id
            )
            log.info("Using RESY_AUTH_TOKEN from environment")
            return
        if not (self.secrets.resy_email and self.secrets.resy_password):
            raise AuthError("No credentials: set RESY_EMAIL and RESY_PASSWORD, or RESY_AUTH_TOKEN")
        await self.client.authenticate(self.secrets.resy_email, self.secrets.resy_password)
        if self.secrets.resy_payment_method_id:
            self.client.payment_method_id = self.secrets.resy_payment_method_id

    async def resolve_venue(self, target: Target) -> int:
        """Slug -> venue id, cached. Configured `venue_id` short-circuits the lookup."""
        if target.venue_id:
            return target.venue_id
        if target.slug in self._venue_ids:
            return self._venue_ids[target.slug]

        venue = await self.client.venue_by_slug(target.slug, location=target.location)
        self._venue_ids[target.slug] = venue.id
        log.info("Resolved %s -> %s", target.slug, venue)
        return venue.id

    # -------------------------------------------------------------- searching

    async def search(
        self, target: Target, days: list[date], *, throttle: bool = True
    ) -> list[Slot]:
        """Every acceptable slot across the given days, best-first."""
        venue_id = await self.resolve_venue(target)
        found: list[Slot] = []

        for day in days:
            for party_size in target.party_sizes:
                try:
                    slots = await self.client.find(
                        venue_id=venue_id,
                        day=day.isoformat(),
                        party_size=party_size,
                        throttle=throttle,
                    )
                except RateLimited as exc:
                    log.warning("Rate limited; backing off %.0fs", exc.retry_after)
                    await asyncio.sleep(exc.retry_after)
                    return best_slots(target, found)
                except ResyError as exc:
                    log.debug("find failed for %s on %s: %s", target.name, day, exc)
                    continue

                if slots:
                    found.extend(slots)
                    # The requested party size returned inventory, so there is no
                    # reason to spend requests probing fallback sizes on this day.
                    break

        return best_slots(target, found)

    # ---------------------------------------------------------------- actions

    async def handle(self, target: Target, slots: list[Slot]) -> Booking | None:
        """Act on ranked slots: book the best one, or alert about it."""
        if not slots:
            return None

        if target.action == "book":
            booking = await self.try_book(target, slots)
            if booking is not None:
                return booking
            if self.config.settings.dry_run:
                return None  # try_book already reported what it would have done
            # Booking failed on every candidate -- tell the user, the table may
            # still be reachable by hand for a few more seconds.
            await self.alert_slots(target, slots[:1], missed=True)
            return None

        await self.alert_slots(target, slots[:3])
        return None

    async def try_book(self, target: Target, slots: list[Slot]) -> Booking | None:
        """Walk ranked candidates until one books. Returns None if all are gone."""
        if self.store.booking_count(target.name) >= target.max_bookings:
            log.debug("%s already at its booking cap", target.name)
            return None

        if self.config.settings.dry_run:
            # Report once on what we would have taken, rather than walking the
            # candidate list and alerting on every one of them.
            best = slots[0]
            log.warning("DRY RUN -- would book %s for %s", best, target.name)
            self.store.log_event("dry-run", f"would book {best}", target.name)
            await self.alert_slots(target, [best], dry_run=True)
            return None

        for slot in slots[:4]:
            if self.store.has_booking_on(target.name, slot.day.isoformat()):
                continue
            try:
                return await self._book_one(target, slot)
            except SlotTaken:
                log.info("Beaten to %s -- trying next candidate", slot)
                continue
            except BookingBudgetExhausted:
                log.warning("Global booking budget reached; not booking %s", slot)
                return None
            except RateLimited as exc:
                log.warning("Rate limited mid-booking; backing off %.0fs", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
                return None
            except ResyError as exc:
                log.error("Booking error on %s: %s", slot, exc)
                continue
        return None

    async def _book_one(self, target: Target, slot: Slot) -> Booking:
        """details -> book, under a lock so two targets cannot race the budget."""
        async with self._book_lock:
            if self.bookings_this_run >= self.config.settings.max_bookings_per_run:
                raise BookingBudgetExhausted()
            if self.store.booking_count(target.name) >= target.max_bookings:
                raise SlotTaken(f"{target.name} hit its cap while we waited")

            book_token = await self.client.book_token_for(slot)
            resy_token, reservation_id = await self.client.book(slot, book_token)

            booking = Booking(
                resy_token=resy_token,
                reservation_id=reservation_id,
                slot=slot,
                target_name=target.name,
                booked_at=now_utc(),
            )
            self.store.record_booking(booking)
            self.store.log_event("booked", str(slot), target.name)
            self.bookings_this_run += 1

        log.warning("BOOKED %s -- %s", target.name, slot)
        await self.notifier.send(
            f"Booked: {target.name}",
            f"{slot}\nConfirmation {reservation_id or resy_token}",
            priority=PRIORITY_URGENT,
            url=resy_url(
                target.slug,
                day=slot.day.isoformat(),
                party_size=slot.party_size,
                location=target.location,
            ),
            tags=["tada"],
        )
        return booking

    async def alert_slots(
        self,
        target: Target,
        slots: list[Slot],
        *,
        missed: bool = False,
        dry_run: bool = False,
    ) -> None:
        fresh = [s for s in slots if self.store.is_new_slot(s, target.name)]
        if not fresh:
            return

        if dry_run:
            title = f"[dry run] Would book: {target.name}"
        elif missed:
            if not self.config.settings.notify_on_miss:
                return
            title = f"Missed: {target.name}"
        else:
            title = f"Open table: {target.name}"

        body = "\n".join(str(s) for s in fresh)
        top = fresh[0]
        await self.notifier.send(
            title,
            body,
            priority=PRIORITY_HIGH if not missed else PRIORITY_HIGH,
            url=resy_url(
                target.slug,
                day=top.day.isoformat(),
                party_size=top.party_size,
                location=target.location,
            ),
            tags=["fork_and_knife"],
        )
        self.store.log_event("alert", body.replace("\n", " | "), target.name)

    # ------------------------------------------------------------- poll engine

    async def poll_once(self, target: Target) -> Booking | None:
        days = candidate_days(target, today_nyc())
        if not days:
            return None
        slots = await self.search(target, days)
        if slots:
            log.info("%s: %d matching slot(s), best = %s", target.name, len(slots), slots[0])
        return await self.handle(target, slots)

    async def poll_loop(self, target: Target) -> None:
        """Poll a target forever, with jitter, until it is satisfied."""
        log.info("Polling %s every ~%.0fs", target.name, target.poll_interval_seconds)
        while True:
            if self.store.booking_count(target.name) >= target.max_bookings:
                log.info("%s satisfied (%d booking(s)); stopping poll", target.name,
                         target.max_bookings)
                return
            try:
                await self.poll_once(target)
            except AuthError:
                raise
            except RateLimited as exc:
                await asyncio.sleep(exc.retry_after)
                continue
            except Exception as exc:
                log.exception("Poll failed for %s: %s", target.name, exc)

            base = target.poll_interval_seconds
            jitter = base * target.poll_jitter
            await asyncio.sleep(max(5.0, random.uniform(base - jitter, base + jitter)))

    # ------------------------------------------------------------ snipe engine

    async def snipe(self, target: Target) -> Booking | None:
        """Fire a burst at the exact moment a venue releases inventory."""
        drop = target.drop
        if drop is None or not drop.enabled:
            return None

        day = drop_target_day(target, today_nyc())
        if day is None:
            log.info("%s: next drop date does not match this target's filters; skipping",
                     target.name)
            return None

        venue_id = await self.resolve_venue(target)

        # Sync the clock and warm the socket while there is still time to spare.
        offset = await measure_clock_offset(self.client.http, "/3/venue")
        if offset.is_trustworthy:
            log.info(
                "Clock offset vs Resy: %+.3fs (±%.3fs, %d samples)",
                offset.offset, offset.uncertainty, offset.samples,
            )
        else:
            log.warning("Clock sync inconclusive; firing on the local clock")
            offset = ZERO_OFFSET
        await self.client.warm()

        drop_at_server = nyc_at(now_nyc().date(), drop.at)
        fire_at = offset.local_time_for_server_time(drop_at_server) - timedelta(
            milliseconds=drop.lead_ms
        )

        wait = (fire_at - now_utc()).total_seconds()
        if wait < -5.0:
            log.warning("%s: drop time already passed %s ago", target.name, humanize_delta(-wait))
            return None

        log.warning(
            "SNIPE ARMED %s -- %s inventory, firing in %s",
            target.name, day.isoformat(), humanize_delta(max(0, wait)),
        )
        await sleep_until(fire_at)

        return await self._burst(target, venue_id, day, drop)

    async def _burst(self, target: Target, venue_id: int, day: date, drop) -> Booking | None:
        """Hammer `find` for a bounded window, booking the first acceptable slot.

        Bounded on purpose: `burst_seconds` after the drop, either the inventory
        landed and we got a shot at it, or the room sold out and no amount of
        further requests will help.
        """
        deadline = now_utc() + timedelta(seconds=drop.burst_seconds)
        attempts = 0
        stop = asyncio.Event()
        result: list[Booking] = []

        async def worker(worker_id: int) -> None:
            nonlocal attempts
            await asyncio.sleep(worker_id * (drop.burst_interval_ms / 1000.0)
                                / max(1, drop.burst_concurrency))
            while not stop.is_set() and now_utc() < deadline:
                attempts += 1
                try:
                    for party_size in target.party_sizes:
                        slots = await self.client.find(
                            venue_id=venue_id,
                            day=day.isoformat(),
                            party_size=party_size,
                            throttle=False,
                        )
                        ranked = best_slots(target, slots)
                        if not ranked:
                            continue

                        stop.set()
                        log.warning(
                            "Drop landed for %s after %d attempts: %d slot(s)",
                            target.name, attempts, len(ranked),
                        )
                        if target.action == "book":
                            booking = await self.try_book(target, ranked)
                            if booking is not None:
                                result.append(booking)
                                return
                            await self.alert_slots(target, ranked[:1], missed=True)
                        else:
                            await self.alert_slots(target, ranked[:3])
                        return
                except RateLimited as exc:
                    log.warning("Rate limited during burst; pausing %.1fs", exc.retry_after)
                    await asyncio.sleep(exc.retry_after)
                except ResyError:
                    pass
                await asyncio.sleep(drop.burst_interval_ms / 1000.0)

        await asyncio.gather(*(worker(i) for i in range(drop.burst_concurrency)))

        if not result and not stop.is_set():
            log.warning(
                "%s: no inventory appeared in %.0fs (%d attempts). Sold out, or the "
                "drop time / days_ahead in your config is wrong.",
                target.name, drop.burst_seconds, attempts,
            )
            self.store.log_event("snipe-miss", f"{day.isoformat()} after {attempts} attempts",
                                 target.name)
        return result[0] if result else None

    async def snipe_scheduler(self, target: Target) -> None:
        """Sleep until tomorrow's drop, snipe, repeat."""
        drop = target.drop
        if drop is None or not drop.enabled:
            return

        while True:
            if self.store.booking_count(target.name) >= target.max_bookings:
                log.info("%s satisfied; stopping snipe scheduler", target.name)
                return

            now = now_nyc()
            next_drop = nyc_at(now.date(), drop.at)
            if next_drop <= now + timedelta(seconds=90):
                next_drop = nyc_at(now.date() + timedelta(days=1), drop.at)

            # Wake up early enough to sync clocks and warm the connection.
            wake_at = next_drop - timedelta(seconds=75)
            log.info(
                "%s: next drop %s (in %s)",
                target.name,
                next_drop.strftime("%a %b %d %H:%M:%S %Z"),
                humanize_delta((next_drop - now).total_seconds()),
            )
            await sleep_until(wake_at)  # aware datetimes compare across zones

            try:
                await self.snipe(target)
            except AuthError:
                raise
            except Exception as exc:
                log.exception("Snipe failed for %s: %s", target.name, exc)

            await asyncio.sleep(120)  # clear the drop window before rescheduling

    # ------------------------------------------------------------------- main

    async def run(self) -> None:
        """Start every engine for every active target and supervise them."""
        targets = self.config.active_targets
        if not targets:
            log.error("No enabled targets in config")
            return

        await self.login()
        self.store.prune()

        tasks: list[asyncio.Task] = []
        for target in targets:
            tasks.append(asyncio.create_task(self.poll_loop(target), name=f"poll:{target.name}"))
            if target.drop and target.drop.enabled:
                tasks.append(
                    asyncio.create_task(
                        self.snipe_scheduler(target), name=f"snipe:{target.name}"
                    )
                )

        tasks.append(asyncio.create_task(self._reauth_loop(), name="reauth"))

        log.warning(
            "James IV hunting %d target(s) with %d engine(s)%s",
            len(targets), len(tasks) - 1, " [DRY RUN]" if self.config.settings.dry_run else "",
        )

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            raise
        finally:
            for task in tasks:
                task.cancel()

    async def _reauth_loop(self) -> None:
        """Refresh the session periodically so a long run does not die at 3am."""
        interval = self.config.settings.reauth_interval_hours * 3600
        while True:
            await asyncio.sleep(interval)
            if self.secrets.resy_auth_token:
                continue  # a pasted token cannot be refreshed
            try:
                await self.client.authenticate(self.secrets.resy_email, self.secrets.resy_password)
            except Exception as exc:
                log.error("Re-auth failed: %s", exc)
                await self.notifier.send(
                    "James IV: re-auth failed",
                    f"{exc}\nThe bot is still running but may be unable to book.",
                    priority=PRIORITY_HIGH,
                )
