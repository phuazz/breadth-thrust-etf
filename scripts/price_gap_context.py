"""Evidence context for missing prices; never changes capture or eligibility.

These are observations of specific issuer identifiers, not price substitutes,
roster exclusions, successor mappings or exemptions from coverage thresholds.
Unknown symbols remain unresolved. Previously documented identities are labelled
as historical observations so they cannot silently certify a future identity.
"""

EVIDENCE = {
    ("breadth_exh9", "2689012D.L"): {
        "category": "subscription_right",
        "isin": "GB00BWNPZS82",
        "detail": "Issuer snapshot identifies Pennon subscription rights (GB00BWNPZS82); ordinary-share prices are not a substitute.",
        "source": "https://www.cashmarket.deutsche-boerse.com/cash-en/Stay-Informed/newsroom/Xetra-Frankfurt-Newsboard/XFRA-NEW-INSTRUMENT-AVAILABLE-12.10.2026-GB00BWNPZS82-5620582",
    },
    ("breadth_itwn", "2418.TW"): {
        "category": "delisted_security",
        "isin": "TW0002418001",
        "detail": "Issuer snapshot identifies Ya Hsin Industrial (TW0002418001); TWSE records its delisting in 2008. No current listed-price history is available.",
        "source": "https://www.twse.com.tw/downloads/zh/about/company/factbook/2009/1.07.htm",
    },
}


def gap_context(key: str, absent: list[str], behind: dict) -> list[dict]:
    """Annotate observed gaps only; never invent a gap or declare it resolved."""
    result = []
    for ticker in sorted(set(absent) | set(behind)):
        evidence = EVIDENCE.get((key, ticker))
        result.append({"ticker": ticker,
                       "category": evidence["category"] if evidence else "unresolved_price_gap",
                       "last_price_date": behind.get(ticker),
                       **({k: v for k, v in evidence.items() if k != "category"} if evidence else {})})
    return result
