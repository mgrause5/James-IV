# James IV

A reservation hunter for hard-to-get New York City tables on Resy.

It runs two engines against your target list:

- **Snipe** — wakes up before a venue's release time, syncs its clock against
  Resy's servers, warms the connection, and fires a burst the instant inventory
  drops. This is the only thing that works for rooms where the whole month is
  gone in under two seconds.
- **Poll** — watches your date range continuously and pounces on cancellations.
  Less dramatic, and where most tables actually come from: hard rooms leak
  inventory all day as people cancel inside the penalty window.

Each target is independently set to **book automatically** or **just notify you**,
so the bot can commit your card at the two places you really want and leave the
maybes to a push notification.

---

## Quick start

```bash
git clone <this repo> && cd James-IV
make dev                # pip install -e ".[dev]"
james init              # writes config.yaml and .env from the templates
$EDITOR .env            # Resy login + ntfy topic
$EDITOR config.yaml     # your targets
james doctor            # verifies credentials, cards, and every venue slug
james check             # one-shot look at what is available right now
james run               # start hunting (config ships with dry_run: true)
```

`config.yaml` starts in **dry-run mode**. Leave it there for a day or two: the
bot will find and rank tables and tell you exactly what it *would* have booked,
without touching your card. Once the picks look right, set `dry_run: false`.

## Configuring a target

```yaml
targets:
  - name: Tatiana
    slug: tatiana-by-kwame-onwuachi   # from resy.com/cities/ny/<slug>
    party_size: 2
    action: book                      # or: notify

    days_ahead_min: 25
    days_ahead_max: 35
    weekdays: [thu, fri, sat]

    earliest: "17:30"
    latest: "21:30"
    preferred_windows:                # ranked: first is best
      - { start: "19:00", end: "20:30" }
      - { start: "18:00", end: "19:00" }

    seating_types: ["Dining Room", "Bar"]   # ranked
    exclude_seating: ["Counter"]

    drop:
      at: "09:00:00"    # venue-local release time
      days_ahead: 30    # releases the date exactly this far out
      lead_ms: 250

    max_bookings: 1
```

**Ranking**, best to worst: preferred time window → seating type → requested
party size before any fallback → soonest date → earliest time. A bar stool
inside your window beats a dining room seat outside it, which is almost always
what you want; if it isn't, tighten `earliest`/`latest` instead of reordering
`seating_types`.

**`drop.at` and `drop.days_ahead` are the two settings that decide whether a
snipe works.** They're on the venue's Resy page, in the booking policy notes
("Reservations open 30 days in advance at 9AM"). Guessing them is the single
most common reason a snipe fires into an empty result set. `james doctor` will
warn you when today's release date can't satisfy a target's weekday filter, but
it can't tell you the drop time is wrong — only the venue can.

Omit the `drop:` block entirely for a pure cancellation hunter.

## Commands

| Command | What it does |
| --- | --- |
| `james run` | Start every engine for every enabled target. The main command. |
| `james check [target]` | One-shot availability check. Never books, never alerts. |
| `james doctor` | Validate credentials, payment method, and every venue slug. |
| `james snipe <target>` | Arm a single drop and exit after it fires. `--now` to fire immediately. |
| `james status` | Confirmed bookings, target schedule, recent activity. |
| `james venue <slug>` | Resolve a Resy URL slug to a venue id. |
| `james cancel <token>` | Cancel a reservation the bot booked. |
| `james test-notify` | Send a test push, so you find out now and not at 9am. |

## Deployment

```bash
cp .env.example .env && cp config.example.yaml config.yaml
$EDITOR .env config.yaml
docker compose up -d
docker compose logs -f
```

The container pins `TZ=America/New_York`, because every drop time in this bot is
NYC wall clock. State lives in `./state`, mounted as a volume, so bookings and
alert dedupe survive a restart. `config.yaml` is mounted read-only.

Any $5/month VPS is plenty. Put it in a US East region if you have the choice —
you are competing on round-trip latency at 9:00:00.000 and the difference
between Virginia and Frankfurt is real.

## How the sniping works

The interesting problem is that you cannot trust your own clock. If it's 400ms
slow you fire late and the room is gone; 400ms fast and you burn your first
requests on an empty result set.

So `measure_clock_offset` probes Resy's own `Date` response header. That header
only has one-second resolution, which sounds useless — but the *transition* from
second N to N+1 is a hard server-side boundary. Sampling across a boundary
brackets the true offset from both sides and pins it to roughly the sampling
interval rather than a full second. The bot then converts your target wall-clock
time into a local-clock instant, sleeps until just before it, and spins the last
50ms rather than trusting `asyncio.sleep` to land exactly.

Ahead of that, it warms the TLS connection so the handshake isn't on the critical
path, and pre-resolves the venue id. When the burst fires, `find` requests bypass
the rate limiter; the moment inventory appears it goes straight to
`details` → `book` with no ranking round trip wasted.

## Safety rails

Booking is the one irreversible thing here, so it's fenced:

- `dry_run` — find, rank, and report without ever calling `/3/book`. On by default.
- `max_bookings_per_run` — global ceiling across all targets. A bad config
  can't book you five dinners.
- `max_bookings` per target, persisted in SQLite, so a restart doesn't re-book.
- One table per target per night, enforced against the same store.
- A `book`-action target with no payment method on file gets flagged by `doctor`,
  not discovered at 9am.

On rate limits: the client runs every routine request through a token bucket and
*obeys* a 429 rather than retrying through it. Raising `request_rate` won't make
you faster at a drop — the burst path bypasses the bucket anyway — it just makes
you noisier the other 23 hours of the day, which is how an account gets flagged.
A flagged account books nothing.

## Notifications

**ntfy** is the fastest to set up: pick an unguessable topic, put it in `.env`,
subscribe to the same topic in the ntfy app. No account needed. Anyone who knows
the topic name can read your alerts, so make it random.

**Pushover** costs a one-time fee and is more reliable at actually waking you up.
Set `PUSHOVER_TOKEN` and `PUSHOVER_USER`.

Configure both and you get both. Run `james test-notify` before you rely on it.

## A note on what this is

This drives the same endpoints resy.com's own front end uses, authenticated as
you, with your account and your card — it's a faster finger on the same button,
not a privileged back door. There's no documented public API, so the JSON shapes
in `resy.py` are observed rather than contracted, and Resy can change them
without warning. Automated booking is also very plausibly against their terms of
service; the realistic downside is a suspended Resy account, which is worth
knowing before you point this at a restaurant you care about. The polite
defaults are there for your sake as much as theirs.

Book tables you intend to eat at, and cancel the ones you don't — `james cancel`
exists for exactly that. Holding tables you won't use is how restaurants end up
tightening the policies that make this necessary in the first place.

## Development

```bash
make dev     # editable install with test deps
make test    # pytest
make lint    # ruff
```

The logic worth trusting is isolated and directly tested: `matching.py` (which
table wins) and `state.py` (booking caps and alert dedupe) are pure and covered,
and `hunter.py`'s booking decisions are tested against a fake client so the
"never double-book, never exceed budget, never book in dry-run" rules are
assertions rather than hopes.

## Layout

```
src/jamesiv/
├── cli.py         # typer commands
├── config.py      # YAML targets + .env secrets (pydantic)
├── hunter.py      # poll loop, snipe burst, booking sequence
├── matching.py    # which dates to check, which table wins  (pure)
├── models.py      # Slot, Booking, error taxonomy
├── notify.py      # ntfy + pushover
├── resy.py        # API client, token bucket, connection reuse
├── state.py       # SQLite: bookings, dedupe, events
└── timeutil.py    # NYC time, clock sync, precise sleep
```

MIT.
