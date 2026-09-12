"""Verification of independently refreshed book and risk-only HOLD changes."""
import math
from overlay_state import BASE_SLEEVE_WEIGHTS, DEFAULT_TILT_WEIGHT, DEFAULT_DERISK_FRACTION


def expected_budgets(decision):
    if type(decision.get("gate_on")) is not bool or type(decision.get("tilt_on")) is not bool:
        raise ValueError("overlay states must be explicit booleans")
    scale = 1 - DEFAULT_DERISK_FRACTION if decision["gate_on"] else 1.0
    weights = {k: v * scale for k, v in BASE_SLEEVE_WEIGHTS.items()}
    tilt = DEFAULT_TILT_WEIGHT * scale if decision["tilt_on"] else 0.0
    weights["b"] -= tilt
    return {**weights, "tilt_nav": tilt, "shy_overlay": 1 - scale}


def risk_only_hold(book, sleeve):
    """Approve proportional scaling only, never a new selection under HOLD."""
    try:
        decision = book["overlay_decision"]
        if not decision or decision["as_of"] != book["as_of"]:
            return False
        if any(decision[k] != book["as_of"] for k in ("gate_input_date", "tilt_input_date")):
            return False
        target_budget = expected_budgets(decision)[sleeve.lower()]
        rows = [r for r in book["lines"] if r["sleeve"] == sleeve]
        total_held = sum(float(r["held"]) for r in rows)
        if not rows or total_held <= 0:
            return False
        for r in rows:
            held, target, delta = (float(r[k]) for k in ("held", "target", "delta"))
            if not all(math.isfinite(v) for v in (held, target, delta)) or min(held, target) < 0:
                return False
            if not math.isclose(target, held / total_held * target_budget, abs_tol=1e-8):
                return False
            if not math.isclose(delta, target - held, abs_tol=1e-8):
                return False
        return True
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
