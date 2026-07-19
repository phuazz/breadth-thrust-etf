"""WS6b T1 — all-in friction, income and ops model for the PARTIAL-5 set.

Registration: ``C:\\dev\\KICKOFF_ws6b-unscreened-replication.md`` (BINDING,
items 1-4 signed 2026-07-19; item 5 defaulted to feasibility-as-a-function-of-
NAV). This module computes the axes WS6 never modelled — actual broker
commissions, half-spreads, dividend withholding, discrete implementability and
operational load — for the signed PARTIAL-5 adoption set (IUES, IUUS, IUCS,
SOXX, IUFS).

It does NOT re-run the WS6 register and does not re-read any WS6 arm. The
construction it drives is the frozen WS6 I0 construction, reached through
``single_name_impl``'s own builder; nothing here re-tunes M, the pool rule, the
cadence or the weighting basis.

Deployed-pipeline discipline: this module is read-only with respect to every
deployed script. The one liberty it takes is a scoped, restored monkeypatch of
``single_name_impl.BROAD_SLICES`` (used at exactly one site, the builder's
"express this line as its own ETF" branch) so that the six NON-adopted
single-named lines stay ETFs under a PARTIAL adoption set. See
``restricted_to``.

--------------------------------------------------------------------------
THE BASIS QUESTION (read before trusting any number this module emits)
--------------------------------------------------------------------------
The register's two series are NOT on the same tax/fee basis, and neither is on
the basis the personal book actually experiences:

* E0's per-line price series is the deployed engine's ``yfinance_trading_proxy``
  under ``yf.download(auto_adjust=True)``. For the four UCITS lines that proxy
  is the US-listed SPDR sector fund (IUES->XLE, IUUS->XLU, IUCS->XLP,
  IUFS->XLF), NOT the LSE-listed UCITS line the book holds. SOXX has no proxy
  and is modelled on itself. ``auto_adjust=True`` reinvests the distribution,
  so each series is (index gross total return - that proxy fund's TER), gross
  of any investor-level withholding.
* I0's per-name series is Norgate TOTALRETURN, which reinvests the full
  declared cash dividend — gross of withholding and of any fund fee.

So the register already credited I0 the proxy fund's TER and charged it no
withholding at all. The T1 adjustments below are therefore differential: each
arm is moved from its register basis to the basis a Singapore-resident
individual actually experiences, and only the difference is charged.

Per line, with y = gross dividend yield of the held basket:

  UCITS lines   actual E0 = register E0 - 0.15*y - (TER_ucits - TER_proxy)
                actual I0 = register I0 - 0.30*y
                => incremental drag on I0 = 0.15*y - (TER_ucits - TER_proxy)

  SOXX          actual E0 = register E0 - 0.30*(y - TER_soxx)
                actual I0 = register I0 - 0.30*y
                => incremental drag on I0 = 0.30 * TER_soxx

The SOXX result is the non-obvious one: because the fund's TER reduces the
distribution that the investor is taxed on, only ~70% of a US-listed fund's TER
is actually recoverable by replicating it. The UCITS result is the material
one: replication converts a 15% fund-level withholding into a 30% investor-
level withholding, and the fee saving is measured against the PROXY's TER, not
the UCITS line's.

Every rate, TER and yield entering this module is supplied by the caller from a
verified published source and carried with its provenance; nothing is defaulted
silently. See ``LineEconomics.source`` and ``Uncertain``.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import single_name_impl as sni

# --- Signed adoption set (kickoff SS2 / SS6 item 1) -------------------------
# Fixed at sign-off by the pre-stated "WS6 G2 correlation >= 0.99" rule. Not
# searchable; widening towards FULL-11 would be a logged SS5b amendment with its
# own shadow period.
PARTIAL_5: tuple[str, ...] = ("IUES", "IUUS", "IUCS", "SOXX", "IUFS")
FULL_11: tuple[str, ...] = sni.SINGLE_NAMED_LINES

TRADING_DAYS = 252


class Uncertain(float):
    """A float that carries its provenance and an uncertainty flag.

    Used so that no verified-source figure can enter the model without the
    record knowing where it came from, and so that a figure the T1 pulls could
    NOT confirm is loud rather than silent (data-integrity rule: flag uncertain
    numbers, never present them as confident).
    """

    source: str
    uncertain: bool
    note: str

    def __new__(cls, value: float, source: str, uncertain: bool = False,
                note: str = "") -> "Uncertain":
        obj = super().__new__(cls, value)
        obj.source = source
        obj.uncertain = bool(uncertain)
        obj.note = note
        return obj

    def describe(self) -> dict:
        return {"value": float(self), "source": self.source,
                "uncertain": self.uncertain, "note": self.note}


@dataclass(frozen=True)
class LineEconomics:
    """Per-line fee and income facts, all from verified published sources.

    ``held_ter``    TER of the instrument the book ACTUALLY holds for this line.
    ``proxy_ter``   TER of the ``yfinance_trading_proxy`` whose series the
                    register used as E0. Equal to ``held_ter`` when the line is
                    modelled on itself (SOXX).
    ``gross_yield`` Gross dividend yield of the underlying basket (pre-tax,
                    pre-fee), i.e. what the top-15 true-weight basket throws off.
    ``us_situs``    Whether the HELD E0 instrument is US-situs for estate tax.
    """

    line: str
    held_instrument: str
    proxy_instrument: str
    held_ter: Uncertain
    proxy_ter: Uncertain
    gross_yield: Uncertain
    fund_level_wht: Uncertain      # suffered inside the E0 wrapper
    investor_wht_direct: Uncertain  # suffered on direct name holdings
    investor_wht_on_e0: Uncertain   # suffered on the E0 distribution
    us_situs: bool

    def income_fee_drag(self) -> float:
        """Annual incremental drag on I0 vs E0, in return terms (decimal).

        Positive = I0 gives up this much per year relative to E0, measured
        against the register's basis. Derived in the module docstring; this is
        the general form both cases specialise from.

            adj_E0 = -(fund_wht * y) - (held_ter - proxy_ter)
                     - investor_wht_on_e0 * (y * (1 - fund_wht) - held_ter)
            adj_I0 = -(investor_wht_direct * y)
            drag   = adj_E0 - adj_I0
        """
        y = float(self.gross_yield)
        distribution = y * (1.0 - float(self.fund_level_wht)) - float(self.held_ter)
        distribution = max(distribution, 0.0)
        adj_e0 = (-(float(self.fund_level_wht) * y)
                  - (float(self.held_ter) - float(self.proxy_ter))
                  - float(self.investor_wht_on_e0) * distribution)
        adj_i0 = -(float(self.investor_wht_direct) * y)
        return adj_e0 - adj_i0


@dataclass(frozen=True)
class BrokerSchedule:
    """A published IBKR commission tier, as it applies to one order.

    ``per_share``      USD per share.
    ``min_order``      USD floor per order — the term that drives minimum
                       viable NAV, because it is fixed while order notional
                       scales with NAV.
    ``max_pct_value``  cap as a fraction of order notional.
    ``fractional_min_applies`` records whether ``min_order`` bites on a
                       fractional-share order. Descriptive only: the fractional
                       treatment is already encoded in that schedule's own
                       ``min_order``, so nothing reads this field.
    """

    name: str
    per_share: Uncertain
    min_order: Uncertain
    max_pct_value: Uncertain
    fractional_min_applies: bool
    source: str = ""
    # IBKR's Europe/LSE schedule is quoted as a PERCENTAGE OF TRADE VALUE with
    # no maximum, not as a per-share rate. Set this and ``per_share`` is
    # ignored. Modelling an LSE-listed UCITS trade on the US per-share schedule
    # understates it by roughly a factor of five, and in E0's favour.
    pct_of_value: Uncertain | None = None
    has_max: bool = True

    def commission_usd(self, notional: np.ndarray, price: np.ndarray) -> np.ndarray:
        """Vectorised commission on |notional| USD traded at ``price``/share."""
        notional = np.asarray(notional, dtype=float)
        price = np.asarray(price, dtype=float)
        traded = np.abs(notional)
        if self.pct_of_value is not None:
            raw = float(self.pct_of_value) * traded
        else:
            with np.errstate(divide="ignore", invalid="ignore"):
                shares = np.where(price > 0, traded / price, 0.0)
            raw = float(self.per_share) * shares
        capped = (np.minimum(raw, float(self.max_pct_value) * traded)
                  if self.has_max else raw)
        floored = np.maximum(capped, float(self.min_order))
        # A zero-size order costs nothing; the floor applies only to real orders.
        return np.where(traded > 0, floored, 0.0)


@dataclass
class BookMechanics:
    """The frozen-construction trade ledger both arms are costed against."""

    arm_id: str
    adopted: tuple[str, ...]
    name_weights: pd.DataFrame          # daily, columns = names/lines
    gross_daily: pd.Series              # cost-free daily return
    rebal_dates: pd.DatetimeIndex
    eligible: pd.Timestamp
    line_weight_time: pd.Series         # mean held weight per line over window
    trades: pd.DataFrame                # (date, name) -> abs weight delta
    fallback_weeks: dict = field(default_factory=dict)
    basket_sizes: dict = field(default_factory=dict)


@contextlib.contextmanager
def restricted_to(adopted: tuple[str, ...]):
    """Temporarily make every NON-adopted single-named line behave as an ETF.

    ``single_name_impl.BROAD_SLICES`` is consulted at exactly one site — the
    builder's "express this line as its own ETF" branch — so widening it is a
    faithful way to express a PARTIAL adoption set without touching the frozen
    construction or editing any deployed script. Restored unconditionally.
    """
    unknown = set(adopted) - set(sni.SINGLE_NAMED_LINES)
    if unknown:
        raise ValueError(f"not single-named lines: {sorted(unknown)}")
    original = sni.BROAD_SLICES
    held_as_etf = tuple(L for L in sni.SINGLE_NAMED_LINES if L not in adopted)
    try:
        sni.BROAD_SLICES = tuple(original) + held_as_etf
        yield sni.BROAD_SLICES
    finally:
        sni.BROAD_SLICES = original


def trade_ledger(name_weights: pd.DataFrame,
                 rebal_dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Per-rebalance, per-name absolute weight change — the order book.

    The builder's daily panel is a forward-filled step function, so the whole
    trade vector lives on the rebalance rows. Rows with a zero delta are
    dropped: they are not orders and must not attract a per-order minimum.
    Returns a long frame with columns (date, name, delta, weight_after).
    """
    rb = name_weights.reindex(rebal_dates).fillna(0.0)
    delta = rb.diff()
    # The first rebalance establishes the book from flat: its whole row is a
    # trade, not a NaN.
    delta.iloc[0] = rb.iloc[0]
    stacked = delta.stack()
    stacked = stacked[stacked.abs() > 0]
    out = stacked.rename("delta").reset_index()
    out.columns = ["date", "name", "delta"]
    out["abs_delta"] = out["delta"].abs()
    after = rb.stack().rename("weight_after").reset_index()
    after.columns = ["date", "name", "weight_after"]
    return out.merge(after, on=["date", "name"], how="left")


def annualised_drag_from_daily(daily_cost: pd.Series) -> float:
    """Mean daily cost expressed as an annual return drag (decimal)."""
    if daily_cost.empty:
        return 0.0
    return float(daily_cost.mean()) * TRADING_DAYS


def net_sharpe(gross_daily: pd.Series, daily_cost: pd.Series) -> float:
    """Annualised Sharpe of a gross return series net of a daily cost series.

    Mirrors ``run_ws6_single_name._ann_sharpe`` mechanics (zero risk-free, sqrt-252
    scaling) so the output is directly comparable with the WS6 register.
    """
    net = gross_daily.sub(daily_cost.reindex(gross_daily.index).fillna(0.0),
                          fill_value=0.0)
    sd = net.std(ddof=1)
    if not np.isfinite(sd) or sd == 0:
        return float("nan")
    return float(net.mean() / sd * np.sqrt(TRADING_DAYS))
