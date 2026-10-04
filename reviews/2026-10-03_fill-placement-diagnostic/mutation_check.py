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
}


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
