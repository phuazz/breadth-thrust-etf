"""Freeze the existing model-held book before a component refresh.

This is the dashboard's model book, not a claim about broker executions.
"""
import json
import math
from datetime import date
from pathlib import Path


def validate(basis, anchor):
    if basis["anchor"] != anchor or date.fromisoformat(basis["model_as_of"]) > date.fromisoformat(anchor):
        raise ValueError("model-held basis has an invalid vintage")
    rows = basis["lines"]
    keys = [(r["sleeve"], r["etf"]) for r in rows]
    if (not rows or len(set(keys)) != len(rows)
            or any(r["sleeve"] not in {"A", "B", "C", "D", "TILT", "GATE"} for r in rows)
            or any(not math.isfinite(float(r["held"])) or float(r["held"]) < 0 for r in rows)
            or not math.isclose(sum(float(r["held"]) for r in rows), 1.0, abs_tol=1e-4)):
        raise ValueError("model-held basis is incomplete, duplicated or does not conserve NAV")
    return basis


def prepare(data_dir: Path, anchor: str):
    path = data_dir / "component_held_basis.json"
    if path.exists():
        prior = json.loads(path.read_text(encoding="utf-8"))
        if prior.get("anchor") == anchor:
            return validate(prior, anchor)
    book = json.loads((data_dir / "live_targets.json").read_text(encoding="utf-8"))
    rows = [{"sleeve": r["sleeve"], "etf": r["etf"], "held": float(r["held"])}
            for r in book["lines"]]
    keys = [(r["sleeve"], r["etf"]) for r in rows]
    if (not rows or len(set(keys)) != len(rows)
            or any(not math.isfinite(r["held"]) or r["held"] < 0 for r in rows)
            or not math.isclose(sum(r["held"] for r in rows), 1.0, abs_tol=1e-4)):
        raise ValueError("model-held basis is incomplete, duplicated or does not conserve NAV")
    result = {"anchor": anchor, "model_as_of": book["as_of"], "lines": rows,
              "basis": "existing model-held positions; not broker execution confirmation",
              "rounding_residual_nav": sum(r["held"] for r in rows) - 1.0}
    validate(result, anchor)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
