"""Behavior Monitor add-on: learns how devices are normally used and flags unusual use."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

import engine as E

log = logging.getLogger("behavior_monitor")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

DATA = Path("/data")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_URL = "http://supervisor/core/api"
HA_WS = "ws://supervisor/core/websocket"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

DEFAULT_EXCLUDE = [
    # configuration switches that are not real loads
    "switch.*_timer", "switch.*_climate_react", "switch.*shutter*", "switch.*_screen",
    "switch.*_child_lock", "switch.*_led", "switch.*_auto_*", "switch.*_self_clean",
]
ON_NAMES = {"cover": ("open", "closed"), "person": ("home", "not_home"), "device_tracker": ("home", "not_home")}


def load_json(name, default):
    try:
        return json.loads((DATA / name).read_text())
    except Exception:
        return default


def save_json(name, obj):
    tmp = DATA / (name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(DATA / name)


class Monitor:
    def __init__(self, opts: dict):
        self.o = opts
        self.s = E.Settings(
            rare_threshold=opts.get("rare_threshold", 0.05),
            away_threshold=opts.get("away_threshold", 0.10),
            min_on_minutes=opts.get("min_on_minutes", 20),
            long_factor=opts.get("long_factor", 1.5),
            min_long_minutes=opts.get("min_long_minutes", 60),
            away_grace_minutes=opts.get("away_grace_minutes", 20),
        )
        self.tz = ZoneInfo("UTC")
        self.session: aiohttp.ClientSession | None = None
        self.profiles: dict[str, E.Profile] = {
            k: E.Profile.from_dict(v) for k, v in load_json("profiles.json", {}).items()}
        self.rules: list = dedupe_rules(load_json("rules.json", []))
        st = load_json("state.json", {})
        self.sent_keys: dict = st.get("sent_keys", {})
        self.batches: dict = st.get("batches", {})
        self.ignored: dict = st.get("ignored", {})  # entity -> session_start
        self.notify_times: list = st.get("notify_times", [])
        self.current: list = []
        self.learn_info: dict = load_json("learn_info.json", {})
        self.backtest: dict = load_json("backtest.json", {})
        self.history_source = "none"

    # ---------- Home Assistant ----------
    async def ha(self, method, path, **kw):
        async with self.session.request(method, HA_URL + path, headers=HEADERS, **kw) as r:
            if r.status >= 400:
                raise RuntimeError(f"HA {method} {path}: {r.status} {(await r.text())[:200]}")
            return await r.json(content_type=None)

    async def ws_call(self, msg: dict):
        async with self.session.ws_connect(HA_WS) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": TOKEN})
            await ws.receive_json()
            await ws.send_json({"id": 1, **msg})
            while True:
                m = await ws.receive_json()
                if m.get("id") == 1:
                    return m.get("result")

    async def candidates(self, states: list) -> list[str]:
        domains = set(self.o.get("include_domains") or ["light", "switch", "climate", "media_player", "cover"])
        exclude = DEFAULT_EXCLUDE + list(self.o.get("exclude_entities") or [])
        include = set(self.o.get("include_entities") or [])
        try:
            reg = await self.ws_call({"type": "config/entity_registry/list"})
            skip = {r["entity_id"] for r in reg if r.get("entity_category") or r.get("hidden_by") or r.get("disabled_by")}
        except Exception as e:
            log.warning("entity registry unavailable: %s", e)
            skip = set()
        out = []
        for s in states:
            e = s["entity_id"]
            if e in include or (e.split(".")[0] in domains and e not in skip and not E.matches(e, exclude)):
                out.append(e)
        return sorted(out)

    def persons(self, states):
        excl = set(self.o.get("exclude_persons") or [])
        return [s["entity_id"] for s in states if s["entity_id"].startswith("person.") and s["entity_id"] not in excl]

    # ---------- history ----------
    async def influx_history(self, entity_ids, days):
        url = (self.o.get("influx_url") or "").rstrip("/")
        if not url or not self.o.get("influx_password"):
            raise RuntimeError("InfluxDB not configured")
        out = {}
        params_base = {"db": self.o.get("influx_database", "homeassistant"), "u": self.o.get("influx_username", ""),
                       "p": self.o.get("influx_password", ""), "epoch": "s"}
        for e in entity_ids:
            dom, obj = e.split(".", 1)
            q = (f'SELECT "state","value" FROM /.*/ WHERE "entity_id"=\'{obj}\' AND "domain"=\'{dom}\' '
                 f"AND time > now() - {int(days)}d")
            async with self.session.get(url + "/query", params={**params_base, "q": q}) as r:
                if r.status != 200:
                    raise RuntimeError(f"InfluxDB {r.status}: {(await r.text())[:200]}")
                data = await r.json()
            res = data.get("results", [{}])[0]
            if "error" in res:
                raise RuntimeError("InfluxDB: " + res["error"])
            pts = []
            on_n, off_n = ON_NAMES.get(dom, ("on", "off"))
            for series in res.get("series", []) or []:
                cols = series["columns"]
                si, vi = cols.index("state") if "state" in cols else None, cols.index("value") if "value" in cols else None
                for row in series["values"]:
                    st = row[si] if si is not None else None
                    if st is None and vi is not None and row[vi] is not None:
                        st = on_n if row[vi] >= 1 else off_n
                    if st is not None:
                        pts.append((row[0], st))
            pts.sort()
            out[e] = dedupe(pts)
        return out

    async def ha_history(self, entity_ids, days):
        now = datetime.now(ZoneInfo("UTC"))
        start = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        out = {e: [] for e in entity_ids}
        for i in range(0, len(entity_ids), 25):
            chunk = entity_ids[i:i + 25]
            data = await self.ha("GET", f"/history/period/{start}", params={
                "end_time": end, "filter_entity_id": ",".join(chunk), "minimal_response": "", "no_attributes": ""})
            for series in data:
                if not series:
                    continue
                e = series[0]["entity_id"]
                pts = [(datetime.fromisoformat(p["last_changed"]).timestamp(), p["state"]) for p in series]
                out[e] = dedupe(sorted(pts))
        return out

    async def history(self, entity_ids, days):
        try:
            h = await self.influx_history(entity_ids, days)
            self.history_source = f"InfluxDB ({days} days)"
            return h
        except Exception as e:
            log.info("Using Home Assistant history instead of InfluxDB (%s)", e)
        h = await self.ha_history(entity_ids, days)
        self.history_source = "Home Assistant recorder (about 10 days)"
        return h

    # ---------- learning ----------
    async def relearn(self):
        t0 = time.time()
        cfg = await self.ha("GET", "/config")
        self.tz = ZoneInfo(cfg.get("time_zone", "UTC"))
        states = await self.ha("GET", "/states")
        ents = await self.candidates(states)
        persons = self.persons(states)
        days = int(self.o.get("learn_days", 42))
        hist = await self.history(ents + persons, days)
        person_idx = {p: E.as_index(hist.get(p, [])) for p in persons}
        now = time.time()
        start = min((tl[0][0] for tl in hist.values() if tl), default=now - days * 86400)
        start = max(start, now - days * 86400)
        profiles = {}
        for e in ents:
            profiles[e] = E.learn(e, hist.get(e, []), person_idx, start, now, self.tz)
            await asyncio.sleep(0)
        self.profiles = profiles
        save_json("profiles.json", {k: v.to_dict() for k, v in profiles.items()})
        ready = sum(1 for p in profiles.values() if p.days_observed >= self.s.min_days)
        self.learn_info = {"at": datetime.now(self.tz).isoformat(timespec="minutes"), "entities": len(ents),
                           "ready": ready, "persons": persons, "source": self.history_source,
                           "days_of_data": round((now - start) / 86400, 1), "seconds": round(time.time() - t0, 1)}
        save_json("learn_info.json", self.learn_info)
        log.info("Learned %d devices (%d with enough history) from %s in %.0fs",
                 len(ents), ready, self.history_source, time.time() - t0)
        # backtest: learn on all but the last 7 days, replay the last 7 days
        try:
            bt_days = 7
            cut = now - bt_days * 86400
            if cut - start >= self.s.min_days * 86400:
                train = {e: E.learn(e, hist.get(e, []), person_idx, start, cut, self.tz) for e in ents}
                sent = E.replay({e: hist.get(e, []) for e in ents}, person_idx, train, cut, now, self.tz, self.s, self.rules)
                by_reason = {}
                for _, f in sent:
                    by_reason[f.reason] = by_reason.get(f.reason, 0) + 1
                self.backtest = {"days": bt_days, "total": len(sent), "by_reason": by_reason,
                                 "alerts": [{"time": datetime.fromtimestamp(t, self.tz).strftime("%a %d %H:%M"),
                                             "entity": f.entity_id, "name": self.name(states, f.entity_id),
                                             "reason": f.reason, "detail": f.detail} for t, f in sent]}
            else:
                self.backtest = {"days": 0, "total": 0, "note": "not enough history yet for a test replay"}
            save_json("backtest.json", self.backtest)
            log.info("Backtest over last 7 days: %s", {k: v for k, v in self.backtest.items() if k != "alerts"})
            for a in self.backtest.get("alerts", [])[:60]:
                log.info("  would alert %s %s [%s] %s", a["time"], a["name"], a["reason"], a["detail"])
        except Exception:
            log.exception("backtest failed")

    @staticmethod
    def name(states, entity_id):
        for s in states:
            if s["entity_id"] == entity_id:
                return s.get("attributes", {}).get("friendly_name", entity_id).strip()
        return entity_id

    # ---------- detection ----------
    async def check(self):
        states = await self.ha("GET", "/states")
        by_id = {s["entity_id"]: s for s in states}
        now = time.time()
        hour = datetime.now(self.tz).hour
        # presence
        known = [by_id[p] for p in self.persons(states) if by_id[p]["state"] not in E.UNKNOWN]
        away_since = None
        if known and all(p["state"] != "home" for p in known):
            away_since = max(datetime.fromisoformat(p["last_changed"]).timestamp() for p in known)
        findings = []
        for e, prof in self.profiles.items():
            s = by_id.get(e)
            if not s:
                continue
            active = E.is_active(e, s["state"])
            since = datetime.fromisoformat(s["last_changed"]).timestamp()
            if not active:
                self.ignored.pop(e, None)
                continue
            if self.ignored.get(e) == since:
                continue
            for f in E.detect(e, True, since, now, prof, away_since, hour, self.s, self.rules):
                f.name = s.get("attributes", {}).get("friendly_name", e).strip()
                findings.append(f)
        self.current = [{"entity": f.entity_id, "name": f.name, "reason": f.reason, "detail": f.detail} for f in findings]
        new = []
        for f in findings:
            key = f"{f.entity_id}|{f.reason}|{int(f.session_start)}"
            if key not in self.sent_keys:
                self.sent_keys[key] = now
                new.append(f)
        # forget old keys
        self.sent_keys = {k: v for k, v in self.sent_keys.items() if now - v < 3 * 86400}
        if new:
            await self.notify(new, now)
        await self.publish(away_since)
        self.save_state()

    async def notify(self, findings, now):
        self.notify_times = [t for t in self.notify_times if now - t < 3600]
        if len(self.notify_times) >= int(self.o.get("max_notifications_per_hour", 4)):
            log.info("Rate limit reached, not notifying %d findings", len(findings))
            return
        # one line per device, merging reasons
        per = {}
        for f in findings:
            per.setdefault(f.entity_id, []).append(f)
        ents = list(per)
        bid = uuid.uuid4().hex[:8]
        self.batches[bid] = {"entities": ents, "reasons": {e: [f.reason for f in per[e]] for e in ents},
                             "sessions": {e: per[e][0].session_start for e in ents},
                             "hour": datetime.now(self.tz).hour, "at": now}
        self.batches = {k: v for k, v in self.batches.items() if now - v["at"] < 2 * 86400}
        lines = []
        for e in ents[:6]:
            fs = per[e]
            lines.append(f"{fs[0].name}: " + "; ".join(dict.fromkeys(f.detail for f in fs)))
        if len(ents) > 6:
            lines.append(f"+{len(ents) - 6} more")
        title = (f"🔍 Unusual: {per[ents[0]][0].name}" if len(ents) == 1 else f"🔍 {len(ents)} devices on unusually")
        no_action = set(self.o.get("no_action_entities") or [])
        actions = []
        if any(e not in no_action for e in ents):
            actions.append({"action": f"BM_OFF:{bid}", "title": "Turn off" if len(ents) == 1 else "Turn all off"})
        actions += [{"action": f"BM_IGN:{bid}", "title": "Ignore"}, {"action": f"BM_OK:{bid}", "title": "This is normal"}]
        sent = await self.send(title, "\n".join(lines), f"behavior_{bid}", actions)
        if sent:
            self.notify_times.append(now)
            log.info("Notified %s: %s | %s", ", ".join(sent), title, " / ".join(lines))

    async def publish(self, away_since):
        try:
            await self.ha("POST", "/states/sensor.behavior_monitor", json={
                "state": len(self.current),
                "attributes": {"friendly_name": "Behavior Monitor", "icon": "mdi:home-search",
                               "unusual_now": self.current, "everyone_away": away_since is not None,
                               "devices_learned": len(self.profiles), "last_learn": self.learn_info.get("at"),
                               "history_source": self.learn_info.get("source")}})
        except Exception as e:
            log.warning("could not publish sensor: %s", e)

    def save_state(self):
        save_json("state.json", {"sent_keys": self.sent_keys, "batches": self.batches, "ignored": self.ignored,
                                 "notify_times": self.notify_times})

    # ---------- notification buttons ----------
    async def handle_action(self, action: str):
        kind, _, bid = action.partition(":")
        if kind == "BM_TEST":
            await self.send(None, "clear_notification", "behavior_test", None)
            return
        b = self.batches.get(bid)
        if not b:
            log.info("Unknown or expired alert %s", bid)
            return
        no_action = set(self.o.get("no_action_entities") or [])
        if kind == "BM_OFF":
            for e in b["entities"]:
                if e in no_action:
                    continue
                dom = e.split(".")[0]
                svc = {"cover": "close_cover"}.get(dom, "turn_off")
                try:
                    await self.ha("POST", f"/services/{dom}/{svc}", json={"entity_id": e})
                    log.info("Turned off %s", e)
                except Exception as ex:
                    log.error("Could not turn off %s: %s", e, ex)
        elif kind == "BM_IGN":
            for e in b["entities"]:
                self.ignored[e] = b["sessions"][e]
            log.info("Ignoring current session of %s", b["entities"])
        elif kind == "BM_OK":
            h = b["hour"]
            for e in b["entities"]:
                for reason in b["reasons"][e]:
                    rule = {"entity_id": e, "reason": reason, "added": datetime.now(self.tz).isoformat(timespec="minutes")}
                    if reason == "rare_hour" or reason == "away":
                        rule["hours"] = [(h - 1) % 24, h, (h + 1) % 24]
                    if reason == "too_long":
                        rule["min_minutes"] = (time.time() - b["sessions"][e]) / 60 * 1.5
                    if not any(rule_key(r) == rule_key(rule) for r in self.rules):
                        self.rules.append(rule)
                self.ignored[e] = b["sessions"][e]
            save_json("rules.json", self.rules)
            log.info("Marked as normal: %s", b["entities"])
        self.save_state()
        await self.clear_notification(bid)

    async def clear_notification(self, bid):
        """Remove the alert from the phone(s) once a button was pressed."""
        await self.send(None, "clear_notification", f"behavior_{bid}", None)

    # ---------- sending to phones ----------
    async def phone_targets(self) -> list[str]:
        """Notify services (without 'notify.') for the configured phones.

        Each entry in `phones` may be a phone's name in the Home Assistant app
        ("S24 Ultra"), a notify service ("notify.mobile_app_s24_ultra") or its short
        form. Names are resolved on every send, so a phone that re-registers under a
        new service name keeps working. An empty list means every phone with the app.
        """
        services = await self.ha("GET", "/services")
        notify = next((d["services"] for d in services if d["domain"] == "notify"), {})
        mobile = sorted(k for k in notify if k.startswith("mobile_app_"))
        wanted = [str(p).strip() for p in (self.o.get("phones") or []) if str(p).strip()]
        if not wanted:
            return mobile
        out = []
        for w in wanted:
            short = w.split(".", 1)[1] if w.startswith("notify.") else w
            slug = slugify(short)
            for cand in (short, slug, "mobile_app_" + slug):
                if cand in notify:
                    out.append(cand)
                    break
            else:
                # fall back to a phone whose service name contains the slug (e.g. "_2" suffix)
                match = [m for m in mobile if slug and slug in m]
                if match:
                    out.append(match[0])
                else:
                    log.warning("Phone '%s' not found among: %s", w, ", ".join(mobile) or "no phones")
        return list(dict.fromkeys(out))

    async def send(self, title, message, tag, actions) -> list[str]:
        """Send to the configured phones, or through notify_script if one is set."""
        script = (self.o.get("notify_script") or "").strip()
        if script:
            payload = {"message": message, "tag": tag}
            if title:
                payload["title"] = title
            if actions:
                payload["actions"] = actions
            try:
                await self.ha("POST", f"/services/script/{script.split('.', 1)[-1]}", json=payload)
                return [script]
            except Exception as e:
                log.error("Notification script %s failed: %s", script, e)
                return []
        data = {"tag": tag, "group": "behavior_monitor"}
        if actions:
            data["actions"] = actions
        payload = {"message": message, "data": data}
        if title:
            payload["title"] = title
        sent = []
        try:
            targets = await self.phone_targets()
        except Exception as e:
            log.error("Could not list phones: %s", e)
            return []
        if not targets:
            log.error("No phone to notify: install the Home Assistant app on a phone, or set 'phones'")
        for t in targets:
            try:
                await self.ha("POST", f"/services/notify/{t}", json=payload)
                sent.append(t)
            except Exception as e:
                log.error("Notification to %s failed: %s", t, e)
        return sent

    async def test_notification(self):
        return await self.send("🔍 Behavior Monitor test", "Notifications work. Alerts will arrive like this one.",
                               "behavior_test", [{"action": "BM_TEST:x", "title": "OK"}])

    async def listen_events(self):
        while True:
            try:
                async with self.session.ws_connect(HA_WS, heartbeat=30) as ws:
                    await ws.receive_json()
                    await ws.send_json({"type": "auth", "access_token": TOKEN})
                    await ws.receive_json()
                    await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "mobile_app_notification_action"})
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        d = json.loads(msg.data)
                        if d.get("type") == "event":
                            action = d["event"]["data"].get("action", "")
                            if action.startswith("BM_"):
                                await self.handle_action(action)
            except Exception as e:
                log.warning("event connection lost (%s), reconnecting", e)
            await asyncio.sleep(10)

    # ---------- loops ----------
    async def run(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        asyncio.create_task(self.listen_events())
        try:
            await self.relearn()
        except Exception:
            log.exception("initial learning failed")
        last_learn_day = datetime.now(self.tz).date()
        interval = int(self.o.get("check_interval_minutes", 5)) * 60
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("check failed")
            nowl = datetime.now(self.tz)
            if nowl.date() != last_learn_day and nowl.hour >= 3:
                last_learn_day = nowl.date()
                try:
                    await self.relearn()
                except Exception:
                    log.exception("learning failed")
            await asyncio.sleep(interval)


def slugify(text: str) -> str:
    """Home Assistant style slug: 'Ahmad’s iPad' -> 'ahmads_ipad', 'S24 Ultra' -> 's24_ultra'."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"['’`]", "", t)
    t = re.sub(r"[^a-z0-9]+", "_", t)
    return t.strip("_")


def rule_key(r):
    return (r.get("entity_id"), r.get("reason"), tuple(r.get("hours") or []), round(r.get("min_minutes") or 0))


def dedupe_rules(rules):
    seen, out = set(), []
    for r in rules:
        k = rule_key(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def dedupe(pts):
    out = []
    for t, s in pts:
        if not out or out[-1][1] != s:
            out.append((t, s))
    return out


# ---------- web page (ingress) ----------
REASON = {"rare_hour": "unusual hour", "too_long": "on too long", "away": "on while away"}


def page(m: Monitor) -> str:
    esc = html.escape
    li = m.learn_info
    rows_now = "".join(f"<tr><td>{esc(c['name'])}</td><td>{REASON.get(c['reason'], c['reason'])}</td><td>{esc(c['detail'])}</td></tr>"
                       for c in m.current) or "<tr><td colspan=3>Nothing unusual right now</td></tr>"
    bt = m.backtest
    rows_bt = "".join(f"<tr><td>{esc(a['time'])}</td><td>{esc(a['name'])}</td><td>{REASON.get(a['reason'], a['reason'])}</td><td>{esc(a['detail'])}</td></tr>"
                      for a in bt.get("alerts", [])) or "<tr><td colspan=4>None</td></tr>"
    rows_rules = "".join(
        f"<tr><td>{esc(r['entity_id'])}</td><td>{REASON.get(r['reason'], r['reason'])}</td><td>{esc(str(r.get('hours', '')))}</td>"
        f"<td>{esc(r.get('added', ''))}</td><td><form method=post action='rules/delete'><input type=hidden name=i value={i}><button>Remove</button></form></td></tr>"
        for i, r in enumerate(m.rules)) or "<tr><td colspan=5>None yet</td></tr>"
    prof_rows = []
    for e, p in sorted(m.profiles.items()):
        hrs = "".join(
            f"<td style='background:rgba(3,169,244,{(p.on[0][h] / p.obs[0][h]) if p.obs[0][h] else 0:.2f})'></td>"
            for h in range(24))
        lim = p.long_limit(m.s.long_factor, m.s.min_long_minutes)
        prof_rows.append(f"<tr><td>{esc(e)}</td><td>{p.days_observed}</td>{hrs}<td>{E.fmt_minutes(lim) if lim else '-'}</td></tr>")
    hours_head = "".join(f"<th>{h}</th>" for h in range(24))
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Behavior Monitor</title><style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#111;color:#eee}} h2{{margin-top:28px}}
table{{border-collapse:collapse;font-size:13px}} td,th{{border:1px solid #333;padding:4px 6px;text-align:left}}
.hm td{{width:14px;padding:0;height:16px}} .hm td:first-child,.hm td:nth-child(2),.hm td:last-child{{width:auto;padding:2px 6px}}
button{{background:#03a9f4;color:#fff;border:0;padding:4px 10px;border-radius:4px;cursor:pointer}} .muted{{color:#999}}
</style></head><body>
<h1>Behavior Monitor</h1>
<p class=muted>Learned {li.get('entities', 0)} devices ({li.get('ready', 0)} with enough history) at {esc(str(li.get('at', '-')))}
from {esc(str(li.get('source', '-')))}, {li.get('days_of_data', 0)} days of data. People: {esc(', '.join(li.get('persons', [])))}.</p>
<form method=post action='relearn' style='display:inline'><button>Learn again now</button></form>
<form method=post action='test' style='display:inline;margin-left:8px'><button>Send test notification</button></form>
<p class=muted>Alerts go to: {esc(m.o.get('notify_script') or ', '.join(m.o.get('phones') or []) or 'every phone with the Home Assistant app')}</p>
<h2>Unusual right now</h2><table><tr><th>Device</th><th>Why</th><th>Detail</th></tr>{rows_now}</table>
<h2>Test: alerts it would have sent in the last {bt.get('days', 0)} days ({bt.get('total', 0)})</h2>
<p class=muted>{esc(json.dumps(bt.get('by_reason', {})))} {esc(bt.get('note', ''))}</p>
<table><tr><th>When</th><th>Device</th><th>Why</th><th>Detail</th></tr>{rows_bt}</table>
<h2>"This is normal" rules</h2><table><tr><th>Device</th><th>Alert</th><th>Hours</th><th>Added</th><th></th></tr>{rows_rules}</table>
<h2>Learned usage (share of time on, by hour, when someone is home)</h2>
<table class=hm><tr><th>Device</th><th>Days</th>{hours_head}<th>Long limit</th></tr>{''.join(prof_rows)}</table>
</body></html>"""


async def web_app(m: Monitor):
    app = web.Application()

    async def index(_):
        return web.Response(text=page(m), content_type="text/html")

    async def status(_):
        return web.json_response({"learn": m.learn_info, "current": m.current, "backtest": m.backtest, "rules": m.rules})

    async def relearn(_):
        asyncio.create_task(m.relearn())
        raise web.HTTPSeeOther("./")

    async def rule_delete(req):
        form = await req.post()
        i = int(form.get("i", -1))
        if 0 <= i < len(m.rules):
            m.rules.pop(i)
            save_json("rules.json", m.rules)
        raise web.HTTPSeeOther("../")

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/relearn", relearn)
    app.router.add_post("/rules/delete", rule_delete)

    async def test(_):
        sent = await m.test_notification()
        log.info("Test notification sent to: %s", ", ".join(sent) or "nobody")
        raise web.HTTPSeeOther("./")

    app.router.add_post("/test", test)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8099).start()


async def main():
    opts = load_json("options.json", {})
    m = Monitor(opts)
    await web_app(m)
    await m.run()


if __name__ == "__main__":
    asyncio.run(main())
