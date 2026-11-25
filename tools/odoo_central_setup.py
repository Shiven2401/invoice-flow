"""Set up the platform view in Odoo (app/central_schema.py): an "Invoice Flow Platform" menu with users, folders,
invoices, purchase orders, vendors, the activity log and a dashboard, for the group "Invoice Flow Platform Admin" only.

    python tools/odoo_central_setup.py plan  [path to .env]     list what would be added or changed (writes nothing)
    python tools/odoo_central_setup.py apply [path to .env]     add or update it (never deletes anything)
    python tools/odoo_central_setup.py check [path to .env]     who is in the group, and that only the group can use the models

Idempotent: run apply again after changing the schema. The API user (whose key the sync uses) is always put in the group.
ODOO_PLATFORM_USERS (Odoo logins, comma-separated) adds the people who should see the platform view.
The key is read from the .env (or the environment) and never printed.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
args = sys.argv[1:]
cmd = args[0] if args else "plan"
load_dotenv(args[1] if len(args) > 1 else ROOT / ".env")
from app import central_schema as S  # noqa: E402
from app import central_setup, odoo  # noqa: E402

if not odoo.enabled():
    sys.exit("Odoo isn't configured (ODOO_URL, ODOO_DB, ODOO_API_KEY).")


def extra_users():
    logins = [x.strip() for x in os.getenv("ODOO_PLATFORM_USERS", "").split(",") if x.strip()]
    if not logins:
        return []
    rows = odoo._call("res.users", "search_read", domain=[["login", "in", logins]], fields=["id", "login"])
    missing = set(logins) - {r["login"] for r in rows}
    if missing:
        print(f"!! no Odoo user with login {sorted(missing)}")
    return [r["id"] for r in rows]


if cmd in ("plan", "apply"):
    for attempt in range(6):     # a network blip midway: run again (every step looks before it adds, so nothing is made twice)
        try:
            lines, model_ids, root = central_setup.setup(dry=cmd == "plan", extra_users=extra_users())
            break
        except odoo.OdooUnreachable as e:
            print(f"Odoo didn't answer ({e}); trying again ({attempt + 1}/6)")
            import time
            time.sleep(10 * (attempt + 1))
    else:
        sys.exit("Odoo kept not answering; run it again later (it carries on where it stopped).")
    print(f"{'Would change' if cmd == 'plan' else 'Changed'}: {len(lines)} item(s)")
    for ln in lines:
        print("  ", ln)
    if cmd == "apply":
        print(f"Menu: {os.environ['ODOO_URL'].rstrip('/')}/odoo (top bar: '{S.ROOT_MENU}', root menu id {root})")
elif cmd == "check":
    g = odoo._call("res.groups", "search_read", domain=[["name", "=", S.GROUP]], fields=["id", "user_ids"])
    if not g:
        sys.exit(f"No group '{S.GROUP}' yet: run apply.")
    users = odoo._call("res.users", "read", ids=g[0]["user_ids"], fields=["name"]) if g[0]["user_ids"] else []
    print(f"Group '{S.GROUP}' (#{g[0]['id']}): {', '.join(u['name'] for u in users) or 'nobody'}")
    ok = True
    for model in S.ORDER:
        m = odoo._call("ir.model", "search_read", domain=[["model", "=", model]], fields=["id"])
        if not m:
            print(f"  {model}: missing")
            ok = False
            continue
        acc = odoo._call("ir.access", "search_read", domain=[["model_id", "=", m[0]["id"]], ["kind", "=", "permission"], ["active", "=", True]],
                         fields=["name", "group_id", "operation"])
        others = [a for a in acc if not a["group_id"] or a["group_id"][0] != g[0]["id"]]
        n = odoo._call(model, "search_count", domain=[])
        print(f"  {model}: {n} record(s); permissions: {[(a['group_id'][1] if a['group_id'] else 'everyone', a['operation']) for a in acc]}")
        ok = ok and not others and acc
    print("OK: only the group can use the platform models." if ok else "!! something other than the group has access, or a model is missing")
else:
    sys.exit(__doc__)
