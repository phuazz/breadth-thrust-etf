"""Every deployed line's trading proxy is exported, held or not.

2026-09-27: sleeve C ranked Bitcoin in for the first time under the component
release contract. The export named only HELD lines and cache columns, so it
carried BTC-USD but no IBIT, and the release preflight refused the book.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_holdings_prices as ehp
import run_asset_class_rotation as ac
import run_thematic_rotation as th
from etf_registry import ETF_REGISTRY, UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS

DEPLOYED = (set(UNIVERSE_ETFS) | set(UNIVERSE_EUROPE_SECTORS)
            | set(ac.TICKERS) | set(th.TICKERS))


def test_ibit_is_exported_although_bitcoin_is_keyed_btc_usd():
    assert ETF_REGISTRY["BTC-USD"]["yfinance_trading_proxy"] == "IBIT"
    assert "IBIT" in ehp.registry_trading_proxies()


def test_every_deployed_proxy_is_covered():
    proxies = {ETF_REGISTRY[k]["yfinance_trading_proxy"] for k in DEPLOYED
               if (ETF_REGISTRY.get(k) or {}).get("yfinance_trading_proxy")}
    assert proxies <= ehp.registry_trading_proxies()


def test_a_candidate_panel_proxy_is_not_book_critical():
    """EXFB is a screening candidate trading as EXH3.DE; a gap on it must not
    be able to fail the export."""
    assert "EXFB" not in DEPLOYED
    assert ETF_REGISTRY["EXFB"]["yfinance_trading_proxy"] == "EXH3.DE"
    assert "EXH3.DE" not in ehp.registry_trading_proxies()


def test_proxies_reach_both_the_candidate_and_critical_sets():
    src = Path(ehp.__file__).read_text(encoding="utf-8")
    assert "tickers.update(registry_trading_proxies())" in src
    assert "registry_trading_proxies() | book" in src
