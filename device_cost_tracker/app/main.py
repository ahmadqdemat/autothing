"""Device Power Cost Tracker – aiohttp web server + background services."""
import asyncio
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

import storage
from calculator import RuntimeEngine
from ha_client import HAClient
from influx_client import InfluxWriter
from models import ON_STATES, DeviceConfig, power_sensor_id

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent.parent
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", BASE_DIR / "frontend"))
DATA_DIR     = os.environ.get("DATA_DIR", "/data")


def _load_options() -> dict:
    options_path = os.path.join(DATA_DIR, "options.json")
    defaults = {
        "influx_url": "http://localhost:8086",
        "influx_username": "homeassistant",
        "influx_password": "",
        "influx_database": "homeassistant",
        "default_price_per_kwh": 0.62,
        "update_interval_sec": 60,
    }
    try:
        with open(options_path, "r", encoding="utf-8") as f:
            defaults.update(json.load(f))
        logger.info("Loaded options.json")
    except FileNotFoundError:
        logger.warning("options.json not found, using defaults")
    except Exception as exc:
        logger.error("Failed to read options.json: %s", exc)
    return defaults


CFG = _load_options()
devices: dict = {}
engine: RuntimeEngine = None
influx: InfluxWriter  = None
ha: HAClient          = None


def _read_token() -> str:
    return CFG.get("ha_token", "") or os.environ.get("SUPERVISOR_TOKEN", "")


# ── Static files ──────────────────────────────────────────────────────────────
async def handle_index(request):
    return web.FileResponse(FRONTEND_DIR / "index.html")

async def handle_static(request):
    fp = FRONTEND_DIR / request.match_info["filename"]
    if not fp.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(fp)


# ── /api/status ───────────────────────────────────────────────────────────────
async def api_status(request):
    return _json({
        "ha_connected":    ha.connected if ha else False,
        "influx_status":   influx.status if influx else "offline",
        "active_sessions": len(engine.sessions) if engine else 0,
        "total_devices":   len(devices),
        "has_token":       bool(_read_token()),
        "version":         "1.0.0",
    })


# ── /api/config ───────────────────────────────────────────────────────────────
async def api_config(request):
    safe = {k: v for k, v in CFG.items() if k not in ("influx_password",)}
    return _json(safe)


# ── /api/entities ─────────────────────────────────────────────────────────────
async def api_entities(request):
    domain_filter = request.rel_url.query.getall("domain", [])
    entities = await ha.get_entities(domain_filter or None)
    return _json(entities)


# ── /api/devices  GET/POST ────────────────────────────────────────────────────
async def api_devices_list(request):
    live = {s["entity_id"]: s for s in engine.live_stats()}
    result = []
    for eid, dev in devices.items():
        d = dev.to_dict()
        d["is_active"] = eid in live
        d["live"] = live.get(eid)
        result.append(d)
    return _json(result)

async def api_devices_add(request):
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")
    entity_id = (body.get("entity_id") or "").strip()
    name = (body.get("name") or entity_id).strip()
    power_w = body.get("power_w")
    price = body.get("price_per_kwh", CFG["default_price_per_kwh"])
    if not entity_id:
        raise web.HTTPBadRequest(reason="entity_id is required")
    try:
        power_w = float(power_w)
        assert power_w > 0
    except (TypeError, ValueError, AssertionError):
        raise web.HTTPBadRequest(reason="power_w must be a positive number")
    if entity_id in devices:
        raise web.HTTPConflict(reason="Device already configured")
    dev = DeviceConfig(entity_id=entity_id, name=name,
                       power_w=power_w, price_per_kwh=float(price),
                       power_entity_id=power_sensor_id(entity_id))
    storage.add_device(dev, devices)
    logger.info("Added device: %s (%s W)", entity_id, power_w)
    # Check if the device is already ON and start a session
    current_state = await ha.get_state(entity_id) or "off"
    engine.on_startup_state(entity_id, current_state)
    logger.info("Device %s current state: %s", entity_id, current_state)
    await _publish_power(dev, is_on=current_state.lower() in ON_STATES)
    return _json(dev.to_dict(), status=201)


# ── /api/devices/{id}  PUT/DELETE ─────────────────────────────────────────────
async def api_devices_update(request):
    entity_id = request.match_info["entity_id"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON")
    allowed = {"name", "power_w", "price_per_kwh"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if "power_w" in updates:
        try:
            updates["power_w"] = float(updates["power_w"])
            assert updates["power_w"] > 0
        except (ValueError, AssertionError):
            raise web.HTTPBadRequest(reason="power_w must be a positive number")
    dev = storage.update_device(entity_id, updates, devices)
    if dev is None:
        raise web.HTTPNotFound(reason="Device not found")
    if not dev.power_entity_id:
        dev.power_entity_id = power_sensor_id(entity_id)
        storage.save_devices(devices)
    is_on = entity_id in engine.sessions
    await _publish_power(dev, is_on=is_on)
    return _json(dev.to_dict())

async def api_devices_delete(request):
    entity_id = request.match_info["entity_id"]
    dev = devices.get(entity_id)
    power_eid = (dev.power_entity_id if dev else "") or power_sensor_id(entity_id)
    if not storage.remove_device(entity_id, devices):
        raise web.HTTPNotFound(reason="Device not found")
    engine.sessions.pop(entity_id, None)
    if ha and power_eid:
        await ha.remove_entity(power_eid)
    return _json({"deleted": entity_id, "power_entity_removed": power_eid})


# ── /api/dashboard ────────────────────────────────────────────────────────────
async def api_dashboard(request):
    live = engine.live_stats()
    today_str = date.today().isoformat()
    month_str = date.today().strftime("%Y-%m")
    tc = te = mc = me = 0.0
    for dev in devices.values():
        if dev.last_day != today_str:
            dev.today_cost = 0.0; dev.today_energy_kwh = 0.0; dev.last_day = today_str
        if dev.last_month != month_str:
            dev.month_cost = 0.0; dev.month_energy_kwh = 0.0; dev.last_month = month_str
        tc += dev.today_cost; te += dev.today_energy_kwh
        mc += dev.month_cost; me += dev.month_energy_kwh
    for s in live:
        tc += s["current_cost"]; te += s["current_energy_kwh"]
    return _json({
        "active_devices": live, "active_count": len(live),
        "total_devices": len(devices),
        "today_cost": round(tc, 4), "today_energy_kwh": round(te, 4),
        "month_cost": round(mc, 4), "month_energy_kwh": round(me, 4),
        "influx_status": influx.status if influx else "offline",
        "ha_connected": ha.connected if ha else False,
        "timestamp": datetime.utcnow().isoformat(),
    })


# ── Background ────────────────────────────────────────────────────────────────
async def periodic_influx_writer():
    while True:
        await asyncio.sleep(CFG["update_interval_sec"])
        for eid in list(engine.sessions.keys()):
            payload = engine.periodic_update(eid)
            if payload and influx:
                influx.enqueue(payload)
        if devices:
            storage.save_devices(devices)

async def _publish_power(dev: DeviceConfig, is_on: bool) -> None:
    if not ha or not dev:
        return
    if not dev.power_entity_id:
        dev.power_entity_id = power_sensor_id(dev.entity_id)
    watts = float(dev.power_w) if is_on else 0.0
    await ha.set_power_sensor(
        power_entity_id=dev.power_entity_id,
        name=dev.name,
        source_entity_id=dev.entity_id,
        power_w=watts,
    )


async def _sync_all_power_sensors() -> None:
    """Recreate / refresh power sensors for every configured device."""
    for eid, dev in list(devices.items()):
        if not dev.power_entity_id:
            dev.power_entity_id = power_sensor_id(eid)
        is_on = eid in engine.sessions
        if not is_on:
            state = await ha.get_state(eid)
            is_on = bool(state and state.lower() in ON_STATES)
        await _publish_power(dev, is_on=is_on)
    storage.save_devices(devices)


def on_state_change(eid, old, new):
    payload = engine.on_state_change(eid, old, new)
    if payload and influx:
        influx.enqueue(payload)
    if payload:
        storage.save_devices(devices)
    dev = devices.get(eid)
    if not dev:
        return None
    new_is_on = new.lower() in ON_STATES
    old_is_on = old.lower() in ON_STATES
    if new_is_on != old_is_on:
        return _publish_power(dev, is_on=new_is_on)
    return None

def on_startup_state(eid, state):
    engine.on_startup_state(eid, state)


# ── App lifecycle ─────────────────────────────────────────────────────────────
async def on_startup(app):
    global devices, engine, influx, ha
    devices = storage.load_devices()
    for eid, dev in devices.items():
        if not dev.power_entity_id:
            dev.power_entity_id = power_sensor_id(eid)
    logger.info("Loaded %d device configs", len(devices))
    engine = RuntimeEngine(devices)
    influx = InfluxWriter(url=CFG["influx_url"], username=CFG["influx_username"],
                          password=CFG["influx_password"], database=CFG["influx_database"])
    await influx.start()
    token = _read_token()
    token_src = "options.json" if CFG.get("ha_token") else "SUPERVISOR_TOKEN" if token else "NONE"
    logger.info("HA auth token source: %s (length=%d)", token_src, len(token))
    ha = HAClient(on_state_change=on_state_change,
                  on_startup_state=on_startup_state,
                  ha_token=token)
    await ha.start()
    # Wait briefly for WS auth, then publish power sensors
    asyncio.create_task(_bootstrap_power_sensors())
    asyncio.create_task(periodic_influx_writer())
    logger.info("Started on port %s", os.environ.get("PORT", 8099))


async def _bootstrap_power_sensors() -> None:
    for _ in range(20):
        if ha and ha.connected:
            break
        await asyncio.sleep(0.5)
    try:
        await _sync_all_power_sensors()
        logger.info("Power sensors synced for %d devices", len(devices))
    except Exception as exc:
        logger.error("Power sensor sync failed: %s", exc)

async def on_shutdown(app):
    if ha:     await ha.stop()
    if influx: await influx.stop()
    storage.save_devices(devices)

def _json(data: Any, status: int = 200):
    return web.Response(text=json.dumps(data, default=str), status=status,
                        content_type="application/json")

def create_app():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", handle_index)
    app.router.add_get("/{filename:.+\\.(?:js|css|ico|png|svg)}", handle_static)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/entities", api_entities)
    app.router.add_get("/api/devices", api_devices_list)
    app.router.add_post("/api/devices", api_devices_add)
    app.router.add_put("/api/devices/{entity_id}", api_devices_update)
    app.router.add_delete("/api/devices/{entity_id}", api_devices_delete)
    app.router.add_get("/api/dashboard", api_dashboard)
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", 8099)))
