"""Attribute an aggregated holding without counting it twice in the page."""
import math

from component_contract import expected_budgets
from mark_to_market_live import SLEEVE_ROUNDING_TOLERANCE


def holding_contributions(live, fallback, tilt_ticker, reserve_key):
    effective = live.get('effective_weights') or {}
    members = {}
    extensions = live.get('sleeve_extensions') or {}
    for key, sleeve in extensions.items():
        for ticker, value in (sleeve.get('weights') or {}).items():
            if float(value) > 0:
                members.setdefault(ticker, []).append(key)
    budgets = expected_budgets({'gate_on': live.get('regime_state') == 'RISK_OFF',
                               'tilt_on': bool(live.get('eem_tilt_active'))})
    out = {}
    for ticker, weight in effective.items():
        owners = members.get(ticker, [])
        if (len(owners) <= 1 and ticker != fallback
                and not (ticker == tilt_ticker and owners and live.get('eem_tilt_active'))):
            # Existing singleton presentation: effective NAV is authoritative.
            if owners:
                out[ticker] = {owners[0]: weight}
            elif ticker == tilt_ticker:
                out[ticker] = {'tilt': weight}
            else:
                raise ValueError(f'held but in no sleeve: {ticker}')
            continue
        if owners and (live.get('regime_state') not in ('RISK_ON', 'RISK_OFF')
                       or type(live.get('eem_tilt_active')) is not bool):
            raise ValueError(f'{ticker}: explicit gate and tilt states required for shared attribution')
        parts = {}
        for owner in owners:
            weights = extensions[owner]['weights']
            total = math.fsum(float(x) for x in weights.values() if float(x) > 0)
            if total > 1 + SLEEVE_ROUNDING_TOLERANCE:
                raise ValueError(f'{owner}: within-sleeve weights exceed NAV')
            divisor = total if abs(total - 1) <= SLEEVE_ROUNDING_TOLERANCE else 1
            parts[owner] = float(weights[ticker]) / divisor * budgets[owner.removeprefix('strategy_')]
        if ticker == tilt_ticker and live.get('eem_tilt_active'):
            parts['tilt'] = budgets['tilt_nav']
        residual = float(weight) - math.fsum(parts.values())
        tolerance = max(1, len(parts)) * 1e-6
        if ticker == fallback and residual > tolerance:
            parts[reserve_key] = residual
        elif parts and abs(residual) <= tolerance:
            # Only reconcile input serialization rounding, never an economic
            # disagreement. The final contributor absorbs at most micro-NAV.
            last = next(reversed(parts))
            parts[last] += residual
        elif not parts and ticker == fallback:
            parts[reserve_key] = float(weight)
        else:
            raise ValueError(f'{ticker}: contribution NAV disagrees with effective holding by {residual:.9f}')
        if any(not math.isfinite(float(x)) or x < 0 for x in parts.values()):
            raise ValueError(f'{ticker}: invalid contribution NAV')
        out[ticker] = parts
    return out
