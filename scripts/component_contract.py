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


def registered_budgets(sleeve):
    """Every NAV budget the overlay can put a sleeve on, in ascending order.

    Derived from ``expected_budgets`` over the four overlay states rather than
    restated as constants, so the two can never disagree. B carries four
    because the EM tilt comes out of B and scales with the gate; A, C and D
    carry two each, gated and ungated.

    The smallest gap between two registered budgets is B's 0.05, five hundred
    times the rounding tolerance below, so no genuine transition can be
    mistaken for rounding.
    """
    key = str(sleeve).lower()
    return sorted({expected_budgets({"gate_on": g, "tilt_on": t})[key]
                   for g in (False, True) for t in (False, True)})


def unchanged_hold_budget(sleeve, total_held, target_budget):
    """Recognise rounded model weights at an unchanged registered budget.

    This is not a trade-size threshold. Only a REGISTERED budget qualifies, and
    only when the held basket already sits on that same budget: moving between
    two registered budgets, or to any other number, is a real resize and stays
    a trade. The residual bound is the one already accepted for the held basis.

    GENERALISED 2026-09-19 with hold_rounding_residual, not before it. The two
    are one contract: this decides whether a held basket is preserved exactly,
    and that one accounts for the NAV residual which preserving it leaves
    behind. Generalising this alone would have preserved a held C or B basket
    while the residual still counted D only, and the target book would have
    failed to conserve NAV - a refusal to publish, but for the wrong reason and
    at the worst moment. D's 20%/10% behaviour and tolerance are unchanged:
    registered_budgets("d") is exactly the pair this used to hard-code.
    """
    if not math.isfinite(total_held) or total_held <= 0:
        return False
    for nominal in registered_budgets(sleeve):
        if (math.isclose(target_budget, nominal, rel_tol=0, abs_tol=1e-12)
                and math.isclose(total_held, nominal, rel_tol=0,
                                 abs_tol=MODEL_WEIGHT_ROUNDING_TOL)):
            return True
    return False


def hold_rounding_residual(sleeves, rows, budgets):
    """Derive the non-trading NAV residual, independently of its declared value.

    Summed over EVERY held sleeve whose budget is unchanged, because more than
    one sleeve can hold at once - C on a coverage floor and D on a vendor
    retraction was the 2026-09-19 case, and each contributes its own residual.
    """
    residual = 0.0
    for s in sleeves:
        if s.get("status") != "HOLD":
            continue
        name = s["sleeve"]
        budget = budgets[name.lower()]
        total = math.fsum(float(r["held"]) for r in rows if r["sleeve"] == name)
        if unchanged_hold_budget(name, total, budget):
            residual += total - budget
    return residual


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
