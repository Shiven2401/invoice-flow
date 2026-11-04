"""Drive the real UI with headless Chromium and save screenshots.

    python run.py &            # in another terminal
    python tests/ui_smoke.py   # screenshots land in tests/shots/

Fails on any console error / uncaught exception, so a broken front end can't hide.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OUT = Path(__file__).resolve().parent / "shots"
OUT.mkdir(exist_ok=True)


def post(path):
    req = urllib.request.Request(BASE + path, method="POST")
    return json.load(urllib.request.urlopen(req))


def get(path):
    return json.load(urllib.request.urlopen(BASE + path))


def wait_all_done(n, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        runs = get("/api/runs")["runs"]
        if len(runs) >= n and all(r["status"] in ("done", "error") for r in runs):
            return runs
        time.sleep(0.5)
    raise TimeoutError("runs did not finish")


def main():
    errors = []
    post("/api/reset")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.goto(BASE)
        pg.wait_for_selector(".scn")
        pg.screenshot(path=OUT / "01_empty.png")

        # one invoice, watched live
        pg.click('.scn[data-file="01_happy_path_clean_exact.pdf"]')
        pg.wait_for_selector('.stage[data-status="running"]', timeout=5000)
        pg.screenshot(path=OUT / "02_running.png")
        pg.wait_for_selector(".stamp", timeout=30000)
        time.sleep(0.8)
        pg.screenshot(path=OUT / "03_happy_done.png", full_page=True)

        # everything, in order
        post("/api/reset")
        pg.reload()
        pg.wait_for_selector(".scn")
        pg.click("#run-all")
        runs = wait_all_done(14, 180)
        time.sleep(1.0)
        pg.screenshot(path=OUT / "04_run_all_end.png", full_page=True)

        # inspect a held invoice
        pg.click('.scn[data-file="06_split_third_invoice_overbilled.pdf"]')
        pg.wait_for_selector(".stamp", timeout=10000)
        time.sleep(0.6)
        pg.click('.stage[data-stage="budget"] button.head')
        pg.screenshot(path=OUT / "05_split_overbilled.png", full_page=True)

        for tab in ("dashboard", "ledger", "rules"):
            pg.click(f'.tabs a[data-tab="{tab}"]')
            time.sleep(0.8)
            pg.screenshot(path=OUT / f"06_{tab}.png", full_page=True)

        pg.click('.tabs a[data-tab="dashboard"]')
        pg.wait_for_selector("tr.click")
        pg.click('tr.click:has-text("13_scanned_missing_date")')
        pg.wait_for_selector("#drawer:not([hidden]) .stamp")
        time.sleep(0.6)
        pg.screenshot(path=OUT / "07_drawer_held.png")
        pg.click("#drawer-close")

        # narrow screen
        pg.set_viewport_size({"width": 390, "height": 844})
        pg.click('.tabs a[data-tab="process"]')
        time.sleep(0.5)
        pg.screenshot(path=OUT / "08_mobile.png", full_page=True)
        b.close()

    dec = {r["filename"]: (r["final_decision"] or r["decision"]) for r in runs}
    print(json.dumps(dec, indent=1))
    if errors:
        print("\nBROWSER ERRORS:")
        print("\n".join(errors))
        sys.exit(1)
    print("\nno browser errors; screenshots in", OUT)


if __name__ == "__main__":
    main()
