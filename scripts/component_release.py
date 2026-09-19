"""Seal and revalidate the exact component factsheet inputs. No email or fetches.

Python datetime months are 1-indexed. Current-session, fill-calendar, budget,
source-hash and duplicate-line checks are mandatory at both seal and send.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from component_contract import expected_budgets, risk_only_hold, validate_target_nav
from component_basis import validate as validate_basis
from nyse_sessions import week_final_anchor

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = "data/component_release.json"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical(value) + b"\n")
    temporary.replace(path)


def core_identity(book):
    # Build times, valuation updates and D observations are not core orders.
    fields = ("sleeve", "venue", "status", "weights", "decision_session",
              "decision_session_for_fill", "fill_date")
    return digest({"anchor": book["as_of"], "overlay": book["overlay_decision"],
        "sleeves": [{k: s[k] for k in fields} for s in book["sleeves"] if s["sleeve"] != "D"],
        "lines": sorted([r for r in book["lines"] if r["sleeve"] != "D"],
                        key=lambda r: (r["sleeve"], r["etf"]))})


def validate_book(book, basis, now):
    import pandas as pd
    from venue_calendars import get_calendar as _venue_cal
    from live_targets import decision_session_for, next_fill_date
    from session_bounds import last_completed_session_on

    anchor = week_final_anchor(now).isoformat()
    if book["as_of"] != anchor or book.get("executed") is not False:
        raise ValueError("wrong week or executed book")
    validate_basis(basis, anchor)
    built = datetime.fromisoformat(book["computed_at_utc"])
    if built.tzinfo is None or built > now:
        raise ValueError("invalid build timestamp")
    sleeves = book["sleeves"]
    if len(sleeves) != 4 or {s["sleeve"] for s in sleeves} != set("ABCD"):
        raise ValueError("exactly four sleeves are required")
    # AN AUTHORISED HOLD IS THE SAME OBJECT ON EVERY SLEEVE (2026-09-19).
    #
    # This admitted a HOLD only for D. The contract was written with the
    # two-stage sender (addeb8b, 2026-09-12) on the assumption that the three
    # NYSE sleeves are always READY by the time a weekend release is sealed,
    # and that assumption is false for C: its universe carries BTC-USD and
    # 159801.SZ, whose yfinance bars arrive hours after the session, and
    # live_targets' 100% coverage floor turns one missing name into a HOLD.
    # On Saturday 2026-09-19 Yahoo served no 2026-09-18 BTC-USD bar at all,
    # C held on 24 of 25 names, and every hourly firing of the publication job
    # died here - so sleeve A's and B's trades and the overlay's tilt exit
    # went unpublished because a THIRD sleeve had correctly declined to rank.
    #
    # A coverage-floor HOLD is the right book, not a data fault: the intended
    # book IS the held book, and the floor must never be loosened (a partial
    # row is a different signal, not a smaller one). So the fix is here, in the
    # contract, and not in the data - a manual repair_price_gaps --apply does
    # not survive the engine's next cache rebuild in any case.
    #
    # Everything downstream was already sleeve-generic and needed no change:
    # live_targets._build_lines scales or preserves a held sleeve by name,
    # _targets_final already means "every sleeve READY", and the factsheet
    # reads the sleeve's own status. Only this validator and publication_debt
    # hard-coded D.
    d_ready = False
    # NOT `held`: the position loop further down rebinds that name to a float.
    held_sleeves: list[str] = []
    for s in sleeves:
        venue = "XETR" if s["sleeve"] == "D" else "NYSE"
        calendar = _venue_cal(venue)
        last = last_completed_session_on(calendar, now)
        expected = str(last.date())
        close = calendar.schedule(start_date=last, end_date=last).iloc[0]["market_close"]
        if pd.Timestamp(built) < close or s["venue"] != venue:
            raise ValueError("book predates the required close or venue is wrong")
        fill = next_fill_date(venue, now)
        if (s["fill_date"] != fill or s["decision_session_for_fill"] != decision_session_for(venue, fill)
                or s["decision_session_for_fill"] != expected or s["last_completed_session"] != expected):
            raise ValueError(f"{s['sleeve']}: wrong decision or fill session")
        if s["status"] == "READY":
            if s["decision_session"] != expected:
                raise ValueError("READY on the wrong session")
            weights = s["weights"]
            if (not weights or any(not math.isfinite(float(w)) or w < 0 for w in weights.values())
                    or not math.isclose(sum(weights.values()), 1, abs_tol=1e-5)):
                raise ValueError("invalid ranked weights")
            if s["sleeve"] == "D":
                d_ready = True
        # One message per condition. The single message this replaced - "core
        # is not READY or D HOLD is inconsistent" - covered four different
        # failures, and the operator reading the 2026-09-19 log could not tell
        # which had fired without opening the book.
        elif s["status"] != "HOLD":
            raise ValueError(f"{s['sleeve']}: status {s['status']!r} is neither READY nor HOLD")
        elif not s.get("reason"):
            raise ValueError(f"{s['sleeve']}: a HOLD must carry a reason")
        elif not risk_only_hold(book, s["sleeve"]):
            raise ValueError(f"{s['sleeve']}: HOLD is not risk-only against the held basis")
        else:
            held_sleeves.append(s["sleeve"])
    # Finality is a statement about EVERY sleeve, which is what live_targets
    # has meant by it since 2026-09-02 (_targets_final: every sleeve READY and
    # ranked on the close its fill will use). Reading it off D alone was the
    # same D-shaped assumption: with C held and D ready it raised, and with D
    # held and C ready it would have passed a book that was not final.
    if book["targets_final"] is not (not held_sleeves):
        raise ValueError(
            f"finality conflicts with sleeve statuses "
            f"(held: {sorted(held_sleeves) or 'none'})")
    overlay = book["overlay_decision"]
    if any(overlay[k] != anchor for k in ("as_of", "gate_input_date", "tilt_input_date")):
        raise ValueError("overlay source session mismatch")
    budgets = expected_budgets(overlay)
    if any(not math.isclose(overlay["weights"][k], v, abs_tol=1e-8) for k, v in budgets.items()):
        raise ValueError("overlay budget mismatch")
    rows = book["lines"]
    keys = [(r["sleeve"], r["etf"]) for r in rows]
    if not rows or len(set(keys)) != len(keys):
        raise ValueError("empty or duplicate position lines")
    prior = {(r["sleeve"], r["etf"]): float(r["held"]) for r in basis["lines"]}
    expected = {}
    for s in sleeves:
        if s["status"] == "READY":
            expected.update({(s["sleeve"], e): w * budgets[s["sleeve"].lower()]
                             for e, w in s["weights"].items()})
    from run_risk_overlay import EEM_TICKER, FALLBACK_TICKER
    expected.update({("TILT", EEM_TICKER): budgets["tilt_nav"],
                     ("GATE", FALLBACK_TICKER): budgets["shy_overlay"]})
    for r in rows:
        from etf_registry import display_ticker
        if r["traded"] != display_ticker(r["etf"]):
            raise ValueError("displayed instrument does not match the registry")
        held, target, delta = (float(r[k]) for k in ("held", "target", "delta"))
        key = r["sleeve"], r["etf"]
        if r["sleeve"] not in {*"ABCD", "TILT", "GATE"}:
            raise ValueError("unknown sleeve")
        if (not all(math.isfinite(v) for v in (held, target, delta)) or min(held, target) < 0
                or not math.isclose(held, prior.get(key, 0), abs_tol=1e-9)
                or not math.isclose(delta, target - held, abs_tol=1e-9)):
            raise ValueError("invalid position weights or changed held basis")
        # A held sleeve's lines are its HELD book, proportionally rescaled to
        # its risk budget - they are not in `expected`, which is built from
        # READY weights only. Skipping by sleeve NAME rather than by `d_ready`
        # is the same rule as before for D and the correct one for any sleeve;
        # risk_only_hold above is what audits those lines instead.
        if key[0] not in held_sleeves:
            if not math.isclose(target, expected.get(key, 0), abs_tol=1e-8):
                raise ValueError("position disagrees with verified ranking or overlay")
    if any(v > 0 and k not in keys for mapping in (prior, expected) for k, v in mapping.items()):
        raise ValueError("position omitted from instruction")
    declared_rounding = book.get("rounding_residual_nav", 0.0)
    if declared_rounding is None or isinstance(declared_rounding, bool):
        raise ValueError("invalid declared HOLD rounding residual")
    validate_target_nav(sleeves, rows, budgets, declared_rounding)
    d = next(s for s in sleeves if s["sleeve"] == "D")
    d_fields = ("sleeve", "venue", "status", "weights", "decision_session",
                "decision_session_for_fill", "fill_date")
    # `held_sleeves` is what publication_debt reads to decide whether a HOLD is
    # authorised. It used `d_ready is False` as a proxy for "D is held", which
    # cannot express a held C at all and would have left one OBLIGED, escalating
    # a false missed fill from the following Tuesday.
    return {"anchor": anchor, "d_ready": d_ready,
            "held_sleeves": sorted(held_sleeves),
            "core_identity": core_identity(book),
            "europe_identity": digest({"decision": {k: d[k] for k in d_fields},
                "lines": sorted([r for r in rows if r["sleeve"] == "D"], key=lambda r: r["etf"])})}


def source_paths(root):
    # The sender never trusts a mutable manifest against different source files.
    names = {"live_targets.json", "component_held_basis.json", "overlay_decision.json",
             "strategy_freshness.json", "holdings_prices_1y.json", "multi_strategy.json",
             "risk_overlay.json", "live_track.json", "topk_robustness.json",
             "asset_class_rotation.json", "thematic_rotation.json", "europe_rotation.json"}
    from etf_registry import UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS
    for e in set(UNIVERSE_ETFS) | set(UNIVERSE_EUROPE_SECTORS) | {"CSP1"}:
        names.update({f"breadth_{e.lower()}.json", f"constituents_{e.lower()}.json"})
    return [root / "data" / n for n in sorted(names)]


def price_evidence(root, book, reader=read):
    from etf_registry import get_etf
    prices = reader(root / "data/holdings_prices_1y.json")["prices"]
    evidence = {}
    sessions = {s["sleeve"]: s["last_completed_session"] for s in book["sleeves"]}
    for r in book["lines"]:
        if abs(r["delta"]) <= 1e-8:
            continue
        key = r["etf"]
        try:
            proxy = get_etf(key).get("yfinance_trading_proxy") or key
        except KeyError:
            proxy = key
        entry = prices.get(proxy) or prices.get(key)
        required = sessions.get(r["sleeve"], book["as_of"])
        if not entry or len(entry["dates"]) != len(entry["prices"]) or required not in entry["dates"]:
            raise ValueError(f"missing price observation for changed position {key}")
        value = float(entry["prices"][entry["dates"].index(required)])
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"invalid price observation for {key}")
        evidence[key] = {"price_key": proxy, "session": required, "price": value}
    return evidence


def performance(root, reader=read):
    import pandas as pd
    from build_email_body import (_get_deployed_series, _compute_wtd, _ytd_return,
                                  _one_year_return, _sharpe_full, _max_drawdown, _build_label_map)
    data = root / "data"
    key, dates, equity = _get_deployed_series(reader(data / "multi_strategy.json"),
        reader(data / "risk_overlay.json"), reader(data / "live_track.json"))
    series = pd.Series(equity, index=pd.to_datetime(dates), dtype=float)
    if (series.empty or series.index.has_duplicates or not series.index.is_monotonic_increasing
            or any(not math.isfinite(v) or v <= 0 for v in series)):
        raise ValueError("invalid performance series")
    wtd = _compute_wtd(series)
    values = {"WTD": wtd[0] if wtd else None, "YTD": _ytd_return(series),
              "1Y": _one_year_return(series), "Sharpe": _sharpe_full(series),
              "Max drawdown": _max_drawdown(series)}
    values = {k: float(v) if v is not None and math.isfinite(float(v)) else None for k, v in values.items()}
    labels = _build_label_map({k: reader(data / f) for k, f in
        [("a", "topk_robustness.json"), ("b", "asset_class_rotation.json"),
         ("c", "thematic_rotation.json"), ("d", "europe_rotation.json")]})
    return {"as_of": str(series.index[-1].date()), "series": key,
            "values": values, "wtd_start": wtd[1] if wtd else None}, labels


def preflight(root=ROOT, now=None):
    """Fail early on book/price defects; not a replacement for the final seal."""
    now = now or datetime.now(timezone.utc)
    book = read(root / "data/live_targets.json")
    verdict = validate_book(book, read(root / "data/component_held_basis.json"), now)
    price_evidence(root, book)
    return verdict


def seal(root=ROOT, component="core", now=None):
    now = now or datetime.now(timezone.utc)
    book = read(root / "data/live_targets.json")
    basis = read(root / "data/component_held_basis.json")
    verdict = validate_book(book, basis, now)
    from check_pretrade_ready import build_book_report
    report = build_book_report(root / "data/breadth_csp1.json", now, phase="review")
    if report["status"] not in ("ready", "hold"):
        raise ValueError(report["detail"])
    if os.environ.get("BTE_APPLY_STAGED_ROSTER", "").strip() == "1":
        raise ValueError("staged roster promotion cannot auto-publish")
    # WS21, mirroring the rule above. The staged sleeve C Bitcoin basis is a
    # measurement instrument until the WS7 verdict is filed; a run under it must
    # not be able to reach the published book by any route, including an
    # operator who set the flag in one shell and sealed in the same one.
    # Anything other than the incumbent refuses, including a misspelling — a
    # value this guard does not recognise is not a value it may wave through.
    if os.environ.get("BTE_C_BTC_BASIS", "").strip().lower() not in ("", "incumbent"):
        raise ValueError("staged sleeve C Bitcoin basis cannot auto-publish")
    guards = ["core"] + (["europe"] if verdict["d_ready"] else [])
    for scope in guards:
        subprocess.run([sys.executable, "scripts/check_refresh_guard.py", "--component", scope],
                       cwd=root, check=True)
    quotes = price_evidence(root, book)
    stats, labels = performance(root)
    if stats["as_of"] not in {s["last_completed_session"] for s in book["sleeves"]}:
        raise ValueError("performance does not reach a required venue session")
    payload = {"schema": 1, **verdict, "component": component, "sealed_at": now.isoformat(),
        "book": book, "basis": basis, "performance": stats, "labels": labels,
        "quote_evidence": quotes, "guards": guards,
        "sources": {p.relative_to(root).as_posix(): digest(read(p))
                    for p in source_paths(root)}}
    payload["identity"] = digest(payload)
    write(root / MANIFEST, payload)
    return payload


def verify(root=ROOT, now=None, committed=False):
    now = now or datetime.now(timezone.utc)
    payload = read(root / MANIFEST)
    read_bytes = lambda p: p.read_bytes()
    if committed:
        # Git is the immutable archive: read sources from the commit which
        # last changed this seal, not from later daily valuation commits.
        revision = subprocess.run(["git", "log", "-1", "--format=%H", "--", MANIFEST],
                                  cwd=root, capture_output=True, text=True, check=True).stdout.strip()
        if not revision:
            raise ValueError("release has not been committed")
        def read_bytes(path):
            relative = path.relative_to(root).as_posix()
            return subprocess.run(["git", "show", f"{revision}:{relative}"], cwd=root,
                                  capture_output=True, check=True).stdout
        if json.loads(read_bytes(root / MANIFEST)) != payload:
            raise ValueError("release differs from its committed seal")
    reader = lambda p: json.loads(read_bytes(p))
    body = {k: v for k, v in payload.items() if k != "identity"}
    if payload.get("schema") != 1 or payload.get("identity") != digest(body):
        raise ValueError("release seal is invalid")
    required = {p.relative_to(root).as_posix() for p in source_paths(root)}
    if set(payload["sources"]) != required:
        raise ValueError("source manifest is incomplete")
    for name, expected in payload["sources"].items():
        # JSON content hashes survive Git's Windows/Linux newline conversion.
        if digest(reader(root / name)) != expected:
            raise ValueError(f"sealed source changed: {name}")
    if payload["book"] != reader(root / "data/live_targets.json") or payload["basis"] != reader(root / "data/component_held_basis.json"):
        raise ValueError("sealed book or held basis disagrees with source")
    verdict = validate_book(payload["book"], payload["basis"], now)
    if payload["book"]["overlay_decision"] != reader(root / "data/overlay_decision.json"):
        raise ValueError("overlay decision differs from source")
    # `.get`, not `[...]`: a seal written before a verdict key existed must be
    # REFUSED, which is what a mismatch already does, rather than raising a
    # KeyError that reads as a crash in the verifier.
    if any(payload.get(k) != v for k, v in verdict.items()):
        raise ValueError("release verdict mismatch")
    if payload["guards"] != ["core"] + (["europe"] if payload["d_ready"] else []):
        raise ValueError("missing scoped guard receipt")
    if price_evidence(root, payload["book"], reader) != payload["quote_evidence"]:
        raise ValueError("price evidence mismatch")
    stats, labels = performance(root, reader)
    if stats != payload["performance"] or labels != payload["labels"]:
        raise ValueError("performance or instrument labels differ from source")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("seal", "verify", "preflight"))
    parser.add_argument("--component", choices=("core", "europe"), default="core")
    args = parser.parse_args()
    result = (seal(component=args.component) if args.operation == "seal" else
              preflight() if args.operation == "preflight" else verify())
    label = "Book/price preflight only (not sealed)" if args.operation == "preflight" else "Verified component release"
    print(f"{label}: {result['anchor']}; D ready={result['d_ready']}")
