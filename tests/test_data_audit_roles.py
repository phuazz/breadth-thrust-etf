"""The Data tab must not tell a reader that a research panel is traded.

Spotted 2026-08-15 from the published page: ICHN, IJPN, ITWN and NDIA rendered
as `deployed`. _role keyed on UNIVERSE_GLOBAL, which folds in
UNIVERSE_COUNTRIES — a research universe consumed only by build_data_audit and
run_phase4_experiment, whose sleeve was REJECTED (ledger record
2026-07-02-breadth-thrust-etf-2: "survives only 3 of 6 sub-periods, with a
negative train half").

The function's own docstring already named the risk — "the 14 Europe
supersectors are captured for research and are NOT traded" — so this is a case
of the prose being right and the code not following it, which is exactly the
kind of drift a test pins and a comment cannot.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_data_audit as bda  # noqa: E402
from etf_registry import (  # noqa: E402
    ETF_REGISTRY,
    EUROPE_SUPERSECTORS_CANDIDATE,
    UNIVERSE_COUNTRIES,
    UNIVERSE_ETFS,
    UNIVERSE_EUROPE_SECTORS,
)

TEMPLATE = ROOT / "template.html"

# The three the page's own legend and filter define. Anything else is a label
# no reader can select and no legend explains.
DOCUMENTED = {"deployed", "candidate", "monitored"}


def test_only_the_traded_book_is_labelled_deployed():
    traded = set(UNIVERSE_ETFS) | set(UNIVERSE_EUROPE_SECTORS)
    labelled = {e for e in ETF_REGISTRY if bda._role(e) == "deployed"}
    assert labelled == traded
    assert len(labelled) == 19


@pytest.mark.parametrize("etf", sorted(UNIVERSE_COUNTRIES))
def test_the_rejected_country_sleeve_is_not_deployed(etf):
    """The sleeve these belong to was tested and rejected. Presenting them as
    traded on a public page misstates the book."""
    assert bda._role(etf) != "deployed"
    assert bda._role(etf) == "monitored"


def test_pruned_members_are_monitored_not_an_unknown_label():
    """IUIT was pruned 2026-05-23 and used to render as `registered`, which the
    page neither explains nor offers as a filter — so it was unreachable."""
    assert bda._role("IUIT") == "monitored"


def test_every_emitted_role_is_one_the_page_documents():
    emitted = {bda._role(e) for e in ETF_REGISTRY}
    assert emitted <= DOCUMENTED, f"undocumented role(s): {emitted - DOCUMENTED}"


def test_the_page_filter_offers_exactly_the_roles_that_can_be_emitted():
    """A filter option that matches nothing, or a role with no option, both
    leave rows a reader cannot reach."""
    html = TEMPLATE.read_text(encoding="utf-8")
    block = re.search(r'id="data-sum-role".*?</select>', html, re.S)
    assert block, "role filter not found in template.html"
    options = set(re.findall(r'<option value="(\w+)"', block.group(0)))
    emitted = {bda._role(e) for e in ETF_REGISTRY}
    assert options == emitted, (
        f"filter offers {sorted(options)}, data contains {sorted(emitted)}")


def test_the_candidate_supersectors_keep_their_own_label():
    for etf in EUROPE_SUPERSECTORS_CANDIDATE:
        assert bda._role(etf) == "candidate"


def test_the_split_accounts_for_every_registry_panel():
    from collections import Counter
    counts = Counter(bda._role(e) for e in ETF_REGISTRY)
    assert counts["deployed"] == 19
    assert counts["candidate"] == 14
    assert counts["monitored"] == 5
    assert sum(counts.values()) == len(ETF_REGISTRY) == 38
