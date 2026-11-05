"""Shiven, the guide: served with the page, replayable from the sidebar, and its demo uses samples that exist."""
import json
import re

from fastapi.testclient import TestClient

from app import config, main


def test_the_page_loads_the_guide_and_offers_ask_shiven():
    html = TestClient(main.app).get("/").text
    assert re.search(r'<script src="/static/guide\.js\?v=\d+"></script>', html)    # stamped, so a new version is never cached
    assert 'id="guide-start"' in html and "Ask Shiven" in html


def test_the_demo_uses_real_samples():
    js = (config.STATIC_DIR / "guide.js").read_text(encoding="utf-8")
    preferred = re.search(r"PREFERRED = \[(.*?)\]", js, re.S).group(1)
    names = re.findall(r'"([^"]+\.pdf)"', preferred)
    samples = {m["file"]: m for m in json.loads((config.SAMPLES_DIR / "synthetic" / "manifest.json").read_text())}
    assert names and all(n in samples for n in names)
    assert all(samples[n]["expected"] == "APPROVE" for n in names)   # a first demo ends in a clean approval
