"""Persistent JSON storage with atomic writes."""
import json
import logging
import os
import tempfile
from typing import Dict, Optional

from models import DeviceConfig

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def load_devices() -> Dict[str, DeviceConfig]:
    """Load all device configurations from disk."""
    _ensure_data_dir()
    if not os.path.exists(DEVICES_FILE):
        return {}
    try:
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            raw: dict = json.load(f)
        return {eid: DeviceConfig.from_dict(cfg) for eid, cfg in raw.items()}
    except Exception as exc:
        logger.error("Failed to load devices.json: %s", exc)
        return {}


def save_devices(devices: Dict[str, DeviceConfig]) -> None:
    """Atomically write all device configurations to disk."""
    _ensure_data_dir()
    data = {eid: dev.to_dict() for eid, dev in devices.items()}
    try:
        fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, DEVICES_FILE)
    except Exception as exc:
        logger.error("Failed to save devices.json: %s", exc)
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def add_device(device: DeviceConfig, devices: Dict[str, DeviceConfig]) -> None:
    devices[device.entity_id] = device
    save_devices(devices)


def update_device(entity_id: str, updates: dict,
                  devices: Dict[str, DeviceConfig]) -> Optional[DeviceConfig]:
    if entity_id not in devices:
        return None
    dev = devices[entity_id]
    for key, value in updates.items():
        if hasattr(dev, key) and key not in ("entity_id", "created_at"):
            setattr(dev, key, value)
    save_devices(devices)
    return dev


def remove_device(entity_id: str, devices: Dict[str, DeviceConfig]) -> bool:
    if entity_id not in devices:
        return False
    del devices[entity_id]
    save_devices(devices)
    return True
