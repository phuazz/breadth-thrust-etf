"""WS12/WS13 record charts — reproducible from committed JSON.

Three figures for reviews/2026-08-12_ws12-ws13_execution-timing.docx:
  fig1  the execution surface — blend Sharpe by weekday grid, close fill
        against open fill, with the equivalence band the paired bootstrap
        actually supports drawn around the DEPLOYED cell.
  fig2  the decision path — the six variants that were priced, split into
        their two bases so no reader can subtract across them, annotated
        with the Singapore wall-clock time of each fill.
  fig3  the scope graphic — configurations evaluated, adopted, flagged.

Inputs: data/execution_timing.json (committed projection of
data_local/ws13_execution_grid.json, which folds in WS12's blend legs).

House chart conventions (research-review report_format.md): white theme,
sans-serif, navy #1e3a8a primary, teal #0891b2 / red #dc2626 secondary,
green #dcfce7 "same within noise" fill, every displayed number rounded.

Two conventions matter especially here and are honoured deliberately:

  - The equivalence band is drawn from the PAIRED bootstrap half-width, not
    from the ~0.36 unpaired Sharpe SE quoted elsewhere in this book. These
    legs run on one history and are heavily correlated; an unpaired band
    would be several times too wide and would render every difference as
    noise, including the one that is not.
  - fig2 never places WS12 and WS13 figures on a shared axis. Their panels
    end on different dates, so a single bar chart would invite exactly the
    cross-basis subtraction the record warns against.

Run: python scripts/plot_ws13_summary.py
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FEED = ROOT / "data" / "execution_timing.json"
ASSETS = ROOT / "reviews" / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

NAVY = "#1e3a8a"
TEAL = "#0891b2"
RED = "#dc2626"
GREEN_FILL = "#dcfce7"
INK = "#111827"
FAINT = "#6b7280"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": "#d1d5db",
    "axes.labelcolor": INK,
    "text.color": INK,
    "xtick.color": INK,
    "ytick.color": INK,
    "axes.grid": True,
    "grid.color": "#e5e7eb",
    "grid.linewidth": 0.6,
})


def load() -> dict:
    if not FEED.exists():
        raise SystemExit(f"missing {FEED} - run scripts/run_ws13_execution_grid.py")
    return json.loads(FEED.read_text(encoding="utf-8"))


def fig1_surface(D: dict) -> Path:
    """Levels beside the PAIRED differences that were actually tested.

    An earlier draft drew a single horizontal 'tied with deployment' band from
    the paired half-width. That band contradicted its own annotation: it put
    the Monday open inside the tied region while the arrow beside it said the
    Monday disadvantage survives the paired test. The two are different
    comparisons — the half-width answers 'is open tied with close on THIS
    weekday', not 'is this level tied with deployment' — and a paired interval
    must never be rendered as an absolute band. The lower panel now plots the
    measured differences against zero, where the intervals mean what they say.
    """
    days = D["days"]
    blend = D["blend"]
    close = [blend[d]["close"]["sharpe"] for d in days]
    opn = [blend[d]["open"]["sharpe"] for d in days]
    opn2 = [blend[d]["open_2x"]["sharpe"] for d in days]
    deployed = blend["FRI"]["close"]["sharpe"]
    tests = (D.get("paired_tests") or {}).get("tests", {})

    fig, (ax, ax2) = plt.subplots(
        2, 1, figsize=(9.0, 6.4), sharex=True,
        gridspec_kw={"height_ratios": [1.45, 1.0], "hspace": 0.16})
    x = list(range(len(days)))

    ax.plot(x, close, "o-", color=NAVY, lw=2.2, ms=7, label="fill at the close")
    ax.plot(x, opn, "s-", color=TEAL, lw=2.2, ms=7, label="fill at the open")
    ax.plot(x, opn2, "^--", color=FAINT, lw=1.4, ms=6,
            label="fill at the open, 2× assumed cost")
    ax.axhline(deployed, color=NAVY, lw=0.9, ls=":", zorder=1)
    # Where the two series converge (THU, FRI) a symmetric ±label pair
    # collides. Push the labels apart by the sign of the local gap instead of
    # by a fixed offset, so no pair ever lands on the same pixels.
    for i, (c, o) in enumerate(zip(close, opn)):
        up, dn = (12, -20) if c >= o else (-20, 12)
        ax.annotate(f"{c:.3f}", (i, c), textcoords="offset points",
                    xytext=(0, up), ha="center", fontsize=8.5, color=NAVY)
        ax.annotate(f"{o:.3f}", (i, o), textcoords="offset points",
                    xytext=(0, dn), ha="center", fontsize=8.5, color=TEAL)

    lo_y = min(min(close), min(opn), min(opn2))
    hi_y = max(max(close), max(opn))
    ax.set_ylim(lo_y - 0.035, hi_y + 0.055)          # headroom for annotations

    wed_i = days.index("WED")
    ax.annotate("tests best — REJECTED\n(best of five, no mechanism,\n"
                "sleeves disagree)", (wed_i, close[wed_i]),
                textcoords="offset points", xytext=(-118, -6), ha="left",
                fontsize=8.5, color=RED,
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.0))
    dep_i = days.index("FRI")
    ax.annotate("deployed", (dep_i, deployed), textcoords="offset points",
                xytext=(-64, -26), ha="left", fontsize=9, color=NAVY,
                arrowprops=dict(arrowstyle="->", color=NAVY, lw=1.0))
    ax.set_ylabel("Blend Sharpe")
    ax.set_title("Execution surface — levels above, and the differences that "
                 "were actually tested below", fontsize=11.5, color=INK, pad=26)
    ax.legend(frameon=False, fontsize=8.5, loc="lower center", ncol=3)

    # Lower panel: open minus close, with the paired 90% interval. Zero is the
    # null, so an interval clear of it is the only claim the data supports.
    pts, lo, hi = [], [], []
    for d in days:
        t = tests.get(f"{d}: open minus close", {})
        pts.append(t.get("delta_point"))
        lo.append(t.get("delta_p5"))
        hi.append(t.get("delta_p95"))
    ax2.axhline(0.0, color=INK, lw=1.0)
    for i, (p, a, b) in enumerate(zip(pts, lo, hi)):
        if p is None:
            continue
        excl = (a > 0 and b > 0) or (a < 0 and b < 0)
        col = RED if excl else FAINT
        ax2.plot([i, i], [a, b], color=col, lw=2.4, solid_capstyle="round")
        ax2.plot([i], [p], "o", color=col, ms=8)
        ax2.annotate(f"{p:+.3f}", (i, p), textcoords="offset points",
                     xytext=(11, -3), ha="left", fontsize=8.5, color=col)
    ax2.annotate("interval clear of zero —\nthe Monday auction absorbs\n"
                 "the weekend gap", (0, lo[0]), textcoords="offset points",
                 xytext=(16, -4), ha="left", fontsize=8.5, color=RED)
    ax2.set_xticks(x)
    ax2.set_xticklabels([f"W-{d}" for d in days])
    ax2.set_ylabel("open − close\n(paired, 90% CI)")
    fig.tight_layout()
    out = ASSETS / "ws13_fig1_execution_surface.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def fig2_decision_path(D: dict) -> Path:
    """The clock decided this, so the clock is the axis.

    An earlier draft encoded Sharpe as bar length. It read as a race the
    rejected Wednesday grid was winning, because from a zero baseline a
    1.13-to-1.30 spread is visually almost nothing while the longest bar still
    draws the eye. That inverts the finding: the Sharpe differences do NOT
    decide this and the fill hour does. Position now carries the wall-clock
    time and Sharpe is annotation, which is the argument stated honestly.
    """
    fl = (D.get("fill_lag") or {}).get("legs", {})
    blend = D["blend"]
    rows = [
        ("Deployed  Thu → Fri close", fl.get("friday_close", {}).get("sharpe"),
         28.0, "Sat 04:00", "WS12", "use"),
        ("One session later  Thu → Mon close",
         fl.get("monday_close", {}).get("sharpe"), 28.0, "Tue 04:00", "WS12", "rej"),
        ("Weekly-close grid  Fri → Mon close",
         fl.get("monday_grid", {}).get("sharpe"), 28.0, "Tue 04:00", "WS12", "rej"),
        ("Wednesday grid  Tue → Wed close", blend["WED"]["close"]["sharpe"],
         28.0, "Thu 04:00", "WS13", "rej"),
        ("Monday open  Fri → Mon open", blend["MON"]["open"]["sharpe"],
         21.5, "Mon 21:30", "WS13", "rej"),
        ("Friday open  Thu → Fri open", blend["FRI"]["open"]["sharpe"],
         21.5, "Fri 21:30", "WS13", "rej"),
        ("Friday close, MOC  Thu → Fri close", blend["FRI"]["close"]["sharpe"],
         28.0, "Sat 04:00", "WS13", "adopt"),
    ]
    colour = {"use": NAVY, "rej": FAINT, "adopt": TEAL}
    label = {"use": "in use", "rej": "rejected", "adopt": "ADOPTED"}

    fig, ax = plt.subplots(figsize=(10.4, 4.3))
    ax.axvspan(7.0, 23.0, color=GREEN_FILL, zorder=0)
    ax.axvspan(23.0, 30.0, color="#fee2e2", zorder=0)
    # Band captions sit INSIDE the axes. Placed outside they ride up over the
    # title, which is how the first draft rendered.
    ax.text(15.0, len(rows) - 0.35, "workable Singapore hours", ha="center",
            fontsize=9, color="#15803d")
    # The adopted row sits in this band, so the caption cannot say the band
    # is untradeable — that is the chart contradicting its own conclusion.
    # An overnight auction is unattendable, not untradeable: a
    # market-on-close order is submitted hours earlier.
    ax.text(26.5, len(rows) - 0.35,
            "overnight — order already placed",
            ha="center", fontsize=9, color=FAINT)

    for i, (name, sh, hour, clock, basis, verdict) in enumerate(rows):
        c = colour[verdict]
        ax.plot([hour], [i], "o", ms=13, color=c, zorder=3)
        ax.text(hour + 0.5, i, f"{clock}  ·  "
                + (f"{sh:.4f}" if sh is not None else "n/a")
                + f"  ·  {basis}  ·  {label[verdict]}",
                va="center", ha="left", fontsize=8.6, color=c,
                fontweight="bold" if verdict == "adopt" else "normal")
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in rows], fontsize=9)
    ax.set_ylim(len(rows) - 0.2, -0.7)               # inverted, with headroom
    ax.set_xlim(6.0, 41.0)                           # room for the row text
    ax.set_xticks([8, 12, 16, 20, 24, 28])
    ax.set_xticklabels(["08:00", "12:00", "16:00", "20:00",
                        "00:00⁺¹", "04:00⁺¹"])
    ax.set_xlabel("Singapore time the book would actually turn over "
                  "(summer offsets)")
    ax.grid(axis="y", visible=False)
    ax.set_title("The Sharpe figures mostly tie — what settled it was an "
                 "order type, not a number",
                 fontsize=11.5, color=INK, pad=12)
    fig.tight_layout()
    out = ASSETS / "ws13_fig2_decision_path.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def fig3_scope(D: dict) -> Path:
    cats = [
        ("Fill conventions (WS12)", 3),
        ("Weekday × fill point (WS13)", 10),
        ("Open-leg cost stress", 5),
        ("Paired bootstrap tests", 6),
    ]
    fig, ax = plt.subplots(figsize=(8.4, 3.0))
    names = [c[0] for c in cats]
    vals = [c[1] for c in cats]
    y = range(len(cats))
    ax.barh(list(y), vals, color=NAVY, height=0.5)
    for i, v in enumerate(vals):
        ax.text(v + 0.2, i, str(v), va="center", fontsize=9.5, color=INK)
    ax.set_yticks(list(y))
    ax.set_yticklabels(names, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, max(vals) * 1.25)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("count")
    # Two lines: the single-line form runs past the figure edge and clips.
    title = ("13 execution configurations evaluated  →  1 adopted  →  "
             "1 flagged against")
    subtitle = "adopted: the Friday-open fill   ·   flagged: the Monday open"
    ax.set_title(title + "\n" + subtitle, fontsize=10.5, color=INK, pad=10)
    fig.tight_layout()
    out = ASSETS / "ws13_fig3_scope.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def main() -> int:
    D = load()
    for f in (fig1_surface(D), fig2_decision_path(D), fig3_scope(D)):
        print(f"wrote {f.relative_to(ROOT)}  ({f.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
