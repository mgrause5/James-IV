# Strategy back-test: what actually wins a drop

4,400+ simulated drops racing the **production `Hunter._burst` code,
unmodified**, against a modeled rival field (see `race.py` for the world
model: bot rivals with ~450ms median reaction, human rivals ~8s, 30% late
releases up to 2s, 15% hold bounce-backs, ~85ms round trips, gaussian clock
error). 400 trials per strategy; 95% CI ≈ ±1–4 points.

Absolute rates are model-dependent and are NOT predictions. The *ordering*
is the finding — it is driven by mechanics that hold under any plausible
parameterisation, and each is stated below.

## Main table (clock error σ=60ms, lead=250ms unless stated)

| Strategy | Shots | Take rate | Avg requests | p50 time-to-book |
|---|---|---|---|---|
| 1-shot | 1 | 0.5% | 1.0 | — |
| 3-tight (150ms) | 3 | 70.8% | 2.4 | 0.54s |
| 5-tight (150ms) | 5 | 78.2% | 2.9 | 0.55s |
| **5-quick (400ms) — shipped shape** | 5 | **92.2%** | 2.7 | 0.81s |
| 5-spread (3 quick + 2 bounce probes) | 5 | 93.5% | 2.6 | 0.67s |
| 10-mixed | 10 | 85.0% | 5.5 | 0.80s |
| 25-volley | 25 | 90.8% | 10.7 | 0.66s |
| 100-barrage | 100 | 99.2% | 24.2 | 0.61s |

## Lead-time sweep (5-quick shape)

| lead_ms | σ=60ms sync | σ=250ms sync |
|---|---|---|
| 0 | **97.8%** | 93.8% |
| 100 | 94.0% | **94.3%** |
| 250 | 92.2% | 92.8% |
| 600 | 86.8% | — |
| 1200 | 76.2% | — |

## What the mechanics say

1. **Coverage beats density.** The same five shots spread over 1.6s (400ms
   apart, 92%) beat five shots crammed into 0.75s (150ms apart, 78%), and
   even beat *25* shots crammed into the first second (91%). Late releases
   are common; a burst that finishes firing before the inventory lands
   loses no matter how dense it was. This is why the shipped interval is
   400ms, not 100ms.

2. **Every millisecond of lead costs.** An early shot is a wasted shot; a
   slightly-late first shot barely matters because the next one is 400ms
   behind and rivals need ~450ms anyway. The sweep is monotonic. But the
   lead=0 advantage assumes excellent clock sync — under pessimistic sync
   (σ=250ms, matching the worst live-measured uncertainty) lead=100 edges
   it out. **Shipped default: lead_ms=100**, the robust choice in both
   regimes (was 250; the change is worth ~2–4 points).

3. **The bounce-probe idea loses to late-release coverage.** Reserving the
   last two shots for the 3–8s hold-lapse window (5-spread) looked clever
   and won narrowly under lead=250 — but at lead=0 the dense shape beat it
   (97.8% vs 95.5%): the gap it opens between 0.5s and 3.5s drops exactly
   the 1–2s-late releases that dense coverage catches. Bounce-backs are
   better left to the poll loop, which re-sweeps within a minute anyway.

4. **Spam works, which is why it's capped.** 100-barrage hit 99.2% — brute
   force genuinely wins races. Its price is 24 requests per drop on
   average (up to 100), which is the profile Resy's edge exists to catch;
   a flagged account takes 0% of everything thereafter. The 5-shot cap at
   ~94% pays ~5 points for being quiet. That trade is the owner's explicit
   choice and the default.

5. **One shot is a coin flip you lose.** 0.5% with lead, ~luck without.
   The minimum meaningful burst is 3; 5 is where the curve flattens.

## Shipped profile after this experiment

`burst_concurrency: 1 · burst_interval_ms: 400 · max_requests: 5 ·
lead_ms: 100` — shots at −0.1s, +0.3s, +0.7s, +1.1s, +1.5s relative to the
synced boundary. ~94% modeled take rate, ~2.3 requests actually spent on an
average drop, indistinguishable from a fast human refreshing the page.

Reproduce: `python backtests/race.py --trials 400`
(env `RACE_CLOCK_SIGMA=0.250` for the pessimistic-sync variant).
