"""Step 1 of the Odoo guide: can Invoice Flow reach Odoo? Prints the user the API key signs in as, then up to 5 confirmed
PO lines with their ordered, received and billed quantities.

Uses Odoo's JSON-2 API (Odoo 19 and later), like app/odoo.py: one POST per call, the API key as a bearer token.
Needs ODOO_URL, ODOO_DB and ODOO_API_KEY in .env.     python test_odoo.py
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
url = os.environ["ODOO_URL"].rstrip("/")
db = os.environ["ODOO_DB"]
key = os.environ["ODOO_API_KEY"]
headers = {"Authorization": f"bearer {key}", "X-Odoo-Database": db}


def call(model, method, **params):
    r = requests.post(f"{url}/json/2/{model}/{method}", json=params, headers=headers, timeout=30)
    if r.status_code != 200:
        sys.exit(f"{model}.{method}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


ctx = call("res.users", "context_get")
print("Login uid:", ctx["uid"])
lines = call("purchase.order.line", "search_read", domain=[["order_id.state", "=", "purchase"]],
             fields=["order_id", "product_id", "product_qty", "qty_received", "qty_invoiced", "price_unit"], limit=5)
for line in lines:
    print(line)
