"""Setting up the platform view in Odoo (app/central_setup.py) against a stand-in Odoo: only the admin group gets
access, other permissions on the models are switched off (never deleted), a second run changes nothing, a dry run
writes nothing, and every view parses as XML."""
import xml.etree.ElementTree as ET

import pytest

from app import central_schema as S
from app import central_setup, odoo


class FakeOdoo:
    def __init__(self):
        self.recs, self.next_id, self.writes = {}, 1, []
        self.add("ir.model.data", module="purchase", name="purchase_form_action", res_id=77)

    def add(self, _model, **vals):
        rid, self.next_id = self.next_id, self.next_id + 1
        self.recs.setdefault(_model, {})[rid] = dict(vals, id=rid)
        return rid

    def _match(self, r, domain):
        for field, op, value in domain:
            v = r.get(field)
            if isinstance(v, list) and field.endswith("_id"):
                v = v[0] if v else False
            if op == "=" and not (v == value or (value is False and not v)):
                return False
            if op == "!=" and v == value:
                return False
            if op == "in" and v not in value:
                return False
        return True

    def __call__(self, model, method, **kw):
        if method == "context_get":
            return {"uid": 2}
        table = self.recs.setdefault(model, {})
        if method == "search_read":
            rows = [dict(r) for r in table.values() if self._match(r, kw["domain"])]
            return rows[: kw.get("limit") or None]
        if method == "create":
            vals = kw["vals_list"][0]
            self.writes.append(("create", model))
            if model == "ir.model":                                   # Odoo adds an admins' permission to a new model
                mid = self.add(model, **vals)
                self.add("ir.access", name=f"{vals['model']} admins", model_id=mid, group_id=5, kind="permission", active=True)
                return [mid]
            if model == "res.groups":
                vals = dict(vals, user_ids=[c[1] for c in vals.get("user_ids", [])])
            if model == "ir.model.fields":
                vals = dict(vals, model=next(m["model"] for m in self.recs["ir.model"].values() if m["id"] == vals["model_id"]))
            return [self.add(model, **vals)]
        if method == "write":
            self.writes.append(("write", model))
            for rid in kw["ids"]:
                table[rid].update(kw["vals"])
            return True
        if method == "unlink":
            raise AssertionError("setup must never delete anything in Odoo")
        raise AssertionError(f"unexpected {model}.{method}")


@pytest.fixture
def fake(monkeypatch):
    f = FakeOdoo()
    monkeypatch.setattr(odoo, "_call", f)
    return f


def test_setup_builds_everything_for_the_admin_group_only(fake):
    lines, model_ids, root = central_setup.setup(dry=False)
    assert set(model_ids) == set(S.ORDER) and root
    group = next(g for g in fake.recs["res.groups"].values() if g["name"] == S.GROUP)
    assert 2 in group["user_ids"], "the API user is in the group (the sync writes as it)"
    access = list(fake.recs["ir.access"].values())
    for model, mid in model_ids.items():
        live = [a for a in access if a["model_id"] == mid and a.get("active", True)]
        assert len(live) == 1 and live[0]["group_id"] == group["id"], f"{model}: only the admin group may use it"
        assert any(a["model_id"] == mid and a.get("active") is False for a in access), "Odoo's own permission switched off, not deleted"
    menus = list(fake.recs["ir.ui.menu"].values())
    assert len(menus) == 1 + len(S.MENUS) and all(m["group_ids"] == [[6, 0, [group["id"]]]] for m in menus)
    actions = list(fake.recs["ir.actions.act_window"].values())
    assert all(a["group_ids"] == [[6, 0, [group["id"]]]] for a in actions)
    dash = next(a for a in actions if a["res_model"] == "x_if_platform")
    assert dash["res_id"] and dash["view_id"], "the dashboard opens the platform record in its own form"
    fields = {(f["model"], f["name"]) for f in fake.recs["ir.model.fields"].values()}
    assert ("x_if_invoice", "x_user_id") in fields and ("x_if_invoice", "x_folder_id") in fields and ("x_if_invoice", "x_document") in fields
    assert all((m, "x_key") in fields and (m, "x_active") in fields for m in S.ORDER)
    for v in fake.recs["ir.ui.view"].values():
        ET.fromstring(v["arch"])                                  # every view is well-formed XML
    assert 'name="77"' in next(v["arch"] for v in fake.recs["ir.ui.view"].values() if v["name"] == "invoice_flow.x_if_platform.form"), \
        "the dashboard links to Odoo's own purchase orders"


def test_a_second_run_changes_nothing_and_a_dry_run_writes_nothing(fake):
    central_setup.setup(dry=False)
    n = len(fake.writes)
    lines, _ids, _root = central_setup.setup(dry=False)
    assert len(fake.writes) == n and lines == [], lines
    fresh = FakeOdoo()
    odoo._call = fresh
    lines, _ids, _root = central_setup.setup(dry=True)
    assert fresh.writes == [] and lines


def test_computed_dashboard_code_compiles():
    for model in S.ORDER:
        for name, _t, _label, extra in S.MODELS[model]["fields"]:
            if extra.get("compute"):
                compile(extra["compute"], f"{model}.{name}", "exec")
