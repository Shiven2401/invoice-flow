/* The Odoo ERP page, for an account connected to Odoo: everything Invoice Flow has mirrored from Odoo (purchase orders,
   inventory, receipts, vendors, products, bills), the dashboard figures built from it, and the writes that go back to
   Odoo (new or changed POs, approvals, new vendors and products) through the sync queue. Uses app.js's helpers. */
"use strict";

const ERP = { tab: "overview", from: "", to: "", vendor: "", q: "", state: "", sort: {}, vendors: [], products: [], options: null, status: null, again: 0 };
const ERP_TABS = [["overview", "Overview"], ["pos", "Purchase orders"], ["inventory", "Inventory"], ["receipts", "Receipts"],
  ["partners", "Vendors & products"], ["bills", "Bills"], ["sync", "Sync"]];
const PO_PILL = { draft: "pen", sent: "pen", "to approve": "warn", purchase: "ok", done: "ok", cancel: "bad" };
const SYNC_PILL = { synced: ["Synced", "ok"], pending: ["Waiting for Odoo", "warn"], error: ["Sync failed", "bad"], conflict: ["Conflict", "bad"], deleted: ["Gone from Odoo", "bad"] };
const RECEIPT_LABEL = { pending: "Not received", partial: "Partly received", full: "Received" };
const BILL_STATE = { draft: ["Draft", "pen"], posted: ["Posted", "ok"], cancel: ["Cancelled", "bad"] };
const PAY_LABEL = { not_paid: "Not paid", in_payment: "In payment", paid: "Paid", partial: "Partly paid", reversed: "Reversed", blocked: "Blocked" };
const m2o = v => Array.isArray(v) ? v[1] : (v || "");
const day = v => (v || "").slice(0, 10);
const num = v => v == null ? "–" : (+v).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const pill = (text, cls) => `<span class="mini ${cls}">${esc(text)}</span>`;
const syncPill = s => { const [t, c] = SYNC_PILL[s] || [s || "", "pen"]; return s && s !== "synced" ? pill(t, c) : ""; };
const inWindow = d => (!ERP.from || day(d) >= ERP.from) && (!ERP.to || (day(d) && day(d) <= ERP.to));
const byVendor = partner => !ERP.vendor || (Array.isArray(partner) && String(partner[0]) === ERP.vendor);
const reqId = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
const post = (url, body, method = "POST") => api(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

/* a sortable table: cols [{key, label, get(row) -> sort value, html(row), cls}] */
function erpTable(id, cols, rows, empty) {
  const s = ERP.sort[id] || {}, col = cols.find(c => c.key === s.key);
  if (col) rows = rows.slice().sort((a, b) => { const x = (col.get || (r => r[col.key]))(a), y = (col.get || (r => r[col.key]))(b);
    return (x == null) - (y == null) || (x > y ? 1 : x < y ? -1 : 0) * (s.dir === "desc" ? -1 : 1); });
  return `<div class="table-wrap"><table class="greenbar erp-table" data-table="${id}"><thead><tr>${cols.map(c => `<th class="${c.cls || ""}">
      <button type="button" class="th-sort" data-sort="${id}:${c.key}" aria-label="Sort by ${esc(c.label)}">${esc(c.label)}${s.key === c.key ? (s.dir === "desc" ? " ↓" : " ↑") : ""}</button></th>`).join("")}</tr></thead>
    <tbody>${rows.length ? rows.map(r => `<tr${r._click ? ` class="click" tabindex="0" data-erp-open="${r._click}"` : ""}>${cols.map(c => `<td class="${c.cls || ""}">${c.html ? c.html(r) : esc(r[c.key] ?? "–")}</td>`).join("")}</tr>`).join("")
      : `<tr><td class="empty-row" colspan="${cols.length}">${empty}</td></tr>`}</tbody></table></div>`;
}

function tiles(items) {
  return `<div class="erp-tiles">${items.map(([label, value, sub, go]) => `<${go ? `button type="button" data-go="${go}"` : "div"} class="erp-tile${go ? " go" : ""}">
      <div class="v">${value}</div><div class="l">${esc(label)}</div>${sub ? `<div class="s">${sub}</div>` : ""}</${go ? "button" : "div"}>`).join("")}</div>`;
}

function filtersBar() {
  const showDates = ["overview", "pos", "receipts", "bills"].includes(ERP.tab), showVendor = ["overview", "pos", "bills", "receipts"].includes(ERP.tab);
  const states = { pos: [["", "Every state"], ["draft", "Draft (RFQ)"], ["to approve", "Pending approval"], ["purchase", "Confirmed"], ["cancel", "Cancelled"], ["pending_sync", "Waiting to sync"]],
    bills: [["", "Every bill"], ["draft", "Draft"], ["posted", "Posted"], ["not_paid", "Unpaid"], ["paid", "Paid"]],
    receipts: [["", "Every receipt"], ["assigned", "Ready"], ["done", "Done"], ["cancel", "Cancelled"]] }[ERP.tab];
  $("#erp-filters").innerHTML = `
    ${showDates ? `<label class="f"><span>From</span><input type="date" data-f="from" value="${esc(ERP.from)}"></label>
      <label class="f"><span>To</span><input type="date" data-f="to" value="${esc(ERP.to)}"></label>` : ""}
    ${showVendor ? `<label class="f"><span>Vendor</span><select data-f="vendor"><option value="">Every vendor</option>${ERP.vendors.filter(v => v._odoo_id)
      .map(v => `<option value="${v._odoo_id}"${String(v._odoo_id) === ERP.vendor ? " selected" : ""}>${esc(v.name)}</option>`).join("")}</select></label>` : ""}
    ${states ? `<label class="f"><span>Status</span><select data-f="state">${states.map(([v, t]) => `<option value="${v}"${v === ERP.state ? " selected" : ""}>${t}</option>`).join("")}</select></label>` : ""}
    ${ERP.tab !== "overview" && ERP.tab !== "sync" ? `<input type="search" class="f-search" data-f="q" placeholder="Search" aria-label="Search" value="${esc(ERP.q)}">` : ""}`;
  $("#erp-filters").hidden = !$("#erp-filters").innerHTML.trim();
}

async function loadErp() {
  if (!erpOn()) { location.hash = "#process"; return; }
  $$("#erp-tabs [data-erp-tab]").forEach(b => b.setAttribute("aria-selected", b.dataset.erpTab === ERP.tab));
  const st = ERP.status = await api("/api/erp/status");
  $("#erp-sub").innerHTML = `${esc(st.db)} at ${extLink(st.home, esc(st.url.replace(/^https?:\/\//, "")))} over ${esc(st.protocol)} · mirrored ${st.last_pull ? esc(st.last_pull.replace("T", " ").slice(0, 16)) : "not yet"}`
    + (st.queue.pending ? ` · <b>${st.queue.pending}</b> waiting to reach Odoo` : "") + (st.queue.failed + st.queue.conflicts ? ` · <b class="bad-text">${st.queue.failed + st.queue.conflicts} need a look</b>` : "");
  $("#erp-open-odoo").href = st.home;
  if (!ERP.vendors.length || ERP.tab === "partners") ERP.vendors = (await api("/api/erp/vendors")).vendors;
  filtersBar();
  const body = $("#erp-body");
  let fresh = null;
  try {
    fresh = await ({ overview: erpOverview, pos: erpPos, inventory: erpInventory, receipts: erpReceipts, partners: erpPartners, bills: erpBills, sync: erpSync }[ERP.tab])(body);
  } catch (e) { body.innerHTML = `<p class="auth-error">${esc(e.message)}</p>`; }
  // the mirror was older than 30 s: the server is refreshing it from Odoo, so look again shortly (once)
  if (fresh && fresh.refreshing && ERP.again < 1) { ERP.again++; setTimeout(() => { if (location.hash === "#erp") loadErp(); }, 3500); }
  else ERP.again = 0;
}

async function erpOverview(body) {
  const qs = new URLSearchParams({ date_from: ERP.from, date_to: ERP.to, vendor: ERP.vendor }).toString();
  const o = await api(`/api/erp/overview?${qs}`), i = o.invoices, p = o.purchase_orders, m = o.money, x = o.matching;
  body.innerHTML = `
    <section class="erp-group"><div class="card-head"><h2>Invoices</h2><span class="card-note">Received in Invoice Flow; bills as Odoo has them</span></div>
      ${tiles([["Received", i.received, `${i.processed} processed`], ["Pending", i.pending, `${i.held} held for a person`, "dashboard"], ["Approved", i.approved],
        ["Rejected", i.rejected], ["Draft bills", i.bills_draft, "in Odoo", "bills:draft"], ["Posted bills", i.bills_posted, `${i.bills_paid} paid · ${i.bills_unpaid} unpaid`, "bills:posted"],
        ["Credit notes", i.credit_notes]])}</section>
    <section class="erp-group"><div class="card-head"><h2>Purchase orders</h2><span class="card-note">From Odoo, by order date</span></div>
      ${tiles([["Total", p.total, p.pending_sync ? `${p.pending_sync} waiting to sync` : "", "pos:"], ["Draft (RFQ)", p.draft, "", "pos:draft"], ["Pending approval", p.pending_approval, "", "pos:to approve"],
        ["Confirmed", p.confirmed, `${p.not_received} not received yet`, "pos:purchase"], ["Partly received", p.partially_received], ["Fully received", p.fully_received], ["Cancelled", p.cancelled, "", "pos:cancel"]])}</section>
    <section class="erp-group"><div class="card-head"><h2>Money</h2><span class="card-note">Bills by bill date; credit notes count against</span></div>
      ${tiles([["Invoice value", inr0(m.invoice_value), `${inr0(m.draft_bill_value)} still in draft`], ["Purchase order value", inr0(m.po_value), `incl. ${inr0(m.po_tax)} tax`],
        ["Pending payment", inr0(m.pending_payment), "on posted bills"], ["Paid", inr0(m.paid)], ["Outstanding to vendors", inr0(m.outstanding_liabilities)], ["Tax on bills", inr0(m.tax)]])}</section>
    <section class="erp-group"><div class="card-head"><h2>Matching</h2><span class="card-note">Invoice Flow's checks against the PO and the receipt</span></div>
      ${tiles([["Matched (3-way)", x.matched], ["Partly matched", x.partially_matched], ["Mismatched", x.mismatched], ["Missing PO", x.missing_po],
        ["Price differences", x.price_discrepancies], ["Quantity differences", x.quantity_discrepancies]])}</section>`;
  return o.fresh;
}

async function erpPos(body) {
  const d = await api(`/api/erp/purchase-orders?${new URLSearchParams({ state: ERP.state, q: ERP.q })}`);
  const rows = d.purchase_orders.filter(p => inWindow(p.date_order) && byVendor(p.partner_id)).map(p => ({ ...p, _click: p._id }));
  const can = ERP.status.approver;
  body.innerHTML = `<div class="erp-actions">${can ? `<button class="btn primary" type="button" data-erp-new="po">＋ New purchase order</button>` : ""}
      <span class="muted">${rows.length} purchase order${rows.length === 1 ? "" : "s"}. A PO made here is created in Odoo as an RFQ; approving it confirms it there.</span></div>
    ${erpTable("pos", [
      { key: "name", label: "PO", get: r => r.name || "", html: r => `<b>${esc(r.name || "New (not in Odoo yet)")}</b>${r._odoo_url ? " " + extLink(r._odoo_url, "") : ""}` },
      { key: "vendor", label: "Vendor", get: r => m2o(r.partner_id), html: r => esc(m2o(r.partner_id)) },
      { key: "date_order", label: "Ordered", get: r => r.date_order, html: r => esc(day(r.date_order)) },
      { key: "state", label: "State", get: r => r.state, html: r => pill(r.state_label, PO_PILL[r.state] || "pen") + " " + syncPill(r._status) },
      { key: "received_pct", label: "Received", cls: "right", html: r => r.state === "purchase" ? `${num(r.received_pct)}%` : "–" },
      { key: "billed_pct", label: "Billed", cls: "right", html: r => r.state === "purchase" ? `${num(r.billed_pct)}%` : "–" },
      { key: "amount_total", label: "Total", cls: "right", get: r => r.amount_total ?? r.amount_untaxed, html: r => inr(r.amount_total ?? r.amount_untaxed ?? 0) }],
      rows, "No purchase orders match.")}`;
  return d.fresh;
}

async function erpInventory(body) {
  const d = await api(`/api/erp/inventory?${new URLSearchParams({ q: ERP.q })}`);
  ERP.products = d.products;
  const goods = d.products.filter(p => p.type === "consu");
  body.innerHTML = `${tiles([["Products", d.products.length, `${goods.length} stocked goods`], ["Stock value", inr0(d.stock_value), "on hand at cost"],
      ["Receipts waiting", d.pending_receipts, "goods expected", "receipts:assigned"], ["Warehouses", d.warehouses.length, d.warehouses.map(w => esc(w.name)).join(", ")]])}
    <div class="card-head"><h2>Stock by product</h2><span class="card-note">On hand, incoming and forecast, from Odoo Inventory</span></div>
    ${erpTable("stock", [
      { key: "default_code", label: "SKU", html: r => esc(r.default_code || "–") }, { key: "name", label: "Product" },
      { key: "type", label: "Type", html: r => r.type === "service" ? "Service" : r.is_storable ? "Stocked" : "Goods" },
      { key: "qty_available", label: "On hand", cls: "right", html: r => num(r.qty_available) }, { key: "incoming_qty", label: "Incoming", cls: "right", html: r => num(r.incoming_qty) },
      { key: "virtual_available", label: "Forecast", cls: "right", html: r => num(r.virtual_available) },
      { key: "standard_price", label: "Cost", cls: "right", html: r => inr(r.standard_price || 0) }, { key: "uom", label: "Unit", get: r => m2o(r.uom_id), html: r => esc(m2o(r.uom_id)) }],
      d.products, "No products.")}
    <div class="card-head"><h2>Recent receipt moves</h2><span class="card-note">What came in, against which PO</span></div>
    ${erpTable("moves", [
      { key: "date", label: "Date", html: r => esc(day(r.date)) }, { key: "receipt", label: "Receipt" }, { key: "po", label: "PO", html: r => esc(r.po || "–") },
      { key: "product", label: "Product", get: r => m2o(r.product_id), html: r => esc(m2o(r.product_id)) },
      { key: "product_uom_qty", label: "Expected", cls: "right", html: r => num(r.product_uom_qty) }, { key: "quantity", label: "Received", cls: "right", html: r => r.state === "done" ? num(r.quantity) : "–" },
      { key: "state", label: "State", html: r => pill(r.state === "done" ? "Done" : r.state === "cancel" ? "Cancelled" : "Waiting", r.state === "done" ? "ok" : r.state === "cancel" ? "bad" : "warn") }],
      d.moves, "No receipt moves yet.")}`;
  return d.fresh;
}

async function erpReceipts(body) {
  const d = await api(`/api/erp/receipts?${new URLSearchParams({ state: ERP.state })}`);
  const q = ERP.q.toLowerCase();
  const rows = d.receipts.filter(r => inWindow(r.scheduled_date) && byVendor(r.partner_id) && (!q || [r.name, r.origin, m2o(r.partner_id)].join(" ").toLowerCase().includes(q)));
  body.innerHTML = erpTable("receipts", [
    { key: "name", label: "Receipt (GRN)" }, { key: "po", label: "PO", get: r => m2o(r.purchase_id) || r.origin, html: r => esc(m2o(r.purchase_id) || r.origin || "–") },
    { key: "vendor", label: "Vendor", get: r => m2o(r.partner_id), html: r => esc(m2o(r.partner_id)) },
    { key: "scheduled_date", label: "Expected", html: r => esc(day(r.scheduled_date)) }, { key: "date_done", label: "Received on", html: r => esc(day(r.date_done) || "–") },
    { key: "lines", label: "Lines", get: r => (r.moves || []).length, html: r => (r.moves || []).map(mv => `${esc(m2o(mv.product_id))}: ${num(mv.state === "done" ? mv.quantity : 0)} of ${num(mv.product_uom_qty)}`).join("<br>") },
    { key: "state", label: "State", html: r => pill({ done: "Done", assigned: "Ready", cancel: "Cancelled", confirmed: "Waiting", waiting: "Waiting", draft: "Draft" }[r.state] || r.state, r.state === "done" ? "ok" : r.state === "cancel" ? "bad" : "warn") }],
    rows, "No receipts match.");
  return d.fresh;
}

async function erpPartners(body) {
  const [p] = await Promise.all([api(`/api/erp/products?${new URLSearchParams({ q: ERP.q })}`)]);
  ERP.products = p.products;
  const q = ERP.q.toLowerCase(), can = ERP.status.approver;
  const vendors = ERP.vendors.filter(v => !q || [v.name, v.vat, v.email, v.city].join(" ").toLowerCase().includes(q));
  body.innerHTML = `<div class="erp-actions">${can ? `<button class="btn primary" type="button" data-erp-new="vendor">＋ New vendor</button>
      <button class="btn" type="button" data-erp-new="product">＋ New product</button>` : ""}<span class="muted">Created in Odoo; a GSTIN, reference or name Odoo already has is never added twice.</span></div>
    <div class="card-head"><h2>Vendors</h2><span class="card-note">${vendors.length} in Odoo</span></div>
    ${erpTable("vendors", [
      { key: "name", label: "Vendor", html: r => `${r._odoo_url ? extLink(r._odoo_url, esc(r.name)) : esc(r.name)} ${syncPill(r._status)}` },
      { key: "vat", label: "GSTIN", html: r => esc(r.vat || "–") }, { key: "email", label: "Email", html: r => esc(r.email || "–") }, { key: "phone", label: "Phone", html: r => esc(r.phone || "–") },
      { key: "addr", label: "Address", get: r => r.city, html: r => esc([r.street, r.city, r.zip, m2o(r.state_id)].filter(Boolean).join(", ") || "–") },
      { key: "terms", label: "Payment terms", get: r => m2o(r.property_supplier_payment_term_id), html: r => esc(m2o(r.property_supplier_payment_term_id) || "–") }],
      vendors, "No vendors match.")}
    <div class="card-head"><h2>Products</h2><span class="card-note">${p.products.length} that can be purchased</span></div>
    ${erpTable("products", [
      { key: "default_code", label: "SKU", html: r => esc(r.default_code || "–") }, { key: "name", label: "Product", html: r => `${esc(r.name)} ${syncPill(r._status)}` },
      { key: "categ", label: "Category", get: r => m2o(r.categ_id), html: r => esc(m2o(r.categ_id)) }, { key: "uom", label: "Unit", get: r => m2o(r.uom_id), html: r => esc(m2o(r.uom_id)) },
      { key: "standard_price", label: "Purchase price", cls: "right", html: r => inr(r.standard_price || 0) },
      { key: "taxes", label: "Taxes", get: r => (r.supplier_taxes_id || []).length, html: r => esc(taxNames(r.supplier_taxes_id)) }],
      p.products, "No products match.")}`;
  return p.fresh;
}

function taxNames(ids) {
  const t = (ERP.options && ERP.options.taxes) || [];
  return (ids || []).map(id => (t.find(x => x.id === id) || {}).name || `#${id}`).join(", ") || "–";
}

async function erpBills(body) {
  const pay = ["not_paid", "paid"].includes(ERP.state) ? ERP.state : "", state = pay ? "posted" : ERP.state;
  const d = await api(`/api/erp/bills?${new URLSearchParams({ state, payment: pay })}`);
  const q = ERP.q.toLowerCase();
  const rows = d.bills.filter(b => inWindow(b.invoice_date || b.write_date) && byVendor(b.partner_id) && (!q || [b.name, b.ref, m2o(b.partner_id)].join(" ").toLowerCase().includes(q)));
  body.innerHTML = erpTable("bills", [
    { key: "name", label: "Bill", html: r => extLink(r._odoo_url, esc(r.name && r.name !== "/" ? r.name : `Draft #${r._odoo_id}`)) },
    { key: "ref", label: "Vendor's number", html: r => esc(r.ref || "–") }, { key: "vendor", label: "Vendor", get: r => m2o(r.partner_id), html: r => esc(m2o(r.partner_id)) },
    { key: "move_type", label: "Type", html: r => r.move_type === "in_refund" ? "Credit note" : "Bill" },
    { key: "invoice_date", label: "Date", html: r => esc(r.invoice_date || "–") }, { key: "invoice_date_due", label: "Due", html: r => esc(r.invoice_date_due || "–") },
    { key: "amount_total", label: "Total", cls: "right", html: r => inr(r.amount_total || 0) }, { key: "amount_residual", label: "Still due", cls: "right", html: r => inr(r.amount_residual || 0) },
    { key: "state", label: "State", html: r => { const [t, c] = BILL_STATE[r.state] || [r.state, "pen"]; return pill(t, c) + (r.state === "posted" ? " " + pill(PAY_LABEL[r.payment_state] || r.payment_state, r.payment_state === "paid" ? "ok" : "warn") : ""); } }],
    rows, "No bills match.");
  return d.fresh;
}

async function erpSync(body) {
  const d = await api("/api/erp/sync"), can = ERP.status.approver;
  const when = v => v ? esc(String(v).replace("T", " ").slice(0, 19)) : "never";
  const OPS = { po_create: "Create PO", po_update: "Change PO", po_confirm: "Approve PO", vendor_create: "Add vendor", product_create: "Add product",
    run_recheck: "Check a held invoice again", trail_retry: "Audit note", contact_sync: "Contact for an account" };
  body.innerHTML = `${tiles([["Last pull", when(d.last_pull), "every minute"], ["Last full reconcile", when(d.last_full), "every 15 minutes"],
      ["Last webhook from Odoo", d.last_webhook ? when(d.last_webhook.split(" ")[0]) : "none yet", d.last_webhook ? esc(d.last_webhook.split(" ").slice(1).join(" ")) : "set up with tools/odoo_webhooks.py"],
      ["Waiting", d.queue.pending, "to reach Odoo"], ["Failed", d.queue.failed, "need a look"], ["Conflicts", d.queue.conflicts, "changed in Odoo meanwhile"]])}
    <div class="erp-actions"><button class="btn" type="button" data-erp-sync="pull">Pull changes now</button><button class="btn" type="button" data-erp-sync="full">Full reconcile</button>
      <span class="muted">Mirrored: ${Object.entries(d.counts).map(([k, v]) => `${v} ${esc({ "purchase.order": "POs", "res.partner": "vendors", "product.product": "products", "stock.picking": "receipts", "account.move": "bills" }[k] || k)}`).join(" · ")}</span></div>
    ${Object.keys(d.errors).length ? `<p class="auth-error">${Object.entries(d.errors).map(([k, v]) => `${esc(k)}: ${esc(v)}`).join("<br>")}</p>` : ""}
    <div class="card-head"><h2>Writes to Odoo</h2><span class="card-note">Every change the app sent, in order; nothing is dropped</span></div>
    ${erpTable("queue", [
      { key: "id", label: "#", cls: "right" }, { key: "at", label: "When", html: r => when(r.at) }, { key: "op", label: "What", html: r => esc(OPS[r.op] || r.op) },
      { key: "by", label: "By", html: r => esc(r.by || "–") },
      { key: "status", label: "Status", html: r => pill({ done: "Done", pending: "Waiting", running: "Sending", error: "Failed", conflict: "Conflict" }[r.status] || r.status,
        { done: "ok", pending: "warn", running: "warn", error: "bad", conflict: "bad" }[r.status] || "pen") + (r.attempts > 1 ? ` <span class="muted">${r.attempts} tries</span>` : "") },
      { key: "error", label: "Details", html: r => esc(r.error || (r.result && (r.result.name || r.result.state || r.result.result)) || "") },
      { key: "retry", label: "", html: r => can && ["error", "conflict", "pending"].includes(r.status) ? `<button class="btn quiet" type="button" data-erp-retry="${r.id}">Retry</button>` : "" }],
      d.entries, "Nothing has been sent to Odoo yet.")}`;
  return null;
}

/* ------------------------------------------------------------ one purchase order -- */
async function erpPoDialog(id, editing = false) {
  const [po, opts] = await Promise.all([api(`/api/erp/purchase-orders/${id}`), erpOptions()]);
  if (editing && !ERP.products.length) ERP.products = (await api("/api/erp/products")).products;
  const dlg = $("#erp-dialog"), can = ERP.status.approver;
  const lines = po.lines || [];
  dlg.innerHTML = `<form class="modal-body ledger-form erp-po" novalidate>
    <div class="modal-head">${PO_ICON}<h2>${esc(po.name || "New purchase order")}</h2><button class="icon-btn" type="button" data-close aria-label="Close">${REMOVE_ICON}</button></div>
    <p class="muted lf-sub">${esc(m2o(po.partner_id))} · ${pill(po.state_label, PO_PILL[po.state] || "pen")} ${syncPill(po._status)}
      ${po.receipt_status ? " · " + esc(RECEIPT_LABEL[po.receipt_status] || po.receipt_status) : ""}${po._odoo_url ? " · " + extLink(po._odoo_url, "Open in Odoo") : ""}</p>
    ${po._error ? `<p class="auth-error">${esc(po._error)}</p>` : ""}
    ${editing ? `<div class="lf-two"><label>Vendor's reference<input name="partner_ref" maxlength="64" value="${esc(po.partner_ref || "")}"></label>
        <label>Expected<input name="date_planned" type="date" value="${esc(day(po.date_planned))}"></label></div>` :
      `<p class="muted">Ordered ${esc(day(po.date_order))}${po.date_approve ? ` · approved ${esc(day(po.date_approve))}` : ""}${po.partner_ref ? ` · vendor ref ${esc(po.partner_ref)}` : ""}${po.origin ? ` · ${esc(po.origin)}` : ""}</p>`}
    <div class="table-wrap"><table class="greenbar erp-lines"><thead><tr><th>Product</th><th class="right">Ordered</th><th class="right">Received</th><th class="right">Billed</th><th class="right">Price</th><th class="right">Subtotal</th>${editing ? "<th></th>" : ""}</tr></thead>
      <tbody>${lines.map(ln => `<tr data-line="${ln.id || ""}"><td>${esc(ln.name || m2o(ln.product_id))}</td>
        <td class="right">${editing ? `<input type="number" step="any" min="0" name="qty" value="${ln.product_qty}" aria-label="Quantity">` : num(ln.product_qty)}</td>
        <td class="right">${num(ln.qty_received)}</td><td class="right">${num(ln.qty_invoiced)}</td>
        <td class="right">${editing ? `<input type="number" step="any" min="0" name="price" value="${ln.price_unit}" aria-label="Price">` : inr(ln.price_unit || 0)}</td>
        <td class="right">${inr(ln.price_subtotal ?? (ln.product_qty || 0) * (ln.price_unit || 0))}</td>
        ${editing ? `<td>${ln.qty_received || ln.qty_invoiced ? "" : `<label class="wz-check small"><input type="checkbox" name="delete"><span>Remove</span></label>`}</td>` : ""}</tr>`).join("")}</tbody></table></div>
    ${editing ? `<div data-new-lines></div><button class="btn quiet" type="button" data-add-line>＋ Add a line</button>` : ""}
    <p class="lf-total">Untaxed ${inr(po.amount_untaxed || 0)}${po.amount_tax != null ? ` · tax ${inr(po.amount_tax)} · total <b>${inr(po.amount_total || 0)}</b>` : ""}</p>
    ${!editing && (po.receipts.length || po.bills.length || po.invoices.length) ? `<div class="erp-related">
      ${po.receipts.length ? `<p><b>Receipts:</b> ${po.receipts.map(r => `${esc(r.name)} (${esc({ done: "done", assigned: "ready", cancel: "cancelled" }[r.state] || r.state)})`).join(", ")}</p>` : ""}
      ${po.bills.length ? `<p><b>Bills:</b> ${po.bills.map(b => extLink(b._odoo_url, `${esc(b.name && b.name !== "/" ? b.name : "Draft #" + b._odoo_id)} ${esc(b.state)}`)).join(", ")}</p>` : ""}
      ${po.invoices.length ? `<p><b>Invoices in Invoice Flow:</b> ${po.invoices.map(v => `${esc(v.invoice_number || v.filename)} (${esc((DEC[v.decision] || {}).label || v.decision || "")})`).join(", ")}</p>` : ""}</div>` : ""}
    ${po.queue.length ? `<p class="muted">Sync: ${po.queue.slice(0, 4).map(q => `${esc(q.op.replace("po_", ""))} ${esc(q.status)}`).join(" · ")}</p>` : ""}
    <p class="auth-error" data-error hidden></p>
    <div class="lf-actions">${editing ? `<button class="btn" type="button" data-cancel-edit>Cancel</button><button class="btn primary" type="submit">Save to Odoo</button>`
      : `<button class="btn" type="button" data-close>Close</button>${can && po.can_edit && po._odoo_id ? `<button class="btn" type="button" data-edit>Edit</button>` : ""}
         ${can && po.can_approve && po.state !== "cancel" ? `<button class="btn ok" type="button" data-approve>Approve (confirm in Odoo)</button>` : ""}`}</div></form>`;
  const form = $("form", dlg), error = $("[data-error]", dlg);
  dlg.onclick = async ev => {
    if (ev.target === dlg || ev.target.closest("[data-close]")) { dlg.close(); return; }
    if (ev.target.closest("[data-edit]")) { erpPoDialog(id, true); return; }
    if (ev.target.closest("[data-cancel-edit]")) { erpPoDialog(id); return; }
    if (ev.target.closest("[data-add-line]")) { $("[data-new-lines]", dlg).insertAdjacentHTML("beforeend", lineRow(opts)); return; }
    if (ev.target.closest("[data-drop-line]")) { ev.target.closest(".erp-line").remove(); return; }
    if (ev.target.closest("[data-approve]")) {
      const b = ev.target.closest("[data-approve]"); b.disabled = true;
      try { const r = await post(`/api/erp/purchase-orders/${id}/approve`, { request_id: reqId() });
        toast(r.state === "to approve" ? "Sent to Odoo: it needs a purchase manager's approval there (two-step approval)." : r.message); erpPoDialog(id); loadErp(); }
      catch (e) { error.textContent = e.message; error.hidden = false; b.disabled = false; }
    }
  };
  form.onsubmit = async ev => {
    ev.preventDefault();
    const changed = $$("tr[data-line]", form).map(tr => {
      const ln = lines.find(l => String(l.id) === tr.dataset.line), q = $("[name=qty]", tr), p = $("[name=price]", tr), del = $("[name=delete]", tr);
      if (del && del.checked) return { id: ln.id, delete: true };
      const out = { id: ln.id };
      if (q && +q.value !== ln.product_qty) out.qty = +q.value;
      if (p && +p.value !== ln.price_unit) out.price = +p.value;
      return Object.keys(out).length > 1 ? out : null;
    }).filter(Boolean).concat(newLines(form));
    const body = { base_version: po._version, request_id: reqId(), lines: changed };
    if (form.elements.partner_ref.value !== (po.partner_ref || "")) body.partner_ref = form.elements.partner_ref.value;
    if (form.elements.date_planned.value && form.elements.date_planned.value !== day(po.date_planned)) body.date_planned = form.elements.date_planned.value;
    if (!changed.length && body.partner_ref === undefined && !body.date_planned) { error.textContent = "Nothing has changed."; error.hidden = false; return; }
    try { const r = await post(`/api/erp/purchase-orders/${id}`, body, "PATCH"); toast(r.message); erpPoDialog(id); loadErp(); }
    catch (e) { error.textContent = e.message; error.hidden = false; }
  };
  if (!dlg.open) dlg.showModal();
}

function lineRow(opts) {
  return `<div class="erp-line"><label>Product<select name="product_id" required><option value="">Choose a product</option>${ERP.products.filter(p => p._odoo_id)
      .map(p => `<option value="${p._odoo_id}" data-price="${p.standard_price || 0}" data-taxes="${(p.supplier_taxes_id || []).join(",")}">${esc((p.default_code ? `[${p.default_code}] ` : "") + p.name)}</option>`).join("")}</select></label>
    <label>Qty<input name="qty" type="number" step="any" min="0.0001" required></label>
    <label>Price (₹)<input name="price" type="number" step="any" min="0"></label>
    <label>Tax<select name="tax"><option value="">Product's own</option><option value="none">No tax</option>${(opts.taxes || []).map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select></label>
    <button class="icon-btn" type="button" data-drop-line aria-label="Remove this line">${REMOVE_ICON}</button></div>`;
}

function newLines(form) {
  return $$(".erp-line", form).map(row => {
    const sel = $("[name=product_id]", row), qty = $("[name=qty]", row).value, price = $("[name=price]", row).value, tax = $("[name=tax]", row).value;
    if (!sel.value) return null;
    const ln = { product_id: +sel.value, qty: +qty };
    ln.price = price !== "" ? +price : +sel.selectedOptions[0].dataset.price;
    if (tax === "none") ln.tax_ids = []; else if (tax) ln.tax_ids = [+tax];
    else { const t = sel.selectedOptions[0].dataset.taxes; ln.tax_ids = t ? t.split(",").map(Number) : []; }
    return ln;
  }).filter(Boolean);
}

async function erpOptions() {
  if (!ERP.options) ERP.options = await api("/api/erp/options");
  return ERP.options;
}

async function erpNewDialog(kind) {
  const opts = await erpOptions();
  if (!ERP.products.length) ERP.products = (await api("/api/erp/products")).products;
  const dlg = $("#erp-dialog");
  const head = (icon, title, sub) => `<div class="modal-head">${icon}<h2>${title}</h2><button class="icon-btn" type="button" data-close aria-label="Close">${REMOVE_ICON}</button></div><p class="muted lf-sub">${sub}</p>`;
  const actions = label => `<p class="auth-error" data-error hidden></p><div class="lf-actions"><button class="btn" type="button" data-close>Cancel</button><button class="btn primary" type="submit">${label}</button></div>`;
  dlg.innerHTML = `<form class="modal-body ledger-form erp-po" novalidate>${{
    po: `${head(PO_ICON, "New purchase order", "Created in Odoo as an RFQ. Approve it to confirm it there; Odoo's own approval rules apply.")}
      <label>Vendor<select name="vendor_id" required><option value="">Choose a vendor</option>${ERP.vendors.filter(v => v._odoo_id).map(v => `<option value="${v._odoo_id}">${esc(v.name)}</option>`).join("")}</select></label>
      <div class="lf-two"><label>Vendor's reference <small>optional</small><input name="partner_ref" maxlength="64"></label><label>Expected <small>optional</small><input name="date_planned" type="date"></label></div>
      <div data-new-lines>${lineRow(opts)}</div><button class="btn quiet" type="button" data-add-line>＋ Add a line</button>
      <label class="wz-check"><input type="checkbox" name="approve"><span><b>Approve it now</b><small>Confirms the order in Odoo, so its receipt is created and invoices can be matched to it.</small></span></label>
      ${actions("Create in Odoo")}`,
    vendor: `${head(VENDOR_ICON, "New vendor", "Created in Odoo as a vendor. Invoices are matched to it by GSTIN and name.")}
      <label>Name<input name="name" required maxlength="200"></label>
      <div class="lf-two"><label>GSTIN <small>optional</small><input name="vat" maxlength="15" class="lf-code"></label><label>Payment terms<select name="payment_term_id"><option value="">None</option>${(opts.payment_terms || []).map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select></label></div>
      <div class="lf-two"><label>Email<input name="email" type="email" maxlength="120"></label><label>Phone<input name="phone" maxlength="40"></label></div>
      <label>Street<input name="street" maxlength="200"></label><div class="lf-two"><label>City<input name="city" maxlength="80"></label><label>PIN code<input name="zip" maxlength="12"></label></div>
      ${actions("Create in Odoo")}`,
    product: `${head(PO_ICON, "New product", "Created in Odoo, ready to put on purchase orders.")}
      <label>Name<input name="name" required maxlength="200"></label>
      <div class="lf-two"><label>SKU (internal reference) <small>optional</small><input name="default_code" maxlength="60" class="lf-code"></label>
        <label>Kind<select name="kind"><option value="goods">Goods (stocked)</option><option value="service">Service</option></select></label></div>
      <div class="lf-two"><label>Purchase price (₹)<input name="standard_price" type="number" step="any" min="0"></label>
        <label>Purchase tax<select name="tax"><option value="">None</option>${(opts.taxes || []).map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select></label></div>
      <label>Unit<select name="uom_id"><option value="">Units (default)</option>${(opts.uoms || []).map(u => `<option value="${u.id}">${esc(u.name)}</option>`).join("")}</select></label>
      ${actions("Create in Odoo")}` }[kind]}</form>`;
  const form = $("form", dlg), error = $("[data-error]", dlg), request_id = reqId();
  dlg.onclick = ev => {
    if (ev.target === dlg || ev.target.closest("[data-close]")) dlg.close();
    else if (ev.target.closest("[data-add-line]")) $("[data-new-lines]", dlg).insertAdjacentHTML("beforeend", lineRow(opts));
    else if (ev.target.closest("[data-drop-line]") && $$(".erp-line", dlg).length > 1) ev.target.closest(".erp-line").remove();
  };
  form.onsubmit = async ev => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(form)), button = $("button[type=submit]", form);
    let body = { ...f, request_id };
    if (kind === "po") body = { vendor_id: +f.vendor_id, partner_ref: f.partner_ref, date_planned: f.date_planned, approve: !!form.elements.approve.checked, lines: newLines(form), request_id };
    if (kind === "product") { body.tax_ids = f.tax ? [+f.tax] : []; delete body.tax; }
    error.hidden = true; button.disabled = true;
    try {
      const r = await post({ po: "/api/erp/purchase-orders", vendor: "/api/erp/vendors", product: "/api/erp/products" }[kind], body);
      dlg.close(); toast(r.message + (r.ref && r.ref !== "new PO" ? ` (${r.ref})` : ""));
      if (kind === "vendor") ERP.vendors = (await api("/api/erp/vendors")).vendors;
      if (kind === "product") ERP.products = (await api("/api/erp/products")).products;
      loadErp();
    } catch (e) { error.textContent = e.message; error.hidden = false; button.disabled = false; }
  };
  dlg.showModal();
  (form.querySelector("select, input") || form).focus();
}

function wireErp() {
  $("#erp-tabs").innerHTML = ERP_TABS.map(([k, t]) => `<button type="button" role="tab" data-erp-tab="${k}" aria-selected="${k === ERP.tab}">${t}</button>`).join("");
  $("#erp-tabs").addEventListener("click", ev => { const b = ev.target.closest("[data-erp-tab]"); if (!b) return; ERP.tab = b.dataset.erpTab; ERP.state = ""; ERP.q = ""; loadErp(); });
  $("#erp-filters").addEventListener("change", ev => { const k = ev.target.dataset.f; if (!k || k === "q") return; ERP[k] = ev.target.value; loadErp(); });
  let t;
  $("#erp-filters").addEventListener("input", ev => { if (ev.target.dataset.f !== "q") return; clearTimeout(t); t = setTimeout(() => { ERP.q = ev.target.value; loadErp().then(() => { const s = $("#erp-filters [data-f=q]"); if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); } }); }, 300); });
  $("#erp-sync-now").addEventListener("click", async ev => { ev.target.disabled = true; try { await post("/api/erp/sync", {}); toast("Pulled the latest from Odoo."); loadErp(); } catch (e) { toast(e.message); } ev.target.disabled = false; });
  $("#erp-body").addEventListener("click", async ev => {
    const sort = ev.target.closest("[data-sort]");
    if (sort) { const [id, key] = sort.dataset.sort.split(":"), s = ERP.sort[id] || {}; ERP.sort[id] = { key, dir: s.key === key && s.dir !== "desc" ? "desc" : "asc" }; loadErp(); return; }
    const go = ev.target.closest("[data-go]");
    if (go) { const [tab, state] = go.dataset.go.split(":"); if (tab === "dashboard") { location.hash = "#dashboard"; return; } ERP.tab = tab; ERP.state = state || ""; loadErp(); return; }
    const open = ev.target.closest("[data-erp-open]");
    if (open && !ev.target.closest("a")) { erpPoDialog(open.dataset.erpOpen); return; }
    const nw = ev.target.closest("[data-erp-new]");
    if (nw) { erpNewDialog(nw.dataset.erpNew); return; }
    const retry = ev.target.closest("[data-erp-retry]");
    if (retry) { retry.disabled = true; try { const r = await post(`/api/erp/sync/${retry.dataset.erpRetry}/retry`, {}); toast(`Retried: ${r.status}${r.error ? ` (${r.error})` : ""}`); } catch (e) { toast(e.message); } loadErp(); return; }
    const sync = ev.target.closest("[data-erp-sync]");
    if (sync) { sync.disabled = true; try { await post("/api/erp/sync", { full: sync.dataset.erpSync === "full" }); toast("Synced with Odoo."); } catch (e) { toast(e.message); } loadErp(); }
  });
  $("#erp-body").addEventListener("keydown", ev => { if (ev.key === "Enter" && ev.target.matches("[data-erp-open]")) erpPoDialog(ev.target.dataset.erpOpen); });
}
