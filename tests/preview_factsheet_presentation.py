"""Read committed releases and render design previews. No ledger or SMTP.

Python datetime months are 1-indexed. Historical views use their sealed clock.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import component_release as cr
from component_factsheet_view import verified_context, render_html, render_pdf


def preview(revision, name, action=None):
    def archive(path):
        return json.loads(subprocess.run(["git","show",f"{revision}:{path}"],cwd=cr.ROOT,
                          check=True,capture_output=True).stdout)
    release=archive(cr.MANIFEST)
    with tempfile.TemporaryDirectory(prefix="factsheet-design-") as temporary:
        root=Path(temporary)
        for path in release["sources"]:
            cr.write(root/path,archive(path))
        cr.write(root/cr.MANIFEST,release)
        release=cr.verify(root,datetime.fromisoformat(release["sealed_at"]))
        release={**release,"presentation":verified_context(root,release),"preview_only":True}
        decision={"action":action or ("regular" if release["d_ready"] else "preview"),
                  "d_hold":not release["d_ready"] and action is None,"revision":"preview"}
        out=cr.ROOT/".component-mail"
        out.mkdir(exist_ok=True)
        for full,suffix in ((False,""),(True,"-book")):
            (out/f"rehearsal-{name}{suffix}.html").write_text(render_html(decision,release,full),encoding="utf-8")
        (out/f"{name}.pdf").write_bytes(render_pdf(decision,release))
        print(f"{name}: {release['identity']}; {release['anchor']}; D ready={release['d_ready']}; NO SEND")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision")
    parser.add_argument("name")
    parser.add_argument("--action",choices=("preview","regular","d_update","revision"),default=None)
    args=parser.parse_args()
    preview(args.revision,args.name,args.action)
