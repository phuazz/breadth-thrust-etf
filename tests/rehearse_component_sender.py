"""Generate synthetic sender previews without credentials, SMTP or production writes."""
from pathlib import Path
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from test_component_sender import install, NOW
from component_release import ROOT
from send_component_factsheet import prepare, render


def main():
    out = ROOT / ".component-mail"
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="component-no-send-") as temporary:
        with pytest.MonkeyPatch.context() as patch:
            root = Path(temporary)
            release = install(root, patch, gate=True)
            assert prepare(root, NOW)["action"] == "preview"
            assert not (root / "docs/component_delivery.json").exists()
            for name, action, held in (("initial", "preview", True),
                                      ("consolidated-hold", "regular", True)):
                html = render({"action": action, "d_hold": held}, release)
                (out / f"rehearsal-{name}.html").write_text(html, encoding="utf-8")
            release = install(root, patch, ready=True, gate=True)
            html = render({"action": "regular", "d_hold": False}, release)
            (out / "rehearsal-all-ready.html").write_text(html, encoding="utf-8")
            release = install(root, patch, ready=True)
            html = render({"action": "regular", "d_hold": False}, release, include_unchanged=True)
            (out / "rehearsal-complete-book.html").write_text(html, encoding="utf-8")
    print("NO-SEND REHEARSAL: three synthetic previews generated; no SMTP, no production ledger.")


if __name__ == "__main__":
    main()
