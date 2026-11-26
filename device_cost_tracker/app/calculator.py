"""Runtime engine and energy/cost calculations."""
import logging
from datetime import datetime, date
from typing import Dict, Optional

from models import ActiveSession, DeviceConfig, ON_STATES, OFF_STATES

logger = logging.getLogger(__name__)


class RuntimeEngine:
    """Tracks active device sessions and accumulates energy/cost totals."""

    def __init__(self, devices: Dict[str, DeviceConfig]) -> None:
        self.devices = devices
        # entity_id → ActiveSession
        self.sessions: Dict[str, ActiveSession] = {}
        # Restore sessions for devices whose last known state is ON
        # (We can't know this at startup without querying HA, so ha_client handles it)

    # ------------------------------------------------------------------ #
    # Public API called by HA client                                       #
    # ------------------------------------------------------------------ #

    def on_state_change(self, entity_id: str, old_state: str,
                        new_state: str) -> Optional[dict]:
        """
        Process a state_changed event.
        Returns an InfluxDB write payload dict if a session ended or a
        periodic update should be written, else None.
        """
        if entity_id not in self.devices:
            return None

        old_is_on = old_state.lower() in ON_STATES
        new_is_on = new_state.lower() in ON_STATES
        new_is_off = new_state.lower() in OFF_STATES

        if not old_is_on and new_is_on:
            return self._start_session(entity_id, new_state)
        elif old_is_on and new_is_off:
            return self._end_session(entity_id)
        return None

    def on_startup_state(self, entity_id: str, current_state: str) -> None:
        """Called at startup for each monitored entity to restore live sessions."""
        if entity_id not in self.devices:
            return
        if current_state.lower() in ON_STATES:
            if entity_id not in self.sessions:
                logger.info("Restoring active session for %s (state=%s)",
                            entity_id, current_state)
                self.sessions[entity_id] = ActiveSession(
                    entity_id=entity_id,
                    current_state=current_state,
                    started_at=datetime.utcnow(),
                )

    def periodic_update(self, entity_id: str) -> Optional[dict]:
        """
        Called every update_interval_sec for running devices.
        Returns InfluxDB payload for a live 'running' write.
        """
        if entity_id not in self.sessions:
            return None
        dev = self.devices[entity_id]
        sess = self.sessions[entity_id]
        payload = _build_influx_payload(dev, sess, state="running")
        sess.last_influx_write = datetime.utcnow()
        return payload

    def live_stats(self) -> list:
        """Return current live stats for all active sessions."""
        result = []
        for entity_id, sess in self.sessions.items():
            dev = self.devices.get(entity_id)
            if not dev:
                continue
            runtime_s = sess.runtime_seconds()
            energy = sess.energy_kwh(dev.power_w)
            cost = sess.cost(dev.power_w, dev.price_per_kwh)
            result.append({
                "entity_id": entity_id,
                "name": dev.name,
                "state": sess.current_state,
                "power_w": dev.power_w,
                "runtime_seconds": round(runtime_s, 1),
                "current_energy_kwh": round(energy, 4),
                "current_cost": round(cost, 4),
                "started_at": sess.started_at.isoformat(),
            })
        return result

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _start_session(self, entity_id: str, state: str) -> None:
        logger.info("Session START  %s  state=%s", entity_id, state)
        self.sessions[entity_id] = ActiveSession(
            entity_id=entity_id,
            current_state=state,
            started_at=datetime.utcnow(),
        )
        return None

    def _end_session(self, entity_id: str) -> Optional[dict]:
        sess = self.sessions.pop(entity_id, None)
        if sess is None:
            return None

        dev = self.devices[entity_id]
        runtime_s = sess.runtime_seconds()
        energy_kwh = sess.energy_kwh(dev.power_w)
        cost = sess.cost(dev.power_w, dev.price_per_kwh)

        logger.info("Session END    %s  runtime=%.0fs  energy=%.4fkWh  cost=%.4f",
                    entity_id, runtime_s, energy_kwh, cost)

        # Accumulate lifetime totals
        dev.total_runtime_seconds += runtime_s
        dev.total_energy_kwh += energy_kwh
        dev.total_cost += cost

        # Accumulate daily / monthly totals (with auto-reset)
        today_str = date.today().isoformat()
        month_str = date.today().strftime("%Y-%m")
        if dev.last_day != today_str:
            dev.today_energy_kwh = 0.0
            dev.today_cost = 0.0
            dev.last_day = today_str
        if dev.last_month != month_str:
            dev.month_energy_kwh = 0.0
            dev.month_cost = 0.0
            dev.last_month = month_str

        dev.today_energy_kwh += energy_kwh
        dev.today_cost += cost
        dev.month_energy_kwh += energy_kwh
        dev.month_cost += cost

        return _build_influx_payload(dev, sess, state="completed",
                                     runtime_s=runtime_s,
                                     energy_kwh=energy_kwh, cost=cost)


def _build_influx_payload(dev: DeviceConfig, sess: ActiveSession,
                           state: str,
                           runtime_s: Optional[float] = None,
                           energy_kwh: Optional[float] = None,
                           cost: Optional[float] = None) -> dict:
    if runtime_s is None:
        runtime_s = sess.runtime_seconds()
    if energy_kwh is None:
        energy_kwh = sess.energy_kwh(dev.power_w)
    if cost is None:
        cost = sess.cost(dev.power_w, dev.price_per_kwh)

    safe_name = dev.name.replace(" ", "_").replace(",", "")
    return {
        "measurement": "device_energy",
        "tags": {
            "entity_id": dev.entity_id,
            "device_name": safe_name,
        },
        "fields": {
            "power_w": float(dev.power_w),
            "runtime_seconds": float(runtime_s),
            "energy_kwh": float(energy_kwh),
            "cost": float(cost),
            "state": state,
        },
    }
