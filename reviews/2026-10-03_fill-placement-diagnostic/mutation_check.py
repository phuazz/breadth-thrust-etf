"""Mutation check for the fill-placement diagnostic (S2-3 of the spec-freeze
review): every planted mutant of the engine must make
tests/test_ws_fill_placement.py fail. Each mutant is written to a temporary
folder with the spec and the charts beside it, and the suite is pointed at it
through WS_FILL_ENGINE; the registered engine is never modified.

    python reviews/2026-10-03_fill-placement-diagnostic/mutation_check.py

Exit status 0 when every mutant is killed, 1 when one survives.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STUDY = Path(__file__).resolve().parent
REPO = STUDY.parents[1]
ENGINE_DIR = STUDY / "engine"

# name: (registered text, mutated text); each registered text occurs exactly once in the engine
MUTANTS = {
    "offsets start at 1": ('        cand = list(range(lo, hi + 1))\n', '        cand = list(range(1, hi + 1))\n'),
    "thinness on every verdict": ('    if thin_fires and (thin_scope == "any_verdict" or status in PASSING_STATES):\n', '    if thin_fires:\n'),
    "the 10 bp floor ignored": ('    if passed and effect >= delta:\n', '    if passed:\n'),
    "the independent null verdict-bearing": ('    H_D2 = cell(apost, POST, mask=conf, scale=100,', '    H_D2 = cell(apost, placebo_indep["post"], mask=conf, scale=100,'),
    "the forward direction ignored": ('uniform_variant=uniform_variant, direction="forward", members_mask=conf | preb)',
                                      'uniform_variant=uniform_variant, direction="both", members_mask=conf | preb)'),
    "the end rule removed": ('("confirmatory" if u["full_forward_pool"] else "post_cutoff")', '"confirmatory"'),
    "the end rule one window short": ('u["full_forward_pool"] = bool(u["session_index"] + hi + k <= n_bars - 1)',
                                      'u["full_forward_pool"] = bool(u["session_index"] + hi <= n_bars - 1)'),
    "the forward null unblocked": ('spec["placebo"]["draws_per_fill"], blocked=True,', 'spec["placebo"]["draws_per_fill"], blocked=False,'),
    "the missing-placebo stop removed (accumulated null)": ('                stop(f"a placebo is missing in the {direction} null for the {name} fills")\n',
                                                            '                pass\n'),
    "the missing-placebo stop removed (coverage)": ('    for key in PLACEBO_KEYS:\n        if np.isnan(fwd[key][members]).any():\n'
                                                    '            stop(f"a placebo is missing in the forward null ({key})")\n    U, PRE, POST',
                                                    '    U, PRE, POST'),
    "power at a two-sided alpha": ('    return ND.cdf(delta / sd_null - ND.inv_cdf(1 - alpha))', '    return ND.cdf(delta / sd_null - ND.inv_cdf(1 - alpha / 2))'),
    "the MDE without its power term": ('    return sd_null * (ND.inv_cdf(1 - alpha) + ND.inv_cdf(power))', '    return sd_null * ND.inv_cdf(1 - alpha)'),
    "the point branch at the floor": ('    return delta + ND.inv_cdf(power) * sd_null', '    return delta'),
    "the parity share ignored": ('    if parity_share > inconclusive_share:\n        return "INCONCLUSIVE"\n', ''),
    "the reconciliation ignored": ('    if reconciled is False:\n        return "INFEASIBLE"\n', ''),
    "the missing-key stop removed": ('        stop("the adapter\'s provenance lacks the reconciliation or the parity record")\n    if thin_scope',
                                     '        pass\n    if thin_scope'),
    "a pass never demoted": ('        return "PASS" if powered else "SUGGESTIVE"', '        return "PASS"'),
    "a below-floor detection read as a fail": ('        return "DETECTED-BELOW-FLOOR" if powered else "SUGGESTIVE-BELOW-FLOOR"',
                                               '        return "FAIL" if powered else "UNRESOLVED"'),
    "p at alpha excluded": ('    passed = p_worse <= alpha', '    passed = p_worse < alpha'),
    "the demotion not bound to the p-test power": ('    powered = cov["power"]["H_D2"]["power_at_delta"] >= power_target', '    powered = True'),
    "H-D1 on the independent null": ('    H_D1 = cell(au, U, mask=conf,', '    H_D1 = cell(au, placebo_indep["u"], mask=conf,'),
    "a near-flat disclosure null computed": ('    return not sd_null >= ratio * sd_ref\n', '    return sd_null == 0.0\n'),
    # the second spec-freeze review (S2-A): the arithmetic between the H-D2 statistic and the verdict
    "the worse-tail p computed on the better tail": ('"p_one_sided_worse": round(pvalue_ge(nm, a), 4),', '"p_one_sided_worse": round(pvalue_ge(-nm, -a), 4),'),
    "the verdict read on the better-tail p": ('    st = clause_status(H_D2["p_one_sided_worse"],', '    st = clause_status(H_D2["p_one_sided_better"],'),
    "the p-value without its +1": ('    return float((np.sum(null >= actual) + 1) / (len(null) + 1))', '    return float(np.sum(null >= actual) / len(null))'),
    "the effect's sign reversed": ('"effect": round(a - float(nm.mean()), 4),', '"effect": round(float(nm.mean()) - a, 4),'),
    "the effect without the null's centre": ('"effect": round(a - float(nm.mean()), 4),', '"effect": round(a, 4),'),
    "the floor in price units": ('delta2_pct = round(delta2 * 100, 10)', 'delta2_pct = round(delta2, 10)'),
    "the H-D2 effect in price units": ('    H_D2 = cell(apost, POST, mask=conf, scale=100,', '    H_D2 = cell(apost, POST, mask=conf, scale=1,'),
    "pre-blend fills in the verdict cell": ('    H_D2 = cell(apost, POST, mask=conf, scale=100,', '    H_D2 = cell(apost, POST, mask=conf | preb, scale=100,'),
    "thinness never fires": ('"sign_flips": bool(min(vals) < 0 < max(vals)), "detail": effects}', '"sign_flips": False, "detail": effects}'),
    "thinness needs both line and year": ('thin_fires = thinness["H_D2"]["line"]["sign_flips"] or thinness["H_D2"]["year"]["sign_flips"]',
                                          'thin_fires = thinness["H_D2"]["line"]["sign_flips"] and thinness["H_D2"]["year"]["sign_flips"]'),
    "thinness ignores the year drop": ('thin_fires = thinness["H_D2"]["line"]["sign_flips"] or thinness["H_D2"]["year"]["sign_flips"]',
                                       'thin_fires = thinness["H_D2"]["line"]["sign_flips"]'),
    "thinness read from H-D1": ('thin_fires = thinness["H_D2"]["line"]["sign_flips"] or thinness["H_D2"]["year"]["sign_flips"]',
                                'thin_fires = thinness["H_D1"]["line"]["sign_flips"] or thinness["H_D1"]["year"]["sign_flips"]'),
    "thinness computed leave-one-in": ('            keep = groups != g\n', '            keep = groups == g\n'),
    "thinness on the H-D1 vector": ('"line": leave_one_out(apost[conf], post_null_by_fill[conf], line[conf], 100),',
                                    '"line": leave_one_out(au[conf], u_null_by_fill[conf], line[conf], 1),'),
    "the per-fill null for thinness from one draw": ('post_null_by_fill[members] = POST[members].mean(axis=1);', 'post_null_by_fill[members] = POST[members][:, 0];'),
    "the power on the notional-weighted spread": ('    sd_u, sd_post = float(mu.std(ddof=1)), float(mpost.std(ddof=1))\n',
                                                  '    sd_u, sd_post = float(mu.std(ddof=1)), float(wmpost.std(ddof=1))\n'),
    "the power on the independent null": ('"power_at_delta": round(power_normal(delta2, sd_post, alpha), 4),',
                                          '"power_at_delta": round(power_normal(delta2, float(masked_means(placebo_indep["post"], conf).std(ddof=1)), alpha), 4),'),
    "powered only strictly above the target": ('    powered = cov["power"]["H_D2"]["power_at_delta"] >= power_target', '    powered = cov["power"]["H_D2"]["power_at_delta"] > power_target'),
    "the inception day classed pre-blend": ('u["set"] = "pre_blend" if u["date"] < start else', 'u["set"] = "pre_blend" if u["date"] <= start else'),
    "post-cutoff fills let into the forward null": ('uniform_variant=uniform_variant, direction="forward", members_mask=conf | preb)',
                                                    'uniform_variant=uniform_variant, direction="forward")'),
    "the forward pool without +60": ('        cand = list(range(lo, hi + 1))\n', '        cand = list(range(lo, hi))\n'),
    "the input-hash stops reduced to the spec": ('for key in ("spec_sha256", "engine_sha256", "history_json_sha256", "fx_json_sha256", "trades_json_sha256", "book_json_sha256"):',
                                                 'for key in ("spec_sha256",):'),
    "the complete-count stop removed": ('    if n != cov["counts"]["complete"]:\n', '    if False:\n'),
    "the confirmatory-count stop removed": ('    if int(conf.sum()) != cov["counts"]["confirmatory"]:\n', '    if False:\n'),
    "the frozen-floor stop removed": ('    if cov["power"]["H_D2"]["delta"] != delta2:\n', '    if False:\n'),
    "the u draws hash not checked": ('            or draws_hash(masked_means(fwd["u"], conf)) != cov["null"]["confirmatory"]["draws_sha256_mean_u"]:',
                                     '            or False:'),
    # the third spec-freeze pass: the verdict call's arguments, the masked-mean denominator, the placebo's direction, the relations key
    "the verdict reads the raw mean": ('H_D2["p_one_sided_worse"], H_D2["effect"], delta2_pct, powered, alpha)',
                                       'H_D2["p_one_sided_worse"], H_D2["actual"], delta2_pct, powered, alpha)'),
    "the verdict's alpha replaced by the power target": ('H_D2["effect"], delta2_pct, powered, alpha)', 'H_D2["effect"], delta2_pct, powered, power_target)'),
    "the masked mean over every fill": ('    return (wv @ mat[m]) / wv.sum()', '    return (wv @ mat[m]) / len(m)'),
    "the placebo scored backwards": ('            sc = score_many(s, i + pick, unif, fills[r]["side"], k, price_rule=price_rule)',
                                     '            sc = score_many(s, i - pick, unif, fills[r]["side"], k, price_rule=price_rule)'),
    "the cluster relations key misread": ('spec["placebo"].get("block_relations", BLOCK_RELATIONS_PCC)',
                                          'spec["placebo"].get("block_relation", BLOCK_RELATIONS_PCC)'),
    # the fourth spec-freeze pass: the cluster-wide draw that 7,767 of the 7,768 confirmatory fills take
    "the cluster's offset drawn backwards": ('            pick_block = rng.choice(np.array(sorted(common), dtype=int), size=draws, replace=True)',
                                             '            pick_block = -rng.choice(np.array(sorted(common), dtype=int), size=draws, replace=True)'),
    "the cluster's offsets the union of its members'": ('        common = set.intersection(*per) if per else set()',
                                                        '        common = set.union(*per) if per else set()'),
    # the fifth spec-freeze pass: the per-fill branch
    "a fallback member drawn from the first member's pool": ('offs = np.array(sorted(per[r_idx]), dtype=int)', 'offs = np.array(sorted(per[0]), dtype=int)'),
    "one per-fill pick for every draw": ('                pick = rng.choice(offs, size=draws, replace=True)', '                pick = rng.choice(offs, size=1, replace=True)'),
}
# Not planted, by reading: the engine also applies thinness to a demoted pass (SUGGESTIVE), which amendment 4's
# parenthetical does not name; it cannot arise at the frozen p-test power (0.883), and is flagged in the PREREG.


def main() -> int:
    src = (ENGINE_DIR / "fill_timing.py").read_text(encoding="utf-8")
    base = subprocess.run([sys.executable, "-m", "pytest", "tests/test_ws_fill_placement.py", "-q", "-p", "no:cacheprovider"],
                          cwd=str(REPO), capture_output=True, text=True)
    if base.returncode != 0:
        print("the suite fails on the registered engine; no mutant is meaningful")
        return 1
    survived = 0
    for name, (old, new) in MUTANTS.items():
        if src.count(old) != 1:
            print(f"ANCHOR NOT FOUND ONCE: {name}")
            return 1
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "engine"
            d.mkdir()
            (d / "fill_timing.py").write_text(src.replace(old, new), encoding="utf-8", newline="\n")
            for f in ("prereg_spec.json", "charts.py"):
                shutil.copy(ENGINE_DIR / f, d / f)
            env = dict(os.environ, WS_FILL_ENGINE=str(d / "fill_timing.py"))
            out = subprocess.run([sys.executable, "-m", "pytest", "tests/test_ws_fill_placement.py", "-q", "-p", "no:cacheprovider", "-x"],
                                 cwd=str(REPO), env=env, capture_output=True, text=True)
        failing = [line.split(" ")[1].split("::")[-1] for line in out.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))]
        killed = out.returncode != 0
        survived += not killed
        print(f"{'KILLED' if killed else 'SURVIVED'}: {name}; first failing test: {failing[0] if failing else '-'}")
    print(f"{len(MUTANTS) - survived} of {len(MUTANTS)} mutants killed")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
