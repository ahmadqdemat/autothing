"""Home Assistant WebSocket client."""
import asyncio
import json
import logging
import os
from typing import Callable, List, Optional

import aiohttp
from models import TRACKED_DOMAINS

logger = logging.getLogger(__name__)

HA_WS_URL   = "ws://homeassistant:8123/api/websocket"
HA_REST_URL = "http://homeassistant:8123/api"
_RECONNECT_BASE = 5
_RECONNECT_MAX  = 120


class HAClient:
    def __init__(self, on_state_change: Callable, on_startup_state: Callable,
                 ha_token: str = "") -> None:
        self._token = ha_token or os.environ.get("SUPERVISOR_TOKEN", "")
        self._on_state_change = on_state_change
        self._on_startup_state = on_startup_state
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws = None
        self._msg_id = 1
        self._pending: dict = {}
        self._running = False
        self.connected = False

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    async def start(self) -> None:
        self._running = True
        self._session = aiohttp.ClientSession()
        await self._load_initial_states()
        asyncio.create_task(self._connect_loop())

    async def stop(self) -> None:
        self._running = False
        if self._session:
            await self._session.close()

    async def get_entities(self, domains: Optional[List[str]] = None) -> List[dict]:
        if not self._session:
            return []
        try:
            async with self._session.get(
                f"{HA_REST_URL}/states", headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.error("HA states fetch failed: %d", resp.status)
                    return []
                states = await resp.json()
        except Exception as exc:
            logger.error("HA REST error: %s", exc)
            return []
        allowed = set(domains) if domains else TRACKED_DOMAINS
        result = []
        for s in states:
            eid = s.get("entity_id", "")
            domain = eid.split(".")[0] if "." in eid else ""
            if domain in allowed:
                result.append({
                    "entity_id": eid,
                    "name": s.get("attributes", {}).get("friendly_name", eid),
                    "state": s.get("state", ""),
                    "domain": domain,
                })
        return sorted(result, key=lambda x: x["name"].lower())

    async def get_state(self, entity_id: str) -> Optional[str]:
        if not self._session:
            return None
        try:
            async with self._session.get(
                f"{HA_REST_URL}/states/{entity_id}", headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("state")
        except Exception as exc:
            logger.warning("get_state(%s) failed: %s", entity_id, exc)
            return None

    async def set_power_sensor(self, power_entity_id: str, name: str,
                               source_entity_id: str, power_w: float) -> bool:
        """Create or update a power sensor (W) for charting in Home Assistant."""
        if not self._session:
            return False
        payload = {
            "state": str(round(float(power_w), 2)),
            "attributes": {
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
                "friendly_name": f"{name} Power",
                "icon": "mdi:flash",
                "source_entity": source_entity_id,
                "attribution": "Device Power Cost Tracker",
            },
        }
        try:
            async with self._session.post(
                f"{HA_REST_URL}/states/{power_entity_id}",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 201):
                    logger.info("Power sensor %s = %s W", power_entity_id, power_w)
                    return True
                text = await resp.text()
                logger.error("set_power_sensor %s failed %d: %s",
                             power_entity_id, resp.status, text)
                return False
        except Exception as exc:
            logger.error("set_power_sensor(%s) error: %s", power_entity_id, exc)
            return False

    async def remove_entity(self, entity_id: str) -> bool:
        """Remove an entity from the HA entity registry (and its state)."""
        if not self.connected or self._ws is None or self._ws.closed:
            logger.warning("Cannot remove %s: WebSocket not connected", entity_id)
            return False
        msg_id = self._next_id()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send_json({
                "id": msg_id,
                "type": "config/entity_registry/remove",
                "entity_id": entity_id,
            })
            result = await asyncio.wait_for(fut, timeout=10)
            if result.get("success"):
                logger.info("Removed HA entity %s", entity_id)
                return True
            logger.warning("Remove %s returned: %s", entity_id, result)
            # Entity may exist only as a state (not in registry). Clear it.
            return await self._clear_state(entity_id)
        except Exception as exc:
            logger.warning("remove_entity(%s) failed: %s", entity_id, exc)
            self._pending.pop(msg_id, None)
            return await self._clear_state(entity_id)

    async def _clear_state(self, entity_id: str) -> bool:
        """Best-effort clear when the entity is not in the registry."""
        if not self._session:
            return False
        try:
            async with self._session.post(
                f"{HA_REST_URL}/states/{entity_id}",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"state": "unavailable", "attributes": {}},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                ok = resp.status in (200, 201)
                if ok:
                    logger.info("Cleared state for %s", entity_id)
                return ok
        except Exception as exc:
            logger.warning("clear_state(%s) failed: %s", entity_id, exc)
            return False

    async def _load_initial_states(self) -> None:
        for ent in await self.get_entities():
            self._on_startup_state(ent["entity_id"], ent["state"])

    async def _connect_loop(self) -> None:
        delay = _RECONNECT_BASE
        while self._running:
            try:
                await self._listen()
                delay = _RECONNECT_BASE
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("HA WS disconnected: %s – reconnecting in %ds", exc, delay)
                self.connected = False
                self._ws = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX)

    async def _listen(self) -> None:
        async with self._session.ws_connect(HA_WS_URL, heartbeat=30) as ws:
            self._ws = ws
            msg = await ws.receive_json()
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"Unexpected: {msg}")
            await ws.send_json({"type": "auth", "access_token": self._token})
            msg = await ws.receive_json()
            if msg.get("type") != "auth_ok":
                raise RuntimeError(f"Auth failed: {msg}")
            logger.info("HA WebSocket authenticated")
            self.connected = True
            sub_id = self._next_id()
            await ws.send_json({"id": sub_id, "type": "subscribe_events",
                                "event_type": "state_changed"})
            ack = await ws.receive_json()
            if not ack.get("success"):
                raise RuntimeError(f"Subscribe failed: {ack}")
            logger.info("Subscribed to state_changed events")
            async for raw in ws:
                if raw.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(json.loads(raw.data))
                elif raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    async def _handle_message(self, msg: dict) -> None:
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._pending:
            fut = self._pending.pop(msg_id)
            if not fut.done():
                fut.set_result(msg)
            return
        if msg.get("type") != "event":
            return
        data = msg.get("event", {}).get("data", {})
        entity_id = data.get("entity_id", "")
        old = (data.get("old_state") or {}).get("state", "unknown")
        new = (data.get("new_state") or {}).get("state", "unknown")
        if old != new:
            result = self._on_state_change(entity_id, old, new)
            if asyncio.iscoroutine(result):
                await result

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id
