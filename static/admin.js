/* The Platform page, for platform administrators only (ADMIN_ACCOUNTS; the server answers 403 to everyone else):
   every account's invoices, folders, purchase orders, vendors and activity, the same data the central sync keeps in Odoo.
   Opening someone's invoice or document is recorded in the access log. Uses app.js's and erp.js's helpers. */
"use strict";

const ADM = { tab: "overview", f: { user_id: "", q: "", folder: "", vendor: "", po: "", approval: "", status: "", payment: "", match: "",
  date_from: "", date_to: "", upload_from: "", upload_to: "" }, users: [], folders: [] };
const ADM_TABS = [["overview", "Overview"], ["users", "Users"], ["invoices", "Invoices"], ["folders", "Folders"], ["pos", "Purchase orders"],
  ["vendors", "Vendors"], ["activity", "Activity"], ["sync", "Sync to Odoo"], ["audit", "Access log"]];
const APPROVAL_PILL = { Approved: "ok", "On hold": "warn", Rejected: "bad", Pending: "pen" };
const when = v => v ? esc(String(v).replace("T", " ").slice(0, 16)) : "–";

function wireAdmin() {
  $("#admin-tabs").innerHTML = ADM_TABS.map(([k, t]) => `<button type="button" role="tab" data-adm-tab="${k}" aria-selected="${k === ADM.tab}">${t}</button>`).join("");
  $("#admin-tabs").addEventListener("click", e => { const b = e.target.closest("[data-adm-tab]"); if (b) { ADM.tab = b.dataset.admTab; loadAdmin(); } });
  $("#admin-body").addEventListener("click", e => {
    const s = e.target.closest("[data-sort]");
    if (s) { const [id, key] = s.dataset.sort.split(":"), cur = ERP.sort[id] || {};
      ERP.sort[id] = { key, dir: cur.key === key && cur.dir !== "desc" ? "desc" : "asc" }; loadAdmin(); return; }
    const o = e.target.closest("[data-erp-open]");
    if (o && ADM.tab === "invoices") { adminInvoice(o.dataset.erpOpen); return; }
    const u = e.target.closest("[data-adm-user]");
    if (u) { ADM.f.user_id = u.dataset.admUser; ADM.tab = "invoices"; loadAdmin(); return; }
    const fo = e.target.closest("[data-adm-folder]");
    if (fo) { Object.assign(ADM.f, { user_id: fo.dataset.admFolderUser, folder: fo.dataset.admFolder }); ADM.tab = "invoices"; loadAdmin(); return; }
    const g = e.target.closest("[data-go]");
    if (g) { const [tab, k, v] = g.dataset.go.split(":"); ADM.tab = tab; if (k) { Object.keys(ADM.f).forEach(x => ADM.f[x] = ""); ADM.f[k] = v; } loadAdmin(); return; }
    if (e.target.closest("[data-adm-sync]")) adminSyncNow();
  });
  $("#admin-body").addEventListener("keydown", e => { const o = e.target.closest("[data-erp-open]"); if (o && e.key === "Enter" && ADM.tab === "invoices") adminInvoice(o.dataset.erpOpen); });
  $("#admin-filters").addEventListener("change", e => { const k = e.target.dataset.af; if (k) { ADM.f[k] = e.target.value; if (k === "user_id") ADM.f.folder = ""; loadAdmin(); } });
  $("#admin-filters").addEventListener("search", e => { if (e.target.dataset.af) { ADM.f[e.target.dataset.af] = e.target.value; loadAdmin(); } });
}

function adminFilters() {
  const box = $("#admin-filters"), f = ADM.f;
  if (ADM.tab !== "invoices" && ADM.tab !== "activity") { box.hidden = true; box.innerHTML = ""; return; }
  const sel = (k, label, opts) => `<label class="f"><span>${label}</span><select data-af="${k}">${opts.map(([v, t]) => `<option value="${esc(v)}"${String(v) === String(f[k]) ? " selected" : ""}>${esc(t)}</option>`).join("")}</select></label>`;
  const users = [["", "Every user"], ...ADM.users.map(u => [u.id, u.username + (u.email && u.email !== u.username ? ` (${u.email})` : "")])];
  if (ADM.tab === "activity") { box.innerHTML = sel("user_id", "User", users); box.hidden = false; return; }
  const folders = [["", "Every folder"], ...ADM.folders.filter(x => !f.user_id || String(x.user_id) === String(f.user_id)).map(x => [x.id, `${x.username} / ${x.name}`])];
  const date = (k, label) => `<label class="f"><span>${label}</span><input type="date" data-af="${k}" value="${esc(f[k])}"></label>`;
  box.innerHTML = sel("user_id", "User", users) + sel("folder", "Folder", folders)
    + sel("approval", "Approval", [["", "Any"], ["Approved", "Approved"], ["On hold", "On hold"], ["Rejected", "Rejected"], ["Pending", "Pending"]])
    + sel("status", "Processing", [["", "Any"], ["Processed", "Processed"], ["Failed", "Failed"], ["Processing", "Processing"], ["Waiting for review", "Waiting for review"]])
    + sel("payment", "Payment", [["", "Any"], ["none", "No Odoo bill"], ["not_paid", "Not paid"], ["partial", "Partly paid"], ["in_payment", "In payment"], ["paid", "Paid"]])
    + sel("match", "Matching", [["", "Any"], ["Matched", "Matched"], ["Mismatched", "Mismatched"], ["PO not found", "PO not found"], ["Partly matched", "Partly matched"], ["No PO", "No PO"]])
    + date("date_from", "Invoice from") + date("date_to", "Invoice to") + date("upload_from", "Uploaded from") + date("upload_to", "Uploaded to")
    + `<label class="f"><span>Vendor</span><input type="search" data-af="vendor" value="${esc(f.vendor)}" placeholder="Name"></label>`
    + `<label class="f"><span>PO</span><input type="search" data-af="po" value="${esc(f.po)}" placeholder="Number"></label>`
    + `<input type="search" class="f-search" data-af="q" placeholder="Invoice no., file, user, email" aria-label="Search" value="${esc(f.q)}">`;
  box.hidden = false;
}

async function loadAdmin() {
  if (!(S.cfg && S.cfg.admin)) { location.hash = "#process"; return; }
  $$("#admin-tabs [data-adm-tab]").forEach(b => b.setAttribute("aria-selected", b.dataset.admTab === ADM.tab));
  const body = $("#admin-body");
  try {
    if (!ADM.users.length || ADM.tab === "users") ADM.users = (await api("/api/admin/users")).users;
    if (!ADM.folders.length || ADM.tab === "folders") ADM.folders = (await api("/api/admin/folders")).users.flatMap(u => u.folders.map(x => ({ ...x, user_id: u.user_id, username: u.username })));
    adminFilters();
    await ({ overview: admOverview, users: admUsers, invoices: admInvoices, folders: admFolders, pos: admPos, vendors: admVendors, activity: admActivity,
      sync: admSync, audit: admAudit }[ADM.tab])(body);
  } catch (e) { body.innerHTML = `<p class="auth-error">${esc(e.message)}</p>`; }
}

async function admOverview(body) {
  const o = await api("/api/admin/overview"), u = o.users, i = o.invoices, m = o.money, s = o.sync;
  $("#admin-sub").innerHTML = `All ${u.total} accounts. Each account still sees only its own data; this page is for platform administrators.`
    + (o.odoo ? ` The same data is in ${extLink(o.odoo.platform_menu, "<b>Odoo</b>")} under <b>Invoice Flow Platform</b>.` : "");
  body.innerHTML = `
    <section class="erp-group"><div class="card-head"><h2>Users</h2></div>
      ${tiles([["Registered", u.total, `${u.admins} admin${u.admins === 1 ? "" : "s"}`, "users"], ["Active", u.active_30d, "logged in, last 30 days"], ["Folders", o.folders, "", "folders"]])}</section>
    <section class="erp-group"><div class="card-head"><h2>Invoices</h2><span class="card-note">Every account</span></div>
      ${tiles([["Total", i.total, `${i.processed} processed`, "invoices"], ["Pending", i.pending, "processing or on hold", "invoices:approval:On hold"],
        ["Approved", i.approved, "", "invoices:approval:Approved"], ["Rejected", i.rejected, "", "invoices:approval:Rejected"], ["Matched", i.matched, `${i.unmatched} not matched`, "invoices:match:Matched"],
        ["Failed processing", i.failed, "", "invoices:status:Failed"], ["Duplicate alerts", i.duplicates], ["Odoo bills", i.bills_draft + i.bills_posted, `${i.bills_draft} draft · ${i.bills_posted} posted`]])}</section>
    <section class="erp-group"><div class="card-head"><h2>Money</h2></div>
      ${tiles([["Invoice value", inr0(m.invoice_value)], ["Approved", inr0(m.approved_value)], ["On hold", inr0(m.held_value)], ["Paid (Odoo)", inr0(m.paid_value)],
        ["PO value", inr0(m.po_value), `${o.pos.total} purchase orders`, "pos"]])}</section>
    <section class="erp-group"><div class="card-head"><h2>Sync to Odoo</h2></div>
      ${tiles([["State", s.enabled ? (s.last_error ? "Needs a look" : "On") : "Off", s.enabled ? `last round ${when(s.last_round)}` : "CENTRAL_SYNC=1 switches it on", "sync"],
        ["In sync", s.synced], ["Waiting", s.pending], ["Failed", s.failed, "", "sync"], ["API failures", s.api_failures_24h, "last 24 h"]])}</section>`;
}

async function admUsers(body) {
  $("#admin-sub").textContent = `${ADM.users.length} accounts. Click one to see its invoices.`;
  body.innerHTML = erpTable("adm-users", [
    { key: "username", label: "User", html: r => `<button type="button" class="link" data-adm-user="${r.id}"><b>${esc(r.username)}</b></button>${r.role !== "User" ? " " + pill(r.role, "ok") : ""}` },
    { key: "email", label: "Email", html: r => esc(r.email || "–") }, { key: "login_method", label: "Signs in with" },
    { key: "created_at", label: "Registered", html: r => when(r.created_at) }, { key: "last_login_at", label: "Last login", html: r => when(r.last_login_at) },
    { key: "invoices", label: "Invoices", cls: "right" }, { key: "folders", label: "Folders", cls: "right" }, { key: "pos", label: "POs", cls: "right" },
    { key: "pending", label: "Pending", cls: "right" }, { key: "failed", label: "Failed", cls: "right" }, { key: "approved", label: "Approved", cls: "right" },
    { key: "rejected", label: "Rejected", cls: "right" }, { key: "value", label: "Value", cls: "right", html: r => inr0(r.value) },
    { key: "odoo_connected", label: "Odoo bills", html: r => r.odoo_connected ? pill("Connected", "ok") : "–" }], ADM.users, "No accounts yet.");
}

async function admInvoices(body) {
  const qs = new URLSearchParams(Object.entries(ADM.f).filter(([, v]) => v !== "")).toString();
  const d = await api(`/api/admin/invoices?${qs}`);
  $("#admin-sub").textContent = `${d.invoices.length} of ${d.total} invoices across every account. Click one for its details and document.`;
  body.innerHTML = erpTable("adm-inv", [
    { key: "uploaded_at", label: "Uploaded", html: r => when(r.uploaded_at) }, { key: "username", label: "User", html: r => esc(r.username) },
    { key: "folder", label: "Folder", html: r => esc(r.folder || "–") }, { key: "filename", label: "File", html: r => esc(r.filename || "") },
    { key: "vendor", label: "Vendor", html: r => esc(r.vendor || "–") }, { key: "invoice_number", label: "Invoice no.", html: r => esc(r.invoice_number || "–") },
    { key: "invoice_date", label: "Date", html: r => esc(r.invoice_date || "–") }, { key: "po_number", label: "PO", html: r => esc(r.po_number || "–") },
    { key: "total", label: "Total", cls: "right", html: r => r.total == null ? "–" : inr(r.total) },
    { key: "approval", label: "Approval", html: r => pill(r.approval, APPROVAL_PILL[r.approval] || "pen") + (r.duplicate ? " " + pill("Duplicate", "bad") : "") },
    { key: "status", label: "Processing", html: r => r.status === "Failed" ? pill("Failed", "bad") : esc(r.status) }, { key: "match", label: "Matching" },
    { key: "bill_id", label: "Odoo bill", html: r => r.bill_id ? `#${r.bill_id} ${esc(r.payment || r.bill_state || "")}` : "–" }],
    d.invoices.map(r => ({ ...r, _click: r.id })), "No invoices match.");
}

async function adminInvoice(id) {
  const r = await api(`/api/admin/invoices/${encodeURIComponent(id)}`), res = r.result || {}, f = res.fields || {};
  const dlg = $("#admin-dialog");
  dlg.innerHTML = `<div class="modal-head"><h2>${esc(r.invoice_number || r.filename)}</h2><button class="icon-btn" type="button" data-close aria-label="Close">✕</button></div>
    <p class="muted">Owned by <b>${esc(r.username)}</b>${r.folder ? ` · folder <b>${esc(r.folder)}</b>` : " · not in a folder"} · uploaded ${when(r.uploaded_at)} · ${esc(r.id)}.
      Opening this is recorded in the access log.</p>
    <div class="kv">${[["Approval", pill(r.approval, APPROVAL_PILL[r.approval] || "pen")], ["Decided by", esc(r.decided_by || "–")], ["Reason", esc(r.reason || "–")],
      ["Processing", esc(r.status)], ["Matching", esc(r.match)], ["Vendor", esc(r.vendor || "–") + (r.gstin ? ` · ${esc(r.gstin)}` : "")],
      ["Invoice date", esc(r.invoice_date || "–")], ["PO", esc(r.po_number || "–")], ["Net / total", `${r.net == null ? "–" : inr(r.net)} / ${r.total == null ? "–" : inr(r.total)}`],
      ["Read by", esc(r.extractor || "–")], ["Odoo bill", r.bill_id ? (r.odoo_bill_url ? extLink(r.odoo_bill_url, `#${r.bill_id}`) : `#${r.bill_id}`) + ` ${esc(r.payment || r.bill_state || "")}` : "–"]]
      .map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("")}</div>
    <p><a class="btn" href="/api/admin/invoices/${encodeURIComponent(r.id)}/file" target="_blank" rel="noopener">Open the original document ↗</a></p>
    ${res.headline ? `<h3>Checks</h3><p>${esc(res.headline)}</p><ul>${(res.findings || []).map(x => `<li>${pill(x.severity, x.severity === "reject" ? "bad" : x.severity === "hold" ? "warn" : "pen")} ${esc(x.message)}</li>`).join("")}</ul>` : ""}
    <h3>Extracted fields</h3><div class="kv">${Object.entries(f).filter(([, v]) => v != null && v !== "" && typeof v !== "object").map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("") || "<p class='muted'>None</p>"}</div>
    <h3>Activity</h3>${r.activity.length ? `<ul>${r.activity.map(a => `<li>${when(a.decided_at || a.uploaded_at)} · ${esc(a.username)} (${esc(a.session_id)}) · ${esc(a.decision || "uploaded")} by ${esc(a.decided_by || "–")}</li>`).join("")}</ul>` : "<p class='muted'>None recorded</p>"}`;
  dlg.querySelector("[data-close]").addEventListener("click", () => dlg.close());
  dlg.showModal();
}

async function admFolders(body) {
  const d = await api("/api/admin/folders");
  $("#admin-sub").textContent = "Every account's folders, as its owner keeps them. Click a folder to see its invoices.";
  body.innerHTML = d.users.map(u => `<section class="erp-group adm-tree"><div class="card-head"><h2><button type="button" class="link" data-adm-user="${u.user_id}">${esc(u.username)}</button></h2>
      <span class="card-note">${u.invoices} invoice${u.invoices === 1 ? "" : "s"} · ${u.folders.length} folder${u.folders.length === 1 ? "" : "s"} · ${u.unfiled} not in a folder</span></div>
      ${u.folders.length ? `<ul class="adm-folders">${u.folders.map(f => `<li><button type="button" class="link" data-adm-folder="${f.id}" data-adm-folder-user="${u.user_id}">📁 <b>${esc(f.name)}</b></button>
        <span class="muted">${f.invoices.length} invoice${f.invoices.length === 1 ? "" : "s"}</span>
        ${f.invoices.length ? `<ul>${f.invoices.map(i => `<li>${esc(i.name)} ${pill(i.approval, APPROVAL_PILL[i.approval] || "pen")} ${i.total == null ? "" : inr(i.total)}</li>`).join("")}</ul>` : ""}</li>`).join("")}</ul>`
        : "<p class='muted'>No folders.</p>"}</section>`).join("") || "<p class='muted'>No accounts yet.</p>";
}

async function admPos(body) {
  const d = await api("/api/admin/pos");
  $("#admin-sub").textContent = `${d.pos.length} purchase orders: the shared register, each account's own, and the Odoo POs accounts raised here.`;
  body.innerHTML = erpTable("adm-pos", [
    { key: "po_number", label: "PO", html: r => `<b>${esc(r.po_number)}</b>${r.odoo_url ? " " + extLink(r.odoo_url, "") : ""}` }, { key: "owner", label: "Owner", html: r => esc(r.owner || "shared") },
    { key: "source", label: "Kept in" }, { key: "vendor", label: "Vendor", html: r => esc(r.vendor || "–") }, { key: "po_date", label: "Date", html: r => esc(r.po_date || "–") },
    { key: "status", label: "Status", html: r => esc(r.status || "–") }, { key: "receipt", label: "Receipt", html: r => esc(r.receipt || "–") },
    { key: "total", label: "Total", cls: "right", html: r => inr(r.total || 0) }, { key: "billed", label: "Billed (net)", cls: "right", html: r => inr(r.billed || 0) },
    { key: "remaining", label: "Remaining", cls: "right", html: r => inr(r.remaining || 0) }], d.pos, "No purchase orders.");
}

async function admVendors(body) {
  const d = await api("/api/admin/vendors");
  $("#admin-sub").textContent = `${d.vendors.length} vendors: the shared master list and every account's approved vendors.`;
  body.innerHTML = erpTable("adm-vendors", [
    { key: "name", label: "Vendor", html: r => `<b>${esc(r.name)}</b>${r.odoo_url ? " " + extLink(r.odoo_url, "") : ""}` }, { key: "vendor_id", label: "ID" },
    { key: "owner", label: "Approved by", html: r => esc(r.owner || "shared list") }, { key: "gstin", label: "GSTIN", html: r => esc(r.gstin || "–") },
    { key: "status", label: "Status", html: r => pill(r.status, /block/i.test(r.status) ? "bad" : "ok") }, { key: "approved_at", label: "Approved", html: r => when(r.approved_at) },
    { key: "link", label: "In Odoo", html: r => esc(r.link || "–") }], d.vendors, "No vendors.");
}

async function admActivity(body) {
  const qs = ADM.f.user_id ? `?user_id=${encodeURIComponent(ADM.f.user_id)}` : "";
  const d = await api(`/api/admin/activity${qs}`);
  $("#admin-sub").innerHTML = `${d.rows.length} rows: one per invoice per login session. <a href="/api/admin/activity.csv${qs}">Download CSV</a>`;
  body.innerHTML = erpTable("adm-act", [
    { key: "decided_at", label: "When", get: r => r.decided_at || r.uploaded_at, html: r => when(r.decided_at || r.uploaded_at) }, { key: "username", label: "User" },
    { key: "session_id", label: "Session" }, { key: "filename", label: "File", html: r => esc(r.filename || "–") }, { key: "invoice_number", label: "Invoice no.", html: r => esc(r.invoice_number || "–") },
    { key: "decision", label: "Decision", html: r => esc(r.decision || "uploaded") }, { key: "decided_by", label: "By", html: r => esc(r.decided_by || "–") },
    { key: "reason", label: "Reason", html: r => esc(r.reason || "–") }, { key: "sync_status", label: "Odoo", html: r => esc(r.sync_status || "–") }], d.rows, "No activity yet.");
}

async function admSync(body) {
  const s = await api("/api/admin/sync");
  $("#admin-sub").textContent = s.enabled ? `Server "${s.site}" keeps Odoo's Invoice Flow Platform menu in step: on every change, and a full check every 5 minutes.`
    : "The central sync to Odoo is off on this server (CENTRAL_SYNC=1 switches it on).";
  body.innerHTML = `<div class="erp-actions">${s.enabled ? `<button class="btn primary" type="button" data-adm-sync>Sync now</button>` : ""}
      <span class="muted">Last round ${when(s.last_round)} · last success ${when(s.last_ok)}${s.waiting_until ? " · waiting for Odoo, retrying by itself" : ""}</span></div>
    ${tiles([["In sync", s.synced], ["Waiting", s.pending], ["Failed", s.failed], ["Archived", s.archived, "deleted in Invoice Flow, kept in Odoo"], ["API failures", s.api_failures_24h, "last 24 h"],
      ["ERP writes waiting", s.queue.pending], ["ERP writes failed", s.queue.failed], ["Processing errors", s.processing_errors]])}
    ${s.last_error ? `<p class="auth-error">${esc(s.last_error)}</p>` : ""}
    ${erpTable("adm-sync", [{ key: "kind", label: "Record" }, { key: "id", label: "ID" }, { key: "attempts", label: "Tries", cls: "right" }, { key: "error", label: "Error" }],
      s.errors, "Nothing failed.")}`;
}

async function adminSyncNow() {
  try { await api("/api/admin/sync", { method: "POST" }); toast("Sync started"); setTimeout(() => { if (location.hash === "#admin" && ADM.tab === "sync") loadAdmin(); }, 4000); }
  catch (e) { toast(e.message); }
}

async function admAudit(body) {
  const d = await api("/api/admin/audit");
  $("#admin-sub").textContent = "What platform administrators opened of other people's data.";
  body.innerHTML = erpTable("adm-audit", [{ key: "at", label: "When", html: r => when(r.at) }, { key: "admin", label: "Administrator" }, { key: "action", label: "Did" },
    { key: "target", label: "What" }, { key: "owner", label: "Whose", html: r => esc(r.owner || "–") }], d.rows, "Nothing opened yet.");
}
