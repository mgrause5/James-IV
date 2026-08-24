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

**Never used a terminal before?** [SETUP.md](SETUP.md) walks the whole thing
from zero — renting the server, pasting the commands, first booking — with no
assumed knowledge. The quick start below is the short version for people who
have done this kind of thing.

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

## Your credentials

Your Resy email and password go in `.env` **on the machine that runs the bot**,
and nowhere else. That file is gitignored and never leaves the box.

Don't paste them into a chat window, a commit, or an issue — anything you type
into an AI assistant lands in a transcript. Nobody needs your login to work on
this code: the whole test suite runs against a simulated Resy (`simulator.py`),
and `james simulate` will rehearse your real config without any credentials at
all.

If you'd rather not put a password on the VPS, `RESY_AUTH_TOKEN` takes a token
copied out of your browser instead. It expires after a while and can't be
auto-refreshed, so for a bot meant to run unattended for weeks the
email/password path is the more reliable one.

## Fully automated: the Torrisi case

The goal is that Torrisi releases Friday tables 30 days out and you do nothing.
Here's the whole path.

**1. Ask the venue's own page for its release policy.**

```bash
james policy torrisi
```

The `/3/venue` payload carries what a human reads on the website — the
"Need to Know" notes ("Reservations open 30 days in advance at 9:00AM") and,
for many venues, a structured lead-time field that drives the site's own
calendar. `policy` parses both and prints a ready-to-paste `drop:` block.

Two fallbacks for venues that publish less:

```bash
james window torrisi --days 45   # infer days_ahead from where inventory exists
```

`window` walks forward day by day; the furthest date with availability, if it
lands on a round number, is your `drop.days_ahead`. And if the page states no
release *time* at all, that one field you set by hand — it's the only thing
left that can require a human.

**2. Write the target — or let it configure itself.**

```yaml
- name: Torrisi
  slug: torrisi
  party_size: 2
  action: book
  weekdays: [fri]        # Fridays only
  days_ahead_min: 25     # wider than days_ahead, so the poll engine also
  days_ahead_max: 35     # catches cancellations either side of the drop
  earliest: "18:00"
  latest: "21:30"
  preferred_windows:
    - { start: "19:00", end: "20:30" }
  drop:
    auto: true           # read at/days_ahead off the venue page, re-check daily
    at: "09:00:00"       # fallback if the page goes quiet
    days_ahead: 30
  max_bookings: 1
```

With `auto: true` the scheduler re-reads the page every cycle, so a venue that
quietly moves from 30 days to 21 mid-season is picked up without a restart.
Every applied change is logged — auto never means silent. The one shape it
refuses to guess about is a monthly release ("reservations open on the 1st of
the month"): a daily snipe would fire into nothing 29 mornings out of 30, so
the bot flags it, notifies you, and keeps your explicit config instead.

**3. Rehearse it before trusting it.**

```bash
james simulate Torrisi --scenario drop          # inventory lands; do we catch it?
james simulate Torrisi --scenario contested     # competitors hold it first
james simulate Torrisi --scenario cancellation  # a table appears mid-poll
james simulate Torrisi --scenario soldout       # nothing is ever released
```

This runs your *actual* target config — your windows, your seating preferences,
your drop timing — through the real hunting code against a fake venue. No
credentials, no real booking. It's how you find out that `days_ahead` is wrong,
or that your time window excludes everything, on a Tuesday afternoon rather than
at 9am on a Friday.

**4. Check the real thing, then let it run.**

```bash
james doctor        # credentials, payment method, every venue slug -- and it
                    # cross-checks each drop config against the venue's page
james test-notify   # confirm push actually reaches your phone
docker compose up -d
```

That's it. `snipe_scheduler` wakes ~75 seconds before 9:00:00 every day, syncs
the clock, warms the connection, and fires. If today's release date isn't a
Friday it skips and sleeps until tomorrow. When it books, `max_bookings: 1`
stops that target for good and you get a push notification.

Meanwhile the poll engine is independently watching days 25–35 for
cancellations, so you're covered between drops too.

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
snipe works** — and mostly you shouldn't set them by hand. `drop.auto: true`
reads them off the venue's own page and re-checks daily; `james policy <slug>`
shows what would be read; and `james doctor` cross-checks whatever's configured
against the page and warns on a mismatch. Manual values remain as fallbacks,
and matter only for venues that publish nothing.

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
| `james policy <slug>` | Read the release time and window off the venue's own page. |
| `james window <slug>` | Probe the booking horizon empirically, when the page states nothing. |
| `james simulate <target>` | Rehearse a target against a simulated Resy. No credentials, no booking. |
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

The container runs as uid 1000, so create the state directory with matching
ownership before the first run, or the bind mount lands root-owned and SQLite
can't write to it:

```bash
mkdir -p state && sudo chown 1000:1000 state
```

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

The burst is budgeted rather than open-ended. Inventory either lands within a few
seconds of the release or it isn't coming, so the cadence runs hard for
`aggressive_seconds` and then decays, with `max_requests` as a hard ceiling.
Left ungoverned a 20-second burst is ~400 requests, which is simultaneously
useless — the room sold out in the first two seconds — and an excellent way to
get an account flagged.

Losing the first race is not the end of the burst. Tables bounce back within
seconds as other people's holds lapse, so it keeps hunting until the window
closes.

## Safety rails

Booking is the one irreversible thing here, so it's fenced:

- `dry_run` — find, rank, and report without ever calling `/3/book`. On by default.
- `max_bookings_per_run` — global ceiling across all targets. A bad config
  can't book you five dinners.
- `max_bookings` per target, persisted in SQLite, so a restart doesn't re-book.
- One table per target per night, enforced against the same store.
- `min_lead_minutes` — never book a table starting sooner than you could reach
  it, and never one that has already been sat.
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

## What has been verified against real Resy

The simulator (below) proves the *logic*; it cannot prove that our picture of
Resy's API matches reality. So the read-only endpoints were probed against
production (August 2026):

- `/3/venue` — works; the payload parses; **policy discovery read the real
  Torrisi page** and returned "30 days ahead, at 10:00 ET" from the sentence
  *"up to 30 days in advance, starting at 10:00 AM EST"*.
- `/4/find` — the envelope and slot shapes match: **104 of 104 live slots
  parsed** with real config tokens intact. Two production-only bugs surfaced
  and were fixed: `lat=0&long=0` (which the client originally sent) is
  rejected with an empty HTTP 500, and the same empty 500 also occurs
  intermittently on well-formed requests when Resy's edge dislikes your IP.
  The client now sends real NYC coordinates, retries the flake, raises
  instead of reporting "no availability" when fully blocked, and pushes a
  **"bot is blind" alert** after repeated total failures.
- `/3/details` and `/3/book` are deliberately unprobed — they touch real
  inventory. They get verified by *your* first booking, made on purpose at an
  easily-cancelable venue (see SETUP.md's graduation steps), and undone with
  `james cancel`.

Because the edge throttling is per-IP, the only network that matters is the
one your server is on — which is why `james doctor` runs a live availability
probe from wherever it executes and fails loudly if that network is blocked.

## Development and back-testing

```bash
make dev     # editable install with test deps
make test    # pytest
make lint    # ruff
```

79 tests. The pure logic — `matching.py` (which table wins) and `state.py`
(booking caps, alert dedupe) — is directly unit tested.

The interesting half is `tests/test_backtest.py`, which runs the **real**
`Hunter` and the **real** `ResyClient` against `simulator.py`, a small stateful
fake Resy that models what actually happens at a drop: inventory appearing at an
instant, competitors taking tables out from under you, holds lapsing and tables
bouncing back, sessions going stale, and rate limits. It runs over the real event
loop with real timing, so what's under test is the code path that will run at
9am — not a paraphrase of it.

That harness earned its keep. Four bugs it caught, each now a regression test:

- **`AuthError` was silently swallowed.** `AuthError` subclasses `ResyError`, and
  three `except ResyError: continue` handlers caught it. An expired session
  meant the bot polled forever, found nothing, booked nothing, and never said a
  word — the worst possible failure for something you aren't watching.
- **Past tables were bookable.** `slot_matches` checked time-of-day but not
  whether the slot had already happened, so a poll at 8pm would try to book
  tonight's 7pm table. Hence `min_lead_minutes`.
- **Losing one race ended the whole snipe.** The burst stopped the instant it saw
  inventory, even if every booking attempt lost. Tables bounce back within
  seconds as competitors' holds lapse, and that window was being thrown away.
- **A useless slot hid a good one.** `search` broke out of the party-size loop on
  any returned slot, so an out-of-window 2-top stopped it from ever probing the
  bookable 3-top behind it.

## Layout

```
src/jamesiv/
├── cli.py         # typer commands
├── config.py      # YAML targets + .env secrets (pydantic)
├── hunter.py      # poll loop, snipe burst, booking sequence
├── matching.py    # which dates to check, which table wins  (pure)
├── models.py      # Slot, Booking, error taxonomy
├── notify.py      # ntfy + pushover
├── policy.py      # release-policy discovery from the venue's own page
├── resy.py        # API client, token bucket, connection reuse
├── simulator.py   # stateful fake Resy, for back-testing and `james simulate`
├── state.py       # SQLite: bookings, dedupe, events
└── timeutil.py    # NYC time, clock sync, precise sleep
```

MIT.
