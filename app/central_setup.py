"""Puts the platform view (app/central_schema.py) into an Odoo: the group, custom models and fields, access rights,
views, the dashboard record, actions and menus. Idempotent: run it again and it only adds or updates what changed,
never deletes. Run with tools/odoo_central_setup.py.

Access: only the group "Invoice Flow Platform Admin" may use these models. The API user (whose key the sync uses)
and the person who should see the platform are put in it; nobody else in Odoo sees any of it.
"""
from loguru import logger

from . import central_schema as S
from . import odoo

PREFIX = "invoice_flow"


def _one(model, domain, fields=("id",)):
    rows = odoo._call(model, "search_read", domain=domain, fields=list(fields), limit=1, context={"active_test": False})
    return rows[0] if rows else None


def _create(model, vals):
    return odoo._call(model, "create", vals_list=[vals])[0]


class Plan:
    """What a run did (or, dry, would do): one line per thing added or changed."""

    def __init__(self, dry):
        self.dry, self.lines = dry, []

    def add(self, what):
        self.lines.append(what)
        logger.info("odoo platform setup: {}{}", "(dry) " if self.dry else "", what)


def api_uid():
    return odoo._call("res.users", "context_get")["uid"]


def ensure_group(plan, members):
    g = _one("res.groups", [["name", "=", S.GROUP]], ("id", "user_ids"))
    if not g:
        plan.add(f"group '{S.GROUP}' with users {members}")
        if plan.dry:
            return None
        return _create("res.groups", {"name": S.GROUP, "user_ids": [[4, u] for u in members]})
    missing = [u for u in members if u not in (g["user_ids"] or [])]
    if missing:
        plan.add(f"users {missing} added to '{S.GROUP}'")
        if not plan.dry:
            odoo._call("res.groups", "write", ids=[g["id"]], vals={"user_ids": [[4, u] for u in missing]})
    return g["id"]


def ensure_models(plan):
    ids = {}
    for model in S.ORDER:
        spec = S.MODELS[model]
        m = _one("ir.model", [["model", "=", model]])
        if m:
            ids[model] = m["id"]
            continue
        plan.add(f"model {model} ({spec['name']})")
        if not plan.dry:
            ids[model] = _create("ir.model", {"name": spec["name"], "model": model, "state": "manual", "is_mail_thread": bool(spec["mail"])})
    return ids


def _field_vals(model_id, name, ttype, label, extra):
    vals = {"model_id": model_id, "name": name, "field_description": label, "ttype": ttype, "state": "manual"}
    for k in ("relation", "relation_field", "on_delete", "compute", "store", "readonly"):
        if k in extra:
            vals[k] = extra[k]
    if extra.get("compute"):
        vals.update(store=False, readonly=True, depends=extra.get("depends", ""))
    return vals


def ensure_fields(plan, model_ids):
    """Plain and many2one fields first, then one2many (they need their inverse), then computed ones."""
    stage = lambda t, e: 2 if e.get("compute") else 1 if t == "one2many" else 0
    allf = [(m, *f) for m in S.ORDER for f in S.COMMON + S.MODELS[m]["fields"]]
    for st in (0, 1, 2):
        for model, name, ttype, label, extra in allf:
            if stage(ttype, extra) != st or model not in model_ids:
                continue
            have = _one("ir.model.fields", [["model", "=", model], ["name", "=", name]], ("id", "field_description", "compute", "ttype"))
            vals = _field_vals(model_ids[model], name, ttype, label, extra)
            if not have:
                plan.add(f"field {model}.{name} ({ttype})")
                if not plan.dry:
                    _create("ir.model.fields", vals)
            elif have["ttype"] != ttype:
                plan.add(f"!! {model}.{name} is a {have['ttype']} in Odoo, not {ttype}: left as it is")
            elif have["field_description"] != label or (extra.get("compute") and (have["compute"] or "") != extra["compute"]):
                plan.add(f"field {model}.{name} updated")
                if not plan.dry:
                    odoo._call("ir.model.fields", "write", ids=[have["id"]],
                               vals={k: v for k, v in vals.items() if k in ("field_description", "compute", "depends")})


def ensure_access(plan, model_ids, group_id):
    """One permission per model, for the group only. Any other permission on these models (Odoo may add one for
    administrators when a model is created) is switched off, so nobody outside the group can read them."""
    for model, mid in model_ids.items():
        name = f"{S.GROUP}: {model}"
        if not _one("ir.access", [["name", "=", name], ["model_id", "=", mid]]):
            plan.add(f"access: '{S.GROUP}' may read and sync {model}")
            if not plan.dry:
                _create("ir.access", {"name": name, "model_id": mid, "group_id": group_id, "kind": "permission", "operation": "crud",
                                      "for_read": True, "for_write": True, "for_create": True, "for_unlink": True})
        others = odoo._call("ir.access", "search_read", domain=[["model_id", "=", mid], ["kind", "=", "permission"], ["name", "!=", name],
                                                                 ["active", "=", True]], fields=["id", "name", "group_id"])
        for o in others:
            plan.add(f"access '{o['name']}' on {model} switched off (only '{S.GROUP}' may see it)")
            if not plan.dry:
                odoo._call("ir.access", "write", ids=[o["id"]], vals={"active": False})


def ensure_view(plan, model, vtype, arch):
    name = f"{PREFIX}.{model}.{vtype}"
    v = _one("ir.ui.view", [["name", "=", name]], ("id", "arch"))
    if not v:
        plan.add(f"view {name}")
        return None if plan.dry else _create("ir.ui.view", {"name": name, "model": model, "type": vtype, "arch": arch, "priority": 1})
    if (v.get("arch") or "").replace(" ", "").replace("\n", "") != arch.replace(" ", "").replace("\n", ""):
        plan.add(f"view {name} updated")
        if not plan.dry:
            odoo._call("ir.ui.view", "write", ids=[v["id"]], vals={"arch": arch})
    return v["id"]


def ensure_platform_record(plan):
    r = _one("x_if_platform", [["x_key", "=", "platform"]]) if not plan.dry or _one("ir.model", [["model", "=", "x_if_platform"]]) else None
    if r:
        return r["id"]
    plan.add("dashboard record")
    return None if plan.dry else _create("x_if_platform", {"x_name": "Invoice Flow", "x_key": "platform", "x_active": True})


def ensure_action(plan, key, spec, group_id, extra=None):
    name = f"Invoice Flow: {spec['name']}"
    vals = dict(name=name, res_model=spec["res_model"], view_mode=spec["view_mode"], context=spec.get("context", "{}"),
                group_ids=[[6, 0, [group_id]]] if group_id else [], **(extra or {}))
    a = _one("ir.actions.act_window", [["name", "=", name]], ("id", "view_mode", "context", "res_id", "view_id"))
    if not a:
        plan.add(f"action {name}")
        return None if plan.dry else _create("ir.actions.act_window", vals)
    want = {k: v for k, v in vals.items() if k in ("view_mode", "context", "res_id", "view_id")}
    if any((a.get(k)[0] if isinstance(a.get(k), list) else a.get(k)) != v for k, v in want.items()):
        plan.add(f"action {name} updated")
        if not plan.dry:
            odoo._call("ir.actions.act_window", "write", ids=[a["id"]], vals=vals)
    return a["id"]


def ensure_menus(plan, action_ids, group_id):
    root = _one("ir.ui.menu", [["name", "=", S.ROOT_MENU], ["parent_id", "=", False]])
    if not root:
        plan.add(f"menu '{S.ROOT_MENU}'")
        root_id = None if plan.dry else _create("ir.ui.menu", {"name": S.ROOT_MENU, "sequence": 3, "group_ids": [[6, 0, [group_id]]],
                                                                "action": f"ir.actions.act_window,{action_ids['dashboard']}"})
    else:
        root_id = root["id"]
    for seq, (label, key) in enumerate(S.MENUS, 1):
        if not root_id or not _one("ir.ui.menu", [["name", "=", label], ["parent_id", "=", root_id]]):
            plan.add(f"menu {S.ROOT_MENU} / {label}")
            if not plan.dry and root_id:
                _create("ir.ui.menu", {"name": label, "parent_id": root_id, "sequence": seq, "group_ids": [[6, 0, [group_id]]],
                                       "action": f"ir.actions.act_window,{action_ids[key]}"})
    return root_id


def xmlid(module, name):
    r = _one("ir.model.data", [["module", "=", module], ["name", "=", name]], ("res_id",))
    return r["res_id"] if r else None


def setup(dry=True, extra_users=()):
    """Apply the schema. dry=True only lists what would change. Returns (plan lines, {model: id}, root menu id)."""
    plan = Plan(dry)
    members = [api_uid(), *[u for u in extra_users if u]]
    group_id = ensure_group(plan, sorted(set(members)))
    model_ids = ensure_models(plan)
    if dry and len(model_ids) < len(S.ORDER):
        plan.add(f"... and then {sum(len(S.COMMON) + len(S.MODELS[m]['fields']) for m in S.ORDER)} fields, access, views, actions and menus")
        return plan.lines, model_ids, None
    ensure_fields(plan, model_ids)
    ensure_access(plan, model_ids, group_id)
    for model, views in S.VIEWS.items():
        for vtype, arch in views.items():
            ensure_view(plan, model, vtype, arch)
    platform_id = ensure_platform_record(plan)
    action_ids = {k: ensure_action(plan, k, spec, group_id) for k, spec in S.ACTIONS.items() if k != "dashboard"}
    po_action = xmlid("purchase", "purchase_form_action")
    if po_action:
        action_ids["odoo_pos"] = po_action
    dash_view = ensure_view(plan, "x_if_platform", "form", S.dashboard_form(action_ids))
    action_ids["dashboard"] = ensure_action(plan, "dashboard", S.ACTIONS["dashboard"], group_id,
                                            extra=dict(res_id=platform_id or 0, view_id=dash_view or False))
    root = ensure_menus(plan, action_ids, group_id)
    return plan.lines, model_ids, root
