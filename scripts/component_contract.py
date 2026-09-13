"""Verification of independently refreshed book and risk-only HOLD changes."""
import math
from component_basis import MODEL_WEIGHT_ROUNDING_TOL
from overlay_state import BASE_SLEEVE_WEIGHTS, DEFAULT_TILT_WEIGHT, DEFAULT_DERISK_FRACTION


def expected_budgets(decision):
    if type(decision.get("gate_on")) is not bool or type(decision.get("tilt_on")) is not bool:
        raise ValueError("overlay states must be explicit booleans")
    scale = 1 - DEFAULT_DERISK_FRACTION if decision["gate_on"] else 1.0
    weights = {k: v * scale for k, v in BASE_SLEEVE_WEIGHTS.items()}
    tilt = DEFAULT_TILT_WEIGHT * scale if decision["tilt_on"] else 0.0
    weights["b"] -= tilt
    return {**weights, "tilt_nav": tilt, "shy_overlay": 1 - scale}


def unchanged_hold_budget(sleeve, total_held, target_budget):
    """Recognise rounded model weights at an unchanged registered D budget.

    This is not a trade-size threshold. Only the two registered D risk budgets
    qualify; moving between them, or to any other budget, is a real resize.
    The residual bound is the same one already accepted for the held basis.
    """
    if sleeve != "D" or not math.isfinite(total_held) or total_held <= 0:
        return False
    base = BASE_SLEEVE_WEIGHTS["d"]
    for nominal in (base, base * (1 - DEFAULT_DERISK_FRACTION)):
        if (math.isclose(target_budget, nominal, rel_tol=0, abs_tol=1e-12)
                and math.isclose(total_held, nominal, rel_tol=0,
                                 abs_tol=MODEL_WEIGHT_ROUNDING_TOL)):
            return True
    return False


def hold_rounding_residual(sleeves, rows, budgets):
    """Derive the non-trading NAV residual, independently of its declared value."""
    if not any(s["sleeve"] == "D" and s["status"] == "HOLD" for s in sleeves):
        return 0.0
    total = math.fsum(float(r["held"]) for r in rows if r["sleeve"] == "D")
    return total - budgets["d"] if unchanged_hold_budget("D", total, budgets["d"]) else 0.0


def validate_target_nav(sleeves, rows, budgets, declared_residual=None):
    residual = hold_rounding_residual(sleeves, rows, budgets)
    if declared_residual is not None and (
            not math.isfinite(float(declared_residual)) or not math.isclose(
                float(declared_residual), residual, rel_tol=0, abs_tol=1e-12)):
        raise ValueError("declared HOLD rounding residual disagrees with the held basis")
    total = math.fsum(float(r["target"]) for r in rows)
    if not math.isclose(total, 1.0 + residual, rel_tol=0, abs_tol=1e-6):
        raise ValueError("target book does not conserve NAV after explicit HOLD rounding")
    return residual


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
        unchanged = unchanged_hold_budget(sleeve, total_held, target_budget)
        for r in rows:
            held, target, delta = (float(r[k]) for k in ("held", "target", "delta"))
            if not all(math.isfinite(v) for v in (held, target, delta)) or min(held, target) < 0:
                return False
            if unchanged and (target != held or delta != 0):
                return False
            if not unchanged and not math.isclose(target, held / total_held * target_budget, abs_tol=1e-8):
                return False
            if not math.isclose(delta, target - held, abs_tol=1e-8):
                return False
        return True
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
