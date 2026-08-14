"""Abort work that is running pathologically slowly against a degraded upstream.

WHY THIS EXISTS.

``EndpointCircuit`` in fetch_constituents is a dead/alive detector: it trips on
the first HARD failure and short-circuits the rest for free. It works. On
2026-08-14 the DNS resolver died mid-refresh, IJPN's holdings endpoint went
hard-down, the circuit tripped, and the step gave up in 136 seconds.

The step immediately after it — NDIA — took **228 minutes and reported
``[OK]``**. Clean roster, 429 snapshots, "Staleness OK: last real fetch
2026-08-07 (0 days ago)", not one error line in the log. ICHN took 28 minutes
and EXV5 26 minutes in the same window, against a healthy ~1 minute.

Nothing was wrong with the data. Everything was wrong with the clock, and no
guard in the repo measures the clock. The circuit cannot help here because
there is no failure to detect: every request eventually SUCCEEDED. A dead
endpoint is cheap — it is the *degraded* one that costs four hours, because
each date pays the full timeout and then quietly works.

The mechanism is visible in fetch_product_data and needs no inference: the
request carries ``timeout=30`` and retries over ``RETRY_BACKOFFS = [5, 10,
30]``, throttling 1.5s + jitter on success. A date whose first attempt times
out and whose second succeeds costs 30 + 5 + ~1.5s, or about 36 seconds. NDIA
averaged ~30s across 449 Fridays. Healthy is 1.5-3s, which is the throttle
plus the request. So "slow" here is not a vague smell — it is one timeout per
date, and it is measurable per date.

WHAT THIS COSTS WHEN IT IS MISSING.

The refresh started 08:54 SGT on a Friday whose fill is that evening. NDIA
alone consumed 08:5x-13:45. The hourly catch-up trigger — one hour apart for
six hours — spent its entire window refusing to start because the slow run
still held a dirty tree, and expired at 14:00. One un-instrumented step ate
both the schedule and its own safety net, and reported success.

TWO INSTRUMENTS, ONE IDEA.

``LatencyCircuit`` is for loops over many items where each item can be timed
(the per-Friday walk). It measures only items that actually hit the NETWORK —
cache hits are free and would otherwise mask a stall behind a warm cache. It
trips on the rolling mean of the last ``window`` served items, not on a single
slow one, so a lone timeout on an otherwise healthy run is tolerated.

``run_with_deadline`` is for a single opaque bulk call that cannot be
instrumented from outside (``yf.download`` over a whole universe). There is
nothing to count, so it gets a wall clock.

WHY ABORT RATHER THAN FINISH SLOWLY.

Because aborting is nearly free to undo and finishing slowly is not. Raw
responses cache per date under data/raw_ishares/, so a re-run refetches only
what is missing. The committed roster stays untouched, which is the same
judgement compute_breadth already makes when the vendor returns nothing:
"the previous panel is strictly better than anything this run can produce, so
the run must fail and leave it."
"""

from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import dataclass, field


class EndpointDegraded(RuntimeError):
    """The upstream is answering, but far too slowly to be usable."""


class StallTimeout(RuntimeError):
    """A bulk call exceeded its wall-clock budget."""


def _env_float(name: str, default: float) -> float:
    """Read a float override from the environment, ignoring junk.

    Deliberately forgiving: a mistyped env var must not take down a refresh
    that would otherwise be healthy. A bad value falls back to the default
    rather than raising.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


# One timeout per date is ~36s (see module docstring). Healthy is 1.5-3s.
# 12s sits above any plausible healthy mean and a long way below the observed
# pathology, so it separates the two without straddling either.
DEFAULT_LATENCY_THRESHOLD_S = _env_float("BT_STALL_LATENCY_S", 12.0)
DEFAULT_LATENCY_WINDOW = int(_env_float("BT_STALL_WINDOW", 12))

# CSP1 — the widest universe at ~500 tickers — downloads inside a
# compute_breadth step that takes 264s END TO END. 20 minutes is several times
# the worst healthy download and a fraction of the four hours this exists to
# stop.
DEFAULT_DOWNLOAD_DEADLINE_S = _env_float("BT_DOWNLOAD_DEADLINE_S", 1200.0)


@dataclass
class LatencyCircuit:
    """Trip when network-served items are consistently far too slow.

    Feed it ONLY items that hit the network. ``record_cache_hit`` exists so
    the caller can be explicit that it considered an item and chose not to
    time it, rather than silently omitting it.

    The trip is on the mean of the last ``window`` served items. A single slow
    item does not trip it — a genuine one-off timeout on an otherwise healthy
    endpoint is normal and must not abort a refresh. Sustained slowness is not
    normal and is exactly the 2026-08-14 signature.
    """

    threshold_s: float = DEFAULT_LATENCY_THRESHOLD_S
    window: int = DEFAULT_LATENCY_WINDOW
    label: str = "endpoint"
    dead: bool = False
    reason: str | None = None
    n_served: int = 0
    n_cache_hits: int = 0
    total_served_s: float = 0.0
    _recent: deque[float] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ValueError(f"window must be >= 1, got {self.window}")
        if self.threshold_s <= 0:
            raise ValueError(
                f"threshold_s must be > 0, got {self.threshold_s}")
        self._recent = deque(maxlen=self.window)

    # -- observation -------------------------------------------------------

    def record_cache_hit(self) -> None:
        self.n_cache_hits += 1

    def record_served(self, seconds: float, item: object = None) -> None:
        """Time one network-served item and trip if the window is now slow."""
        self.n_served += 1
        self.total_served_s += seconds
        self._recent.append(seconds)
        if self.dead or len(self._recent) < self.window:
            return
        mean = sum(self._recent) / len(self._recent)
        if mean > self.threshold_s:
            self.dead = True
            at = f" at {item}" if item is not None else ""
            self.reason = (
                f"{self.label} is degraded{at}: the last {self.window} "
                f"network-served requests averaged {mean:.1f}s against a "
                f"{self.threshold_s:.0f}s threshold (healthy is 1.5-3s). The "
                f"endpoint is answering, so nothing has failed and nothing "
                f"will — it is simply too slow to finish in time. Raw "
                f"responses cache under data/raw_ishares/, so a re-run once "
                f"the network recovers refetches only what is missing."
            )

    # -- reporting ---------------------------------------------------------

    @property
    def mean_recent_s(self) -> float | None:
        return (sum(self._recent) / len(self._recent)) if self._recent else None

    @property
    def mean_served_s(self) -> float | None:
        return (self.total_served_s / self.n_served) if self.n_served else None

    def raise_if_dead(self) -> None:
        if self.dead:
            raise EndpointDegraded(self.reason or f"{self.label} is degraded")

    def summary(self) -> dict:
        return {
            "label": self.label,
            "degraded": self.dead,
            "reason": self.reason,
            "threshold_s": self.threshold_s,
            "window": self.window,
            "n_served": self.n_served,
            "n_cache_hits": self.n_cache_hits,
            "mean_served_s": (round(self.mean_served_s, 2)
                              if self.mean_served_s is not None else None),
            "mean_recent_s": (round(self.mean_recent_s, 2)
                              if self.mean_recent_s is not None else None),
        }


def run_with_deadline(fn, seconds: float = DEFAULT_DOWNLOAD_DEADLINE_S,
                      label: str = "call"):
    """Run ``fn()`` on a worker thread and give up after ``seconds``.

    The thread is a daemon and is ABANDONED on timeout rather than killed —
    Python cannot kill a thread, and the underlying vendor library holds its
    own pool besides. That is acceptable here and only here: every caller runs
    as its own short-lived subprocess (refresh_all spawns
    ``python scripts/compute_breadth.py --etf X`` per step), the abandoned
    work touches nothing on disk, and the process exits moments later carrying
    the raised error. Do not lift this into a long-lived process without
    replacing the mechanism.
    """
    if seconds <= 0:
        raise ValueError(f"deadline must be > 0, got {seconds}")

    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller
            box["error"] = exc

    worker = threading.Thread(target=_target, name=f"deadline:{label}",
                              daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise StallTimeout(
            f"{label} exceeded its {seconds:.0f}s budget and was abandoned. "
            f"Nothing failed — the upstream is answering too slowly to be "
            f"usable, which is the failure mode that cost 228 minutes on one "
            f"step on 2026-08-14 while reporting success. Re-run once the "
            f"network recovers; caches make the retry cheap. Raise the budget "
            f"with BT_DOWNLOAD_DEADLINE_S if this universe is legitimately "
            f"this slow."
        )
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")
