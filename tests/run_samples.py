"""Run every synthetic scenario through the real pipeline and compare with the manifest.

    python tests/run_samples.py            # table of results
    python -m pytest tests -q              # same thing as assertions

Uses a throw-away runtime dir + database, so it never touches your demo state.
"""
import json
import os
import sys
import tempfile
import uuid
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


TEST_USER = 900


def run_all(extractor="rules", verbose=False):
    os.environ["RUNTIME_DIR"] = tempfile.mkdtemp(prefix="invflow_")
    os.environ["STAGE_DELAY"] = "0"
    os.environ["EXTRACTOR"] = extractor
    from app import config, db, pipeline
    if not config.RUNTIME_DIR.name.startswith("invflow_"):
        raise RuntimeError(f"refusing to wipe {config.RUNTIME_DIR}: app was imported before RUNTIME_DIR was set")
    db.close()
    db.reset(TEST_USER)
    manifest = json.loads((ROOT / "samples/synthetic/manifest.json").read_text())
    rows = []
    for m in manifest:
        path = ROOT / "samples/synthetic" / m["file"]
        rid = uuid.uuid4().hex[:8]
        db.create_run(rid, m["file"], path, TEST_USER)
        events = []
        res = pipeline.run(rid, path, m["file"], lambda *a: events.append(a), extractor)
        codes = {x["code"] for x in res["findings"]}
        rows.append(dict(m=m, res=res, codes=codes, events=events, run_id=rid))
        if verbose:
            print(f"{m['order']:02d} {res['decision']:7s} (want {m['expected']:7s}) {m['file']}")
    return rows


if __name__ == "__main__":
    rows = run_all(verbose=False)
    bad = 0
    for r in rows:
        m, res = r["m"], r["res"]
        ok_dec = res["decision"] == m["expected"]
        missing = [c for c in m["codes"] if c not in r["codes"]]
        ok = ok_dec and not missing
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {m['order']:02d}  {res['decision']:7s} (want {m['expected']:7s})  {m['file']}")
        if not ok or "-v" in sys.argv:
            print("       ", res["headline"][:230])
            for x in res["findings"]:
                if x["severity"] != "info" or "-v" in sys.argv:
                    print(f"        [{x['severity']:6s}] {x['code']}: {x['message'][:170]}")
            if missing:
                print("        expected codes not raised:", missing)
    print(f"\n{len(rows) - bad}/{len(rows)} scenarios behave as designed")
    sys.exit(1 if bad else 0)
