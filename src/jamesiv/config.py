"""Configuration: secrets from the environment, hunting targets from YAML.

Credentials never go in the YAML file, so `config.yaml` stays diffable and
shareable while `.env` stays out of git.
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .timeutil import parse_hhmm

Action = Literal["book", "notify"]
Provider = Literal["resy", "sevenrooms"]

WEEKDAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


class Secrets(BaseSettings):
    """Everything sensitive. Sourced from environment / .env only."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix=""
    )

    resy_email: str = ""
    resy_password: str = ""
    # Alternative to email/password: paste a token straight out of the browser.
    resy_auth_token: str = ""
    resy_api_key: str = ""
    resy_payment_method_id: int | None = None

    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_token: str = ""

    pushover_token: str = ""
    pushover_user: str = ""

    # Guest details for SevenRooms (DoorDash) bookings, which are made without
    # an account -- the reservation is placed under this name.
    guest_first_name: str = ""
    guest_last_name: str = ""
    guest_phone: str = ""
    guest_email: str = ""

    # Residential-proxy URL for every provider request, e.g.
    #   http://user:pass@gate.provider.com:7000
    # Routes traffic through a residential address so Resy's edge does not
    # throttle it the way it throttles datacenter IPs. This is the single
    # setting that makes aggressive polling viable. Read from PROXY_URL.
    proxy_url: str = ""

    def guest_info(self) -> dict[str, str]:
        return {
            "first_name": self.guest_first_name,
            "last_name": self.guest_last_name,
            "phone": self.guest_phone,
            "email": self.guest_email or self.resy_email,
        }

    @property
    def has_credentials(self) -> bool:
        return bool(self.resy_auth_token) or bool(self.resy_email and self.resy_password)

    @property
    def has_notifier(self) -> bool:
        return bool(self.ntfy_topic) or bool(self.pushover_token and self.pushover_user)

    @property
    def has_proxy(self) -> bool:
        return bool(self.proxy_url)


class TimeWindow(BaseModel):
    """A preferred seating range. Earlier windows in the list rank higher."""

    start: time
    end: time

    @field_validator("start", "end", mode="before")
    @classmethod
    def _parse(cls, v: Any) -> Any:
        return parse_hhmm(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _ordered(self) -> TimeWindow:
        if self.start > self.end:
            raise ValueError(f"window start {self.start} is after end {self.end}")
        return self

    def contains(self, t: time) -> bool:
        return self.start <= t <= self.end

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


class DropConfig(BaseModel):
    """When a venue releases new inventory.

    Most hard NYC rooms drop a fixed number of days ahead at a fixed local time
    -- 9:00am ET, 30 days out, is the single most common pattern. It is stated
    on the venue's Resy page, and `auto: true` reads it from there so you do
    not have to transcribe it. `james policy <slug>` shows what would be read.
    """

    enabled: bool = True
    # Discover `at` and `days_ahead` from the venue's own Resy page at startup
    # (and re-check daily). Values you set explicitly below are kept as
    # fallbacks for anything discovery cannot determine.
    auto: bool = False
    at: time = Field(default=time(9, 0, 0))
    days_ahead: int = Field(default=30, ge=1, le=365)
    # Start firing this many ms early. Race back-testing (backtests/RESULTS.md)
    # showed every ms of lead costs: an early shot is a wasted shot, while a
    # slightly-late first shot barely matters because the next follows 400ms
    # behind. 100ms was the robust optimum across good and poor clock sync --
    # near-best in both regimes. Set 0 only if you trust the sync completely.
    lead_ms: int = Field(default=100, ge=0, le=5000)
    # The whole snipe is a handful of precisely timed shots, not a barrage.
    # Clock sync is what wins the race; volume just gets accounts flagged.
    # Defaults: one worker firing every 400ms, five requests total -- the last
    # lands ~1.4s after the boundary, which covers a slightly-late release.
    # If all five miss, the regular poll loop sweeps the same date again within
    # a minute, so a capped burst is a delay, not a defeat.
    burst_seconds: float = Field(default=10.0, gt=0, le=300)
    burst_concurrency: int = Field(default=1, ge=1, le=10)
    burst_interval_ms: int = Field(default=400, ge=50, le=5000)
    # Shot placement: fire at full cadence for this long, then slow by
    # decay_factor. With the 5-shot budget the defaults place shots at
    # ~0s, 0.4s, 0.8s, 2.8s and 4.8s past the boundary: three dense for a
    # punctual release, two held back for one that lands seconds late.
    # Back-tested (RESULTS.md round 3): vs the old all-dense shape this
    # costs ~1 point when releases are punctual and gains ~29 when they
    # run late -- and the first live morning (nine drops, nine empties,
    # two platforms) looked exactly like the late world.
    aggressive_seconds: float = Field(default=1.0, ge=0.5, le=60)
    decay_factor: float = Field(default=5.0, ge=1.0, le=50)
    # Hard ceiling on find requests per burst, across all workers. This is the
    # knob that decides how loud a drop is; everything else shapes timing.
    max_requests: int = Field(default=5, ge=1, le=2000)
    # HEAD probes used to measure clock skew before firing. These are cheap
    # metadata requests a minute before the drop, not availability polling.
    clock_probes: int = Field(default=8, ge=2, le=40)
    # RECONNAISSANCE MODE -- a deliberate, temporary exception to the request
    # budget. When true, the next drop replaces the 5-shot burst with ~26
    # probes mapping the whole release window (60s before the bell to 120s
    # after), reporting exactly when tables appear and vanish -- and booking
    # on sight if any match. Use on ONE venue for ONE morning to learn its
    # true release timing, then REMOVE the flag and re-aim the normal burst.
    # It applies to every drop until removed, and the report reminds you.
    recon: bool = False

    @field_validator("at", mode="before")
    @classmethod
    def _parse(cls, v: Any) -> Any:
        return parse_hhmm(v) if isinstance(v, str) else v


class Target(BaseModel):
    """One restaurant you are hunting, with the rules for what counts as a win."""

    name: str
    slug: str
    # "resy", or "sevenrooms" -- the system behind DoorDash reservations.
    provider: Provider = "resy"
    location: str = "ny"
    venue_id: int | None = None

    party_size: int = Field(default=2, ge=1, le=20)
    # Tried in order if the preferred size is unavailable. A 2-top hunter who
    # would accept a 3-top sets [3]; most people leave this empty.
    party_size_fallback: list[int] = Field(default_factory=list)

    # Which dates to hunt. `dates` pins specific days; otherwise we sweep
    # `days_ahead_min..days_ahead_max` filtered by `weekdays`.
    dates: list[date] = Field(default_factory=list)
    days_ahead_min: int = Field(default=0, ge=0, le=365)
    days_ahead_max: int = Field(default=30, ge=0, le=365)
    weekdays: list[int] = Field(default_factory=list)

    earliest: time = Field(default=time(17, 0))
    latest: time = Field(default=time(21, 30))
    # Ignore tables starting sooner than this. Same-day cancellation hunting
    # otherwise surfaces slots that have already been sat, or ones you could
    # not physically reach. Set to 0 if you live upstairs.
    min_lead_minutes: int = Field(default=90, ge=0, le=10080)
    # Ranked preferences. A slot inside windows[0] beats one inside windows[1].
    preferred_windows: list[TimeWindow] = Field(default_factory=list)

    # Ranked seating preferences, matched case-insensitively as substrings so
    # "dining" matches "Dining Room". Empty means "anything not excluded".
    seating_types: list[str] = Field(default_factory=list)
    exclude_seating: list[str] = Field(default_factory=list)

    action: Action = "notify"
    enabled: bool = True

    drop: DropConfig | None = None
    poll_interval_seconds: float = Field(default=45.0, ge=5.0)
    # Requests per poll cycle. A 0-30 day range is 31 days; sweeping all of
    # them every poll is ~40 requests/minute of sustained noise. Instead each
    # poll checks the 3 nearest days (where cancellations are hottest and
    # lead-time matters) plus a rotating chunk of the rest, covering the full
    # range every few cycles.
    poll_days_per_sweep: int = Field(default=10, ge=1)
    # Randomised +/- this fraction so we do not become a metronome.
    poll_jitter: float = Field(default=0.35, ge=0.0, le=1.0)

    # Stop after this many successful bookings for this target, ever.
    max_bookings: int = Field(default=1, ge=1)
    priority: int = Field(default=0)
    notes: str = ""

    @field_validator("earliest", "latest", mode="before")
    @classmethod
    def _parse_time(cls, v: Any) -> Any:
        return parse_hhmm(v) if isinstance(v, str) else v

    @field_validator("weekdays", mode="before")
    @classmethod
    def _parse_weekdays(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return v
        out: list[int] = []
        for item in v:
            if isinstance(item, str):
                key = item.strip().lower()
                if key not in WEEKDAY_NAMES:
                    raise ValueError(f"unknown weekday {item!r}")
                out.append(WEEKDAY_NAMES[key])
            else:
                out.append(int(item))
        return out

    @model_validator(mode="after")
    def _check(self) -> Target:
        if self.earliest > self.latest:
            raise ValueError(f"{self.name}: earliest {self.earliest} is after latest {self.latest}")
        if self.days_ahead_min > self.days_ahead_max:
            raise ValueError(f"{self.name}: days_ahead_min exceeds days_ahead_max")
        return self

    @property
    def party_sizes(self) -> list[int]:
        sizes = [self.party_size]
        sizes.extend(s for s in self.party_size_fallback if s not in sizes)
        return sizes

    def seating_allowed(self, seating_type: str) -> bool:
        lowered = seating_type.lower()
        if any(x.lower() in lowered for x in self.exclude_seating):
            return False
        if not self.seating_types:
            return True
        return any(x.lower() in lowered for x in self.seating_types)

    def seating_rank(self, seating_type: str) -> int:
        lowered = seating_type.lower()
        for i, pref in enumerate(self.seating_types):
            if pref.lower() in lowered:
                return i
        return len(self.seating_types)

    def window_rank(self, t: time) -> int:
        for i, window in enumerate(self.preferred_windows):
            if window.contains(t):
                return i
        return len(self.preferred_windows)


class Settings(BaseModel):
    """Global knobs."""

    dry_run: bool = False
    # Global ceiling across all targets, so a misconfigured sweep cannot book
    # you ten dinners. Counted per process run.
    max_bookings_per_run: int = Field(default=3, ge=1)
    request_rate: float = Field(default=4.0, gt=0, le=20)
    request_burst: int = Field(default=8, ge=1, le=40)
    # Master switch for the cancellation-polling engine. Off means the bot
    # contacts the providers ONLY around each venue's release time -- a few
    # clock-sync probes, the capped burst, and (for auto targets) one policy
    # re-read a day. Roughly a hundred requests per DAY across the whole
    # roster, concentrated at the drop minutes: the quietest possible
    # footprint. The cost: no cancellation catches, and no second chance
    # between drops if a burst loses its race. Watcher-only targets (no
    # drop block) sit idle while this is off.
    poll_for_cancellations: bool = True
    state_path: str = "state/james.db"
    log_level: str = "INFO"
    # Re-login this often; Resy tokens are long-lived but not eternal.
    reauth_interval_hours: float = Field(default=12.0, gt=0)
    # Push an alert after this many consecutive polls where every availability
    # request failed outright -- the "bot is blind" alarm.
    blind_poll_alert_after: int = Field(default=8, ge=1)
    notify_on_miss: bool = False

    # Aggressive polling: when a residential proxy is configured, the drop
    # burst abandons the 5-shot politeness cap (a workaround for datacenter-IP
    # throttling that no longer applies behind a proxy) and polls hard at the
    # release, the way the bots that actually win do -- ~10 requests/second
    # for a few seconds. Off by default and IGNORED without a proxy: firing
    # this volume from a bare datacenter IP is how an account gets banned
    # fast. doctor warns loudly if this is on with no proxy.
    aggressive_polling: bool = False
    aggressive_max_requests: int = Field(default=40, ge=5, le=300)
    aggressive_interval_ms: int = Field(default=120, ge=50, le=1000)
    aggressive_seconds: float = Field(default=8.0, gt=0, le=60)


class Config(BaseModel):
    settings: Settings = Field(default_factory=Settings)
    targets: list[Target] = Field(default_factory=list)

    @property
    def active_targets(self) -> list[Target]:
        return sorted(
            (t for t in self.targets if t.enabled),
            key=lambda t: (-t.priority, t.name),
        )

    def target(self, name: str) -> Target | None:
        lowered = name.lower()
        for t in self.targets:
            if t.name.lower() == lowered or t.slug.lower() == lowered:
                return t
        return None


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.yaml to config.yaml and edit it."
        )
    data = yaml.safe_load(path.read_text()) or {}
    return Config.model_validate(data)


def load_secrets() -> Secrets:
    return Secrets()
