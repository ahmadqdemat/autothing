from datetime import datetime, timedelta, timezone

import engine as E

TZ = timezone(timedelta(hours=2))
DAY = 86400


def ts(d, h, m=0):
    return datetime(2026, 11, d, h, m, tzinfo=TZ).timestamp()


def kitchen_timeline(days=20, start_day=1):
    """Kitchen light on 18:00-23:00 every evening."""
    tl = []
    for d in range(start_day, start_day + days):
        tl.append((ts(d, 18), "on"))
        tl.append((ts(d, 23), "off"))
    return tl


def home_always():
    return {"person.a": E.as_index([(ts(1, 0), "home")])}


def test_is_active():
    assert E.is_active("light.x", "on") is True
    assert E.is_active("light.x", "off") is False
    assert E.is_active("light.x", "unavailable") is None
    assert E.is_active("climate.x", "cool") is True
    assert E.is_active("climate.x", "off") is False
    assert E.is_active("media_player.x", "idle") is False
    assert E.is_active("cover.x", "open") is True


def test_learn_profile():
    p = E.learn("switch.k", kitchen_timeline(), home_always(), ts(1, 0), ts(21, 0), TZ)
    f20, _ = p.frac(20, False)
    f3, _ = p.frac(3, False)
    assert f20 > 0.9
    assert f3 == 0
    assert len(p.sessions) >= 19
    assert 290 <= E.percentile(p.sessions, 0.5) <= 305
    assert p.days_observed > 19


def test_rare_hour_detected_at_night():
    p = E.learn("switch.k", kitchen_timeline(), home_always(), ts(1, 0), ts(21, 0), TZ)
    s = E.Settings()
    now = ts(22, 2, 30)
    f = E.detect("switch.k", True, ts(22, 1, 0), now, p, None, 2, s)
    reasons = {x.reason for x in f}
    assert "rare_hour" in reasons
    # normal evening use: no alert
    f = E.detect("switch.k", True, ts(22, 18, 0), ts(22, 20, 0), p, None, 20, s)
    assert f == []


def test_short_night_use_ignored():
    p = E.learn("switch.k", kitchen_timeline(), home_always(), ts(1, 0), ts(21, 0), TZ)
    f = E.detect("switch.k", True, ts(22, 2, 0), ts(22, 2, 10), p, None, 2, E.Settings())
    assert f == []  # only 10 minutes, below min_on_minutes


def test_too_long():
    p = E.learn("switch.k", kitchen_timeline(), home_always(), ts(1, 0), ts(21, 0), TZ)
    # started at normal time but still on 9 hours later (limit = 1.5 * 5h = 7.5h)
    f = E.detect("switch.k", True, ts(22, 18, 0), ts(23, 3, 0), p, None, 3, E.Settings())
    assert "too_long" in {x.reason for x in f}


def test_away_detection_and_grace():
    p = E.learn("switch.k", kitchen_timeline(), home_always(), ts(1, 0), ts(21, 0), TZ)
    s = E.Settings()
    # everyone left 10 min ago: grace period, no away alert
    f = E.detect("switch.k", True, ts(22, 18, 0), ts(22, 19, 0), p, ts(22, 18, 50), 19, s)
    assert "away" not in {x.reason for x in f}
    # everyone left 30 min ago, no away history -> alert
    f = E.detect("switch.k", True, ts(22, 18, 0), ts(22, 19, 0), p, ts(22, 18, 30), 19, s)
    assert "away" in {x.reason for x in f}


def test_always_on_never_flagged():
    fridge = [(ts(1, 0), "on")]
    p = E.learn("switch.fridge", fridge, home_always(), ts(1, 0), ts(21, 0), TZ)
    f = E.detect("switch.fridge", True, ts(1, 0), ts(22, 3, 0), p, ts(22, 1, 0), 3, E.Settings())
    assert f == []


def test_rules_suppress():
    p = E.learn("switch.k", kitchen_timeline(), home_always(), ts(1, 0), ts(21, 0), TZ)
    rules = [{"entity_id": "switch.k", "reason": "rare_hour", "hours": [1, 2, 3]}]
    f = E.detect("switch.k", True, ts(22, 1, 0), ts(22, 2, 30), p, None, 2, E.Settings(), rules)
    assert "rare_hour" not in {x.reason for x in f}


def test_not_enough_history():
    p = E.learn("switch.k", kitchen_timeline(days=3), home_always(), ts(1, 0), ts(4, 0), TZ)
    f = E.detect("switch.k", True, ts(5, 1, 0), ts(5, 3, 0), p, None, 3, E.Settings())
    assert f == []


def test_replay_counts_once_per_session():
    learn_tl = kitchen_timeline(days=20)
    p = E.learn("switch.k", learn_tl, home_always(), ts(1, 0), ts(21, 0), TZ)
    test_tl = kitchen_timeline(days=3, start_day=21) + [(ts(25, 1, 0), "on"), (ts(25, 4, 0), "off")]
    test_tl.sort()
    sent = E.replay({"switch.k": test_tl}, home_always(), {"switch.k": p}, ts(21, 0), ts(26, 0), TZ, E.Settings())
    reasons = [f.reason for _, f in sent]
    assert reasons.count("rare_hour") == 1
