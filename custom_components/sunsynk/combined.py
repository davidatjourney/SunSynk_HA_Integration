"""Monitoring-only readings for one parallel installation."""

from __future__ import annotations

import hashlib
from typing import Any


def combined_prefix(entry_id: str, serials: list[str]) -> str:
    """Keep statistics separate when installation membership changes."""
    members = hashlib.sha256("\0".join(sorted(serials)).encode()).hexdigest()[:12]
    return f"{entry_id}_combined_{members}_"


def shared_master(serials: list[str], data: dict[str, Any]) -> str | None:
    """Require one live parallel group with one master in one plant."""
    if len(serials) < 2:
        return None
    plants = set()
    masters = []
    for serial in serials:
        payload = data.get(serial, {})
        info = payload.get("inverter", {})
        settings = payload.get("settings", {})
        plant = info.get("plant", {}).get("id")
        if not info or plant is None or isinstance(plant, bool):
            return None
        parallel = str(settings.get("parallel", info.get("parallel")))
        role = str(settings.get("equipMode", info.get("equipMode")))
        if parallel != "1" or role not in {"0", "1"}:
            return None
        plants.add(str(plant))
        if role == "1":
            masters.append(serial)
    return masters[0] if len(plants) == 1 and len(masters) == 1 else None
