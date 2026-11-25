"""What the platform view looks like inside Odoo: the custom models (users, folders, vendors, purchase orders, invoices,
activity and one dashboard record), their views, menus and the one group allowed to see them.

Invoice Flow stays the source of truth for its own data: who has an account, who owns which invoice and folder, who
may see what. Odoo gets a read-only copy of all of it for the platform administrator (app/central_sync.py). Nothing
here creates accounting entries: invoices are records of their own; only accounts connected to Odoo get bills, as before.

Every model starts with x_ (Odoo's prefix for models made outside a module, so this works on Odoo Online without
installing anything). app/central_setup.py applies this file to an Odoo, idempotently.
"""

GROUP = "Invoice Flow Platform Admin"

# common to every synced model: the record's key in Invoice Flow (one Odoo record per key, so nothing is made twice),
# which server it came from, and when it was last synced. x_active is Odoo's archive flag for custom models: a record
# deleted in Invoice Flow is archived in Odoo (its history stays), never deleted.
COMMON = [
    ("x_key", "char", "Invoice Flow key", {}),
    ("x_site", "char", "Invoice Flow server", {}),
    ("x_last_sync", "datetime", "Last synced", {}),
    ("x_deleted_in_app", "boolean", "Deleted in Invoice Flow", {}),
    ("x_active", "boolean", "Active", {}),
]


def _count(model, field, extra=""):
    return (f"M = self.env['{model}'].with_context(active_test=True)\nfor rec in self:\n"
            f"    rec['{{f}}'] = M.search_count([('{field}', '=', rec.id){extra}])")


def _c(code):
    return dict(compute=code, store=False, readonly=True)


USER_COUNTS = {
    "x_invoice_count": ("Invoices", "x_if_invoice", ""),
    "x_folder_count": ("Folders", "x_if_folder", ""),
    "x_po_count": ("Purchase orders", "x_if_po", ""),
    "x_pending_count": ("Pending invoices", "x_if_invoice", ", ('x_approval', 'in', ['Pending', 'On hold'])"),
    "x_failed_count": ("Failed processing", "x_if_invoice", ", ('x_processing_status', '=', 'Failed')"),
    "x_approved_count": ("Approved invoices", "x_if_invoice", ", ('x_approval', '=', 'Approved')"),
    "x_rejected_count": ("Rejected invoices", "x_if_invoice", ", ('x_approval', '=', 'Rejected')"),
}

MODELS = {
    "x_if_user": dict(name="Invoice Flow User", mail=False, fields=[
        ("x_name", "char", "Username", {}),
        ("x_app_user_id", "integer", "Invoice Flow user ID", {}),
        ("x_email", "char", "Email", {}),
        ("x_role", "char", "Role", {}),
        ("x_login_method", "char", "Signs in with", {}),
        ("x_created_at", "datetime", "Registered", {}),
        ("x_last_login", "datetime", "Last login", {}),
        ("x_odoo_connected", "boolean", "Connected to Odoo (bills in Odoo)", {}),
        ("x_folder_ids", "one2many", "Folders", dict(relation="x_if_folder", relation_field="x_user_id")),
        ("x_invoice_ids", "one2many", "Invoices", dict(relation="x_if_invoice", relation_field="x_user_id")),
        ("x_po_ids", "one2many", "Purchase orders", dict(relation="x_if_po", relation_field="x_user_id")),
        ("x_vendor_ids", "one2many", "Vendors", dict(relation="x_if_vendor", relation_field="x_user_id")),
        ("x_activity_ids", "one2many", "Activity", dict(relation="x_if_activity", relation_field="x_user_id")),
    ] + [(f, "integer", label, _c(_count(m, "x_user_id", extra).replace("{f}", f))) for f, (label, m, extra) in USER_COUNTS.items()] + [
        ("x_invoice_value", "float", "Invoice value", _c(
            "M = self.env['x_if_invoice'].with_context(active_test=True)\nfor rec in self:\n"
            "    rec['x_invoice_value'] = sum(M.search([('x_user_id', '=', rec.id)]).mapped('x_total'))")),
    ]),
    "x_if_folder": dict(name="Invoice Flow Folder", mail=False, fields=[
        ("x_name", "char", "Folder", {}),
        ("x_app_folder_id", "integer", "Invoice Flow folder ID", {}),
        ("x_user_id", "many2one", "Owner", dict(relation="x_if_user", on_delete="set null")),
        ("x_owner_name", "char", "Owner username", {}),
        ("x_created_at", "datetime", "Created", {}),
        ("x_invoice_ids", "one2many", "Invoices", dict(relation="x_if_invoice", relation_field="x_folder_id")),
        ("x_invoice_count", "integer", "Invoices", _c(_count("x_if_invoice", "x_folder_id").replace("{f}", "x_invoice_count"))),
    ]),
    "x_if_vendor": dict(name="Invoice Flow Vendor", mail=False, fields=[
        ("x_name", "char", "Vendor", {}),
        ("x_vendor_ref", "char", "Invoice Flow vendor ID", {}),
        ("x_user_id", "many2one", "Approved by account", dict(relation="x_if_user", on_delete="set null")),
        ("x_owner_name", "char", "Owner username", {}),
        ("x_gstin", "char", "GSTIN", {}),
        ("x_status", "char", "Status", {}),
        ("x_approved_at", "datetime", "Approved", {}),
        ("x_aliases", "char", "Other names", {}),
        ("x_note", "text", "Note", {}),
        ("x_partner_id", "many2one", "Odoo vendor", dict(relation="res.partner", on_delete="set null")),
        ("x_link_status", "char", "Odoo link", {}),
    ]),
    "x_if_po": dict(name="Invoice Flow Purchase Order", mail=False, fields=[
        ("x_name", "char", "PO number", {}),
        ("x_user_id", "many2one", "Owner", dict(relation="x_if_user", on_delete="set null")),
        ("x_owner_name", "char", "Owner username", {}),
        ("x_source", "char", "Kept in", {}),
        ("x_vendor_ref", "char", "Invoice Flow vendor ID", {}),
        ("x_vendor_name", "char", "Vendor", {}),
        ("x_po_date", "date", "PO date", {}),
        ("x_description", "char", "Description", {}),
        ("x_net", "float", "Net value", {}),
        ("x_gst_rate", "float", "GST %", {}),
        ("x_total", "float", "Total value", {}),
        ("x_status", "char", "Status", {}),
        ("x_receipt_status", "char", "Goods receipt", {}),
        ("x_billed", "float", "Billed so far (net)", {}),
        ("x_remaining", "float", "Remaining (net)", {}),
        ("x_po_id", "many2one", "Odoo purchase order", dict(relation="purchase.order", on_delete="set null")),
        ("x_created_at", "datetime", "Created", {}),
        ("x_invoice_count", "integer", "Invoices", _c(
            "M = self.env['x_if_invoice'].with_context(active_test=True)\nfor rec in self:\n"
            "    rec['x_invoice_count'] = M.search_count([('x_po_number', '=', rec.x_name), ('x_user_id', '=', rec.x_user_id.id)])")),
    ]),
    "x_if_invoice": dict(name="Invoice Flow Invoice", mail=True, fields=[
        ("x_name", "char", "Invoice", {}),
        ("x_run_id", "char", "Invoice Flow invoice ID", {}),
        ("x_user_id", "many2one", "Owner", dict(relation="x_if_user", on_delete="set null")),
        ("x_owner_name", "char", "Owner username", {}),
        ("x_owner_email", "char", "Owner email", {}),
        ("x_app_user_id", "integer", "Invoice Flow user ID", {}),
        ("x_folder_id", "many2one", "Folder", dict(relation="x_if_folder", on_delete="set null")),
        ("x_folder_name", "char", "Folder name", {}),
        ("x_app_folder_id", "integer", "Invoice Flow folder ID", {}),
        ("x_filename", "char", "File", {}),
        ("x_document", "binary", "Original document", {}),
        ("x_uploaded_at", "datetime", "Uploaded", {}),
        ("x_decided_at", "datetime", "Processed / decided", {}),
        ("x_processing_status", "char", "Processing status", {}),
        ("x_approval", "char", "Approval status", {}),
        ("x_auto_decision", "char", "Decision by the checks", {}),
        ("x_decided_by", "char", "Decided by", {}),
        ("x_reason", "text", "Reason", {}),
        ("x_match", "char", "Matching result", {}),
        ("x_headline", "text", "Summary", {}),
        ("x_codes", "char", "Findings", {}),
        ("x_findings", "text", "Findings (detail)", {}),
        ("x_extracted", "text", "Extracted fields", {}),
        ("x_extractor", "char", "Read by", {}),
        ("x_vendor_name", "char", "Vendor", {}),
        ("x_vendor_gstin", "char", "Vendor GSTIN", {}),
        ("x_vendor_ref", "char", "Invoice Flow vendor ID", {}),
        ("x_partner_id", "many2one", "Odoo vendor", dict(relation="res.partner", on_delete="set null")),
        ("x_invoice_number", "char", "Invoice number", {}),
        ("x_invoice_date", "date", "Invoice date", {}),
        ("x_due_date", "date", "Due date", {}),
        ("x_po_number", "char", "PO number", {}),
        ("x_po_id", "many2one", "Odoo purchase order", dict(relation="purchase.order", on_delete="set null")),
        ("x_net", "float", "Net", {}),
        ("x_tax", "float", "Tax", {}),
        ("x_total", "float", "Total", {}),
        ("x_duplicate", "boolean", "Duplicate alert", {}),
        ("x_bill_id", "many2one", "Odoo bill", dict(relation="account.move", on_delete="set null")),
        ("x_bill_state", "char", "Bill status", {}),
        ("x_payment_state", "char", "Payment status", {}),
    ]),
    "x_if_activity": dict(name="Invoice Flow Activity", mail=False, fields=[
        ("x_name", "char", "Activity", {}),
        ("x_user_id", "many2one", "User", dict(relation="x_if_user", on_delete="set null")),
        ("x_username", "char", "Username", {}),
        ("x_session_id", "char", "Login session", {}),
        ("x_invoice_id", "many2one", "Invoice", dict(relation="x_if_invoice", on_delete="set null")),
        ("x_run_id", "char", "Invoice Flow invoice ID", {}),
        ("x_filename", "char", "File", {}),
        ("x_invoice_number", "char", "Invoice number", {}),
        ("x_vendor", "char", "Vendor", {}),
        ("x_decision", "char", "Decision", {}),
        ("x_decided_by", "char", "Decided by", {}),
        ("x_reason", "text", "Reason", {}),
        ("x_uploaded_at", "datetime", "Uploaded", {}),
        ("x_decided_at", "datetime", "Decided", {}),
        ("x_bill_ref", "integer", "Odoo bill ID", {}),
        ("x_sync_status", "char", "Sync status", {}),
    ]),
}

# the dashboard: one record. The sync figures are sent by Invoice Flow; everything else is counted by Odoo itself from
# the synced records and its own bills and POs each time the page opens, so the numbers are never stale or made up
_INV = "self.env['x_if_invoice'].with_context(active_test=True)"
_BILLS = ("self.env['account.move'].search([('move_type', 'in', ['in_invoice', 'in_refund']), ('state', '!=', 'cancel')])")
_SIGN = "(-1 if m.move_type == 'in_refund' else 1)"
_PO = "self.env['purchase.order']"
DASH = [
    # users
    ("x_users_total", "integer", "Registered users", f"self.env['x_if_user'].with_context(active_test=True).search_count([])"),
    ("x_users_active", "integer", "Active users (logged in, last 30 days)",
     "self.env['x_if_user'].with_context(active_test=True).search_count([('x_last_login', '>=', datetime.datetime.now() - datetime.timedelta(days=30))])"),
    ("x_folders_total", "integer", "Folders", "self.env['x_if_folder'].with_context(active_test=True).search_count([])"),
    # invoices
    ("x_inv_total", "integer", "Invoices (all users)", f"{_INV}.search_count([])"),
    ("x_inv_processed", "integer", "Processed", f"{_INV}.search_count([('x_processing_status', '=', 'Processed')])"),
    ("x_inv_pending", "integer", "Pending (processing or on hold)", f"{_INV}.search_count([('x_approval', 'in', ['Pending', 'On hold'])])"),
    ("x_inv_approved", "integer", "Approved", f"{_INV}.search_count([('x_approval', '=', 'Approved')])"),
    ("x_inv_held", "integer", "On hold", f"{_INV}.search_count([('x_approval', '=', 'On hold')])"),
    ("x_inv_rejected", "integer", "Rejected", f"{_INV}.search_count([('x_approval', '=', 'Rejected')])"),
    ("x_inv_matched", "integer", "Matched (3-way)", f"{_INV}.search_count([('x_match', '=', 'Matched')])"),
    ("x_inv_unmatched", "integer", "Not matched", f"{_INV}.search_count([('x_match', 'in', ['Mismatched', 'PO not found', 'Partly matched'])])"),
    ("x_inv_failed", "integer", "Failed processing", f"{_INV}.search_count([('x_processing_status', '=', 'Failed')])"),
    ("x_inv_duplicates", "integer", "Duplicate alerts", f"{_INV}.search_count([('x_duplicate', '=', True)])"),
    ("x_bills_draft", "integer", "Draft vendor bills", "self.env['account.move'].search_count([('move_type', 'in', ['in_invoice', 'in_refund']), ('state', '=', 'draft')])"),
    ("x_bills_posted", "integer", "Posted vendor bills", "self.env['account.move'].search_count([('move_type', 'in', ['in_invoice', 'in_refund']), ('state', '=', 'posted')])"),
    # procurement (Odoo's POs plus the POs accounts keep in Invoice Flow's own register)
    ("x_po_total", "integer", "Purchase orders (Odoo)", f"{_PO}.search_count([])"),
    ("x_po_app", "integer", "Purchase orders (Invoice Flow registers)", "self.env['x_if_po'].with_context(active_test=True).search_count([('x_po_id', '=', False)])"),
    ("x_po_pending", "integer", "Pending approval", f"{_PO}.search_count([('state', 'in', ['draft', 'sent', 'to approve'])])"),
    ("x_po_approved", "integer", "Approved", f"{_PO}.search_count([('date_approve', '!=', False)])"),
    ("x_po_confirmed", "integer", "Confirmed", f"{_PO}.search_count([('state', 'in', ['purchase', 'done'])])"),
    ("x_po_partial", "integer", "Partially received", f"{_PO}.search_count([('state', '!=', 'cancel'), ('receipt_status', '=', 'partial')])"),
    ("x_po_full", "integer", "Fully received", f"{_PO}.search_count([('state', '!=', 'cancel'), ('receipt_status', '=', 'full')])"),
    ("x_po_cancelled", "integer", "Cancelled", f"{_PO}.search_count([('state', '=', 'cancel')])"),
    # money
    ("x_val_invoices", "float", "Invoice value (all users)", f"sum({_INV}.search([]).mapped('x_total'))"),
    ("x_val_approved", "float", "Approved invoice value", f"sum({_INV}.search([('x_approval', '=', 'Approved')]).mapped('x_total'))"),
    ("x_val_po", "float", "PO value", f"sum({_PO}.search([('state', '!=', 'cancel')]).mapped('amount_total')) + "
                                      "sum(self.env['x_if_po'].with_context(active_test=True).search([('x_po_id', '=', False)]).mapped('x_total'))"),
    ("x_val_outstanding", "float", "Outstanding vendor payments (posted, unpaid)",
     f"sum({_SIGN} * m.amount_residual for m in {_BILLS} if m.state == 'posted')"),
    ("x_val_paid", "float", "Paid", f"sum({_SIGN} * (m.amount_total - m.amount_residual) for m in {_BILLS} if m.state == 'posted')"),
    ("x_val_pending", "float", "Pending payment (draft bills + posted unpaid)",
     f"sum({_SIGN} * (m.amount_total if m.state == 'draft' else m.amount_residual) for m in {_BILLS})"),
]
SYNC_FIELDS = [   # sent by Invoice Flow at the end of every sync round
    ("x_sync_ok", "integer", "Records in sync", {}),
    ("x_sync_failed", "integer", "Records that failed to sync", {}),
    ("x_sync_pending", "integer", "Records waiting to sync", {}),
    ("x_queue_pending", "integer", "ERP writes waiting (bills, POs, vendors)", {}),
    ("x_queue_failed", "integer", "ERP writes failed", {}),
    ("x_queue_done", "integer", "ERP writes done", {}),
    ("x_api_failures", "integer", "Odoo API failures (last 24 h)", {}),
    ("x_last_sync_ok", "datetime", "Last successful sync", {}),
    ("x_last_error", "text", "Last sync error", {}),
    ("x_processing_errors", "integer", "Invoice processing errors (Invoice Flow)", {}),
]
MODELS["x_if_platform"] = dict(name="Invoice Flow Platform", mail=False, fields=[("x_name", "char", "Platform", {})] + SYNC_FIELDS + [
    (f, t, label, _c(f"for rec in self:\n    rec['{f}'] = {expr}")) for f, t, label, expr in DASH])

ORDER = ["x_if_user", "x_if_folder", "x_if_vendor", "x_if_po", "x_if_invoice", "x_if_activity", "x_if_platform"]


# ------------------------------------------------------------------------------------------------- views --

def _list(model, cols, extra=""):
    opt = ' optional="show"'
    body = "".join(f'<field name="{c}"{opt if i > 5 else ""}/>' for i, c in enumerate(cols))
    return f'<list create="false" edit="false" delete="false"{extra}>{body}</list>'


def _form(title_field, groups, notebook="", chatter=False):
    """groups: [(label, [fields])]. Read-only: Invoice Flow owns these records and would overwrite edits."""
    cols = "".join(f'<group string="{label}">' + "".join(f'<field name="{f}"/>' for f in fs) + "</group>" for label, fs in groups)
    return (f'<form create="false" edit="false" delete="false"><sheet><div class="oe_title"><h1><field name="{title_field}" readonly="1"/></h1></div>'
            f'<group>{cols}</group>{notebook}</sheet>{"<chatter/>" if chatter else ""}</form>')


def _search(fields, filters=(), groupbys=()):
    f = "".join(f'<field name="{x}"/>' for x in fields)
    fl = "".join(f'<filter name="{n}" string="{s}" domain="{d}"/>' for n, s, d in filters)
    gb = "".join(f'<filter name="by_{g}" string="{s}" context="{{\'group_by\': \'{g}\'}}"/>' for g, s in groupbys)
    return f'<search>{f}<separator/>{fl}<group>{gb}</group></search>'   # Odoo 20: no string on a search view's group


INVOICE_FILTERS = [
    ("approved", "Approved", "[('x_approval', '=', 'Approved')]"),
    ("held", "On hold", "[('x_approval', '=', 'On hold')]"),
    ("rejected", "Rejected", "[('x_approval', '=', 'Rejected')]"),
    ("pending", "Pending", "[('x_approval', '=', 'Pending')]"),
    ("failed", "Failed processing", "[('x_processing_status', '=', 'Failed')]"),
    ("matched", "Matched", "[('x_match', '=', 'Matched')]"),
    ("unmatched", "Not matched", "[('x_match', 'in', ['Mismatched', 'PO not found', 'Partly matched'])]"),
    ("dupes", "Duplicate alerts", "[('x_duplicate', '=', True)]"),
    ("billed", "Has an Odoo bill", "[('x_bill_id', '!=', False)]"),
    ("paid", "Paid", "[('x_payment_state', 'in', ['paid', 'in_payment'])]"),
    ("archived", "Deleted in Invoice Flow", "[('x_active', '=', False)]"),
]
INVOICE_GROUPS = [("x_user_id", "User"), ("x_folder_id", "Folder"), ("x_vendor_name", "Vendor"), ("x_approval", "Approval status"),
                  ("x_processing_status", "Processing status"), ("x_match", "Matching result"), ("x_payment_state", "Payment status"),
                  ("x_po_number", "PO number"), ("x_invoice_date:month", "Invoice month"), ("x_uploaded_at:month", "Upload month")]

VIEWS = {
    "x_if_user": dict(
        list=_list("x_if_user", ["x_name", "x_email", "x_role", "x_created_at", "x_last_login", "x_invoice_count", "x_folder_count",
                                 "x_po_count", "x_pending_count", "x_failed_count", "x_approved_count", "x_rejected_count",
                                 "x_invoice_value", "x_odoo_connected", "x_app_user_id"]),
        form=_form("x_name", [("Account", ["x_app_user_id", "x_email", "x_role", "x_login_method", "x_odoo_connected"]),
                              ("Activity", ["x_created_at", "x_last_login", "x_last_sync"]),
                              ("Invoices", ["x_invoice_count", "x_pending_count", "x_approved_count", "x_rejected_count", "x_failed_count", "x_invoice_value"]),
                              ("Other", ["x_folder_count", "x_po_count", "x_key", "x_site"])],
                   notebook='<notebook><page string="Folders"><field name="x_folder_ids"/></page>'
                            '<page string="Invoices"><field name="x_invoice_ids"/></page>'
                            '<page string="Purchase orders"><field name="x_po_ids"/></page>'
                            '<page string="Vendors"><field name="x_vendor_ids"/></page>'
                            '<page string="Activity history"><field name="x_activity_ids"/></page></notebook>'),
        search=_search(["x_name", "x_email", "x_app_user_id"], [("connected", "Connected to Odoo", "[('x_odoo_connected', '=', True)]"),
                                                                ("admins", "Administrators", "[('x_role', '=', 'Platform admin')]")],
                       [("x_role", "Role")])),
    "x_if_folder": dict(
        list=_list("x_if_folder", ["x_user_id", "x_name", "x_invoice_count", "x_created_at", "x_app_folder_id"]),
        form=_form("x_name", [("Folder", ["x_user_id", "x_owner_name", "x_app_folder_id", "x_created_at"]), ("Sync", ["x_key", "x_last_sync"])],
                   notebook='<notebook><page string="Invoices"><field name="x_invoice_ids"/></page></notebook>'),
        search=_search(["x_name", "x_user_id", "x_owner_name"], [], [("x_user_id", "User")])),
    "x_if_vendor": dict(
        list=_list("x_if_vendor", ["x_name", "x_gstin", "x_status", "x_user_id", "x_partner_id", "x_link_status", "x_vendor_ref", "x_approved_at"]),
        form=_form("x_name", [("Vendor", ["x_vendor_ref", "x_gstin", "x_status", "x_aliases", "x_note"]),
                              ("Owner and Odoo", ["x_user_id", "x_approved_at", "x_partner_id", "x_link_status", "x_last_sync"])]),
        search=_search(["x_name", "x_gstin", "x_user_id", "x_vendor_ref"], [("blocked", "Blocked", "[('x_status', 'ilike', 'block')]")],
                       [("x_user_id", "User"), ("x_status", "Status")])),
    "x_if_po": dict(
        list=_list("x_if_po", ["x_name", "x_user_id", "x_vendor_name", "x_po_date", "x_status", "x_total", "x_receipt_status", "x_billed",
                               "x_remaining", "x_source", "x_po_id", "x_invoice_count"]),
        form=_form("x_name", [("Order", ["x_vendor_name", "x_vendor_ref", "x_po_date", "x_description", "x_status", "x_receipt_status"]),
                              ("Value", ["x_net", "x_gst_rate", "x_total", "x_billed", "x_remaining", "x_invoice_count"]),
                              ("Owner", ["x_user_id", "x_source", "x_po_id", "x_created_at", "x_last_sync"])]),
        search=_search(["x_name", "x_user_id", "x_vendor_name"], [("open", "Open", "[('x_status', 'in', ['open', 'Confirmed'])]"),
                                                                  ("in_odoo", "In Odoo", "[('x_po_id', '!=', False)]")],
                       [("x_user_id", "User"), ("x_vendor_name", "Vendor"), ("x_status", "Status"), ("x_source", "Kept in")])),
    "x_if_invoice": dict(
        list=_list("x_if_invoice", ["x_name", "x_user_id", "x_folder_id", "x_vendor_name", "x_invoice_date", "x_total", "x_approval",
                                    "x_processing_status", "x_match", "x_po_number", "x_bill_state", "x_payment_state", "x_uploaded_at",
                                    "x_decided_by", "x_owner_email", "x_run_id", "x_last_sync"],
                   ' decoration-success="x_approval == \'Approved\'" decoration-warning="x_approval == \'On hold\'" decoration-danger="x_approval == \'Rejected\'"'),
        form=_form("x_name", [("Invoice", ["x_invoice_number", "x_invoice_date", "x_due_date", "x_vendor_name", "x_vendor_gstin", "x_partner_id",
                                           "x_net", "x_tax", "x_total"]),
                              ("Decision", ["x_approval", "x_auto_decision", "x_decided_by", "x_decided_at", "x_reason", "x_match", "x_duplicate"]),
                              ("Owner and folder", ["x_user_id", "x_owner_name", "x_owner_email", "x_app_user_id", "x_folder_id", "x_folder_name",
                                                    "x_run_id", "x_uploaded_at", "x_processing_status"]),
                              ("Purchase order and bill", ["x_po_number", "x_po_id", "x_bill_id", "x_bill_state", "x_payment_state", "x_last_sync"])],
                   notebook='<notebook><page string="Document"><field name="x_filename" invisible="1"/>'
                            '<field name="x_document" filename="x_filename" widget="binary"/></page>'
                            '<page string="Checks"><field name="x_headline"/><field name="x_codes"/><field name="x_findings"/></page>'
                            '<page string="Extracted fields"><field name="x_extractor"/><field name="x_extracted"/></page></notebook>',
                   chatter=True),
        search=_search(["x_name", "x_invoice_number", "x_user_id", "x_owner_name", "x_owner_email", "x_folder_id", "x_vendor_name",
                        "x_po_number", "x_run_id", "x_invoice_date", "x_uploaded_at"], INVOICE_FILTERS, INVOICE_GROUPS),
        pivot='<pivot string="Invoices"><field name="x_user_id" type="row"/><field name="x_approval" type="col"/>'
              '<field name="x_total" type="measure"/></pivot>',
        graph='<graph string="Invoices" type="bar"><field name="x_user_id"/><field name="x_approval"/></graph>'),
    "x_if_activity": dict(
        list=_list("x_if_activity", ["x_decided_at", "x_username", "x_session_id", "x_filename", "x_invoice_number", "x_vendor", "x_decision",
                                     "x_decided_by", "x_reason", "x_uploaded_at", "x_bill_ref", "x_sync_status", "x_invoice_id"]),
        form=_form("x_name", [("Who", ["x_user_id", "x_username", "x_session_id"]),
                              ("What", ["x_invoice_id", "x_run_id", "x_filename", "x_invoice_number", "x_vendor", "x_decision", "x_decided_by", "x_reason"]),
                              ("When", ["x_uploaded_at", "x_decided_at", "x_bill_ref", "x_sync_status"])]),
        search=_search(["x_username", "x_session_id", "x_filename", "x_invoice_number", "x_vendor"], [],
                       [("x_user_id", "User"), ("x_session_id", "Session"), ("x_decision", "Decision")])),
}


def dashboard_form(actions):
    """The dashboard: the platform record's figures in sections, each with a button to the records behind it.
    actions: {key: Odoo action id} made by central_setup."""
    def grp(title, fields, buttons=()):
        b = "".join(f'<button type="action" name="{actions[k]}" string="{s}" class="btn-link"/>' for k, s in buttons if k in actions)
        return (f'<group string="{title}">' + "".join(f'<field name="{f}"/>' for f in fields) + (f'<div colspan="2">{b}</div>' if b else "") + "</group>")
    d = {f: label for f, _t, label, _e in DASH}
    sections = [
        grp("Users", ["x_users_total", "x_users_active", "x_folders_total"], [("users", "Users"), ("folders", "Folders")]),
        grp("Invoices", ["x_inv_total", "x_inv_processed", "x_inv_pending", "x_inv_approved", "x_inv_held", "x_inv_rejected", "x_inv_matched",
                         "x_inv_unmatched", "x_bills_draft", "x_bills_posted"], [("invoices", "All invoices"), ("by_folder", "By user and folder")]),
        grp("Procurement", ["x_po_total", "x_po_app", "x_po_pending", "x_po_approved", "x_po_confirmed", "x_po_partial", "x_po_full", "x_po_cancelled"],
            [("pos", "Invoice Flow POs"), ("odoo_pos", "Odoo POs")]),
        grp("Money", ["x_val_invoices", "x_val_approved", "x_val_po", "x_val_outstanding", "x_val_paid", "x_val_pending"],
            [("by_vendor", "By vendor"), ("vendors", "Vendors")]),
        grp("Processing and sync", ["x_inv_failed", "x_inv_duplicates", "x_processing_errors", "x_sync_ok", "x_sync_pending", "x_sync_failed",
                                    "x_queue_pending", "x_queue_failed", "x_queue_done", "x_api_failures", "x_last_sync_ok", "x_last_error"],
            [("activity", "Activity log")]),
    ]
    return ('<form create="false" edit="false" delete="false"><sheet><div class="oe_title"><h1>Invoice Flow: whole platform</h1>'
            '<p>Counted by Odoo from the synced records each time this page opens. Invoice Flow owns these records; '
            'they are read-only here.</p></div>' + "".join(sections) + "</sheet></form>")


# window actions and menus (menu label, action key); every one is limited to GROUP
ACTIONS = {
    "dashboard": dict(name="Platform dashboard", res_model="x_if_platform", view_mode="form"),
    "users": dict(name="Users", res_model="x_if_user", view_mode="list,form"),
    "folders": dict(name="Folders", res_model="x_if_folder", view_mode="list,form", context="{'group_by': ['x_user_id']}"),
    "invoices": dict(name="Invoices", res_model="x_if_invoice", view_mode="list,form,pivot,graph"),
    "by_folder": dict(name="Invoices by user and folder", res_model="x_if_invoice", view_mode="list,form,pivot",
                      context="{'group_by': ['x_user_id', 'x_folder_id']}"),
    "by_vendor": dict(name="Invoices by vendor", res_model="x_if_invoice", view_mode="pivot,list,graph,form",
                      context="{'pivot_row_groupby': ['x_vendor_name'], 'pivot_column_groupby': ['x_approval'], 'pivot_measures': ['x_total', '__count']}"),
    "pos": dict(name="Purchase orders", res_model="x_if_po", view_mode="list,form"),
    "vendors": dict(name="Vendors", res_model="x_if_vendor", view_mode="list,form"),
    "activity": dict(name="Activity log", res_model="x_if_activity", view_mode="list,form"),
}
MENUS = [("Dashboard", "dashboard"), ("Users", "users"), ("Folders", "folders"), ("Invoices", "invoices"),
         ("Invoices by user and folder", "by_folder"), ("Invoices by vendor", "by_vendor"), ("Purchase orders", "pos"),
         ("Vendors", "vendors"), ("Activity log", "activity")]
ROOT_MENU = "Invoice Flow Platform"
