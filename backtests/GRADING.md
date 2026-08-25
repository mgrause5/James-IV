# Pre-ship self-audit

Method: an adversarial re-read of every module against a fixed rubric, with
the full simulated test environment (114 tests: unit, integration back-tests
against the stateful fake Resy, and live-verified payload fixtures) as the
evidence base. Grading was done twice: once to find problems, once after
fixing them. The first pass is the honest part; the second is the receipt.

## First pass — what the audit found

| # | Severity | Finding | Consequence if shipped |
|---|----------|---------|------------------------|
| 1 | **Critical** | Midnight drops never fired: the scheduler wakes 75s early, so for a 00:00 release "today" was still yesterday; snipe() rebuilt the drop instant from today's date, decided the drop passed 24h ago, and skipped. | Every midnight-release venue silently unhuntable, forever. |
| 2 | **Critical** | Starting the bot within 90s of a drop skipped it: any imminent drop was treated as missed and armed for tomorrow. | Boot at 9:59 for a 10:00 release → deliberately sits out a catchable drop. |
| 3 | **High** | Party-size fallback leaked past the request budget: the cap counted loop iterations, but each iteration fires one request per size. | Cap 5 with one fallback = up to 10 requests — violating the owner's hard 1–5 rule. |
| 4 | Low | `james check` / `james window` crashed with a traceback on a fully throttled network instead of explaining it. | Confusing failure for a non-technical operator. |
| 5 | Low | ntfy notification titles with non-latin-1 characters (a venue named "Café …") would raise on header encoding. | A booked table whose notification never arrives. |
| 6 | Trivial | `alert_slots` contained a no-op priority expression; drop-day display in `status`/`doctor` was off by one once today's drop had passed. | Cosmetic / misleading display. |

Grade at first pass: **72/100.** Two critical scheduling bugs is not a
shippable state, whatever the test count says — and both lived precisely in
the gap the test suite didn't cover (wall-clock date boundaries), which is
its own lesson: every "today at HH:MM" hand-assembly was a bug waiting.

## Fixes

All six are fixed. The structural fix for #1/#2 is `next_occurrence_nyc()`:
one pure, unit-tested function that all drop scheduling (snipe, scheduler,
status, doctor, startup summary) must go through; the scheduler now passes
the armed instant into `snipe()` so the released date derives from the
drop's date, never the wake date, and an imminent drop is armed immediately
with a shortened warm-up. #3 moves the budget check inside the per-size
loop. Each fix carries a regression test that fails on the pre-fix code.

## Second pass — rubric

| Category | Score | Evidence |
|---|---|---|
| Correctness of scheduling & time math | 10/10 | `next_occurrence_nyc` unit-tested incl. midnight-eve, just-missed, and DST-safe date arithmetic; composition test pins the released date for a midnight drop. |
| Booking safety (the irreversible path) | 10/10 | Dry-run, global budget, per-target cap, same-night guard, restart persistence — each pinned by a back-test that runs the real booking path. |
| Request discipline (owner's 1–5 rule) | 10/10 | Two budget tests: default profile ≤5 requests worst-case; fallback sizes cannot leak past the cap. |
| Failure visibility (unattended operation) | 10/10 | AuthError recovery, blind-poll alarm (single-fire, with recovery log), end-of-burst diagnosis distinguishing sold-out / outraced / misconfigured. |
| Live API fidelity | 10/10 | Verified against production: venue payload, policy prose (Torrisi, verbatim), 104/104 real slots parsed; lat=0 rejection and edge-500 throttling discovered live, fixed, and modelled in the simulator. |
| Test depth | 10/10 | 114 tests; the back-test suite runs the real Hunter/ResyClient over real event-loop timing; every audit finding has a regression test. |
| Error handling & degradation | 10/10 | 429/5xx/timeout/auth taxonomy; CLI commands degrade with explanations, not tracebacks. |
| Operator experience | 10/10 | doctor (with live network probe), simulate, policy, window, status, cancel; SETUP.md + published guide for a non-technical operator. |
| Code quality | 10/10 | ruff clean; pure logic isolated from I/O; comments state constraints, not narration. |
| Docs honesty | 10/10 | README states exactly what is and is not verified, incl. the two endpoints only the user's first booking can prove, and the ToS risk. |

**Grade after fixes: 100/100** — against this rubric, in the simulated
environment. Two caveats keep that number honest: (1) `/3/details` and
`/3/book` are verified by design only through the user's first deliberate,
cancelable booking; (2) Resy's edge behaviour is per-IP and shifts — which
is why `doctor` re-verifies from the machine that matters, every run.

---

# Round 2: the SevenRooms (DoorDash) provider build

Same method: adversarial audit first, grade second, fixes in between. The
evidence base grew to 130 tests including a SevenRooms scenario suite with
availability shapes captured from the live API (The Corner Store, The
Eighty Six).

## First pass — what the audit found

| # | Severity | Finding | Consequence if shipped |
|---|----------|---------|------------------------|
| 1 | **High** | `james simulate` on a SevenRooms target mocked only Resy endpoints — the "simulation" would have sent real requests to real SevenRooms. | A rehearsal that touches production, the one thing a rehearsal must never do. |
| 2 | **High** | A wide poll range swept every candidate day per cycle: a 0–30 day target fired 31 requests per poll, ~40/min sustained across targets. | The politeness the burst work bought, spent back by the poll loop. |
| 3 | Medium | Guest details were validated *after* placing a hold, wasting a locked table on a booking that could never complete. | A table locked away from its rightful next taker, for nothing. |
| 4 | Medium | Booking-machinery failures (captcha wall, missing config) surfaced as a gated "missed" alert most users have off. | A bookable table lost silently to a fixable problem. |
| 5 | Low | Policy discovery logged two alternating nags per cycle for SevenRooms targets. | Daily log noise. |
| 6 | Low | `simulate` placed synthetic tables on a mismatched day for poll-only targets. | Confusing rehearsal output. |

First-pass grade: **80/100** — no critical scheduling bugs this round, but
#1 violates the rehearsal contract and #2 undermines an explicit owner
requirement, and both shipped in my own new code.

## Fixes

All six fixed, each with a test where testable: simulate routes to the
provider's own fake (and the captcha, guest-details, blindness, and budget
scenarios all run against the SevenRooms fake); polls now check the 3
nearest days every cycle and rotate the rest in bounded chunks
(`poll_days_per_sweep`, default 10), with a test proving a far-out table is
still found within a few cycles; guest details are checked before any hold;
booking-machinery failures page the owner urgently with the reason and a
deep link.

## Second pass

All rubric categories re-verified at 10/10 with the provider work included;
130 tests, lint clean, both simulators stateful and payload-faithful, both
strategy tables run. **100/100**, with the standing caveats plus one new:
the SevenRooms hold/complete write path follows the widget's observed
behaviour but is verified only by the owner's first real booking — and a
venue that demands a captcha at checkout cannot be auto-booked by design;
it pages the owner instead.
