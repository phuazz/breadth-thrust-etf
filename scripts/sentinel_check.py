"""Deployed-dashboard sentinel: does the LIVE site show the as-of it
should, according to the exchange calendar alone?

This is the outside-in check. Everything else in this repo verifies the
process (run status, guard aborts, capture integrity inside the run);
the sentinel verifies the OUTCOME with none of the pipeline's state: it
fetches https://phuazz.github.io/breadth-thrust-etf/factsheet_meta.json
from the deployed site and compares asof_iso against the last completed
NYSE session (scripts/nyse_sessions.py). It therefore catches whatever
the pipeline cannot see about itself — a green run that committed the
wrong artefacts, a Pages deploy that silently served stale content, or
a failure mode nobody has imagined yet — as long as the symptom is a
wrong as-of on the public site.

Runs from its own workflow (.github/workflows/sentinel.yml) ~90 minutes
after the scheduled publishes. Exit 1 on mismatch -> the workflow's
failure step emails a [SENTINEL] alert.

Python datetime months are 1-indexed (January = 1). Printed strings are
plain ASCII (local consoles may not be UTF-8).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Allow importing sibling scripts/ modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from nyse_sessions import last_completed_session  # noqa: E402

META_URL = "https://phuazz.github.io/breadth-thrust-etf/factsheet_meta.json"

# A sentinel that cries on its own network blips trains the operator to
# ignore it. Three attempts, 30 s apart, before declaring failure.
ATTEMPTS = 3
RETRY_WAIT_S = 30


def fetch_deployed_meta() -> dict:
    last_exc: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            # Cache-buster: GitHub Pages serves with max-age=600 and the
            # CDN keys on the full URL, so a unique query string always
            # reaches a fresh object.
            url = f"{META_URL}?cb={int(time.time())}"
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — any fetch problem retries
            last_exc = exc
            print(f"attempt {attempt}/{ATTEMPTS} failed: {exc}")
            if attempt < ATTEMPTS:
                time.sleep(RETRY_WAIT_S)
    raise RuntimeError(f"could not fetch deployed meta: {last_exc}")


def main() -> int:
    now = datetime.now(timezone.utc)
    expected = last_completed_session(now)
    try:
        meta = fetch_deployed_meta()
    except Exception as exc:
        print(f"SENTINEL FAIL: {exc}")
        return 1
    asof = meta.get("asof_iso")
    computed_at = meta.get("computed_at_utc", "?")
    print(f"deployed asof_iso      : {asof}")
    print(f"deployed computed_at   : {computed_at}")
    print(f"expected NYSE session  : {expected.isoformat()}")
    print(f"checked at (UTC)       : {now.strftime('%Y-%m-%d %H:%M')}")
    if asof == expected.isoformat():
        print("SENTINEL OK: deployed dashboard shows the expected as-of.")
        return 0
    print(
        "SENTINEL FAIL: the live site does not show the expected as-of. "
        "Either a scheduled publish failed (check the [FAIL] email and the "
        "Actions tab) or a run went green while publishing wrong/stale "
        "artefacts - in that second case, run the VERIFY_DASHBOARD.md "
        "audit to localise where the chain diverged."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
