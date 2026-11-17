"""Loads rules/rules.yaml (the single source of every decision parameter) and
evaluates the compatibility rules. No code path hard-codes a business number.

Override the file with SETUHAUL_RULES=/path/to/rules.yaml.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "rules" / "rules.yaml"


@lru_cache(maxsize=4)
def load(path: str | None = None) -> dict:
    import yaml  # PyYAML
    p = Path(path or os.environ.get("SETUHAUL_RULES", DEFAULT_PATH))
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    validate(data)
    return data


def validate(r: dict):
    need = ["timing", "priority_weight", "compatibility", "hard", "costs", "solver", "conversation", "location"]
    missing = [k for k in need if k not in r]
    if missing:
        raise ValueError(f"rules.yaml missing sections: {missing}")
    for p in ("CRITICAL", "HIGH", "NORMAL", "LOW"):
        if p not in r["priority_weight"]:
            raise ValueError(f"priority_weight.{p} missing")
    for rule in r["compatibility"]["rules"]:
        if not {"name", "when", "require"} <= rule.keys():
            raise ValueError(f"bad compatibility rule {rule}")


def R() -> dict:
    return load()


def required_dock(shipment, heavy_kg: int) -> tuple[str, dict]:
    """First matching rule -> (rule name, required dock attributes)."""
    for rule in R()["compatibility"]["rules"]:
        w = rule["when"]
        ok = True
        for k, v in w.items():
            if k == "any":
                continue
            if k == "load_over_heavy_threshold":
                ok &= (shipment["load_weight_kg"] > heavy_kg) == bool(v)
            else:
                ok &= shipment[k] == v
        if ok:
            return rule["name"], rule["require"]
    return "none", {}


def compatible(shipment, dock, heavy_kg: int) -> tuple[bool, list[str]]:
    reasons = []
    _, req = required_dock(shipment, heavy_kg)
    if any(dock[k] != v for k, v in req.items()):
        reasons.append("DOCK_TYPE")
    if R()["compatibility"].get("weight_limit", True) and shipment["load_weight_kg"] > dock["max_vehicle_weight_kg"]:
        reasons.append("WEIGHT")
    return (not reasons), reasons
