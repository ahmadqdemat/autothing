"""Data models for the Device Power Cost Tracker."""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


def power_sensor_id(source_entity_id: str) -> str:
    """Build a stable HA sensor id for a tracked device's estimated power."""
    safe = source_entity_id.strip().lower().replace(".", "_").replace(" ", "_")
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in safe)
    return f"sensor.device_cost_{safe}_power"


@dataclass
class DeviceConfig:
    entity_id: str
    name: str
    power_w: float
    price_per_kwh: float
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    # HA power sensor published by this addon (W while on, 0 while off)
    power_entity_id: str = ""
    # Accumulated lifetime totals (persisted)
    total_runtime_seconds: float = 0.0
    total_energy_kwh: float = 0.0
    total_cost: float = 0.0
    # Daily / monthly totals (persisted, reset by scheduler)
    today_energy_kwh: float = 0.0
    today_cost: float = 0.0
    month_energy_kwh: float = 0.0
    month_cost: float = 0.0
    # Track reset dates so we know when to clear
    last_day: str = ""
    last_month: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "DeviceConfig":
        allowed = DeviceConfig.__dataclass_fields__.keys()
        filtered = {k: v for k, v in d.items() if k in allowed}
        dev = DeviceConfig(**filtered)
        if not dev.power_entity_id:
            dev.power_entity_id = power_sensor_id(dev.entity_id)
        return dev


@dataclass
class ActiveSession:
    entity_id: str
    current_state: str
    started_at: datetime
    last_influx_write: datetime = field(default_factory=datetime.utcnow)

    def runtime_seconds(self) -> float:
        return (datetime.utcnow() - self.started_at).total_seconds()

    def energy_kwh(self, power_w: float) -> float:
        return (power_w * self.runtime_seconds()) / (1000.0 * 3600.0)

    def cost(self, power_w: float, price_per_kwh: float) -> float:
        return self.energy_kwh(power_w) * price_per_kwh


# States that count as "ON" (device is consuming power)
ON_STATES = {"on", "heating", "cooling", "running", "heat", "cool",
             "fan_only", "dry", "heat_cool", "auto"}

# States that count as "OFF" (device stopped)
OFF_STATES = {"off", "idle", "unavailable", "unknown", "standby", "paused"}

# Domains shown in the entity picker
TRACKED_DOMAINS = {"switch", "light", "fan", "climate", "input_boolean",
                   "media_player", "vacuum"}
