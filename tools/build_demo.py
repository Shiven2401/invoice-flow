"""Build invoice-flow-demo.html: the real interface in one file, with no server.

Runs the 14 synthetic samples through the real pipeline, then packs the recorded
results, page images, fonts, CSS and JS into a single page that opens with a
double-click. Only samples/synthetic goes in, so the file is safe to share. It
replays those results: reading new documents, logins and storage need the full app.

    python tools/build_demo.py
"""
import base64
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update(RUNTIME_DIR=tempfile.mkdtemp(prefix="invflow_"), STAGE_DELAY="0", EXTRACTOR="rules", LOG_LEVEL="WARNING")

import pymupdf

from app import config, main

OUT = ROOT / "invoice-flow-demo.html"
USER = dict(id=1, username="demo")
DEMO_CSS = """
.drop, .again, #who, .side-folders, .wz-folder, .row-remove, .f-folder, .drawer-folder, a[href^="/api/runs/"],
[data-role="vendor-approve"], [data-role="po-approve"], .dash-tabs { display: none !important; }
.demo-only { display: block; }
"""


def record():
    for sample in main.samples(USER)["samples"]:
        yield sample["file"], main._enqueue(config.SAMPLES_DIR / "synthetic" / sample["file"], sample["file"], USER["id"])


def preview(filename):
    page = pymupdf.open(config.SAMPLES_DIR / "synthetic" / filename)[0]
    jpeg = page.get_pixmap(dpi=100).tobytes("jpeg", jpg_quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


def data_uri(match):
    font = (config.STATIC_DIR / match[1]).read_bytes()
    return f"url(data:font/woff2;base64,{base64.b64encode(font).decode()})"


def swap(text, old, new):
    assert old in text, f"{old!r} not found: the app changed, so update build_demo.py"
    return text.replace(old, new)


def build():
    before = main.pos(USER)
    runs, previews = [], {}
    for filename, rid in record():
        while not main.BUS[rid]["done"]:
            time.sleep(0.02)
        detail = main.run_detail(rid, USER)
        row = next(r for r in main.runs(USER)["runs"] if r["id"] == rid)
        runs.append(dict(detail, codes=row["codes"]))
        previews[rid] = preview(filename)
    data = dict(config=main.cfg(USER), samples=main.samples(USER)["samples"], runs=runs, previews=previews, pos=before["pos"],
                vendors=before["vendors"], workbook=base64.b64encode(main.export_xlsx(USER).body).decode(),
                vendor_workbook=base64.b64encode(main.vendors_xlsx(USER).body).decode())

    css = re.sub(r"url\((fonts/[^)]+)\)", data_uri, (config.STATIC_DIR / "app.css").read_text(encoding="utf-8")) + DEMO_CSS
    js = (config.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    js = swap(js, 'src="/api/runs/${id}/preview"', 'src="${DEMO.preview(id)}"')
    shim = (ROOT / "tools/demo_shim.js").read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")

    html = (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = swap(html, "<title>Invoice Flow</title>", "<title>Invoice Flow demo</title>")
    html = swap(html, '<link rel="stylesheet" href="/static/app.css">', f"<style>{css}</style>")
    html = swap(html, '<script src="/static/theme.js"></script>', f"<script>{(config.STATIC_DIR / 'theme.js').read_text(encoding='utf-8')}</script>")
    html = swap(html, ">Download Excel report<", ">Download sample Excel report<")
    html = swap(html, '<script src="/static/app.js"></script>',
                f'<script type="application/json" id="demo-data">{payload}</script>\n<script>{shim}</script>\n<script>{js}</script>')
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT.name}: {OUT.stat().st_size / 1024 / 1024:.1f} MB, {len(runs)} recorded invoices")


if __name__ == "__main__":
    build()
