"""InfluxDB v1 writer using HTTP line protocol with a local write queue."""
import asyncio
import base64
import logging
import time
from collections import deque
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

MAX_QUEUE = 500          # drop oldest if queue exceeds this
RETRY_INTERVAL = 30      # seconds between retry flushes when Influx is down


def _to_line_protocol(payload: dict, timestamp_ns: Optional[int] = None) -> str:
    """
    Convert a payload dict to InfluxDB line protocol string.

    payload = {
        "measurement": "device_energy",
        "tags": {"entity_id": "switch.tv", "device_name": "TV"},
        "fields": {"power_w": 120.0, "energy_kwh": 0.08, ...}
    }
    """
    measurement = payload["measurement"]
    tags = payload.get("tags", {})
    fields = payload["fields"]

    tag_str = ""
    if tags:
        parts = []
        for k, v in sorted(tags.items()):
            parts.append(f"{_escape_tag(k)}={_escape_tag(str(v))}")
        tag_str = "," + ",".join(parts)

    field_parts = []
    for k, v in fields.items():
        if isinstance(v, str):
            field_parts.append(f'{_escape_field_key(k)}="{_escape_field_val(v)}"')
        elif isinstance(v, bool):
            field_parts.append(f"{_escape_field_key(k)}={'true' if v else 'false'}")
        elif isinstance(v, float):
            field_parts.append(f"{_escape_field_key(k)}={v}")
        elif isinstance(v, int):
            field_parts.append(f"{_escape_field_key(k)}={v}i")

    field_str = ",".join(field_parts)
    ts = f" {timestamp_ns}" if timestamp_ns else ""
    return f"{measurement}{tag_str} {field_str}{ts}"


def _escape_tag(s: str) -> str:
    return s.replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _escape_field_key(s: str) -> str:
    return s.replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _escape_field_val(s: str) -> str:
    return s.replace('"', '\\"').replace("\\", "\\\\")


class InfluxWriter:
    def __init__(self, url: str, username: str, password: str,
                 database: str) -> None:
        self.url = url.rstrip("/")
        self.database = database
        self._auth = base64.b64encode(
            f"{username}:{password}".encode()).decode()
        self._queue: deque = deque(maxlen=MAX_QUEUE)
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected = False

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Basic {self._auth}"}
        )
        await self._ping()
        asyncio.create_task(self._flush_loop())

    async def _ping(self) -> None:
        try:
            async with self._session.get(
                f"{self.url}/ping",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 204 or resp.status == 200:
                    self._connected = True
                    logger.info("InfluxDB ping OK (%s)", self.url)
                else:
                    logger.warning("InfluxDB ping returned %d", resp.status)
        except Exception as exc:
            logger.warning("InfluxDB ping failed: %s", exc)

    async def stop(self) -> None:
        if self._session:
            await self._session.close()

    def enqueue(self, payload: dict) -> None:
        ts_ns = time.time_ns()
        line = _to_line_protocol(payload, ts_ns)
        self._queue.append(line)
        logger.debug("Queued line: %s", line)

    async def _flush_loop(self) -> None:
        while True:
            if self._queue:
                await self._flush()
            await asyncio.sleep(RETRY_INTERVAL)

    async def _flush(self) -> None:
        if not self._queue or not self._session:
            return

        lines = list(self._queue)
        body = "\n".join(lines)
        write_url = f"{self.url}/write"
        params = {"db": self.database, "precision": "ns"}

        try:
            async with self._session.post(
                write_url, params=params,
                data=body.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 204):
                    self._queue.clear()
                    self._connected = True
                    logger.debug("InfluxDB flush OK (%d points)", len(lines))
                else:
                    body_text = await resp.text()
                    logger.error("InfluxDB write error %d: %s",
                                 resp.status, body_text)
                    self._connected = False
        except Exception as exc:
            logger.warning("InfluxDB unavailable: %s", exc)
            self._connected = False

    @property
    def status(self) -> str:
        return "connected" if self._connected else "offline"
