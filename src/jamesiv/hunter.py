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
from .matching import best_slots, candidate_days, drop_target_day, slot_matches
from .models import AuthError, Booking, RateLimited, ResyError, Slot, SlotTaken
from .notify import PRIORITY_HIGH, PRIORITY_LOW, PRIORITY_URGENT, Notifier, resy_url
from .policy import DropPolicy, extract_policy
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
        self._auth_lock = asyncio.Lock()
        self._auth_generation = 0
        self._policy_seen: dict[str, str] = {}
        self._blind_polls: dict[str, int] = {}

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

    async def recover_auth(self, seen_generation: int) -> bool:
        """Re-login after an auth failure, once, on behalf of every engine.

        Every target runs its own task, so an expired token surfaces as a burst
        of simultaneous AuthErrors. `seen_generation` is the auth epoch the
        caller was using: if another task has already re-logged-in since then,
        this returns immediately instead of stampeding the login endpoint.
        """
        async with self._auth_lock:
            if self._auth_generation != seen_generation:
                return True  # somebody else already fixed it

            if self.secrets.resy_auth_token:
                log.error(
                    "RESY_AUTH_TOKEN was rejected and cannot be refreshed automatically. "
                    "Grab a fresh token from the browser, or switch to RESY_EMAIL/RESY_PASSWORD."
                )
                await self.notifier.send(
                    "James IV: token expired",
                    "RESY_AUTH_TOKEN was rejected. The bot cannot book until you replace it.",
                    priority=PRIORITY_URGENT,
                )
                return False

            try:
                await self.login()
            except Exception as exc:
                log.error("Re-authentication failed: %s", exc)
                await self.notifier.send(
                    "James IV: re-auth failed",
                    f"{exc}\nStill running and will keep retrying, but cannot book right now.",
                    priority=PRIORITY_HIGH,
                )
                return False

            self._auth_generation += 1
            log.warning("Re-authenticated successfully (auth generation %d)", self._auth_generation)
            return True

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
        now = now_nyc()
        errors = 0
        attempts_made = 0
        last_error: ResyError | None = None

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
                except AuthError:
                    # Must escape: AuthError subclasses ResyError, and swallowing
                    # it here would leave the bot polling forever against a dead
                    # session, finding nothing and never telling anyone.
                    raise
                except ResyError as exc:
                    errors += 1
                    last_error = exc
                    log.debug("find failed for %s on %s: %s", target.name, day, exc)
                    continue

                attempts_made += 1
                # Filter here rather than after the loop: a party size that
                # returns only slots we would reject (wrong time, too soon, wrong
                # room) has not actually produced inventory, and we should still
                # try the fallback size. Breaking on the raw list would silently
                # skip a bookable 3-top because a useless 2-top existed.
                matched = [s for s in slots if slot_matches(target, s, now=now)]
                found.extend(matched)
                if matched:
                    break

        if errors and attempts_made == 0 and last_error is not None:
            # Every single request failed: we are blind, not unlucky. Propagate
            # so the poll loop can count it and eventually raise the alarm.
            raise last_error

        return best_slots(target, found, now=now)

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
            except AuthError:
                raise  # see search(): never let this be mistaken for a dead slot
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
            generation = self._auth_generation
            try:
                await self.poll_once(target)
            except AuthError:
                # Never fatal: an expired session at 3am must not silently take
                # the whole bot down before a 9am drop.
                if not await self.recover_auth(generation):
                    await asyncio.sleep(300)
                continue
            except RateLimited as exc:
                await asyncio.sleep(exc.retry_after)
                continue
            except ResyError as exc:
                await self._note_blind_poll(target, exc)
            except Exception as exc:
                log.exception("Poll failed for %s: %s", target.name, exc)
            else:
                self._clear_blind_poll(target)

            base = target.poll_interval_seconds
            jitter = base * target.poll_jitter
            await asyncio.sleep(max(5.0, random.uniform(base - jitter, base + jitter)))

    # ---------------------------------------------------------- drop discovery

    async def resolve_drop_policy(self, target: Target) -> DropPolicy | None:
        """Read the venue's release policy off its own Resy page."""
        try:
            raw = await self.client.venue_raw(target.slug, location=target.location)
        except AuthError:
            raise
        except ResyError as exc:
            log.warning("Could not fetch %s's page for policy discovery: %s", target.name, exc)
            return None
        return extract_policy(raw)

    async def apply_drop_policy(self, target: Target) -> None:
        """Overwrite drop timing with whatever the venue's page states.

        Called once per scheduler cycle (i.e. daily), so a venue that changes
        its policy mid-season is picked up without a restart. Explicit config
        values survive as fallbacks for anything the page does not state, and
        every applied change is logged so `auto` never means `silent`.
        """
        assert target.drop is not None
        policy = await self.resolve_drop_policy(target)
        marker = policy.describe() if policy else None

        if policy is None:
            if self._policy_seen.get(target.name, "") != "none":
                self._policy_seen[target.name] = "none"
                log.warning(
                    "%s: no release policy found on the venue page; using configured "
                    "drop settings (%s, %d days ahead)",
                    target.name, target.drop.at.strftime("%H:%M"), target.drop.days_ahead,
                )
            return

        if policy.cadence == "monthly":
            if self._policy_seen.get(target.name) != marker:
                self._policy_seen[target.name] = marker
                log.warning(
                    "%s: the venue releases inventory monthly (%r), which the daily "
                    "snipe scheduler does not model. Keeping your configured settings; "
                    "consider pinning explicit `dates` for the release day.",
                    target.name, policy.snippet,
                )
                await self.notifier.send(
                    f"{target.name}: monthly release detected",
                    f"\"{policy.snippet}\"\nAuto drop timing is off for this venue.",
                    priority=PRIORITY_HIGH,
                )
            return

        updates = {}
        if policy.days_ahead is not None and policy.days_ahead != target.drop.days_ahead:
            updates["days_ahead"] = policy.days_ahead
        if policy.at is not None and policy.at != target.drop.at:
            updates["at"] = policy.at
        if updates:
            target.drop = target.drop.model_copy(update=updates)

        if self._policy_seen.get(target.name) != marker:
            self._policy_seen[target.name] = marker
            log.warning(
                "%s: release policy from venue page -- %s (source: %s%s)",
                target.name, policy.describe(), policy.source,
                f', "{policy.snippet}"' if policy.snippet else "",
            )

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
        offset = await measure_clock_offset(
            self.client.http, "/3/venue", probes=drop.clock_probes
        )
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
        started = now_utc()
        deadline = started + timedelta(seconds=drop.burst_seconds)
        attempts = 0
        stop = asyncio.Event()
        result: list[Booking] = []

        seen_inventory = False
        handled = False  # a terminal outcome was reported (booked / alerted / dry run)

        async def worker(worker_id: int) -> None:
            nonlocal attempts, seen_inventory, handled
            await asyncio.sleep(worker_id * (drop.burst_interval_ms / 1000.0)
                                / max(1, drop.burst_concurrency))
            while not stop.is_set() and now_utc() < deadline:
                if attempts >= drop.max_requests:
                    stop.set()
                    return
                attempts += 1
                try:
                    for party_size in target.party_sizes:
                        slots = await self.client.find(
                            venue_id=venue_id,
                            day=day.isoformat(),
                            party_size=party_size,
                            throttle=False,
                            retries=0,  # the burst loop IS the retry; a backoff
                                        # here would just slow the next attempt
                        )
                        ranked = best_slots(target, slots, now=now_nyc())
                        if not ranked:
                            continue

                        if not seen_inventory:
                            seen_inventory = True
                            log.warning(
                                "Drop landed for %s after %d attempts: %d slot(s)",
                                target.name, attempts, len(ranked),
                            )

                        if target.action == "book":
                            booking = await self.try_book(target, ranked)
                            if booking is not None:
                                result.append(booking)
                                handled = True
                                stop.set()
                                return
                            if self.config.settings.dry_run:
                                # try_book declined because of dry run, not
                                # because we were outraced. Stop, or the
                                # rehearsal burns the full burst window and
                                # logs an alarming stream of lost races.
                                handled = True
                                stop.set()
                                return
                            # Lost the race on every candidate. Do NOT stop --
                            # at a busy drop, tables bounce back within seconds
                            # as other people's holds expire, and the remaining
                            # burst window is exactly when that happens.
                            log.info(
                                "%s: lost every candidate; still hunting for %.0fs",
                                target.name,
                                max(0.0, (deadline - now_utc()).total_seconds()),
                            )
                        else:
                            await self.alert_slots(target, ranked[:3])
                            handled = True
                            stop.set()
                            return
                except RateLimited as exc:
                    log.warning("Rate limited during burst; pausing %.1fs", exc.retry_after)
                    await asyncio.sleep(exc.retry_after)
                except AuthError:
                    stop.set()
                    raise
                except ResyError:
                    pass

                # Decay the cadence once the release window has passed.
                elapsed = (now_utc() - started).total_seconds()
                interval = drop.burst_interval_ms / 1000.0
                if elapsed > drop.aggressive_seconds:
                    interval *= drop.decay_factor
                await asyncio.sleep(interval)

        await asyncio.gather(*(worker(i) for i in range(drop.burst_concurrency)))

        if not result and not handled:
            if seen_inventory:
                # We saw tables and lost every race. Config is fine; we were slow.
                log.warning(
                    "%s: inventory appeared but every attempt lost the race (%d attempts). "
                    "Consider a VPS closer to us-east, or a larger burst_concurrency.",
                    target.name, attempts,
                )
                self.store.log_event("snipe-lost", f"{day.isoformat()} outraced", target.name)
                await self.notifier.send(
                    f"Missed the drop: {target.name}",
                    f"{day:%a %b %-d} inventory appeared but sold out before we booked.",
                    priority=PRIORITY_HIGH,
                )
            else:
                window = (now_utc() - started).total_seconds()
                log.warning(
                    "%s: no inventory appeared within the request budget (%d request(s) "
                    "over %.1fs). Either it sold out before our first shot, the release "
                    "ran late, or drop.at / drop.days_ahead is wrong -- `james policy` "
                    "shows what the venue states. The poll loop keeps watching this date.",
                    target.name, attempts, window,
                )
                self.store.log_event(
                    "snipe-miss", f"{day.isoformat()} after {attempts} attempts", target.name
                )
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

            if drop.auto:
                try:
                    await self.apply_drop_policy(target)
                except AuthError:
                    generation = self._auth_generation
                    await self.recover_auth(generation)
                except Exception as exc:
                    log.warning("Policy discovery failed for %s: %s", target.name, exc)
                drop = target.drop  # apply_drop_policy may have replaced it

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

            generation = self._auth_generation
            try:
                await self.snipe(target)
            except AuthError:
                if await self.recover_auth(generation):
                    try:
                        await self.snipe(target)  # token was the only problem; go again
                    except Exception as exc:
                        log.exception("Snipe retry failed for %s: %s", target.name, exc)
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

        # A heartbeat on boot is the cheapest possible check that the whole
        # chain works -- credentials, network, and push delivery -- while you
        # are watching, rather than at 9am when you are not.
        await self.notifier.send(
            "James IV started",
            self._startup_summary(targets),
            priority=PRIORITY_LOW,
            tags=["eyes"],
        )

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            raise
        finally:
            for task in tasks:
                task.cancel()

    async def _note_blind_poll(self, target: Target, exc: ResyError) -> None:
        """Count consecutive polls where every request failed, and raise the
        alarm once it stops looking like bad luck. A throttled or blocked bot
        that keeps quietly reporting "nothing available" is the one failure an
        unattended hunter cannot afford."""
        count = self._blind_polls.get(target.name, 0) + 1
        self._blind_polls[target.name] = count
        threshold = self.config.settings.blind_poll_alert_after
        log.warning("Poll for %s is blind (%d/%d): %s", target.name, count, threshold, exc)
        if count == threshold:
            await self.notifier.send(
                f"James IV cannot see availability: {target.name}",
                f"{count} polls in a row failed outright ({exc}).\n"
                "The bot is running but effectively blind -- Resy may be "
                "throttling this server's network. It will keep retrying and "
                "tell you when sight returns.",
                priority=PRIORITY_URGENT,
            )
            self.store.log_event("blind", str(exc), target.name)

    def _clear_blind_poll(self, target: Target) -> None:
        count = self._blind_polls.pop(target.name, 0)
        if count >= self.config.settings.blind_poll_alert_after:
            log.warning("%s: availability searches recovered after %d blind polls",
                        target.name, count)

    def _startup_summary(self, targets: list[Target]) -> str:
        lines = []
        if self.config.settings.dry_run:
            lines.append("DRY RUN -- nothing will actually be booked.")
        now = now_nyc()
        for target in targets:
            if target.drop and target.drop.enabled:
                next_drop = nyc_at(now.date(), target.drop.at)
                if next_drop <= now:
                    next_drop = nyc_at(now.date() + timedelta(days=1), target.drop.at)
                when = humanize_delta((next_drop - now).total_seconds())
                lines.append(f"{target.name}: {target.action}, next drop in {when}")
            else:
                lines.append(f"{target.name}: {target.action}, polling for cancellations")
        return "\n".join(lines)

    async def _reauth_loop(self) -> None:
        """Refresh the session periodically so a long run does not die at 3am."""
        interval = self.config.settings.reauth_interval_hours * 3600
        while True:
            await asyncio.sleep(interval)
            if self.secrets.resy_auth_token:
                continue  # a pasted token cannot be refreshed
            # Via login() rather than client.authenticate() so an explicitly
            # pinned RESY_PAYMENT_METHOD_ID is re-applied afterwards.
            await self.recover_auth(self._auth_generation)
