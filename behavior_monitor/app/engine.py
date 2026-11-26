"""Behaviour learning and anomaly detection. Pure logic, no I/O.

Timeline format: list of (epoch_seconds, state_string) sorted by time.
"""
from __future__ import annotations

import bisect
import fnmatch
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

STEP = 300  # 5-minute sampling
UNKNOWN = {"unavailable", "unknown", "", None}

ACTIVE_STATES = {
    "light": {"on"},
    "switch": {"on"},
    "fan": {"on"},
    "input_boolean": {"on"},
    "climate": None,  # anything except off/unknown
    "media_player": {"on", "playing", "paused", "buffering"},
    "cover": {"open", "opening"},
}


def is_active(entity_id: str, state) -> bool | None:
    """True/False for a known state, None when the state is unknown."""
    if state in UNKNOWN:
        return None
    domain = entity_id.split(".", 1)[0]
    if domain == "climate":
        return state != "off"
    allowed = ACTIVE_STATES.get(domain, {"on"})
    return state in allowed


def state_at(timeline, t):
    """State in force at time t (last change at or before t), or None."""
    times = timeline[0]
    i = bisect.bisect_right(times, t) - 1
    return timeline[1][i] if i >= 0 else None


def as_index(timeline):
    """Turn [(t, s), ...] into ([t...], [s...]) for bisect lookups."""
    return ([p[0] for p in timeline], [p[1] for p in timeline])


def presence_at(person_idx: dict, t) -> bool | None:
    """True if anyone home, False if everyone with a known state is away, None if no data."""
    known = False
    for idx in person_idx.values():
        s = state_at(idx, t)
        if s in UNKNOWN:
            continue
        known = True
        if s == "home":
            return True
    return False if known else None


def percentile(values, q):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * q
    f = math.floor(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


@dataclass
class Profile:
    entity_id: str
    # counts per [presence][hour]: presence 0 = home, 1 = away
    on: list = field(default_factory=lambda: [[0] * 24, [0] * 24])
    obs: list = field(default_factory=lambda: [[0] * 24, [0] * 24])
    sessions: list = field(default_factory=list)  # completed session lengths in minutes
    days_observed: float = 0.0

    def frac(self, hour: int, away: bool, min_obs: int = 24):
        """Smoothed share of time active at this hour (hour-1..hour+1 weighted).

        Returns (fraction, observations) or (None, obs) if not enough data.
        """
        p = 1 if away else 0
        on = obs = 0.0
        for d, w in ((-1, 0.25), (0, 0.5), (1, 0.25)):
            h = (hour + d) % 24
            on += w * self.on[p][h]
            obs += w * self.obs[p][h]
        if obs < min_obs * 0.5:
            return None, obs
        return on / obs, obs

    def overall(self, away: bool | None = None):
        ps = (0, 1) if away is None else ((1,) if away else (0,))
        on = sum(sum(self.on[p]) for p in ps)
        obs = sum(sum(self.obs[p]) for p in ps)
        return (on / obs) if obs else None

    def long_limit(self, factor: float, min_minutes: float):
        if len(self.sessions) < 5:
            return None
        p95 = percentile(self.sessions, 0.95)
        return max(p95 * factor, min_minutes)

    def to_dict(self):
        return {
            "entity_id": self.entity_id,
            "on": self.on,
            "obs": self.obs,
            "sessions": self.sessions[-400:],
            "days_observed": round(self.days_observed, 1),
        }

    @classmethod
    def from_dict(cls, d):
        p = cls(d["entity_id"])
        p.on, p.obs = d["on"], d["obs"]
        p.sessions = d.get("sessions", [])
        p.days_observed = d.get("days_observed", 0)
        return p


def learn(entity_id, timeline, person_idx, start, end, tz) -> Profile:
    """Build a profile from a state timeline between start and end (epoch seconds)."""
    prof = Profile(entity_id)
    idx = as_index(timeline)
    run = 0  # minutes of current active run
    observed_steps = 0
    t = start - (start % STEP) + STEP
    while t <= end:
        a = is_active(entity_id, state_at(idx, t))
        if a is None:
            if run:
                run = 0  # broken by unknown data: drop partial session
            t += STEP
            continue
        observed_steps += 1
        pres = presence_at(person_idx, t)
        away = pres is False
        h = datetime.fromtimestamp(t, tz).hour
        p = 1 if away else 0
        prof.obs[p][h] += 1
        if a:
            prof.on[p][h] += 1
            run += STEP / 60
        elif run:
            prof.sessions.append(run)
            run = 0
        t += STEP
    prof.days_observed = observed_steps * STEP / 86400
    return prof


@dataclass
class Settings:
    rare_threshold: float = 0.05
    away_threshold: float = 0.10
    min_on_minutes: float = 20
    long_factor: float = 1.5
    min_long_minutes: float = 60
    away_grace_minutes: float = 20
    min_days: float = 5
    always_on_fraction: float = 0.8


@dataclass
class Finding:
    entity_id: str
    reason: str  # rare_hour | too_long | away
    session_start: float
    minutes_on: float
    detail: str


def fmt_minutes(m: float) -> str:
    m = int(round(m))
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    return f"{h}h {mm:02d}m" if mm else f"{h}h"


def detect(entity_id, active: bool, since: float, now: float, prof: Profile | None,
           away_since: float | None, hour: int, s: Settings, rules=None) -> list[Finding]:
    """Check one entity at time `now`. `away_since` is when everyone left (None if someone home)."""
    out: list[Finding] = []
    if not active or prof is None or prof.days_observed < s.min_days:
        return out
    minutes = (now - since) / 60
    rules = rules or []

    def suppressed(reason):
        for r in rules:
            if r.get("entity_id") != entity_id or r.get("reason") != reason:
                continue
            hours = r.get("hours")
            if hours is None or hour in hours:
                return True
        return False

    overall = prof.overall()
    always_on = overall is not None and overall >= s.always_on_fraction
    away = away_since is not None

    # 1. On at an hour when it is almost never on
    if minutes >= s.min_on_minutes and not always_on and not suppressed("rare_hour"):
        f, _ = prof.frac(hour, away)
        if f is None and away:
            f, _ = prof.frac(hour, False)
        if f is not None and f < s.rare_threshold:
            out.append(Finding(entity_id, "rare_hour", since, minutes,
                               f"on for {fmt_minutes(minutes)}; it's on only {f * 100:.0f}% of the time at this hour"))

    # 2. On much longer than its usual run
    limit = prof.long_limit(s.long_factor, s.min_long_minutes)
    for r in rules:
        if r.get("entity_id") == entity_id and r.get("reason") == "too_long" and r.get("min_minutes"):
            limit = max(limit or 0, r["min_minutes"])
    if limit and not always_on and limit < 20 * 60 and minutes > limit and not suppressed("too_long"):
        usual = percentile(prof.sessions, 0.95)
        out.append(Finding(entity_id, "too_long", since, minutes,
                           f"on for {fmt_minutes(minutes)}; usually off within {fmt_minutes(usual)}"))

    # 3. On while everyone is away
    if away and (now - away_since) / 60 >= s.away_grace_minutes and not always_on and not suppressed("away"):
        f, _ = prof.frac(hour, True)
        if f is None:
            f = prof.overall(away=True)
        if f is None or f < s.away_threshold:
            out.append(Finding(entity_id, "away", since, minutes,
                               f"on while everyone is away (on for {fmt_minutes(minutes)})"))
    return out


def matches(entity_id: str, patterns) -> bool:
    return any(fnmatch.fnmatch(entity_id, p) for p in patterns)


def replay(entities: dict, person_idx: dict, profiles: dict, start: float, end: float, tz,
           s: Settings, rules=None):
    """Replay history [start, end] and return the findings that would have been sent
    (once per entity, session and reason)."""
    seen = set()
    sent = []
    idxs = {e: as_index(tl) for e, tl in entities.items()}
    # when did "everyone away" start, per step
    t = start - (start % STEP) + STEP
    away_since = None
    while t <= end:
        pres = presence_at(person_idx, t)
        if pres is False:
            away_since = away_since or t
        else:
            away_since = None
        hour = datetime.fromtimestamp(t, tz).hour
        for e, idx in idxs.items():
            st = state_at(idx, t)
            a = is_active(e, st)
            if not a:
                continue
            # session start = time of the last change at or before t
            i = bisect.bisect_right(idx[0], t) - 1
            since = idx[0][i]
            # walk back over consecutive active points (same session)
            while i > 0 and is_active(e, idx[1][i - 1]):
                i -= 1
                since = idx[0][i]
            for f in detect(e, True, since, t, profiles.get(e), away_since, hour, s, rules):
                key = (e, f.reason, f.session_start)
                if key in seen:
                    continue
                seen.add(key)
                sent.append((t, f))
        t += STEP
    return sent
