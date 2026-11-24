"""Event-driven sync: Odoo automation rules that tell Invoice Flow the moment a purchase order, receipt, vendor bill,
vendor or product is created or changed.

    python tools/odoo_webhooks.py setup <site url> [path to .env]    install Automation Rules if needed, create or update the rules
    python tools/odoo_webhooks.py status [path to .env]
    python tools/odoo_webhooks.py remove [path to .env]

Each rule runs a "Send Webhook Notification" action to <site url>/api/odoo/webhook?token=..., where the token is
derived from the API key (app.odoo.webhook_token), so the site and this tool agree without a new secret; rotating the
key means running setup again. Odoo sends the record's model and id; the site then reads the record from Odoo itself.
The site's scheduled pull (every minute, full reconcile every 15) still runs, so a missed webhook is caught anyway.
"""
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
args = sys.argv[1:]
cmd = args[0] if args else "status"
site = args[1].rstrip("/") if cmd == "setup" and len(args) > 1 else None
envfile = args[2] if cmd == "setup" and len(args) > 2 else (args[1] if cmd != "setup" and len(args) > 1 else ROOT / ".env")
load_dotenv(envfile)
from app import odoo  # noqa: E402

URL, DB, KEY = os.environ["ODOO_URL"].rstrip("/"), os.environ["ODOO_DB"], os.environ["ODOO_API_KEY"]
RULES = {  # model -> the records that matter (the same as the mirror's)
    "purchase.order": [],
    "stock.picking": [("picking_type_code", "=", "incoming")],
    "account.move": [("move_type", "in", ["in_invoice", "in_refund"])],
    "res.partner": [("supplier_rank", ">", 0)],
    "product.product": [("purchase_ok", "=", True)],
}
PREFIX = "Invoice Flow sync: "
sys.stdout.reconfigure(encoding="utf-8")


def call(model, method, timeout=60, **params):
    for attempt in range(4):
        try:
            r = requests.post(f"{URL}/json/2/{model}/{method}", json=params, timeout=timeout,
                              headers={"Authorization": f"bearer {KEY}", "X-Odoo-Database": DB})
            break
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(5)
    if r.status_code != 200:
        sys.exit(f"{model}.{method}: HTTP {r.status_code} {r.text[:400]}")
    return r.json()


def ensure_module():
    mod = call("ir.module.module", "search_read", domain=[["name", "=", "base_automation"]], fields=["id", "state"])[0]
    if mod["state"] != "installed":
        print("installing Automation Rules (base_automation) in Odoo...")
        call("ir.module.module", "button_immediate_install", ids=[mod["id"]], timeout=600)
        for _ in range(60):
            if call("ir.module.module", "read", ids=[mod["id"]], fields=["state"])[0]["state"] == "installed":
                break
            time.sleep(5)
    print("Automation Rules: installed")


def existing():
    return call("base.automation", "search_read", domain=[["name", "=like", PREFIX + "%"]], fields=["id", "name", "model_id", "action_server_ids", "active"])


def setup():
    if not site or not site.startswith("https://"):
        sys.exit("give the site's https URL, e.g. https://invoice-flow-....run.app")
    ensure_module()
    hook = f"{site}/api/odoo/webhook?token={odoo.webhook_token()}"
    have = {r["name"]: r for r in existing()}
    for model, domain in RULES.items():
        model_id = call("ir.model", "search_read", domain=[["model", "=", model]], fields=["id"])[0]["id"]
        name = PREFIX + model
        action = dict(name=f"Tell Invoice Flow ({model})", model_id=model_id, state="webhook", webhook_url=hook, usage="base_automation")
        vals = dict(name=name, model_id=model_id, trigger="on_create_or_write", filter_domain=repr([list(d) for d in domain]) if domain else False, active=True)
        if name in have:
            rule = have[name]
            call("base.automation", "write", ids=[rule["id"]], vals=vals)
            if rule["action_server_ids"]:
                call("ir.actions.server", "write", ids=rule["action_server_ids"], vals=dict(webhook_url=hook))
            else:
                call("base.automation", "write", ids=[rule["id"]], vals=dict(action_server_ids=[[0, 0, action]]))
            print(f"updated  {name}")
        else:
            call("base.automation", "create", vals_list=[dict(vals, action_server_ids=[[0, 0, action]])])
            print(f"created  {name}")
    print(f"webhooks go to {site}/api/odoo/webhook?token=***")


def status():
    mod = call("ir.module.module", "search_read", domain=[["name", "=", "base_automation"]], fields=["state"])[0]
    print("Automation Rules:", mod["state"])
    if mod["state"] == "installed":
        for r in existing():
            acts = call("ir.actions.server", "read", ids=r["action_server_ids"], fields=["state", "webhook_url"]) if r["action_server_ids"] else []
            print(f"  {r['name']:<45} active={r['active']}  " + ", ".join(f"{a['state']} -> {a['webhook_url'].split('?')[0]}" for a in acts))


def remove():
    rules = existing()
    if rules:
        call("base.automation", "unlink", ids=[r["id"] for r in rules])
    print(f"removed {len(rules)} rule(s)")


{"setup": setup, "status": status, "remove": remove}[cmd]()
