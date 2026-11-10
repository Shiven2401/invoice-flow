"use strict";
/* Invoice Flow front end. No framework, no build step: one file, easy to read in an interview. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = n => n == null ? "–" : new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2 }).format(n);
const inr0 = n => new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(n || 0);
const pct = (n, d = 1) => `${(+n).toFixed(d).replace(/\.0+$/, "")}%`;

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (r.status === 401) { location.reload(); throw new Error("Please log in."); }  // session ended: back to the sign-in page
  if (!r.ok) {
    let m = r.statusText;
    try { m = (await r.json()).detail || m; } catch (e) { /* not json */ }
    // the app's own "not found" replies say what is missing; a bare one means the route doesn't exist on the server
    if (r.status === 404 && m === "Not Found") m = "The server is running an older version of the app. Restart it (Ctrl+C, then python run.py) and try again.";
    throw new Error(m);
  }
  return r.json();
}

const DEC = { APPROVE: { label: "Approved", cls: "ok" }, HOLD: { label: "Held", cls: "warn" }, REJECT: { label: "Rejected", cls: "bad" } };
const MARK = { ok: "✓", warn: "!", fail: "✕", skipped: "–", pending: "", running: "" };
const SEV_MARK = { reject: "✕", hold: "!", warn: "i", info: "✓" };
const STAGES = [
  ["ingest", "Receive document"], ["read", "Read document"], ["extract", "Extract fields"], ["validate", "Validate and reconcile"],
  ["vendor", "Verify vendor"], ["po", "Match purchase order"], ["duplicates", "Check for duplicates"],
  ["budget", "PO balance and tolerance"], ["decision", "Decide"],
];
const CODE_LABEL = {};  // filled from /api/config: the Excel report uses the same wording

const S = { cfg: null, scenarios: [], runs: new Map(), queue: [], streaming: false, current: null, follow: true, filter: "ALL", history: [], me: "",
  folders: [], view: { group: "month", kind: "all", pick: "", search: "", folder: null }, collapsed: new Set() };

/* ----------------------------------------------------------------- folders -- */
/* same drawing style as the sidebar's other icons: 24px grid, 1.8 stroke, round ends */
const ICON = paths => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
const FOLDER_PATH = `<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h3.6l2 2.5h7.4A1.5 1.5 0 0 1 20 8v10a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18z"/>`;
const FOLDER_ICON = ICON(FOLDER_PATH);
const FOLDER_PLUS = ICON(`${FOLDER_PATH}<path d="M12 10.5v5.5M9.25 13.25h5.5"/>`);
const RESET_ICON = ICON(`<path d="M4 12a8 8 0 1 0 2.5-5.8L4 8.5"/><path d="M4 4v4.5h4.5"/>`);
const REMOVE_ICON = ICON(`<path d="M7 7l10 10M17 7 7 17"/>`);
const RESET_TITLE = "Reset: delete the invoices in this folder";

async function loadFolders() {
  try { S.folders = (await api("/api/folders")).folders; } catch (e) { S.folders = []; }
  if (S.view.folder != null && !S.folders.some(f => f.id === S.view.folder)) S.view.folder = null;
  renderFolders();
}

function renderFolders() {
  const onDashboard = (location.hash || "").startsWith("#dashboard");
  $("#folders").innerHTML = S.folders.length ? S.folders.map(f => `<li>
      <button class="folder" type="button" data-folder-open="${f.id}" ${onDashboard && S.view.folder === f.id ? 'aria-current="page"' : ""}>
        ${FOLDER_ICON}<span class="fname">${esc(f.name)}</span><span class="fcount">${f.count}</span></button>
      <button class="icon-btn folder-reset" type="button" data-folder-reset="${f.id}" aria-label="Reset ${esc(f.name)}" title="${RESET_TITLE}">${RESET_ICON}</button></li>`).join("")
    : `<li class="folders-empty">Save an invoice to a folder at its Decision step, or create one here.</li>`;
  $("#folder-pick").innerHTML = `<option value="">All invoices</option>${S.folders.map(f => `<option value="${f.id}">${esc(f.name)}</option>`).join("")}`;
  $("#folder-pick").value = S.view.folder ?? "";
  updateFilterCount();
}

async function createFolder(name) {
  const f = await api("/api/folders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
  await loadFolders();
  return f.id;
}

async function resetFolder(button) {                       // two clicks: it deletes invoices
  if (!button.classList.contains("armed")) {
    button.classList.add("armed");
    button.title = "Click again to delete this folder's invoices";
    button.dataset.timer = setTimeout(() => { button.classList.remove("armed"); button.title = RESET_TITLE; }, 3500);
    return;
  }
  clearTimeout(+button.dataset.timer);
  try {
    const { removed } = await api(`/api/folders/${button.dataset.folderReset}/reset`, { method: "POST" });
    removed.forEach(id => S.runs.delete(id));
    if (VIEW && removed.includes(VIEW.id)) { S.current = null; VIEW = null; renderEmpty(); }
    await loadFolders();
    refreshVisible();
  } catch (e) { alert(e.message); }
}

/* One popup for choosing a folder: every folder so far, and Create new folder.
   pick(id) does the work (file an invoice, open a folder's dashboard); null means "not in a folder". */
function folderDialog({ title, current = null, allowNone = false, pick }) {
  const dlg = $("#folder-dialog");
  let creating = false, error = "";
  const draw = () => {
    dlg.innerHTML = `<div class="modal-body">
      <div class="modal-head">${FOLDER_ICON}<h2>${esc(title)}</h2><button class="icon-btn" type="button" data-close aria-label="Close">${REMOVE_ICON}</button></div>
      <div class="modal-list">${S.folders.length ? S.folders.map(f => `<button class="modal-folder${f.id === current ? " on" : ""}" type="button" data-pick="${f.id}">
          ${FOLDER_ICON}<span>${esc(f.name)}</span><small>${f.count} invoice${f.count === 1 ? "" : "s"}</small>${f.id === current ? "<b>✓</b>" : ""}</button>`).join("")
        : `<p class="modal-empty">No folders yet. Create your first one below.</p>`}</div>
      ${creating ? `<form class="modal-new"><input type="text" maxlength="60" placeholder="Folder name, any name you like" aria-label="New folder name" required>
          <button class="btn primary" type="submit">Create</button></form>`
        : `<button class="modal-create" type="button" data-create>${FOLDER_PLUS}<span>Create new folder</span></button>`}
      ${allowNone && current != null ? `<button class="modal-out" type="button" data-pick="">Take it out of ${esc(folderName(current))}</button>` : ""}
      <p class="auth-error" ${error ? "" : "hidden"}>${esc(error)}</p></div>`;
    const input = $("input", dlg);
    if (input) input.focus();
  };
  const choose = async id => {
    try { await pick(id); dlg.close(); } catch (e) { error = e.message; draw(); }
  };
  dlg.onclick = ev => {
    if (ev.target === dlg || ev.target.closest("[data-close]")) dlg.close();   // the dark area outside closes it too
    else if (ev.target.closest("[data-create]")) { creating = true; error = ""; draw(); }
    else { const b = ev.target.closest("[data-pick]"); if (b) choose(b.dataset.pick ? +b.dataset.pick : null); }
  };
  dlg.onsubmit = async ev => {
    ev.preventDefault();
    try { const id = await createFolder($("input", dlg).value); creating = false; await choose(id); } catch (e) { error = e.message; draw(); }
  };
  draw();
  dlg.showModal();
}

let toastTimer = null;
function toast(text, undo) {
  const box = $("#toast");
  $("span", box).textContent = text;
  $("button", box).hidden = !undo;
  $("button", box).onclick = async () => { box.hidden = true; await undo(); };
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 6000);
}

/* the ✕ on a History row deletes the invoice for good: record, file and PO posting, so the same file sent again is
   checked as new. It can't be undone, so it takes two clicks, like a folder reset */
const DELETE_TITLE = "Delete this invoice permanently";
async function deleteRun(button) {
  if (!button.classList.contains("armed")) {
    button.classList.add("armed");
    button.title = "Click again to delete it for good";
    setTimeout(() => { button.classList.remove("armed"); button.title = DELETE_TITLE; }, 3500);
    return;
  }
  const id = button.dataset.remove;
  try {
    await api(`/api/runs/${id}`, { method: "DELETE" });
    S.runs.delete(id);
    if (VIEW && VIEW.id === id) { S.current = null; VIEW = null; renderEmpty(); }
    toast("Invoice deleted. If the same file is uploaded again, it will be checked as a new invoice.");
    await loadFolders();
    loadDashboard();
  } catch (e) { alert(e.message); }
}

function openFolder(id) {
  S.view.folder = id;
  if (location.hash === "#dashboard") { renderFolders(); refreshVisible(); } else location.hash = "#dashboard";
}

/* ------------------------------------------------------------- helpers -- */
const eff = r => r.final_decision || r.decision;
const kv = rows => `<table class="kv"><tbody>${rows.filter(Boolean).map(([k, v]) => `<tr><td>${k}</td><td class="val">${v}</td></tr>`).join("")}</tbody></table>`;
const none = t => `<span class="pill warn">${t || "not found"}</span>`;
const val = (v, f) => v == null || v === "" ? none() : esc(f ? f(v) : v);

/* ---------------------------------------------------- stage detail views -- */
const METHOD = { text_layer: "Machine-readable text", ocr: "Scanned image, read with OCR", unreadable: "No readable text" };

const DETAIL = {
  ingest: d => kv([["File", esc(d.filename)], ["Type", esc(d.type)], ["Size", `${d.kb} KB`], ["Pages", d.pages], ["Fingerprint", `<span class="muted">${esc(d.sha256)}</span>`]]),

  read: d => `<p><span class="pill ${d.method === "text_layer" ? "ok" : d.method === "ocr" ? "pen" : "bad"}">${METHOD[d.method]}</span>
    <span class="muted">${d.chars} characters, ${d.pages} page${d.pages !== 1 ? "s" : ""}</span></p>
    ${(d.notes || []).map(n => `<p class="muted">${esc(n)}</p>`).join("")}
    ${d.preview ? `<div><h4>Text recovered</h4><pre class="ocr">${esc(d.preview)}</pre></div>` : ""}`,

  extract: d => {
    const f = d.fields, c = d.confidence || {}, g = d.grounding || {};
    const row = (label, key, fmt, gk) => {
      const v = f[key];
      const conf = c[key] === "user" ? ` <span class="pill pen">typed in</span>` : c[key] ? `<span class="conf ${c[key]}" title="Confidence: ${c[key]}"></span>` : "";
      const ground = g[gk || key] === true ? `<span class="tick" title="Found in the document text">✓</span>` : g[gk || key] === false ? `<span class="tick no" title="Not found in the document text">✕</span>` : "";
      return [label, `${val(v, fmt)}${v != null ? conf + ground : ""}`];
    };
    const items = d.line_items || [];
    return `${kv([
      row("Vendor", "vendor_name"), row("GSTIN", "vendor_gstin"), row("Invoice number", "invoice_number"), row("Invoice date", "invoice_date"),
      row("PO reference", "po_reference"), ["Document type", esc(f.doc_type)],
      f.discount ? ["Discount", esc(inr(f.discount))] : null,
      f.subtotal != null ? row(f.discount ? "Subtotal after discount" : "Subtotal", "subtotal", inr) : null,
      f.tax_amount != null ? row("Tax", "tax_amount", x => inr(x) + (f.tax_rate ? ` at ${f.tax_rate}%` : "")) : null,
      f.tax_included ? ["Tax", `<span class="pill pen">included in the amounts${f.tax_rate ? ` (${f.tax_rate}% GST)` : ""}</span>`] : null,
      row("Total", "total", inr)])}
    ${items.length ? `<div><h4>Line items</h4><table><thead><tr><th>Description</th><th class="right">Qty</th><th class="right">Rate</th><th class="right">Amount</th></tr></thead><tbody>
      ${items.map(i => `<tr><td>${esc(i.description)}</td><td class="right">${i.quantity ?? "–"}</td><td class="right">${i.unit_price != null ? inr(i.unit_price) : "–"}</td><td class="right">${inr(i.amount)}</td></tr>`).join("")}</tbody></table></div>` : ""}
    <p class="muted">Read by ${esc(d.extractor)}. A tick means the value appears word-for-word in the document.</p>
    ${(d.notes || []).filter(n => /fell back|cannot/.test(n)).map(n => `<p><span class="pill warn">${esc(n)}</span></p>`).join("")}`;
  },

  validate: d => `<ul class="checks">${d.checks.map(c => `<li><span class="c ${c.ok ? "" : "no"}">${c.ok ? "✓" : "✕"}</span><span>${esc(c.name)}<small>${esc(c.detail)}</small></span></li>`).join("")}</ul>
    ${d.derived && d.derived.net != null ? `<div><h4>Amount compared with the PO</h4>${kv([
      ["Pre-tax amount", inr(d.derived.net)], d.derived.tax != null ? ["Tax", inr(d.derived.tax)] : null,
      d.derived.rate != null ? ["GST rate", pct(d.derived.rate)] : null, ["How it was worked out", esc(d.derived.basis)]])}</div>` : ""}`,

  vendor: d => `${d.vendor ? kv([["Matched to", `${esc(d.vendor.name)} <span class="muted">${esc(d.vendor.vendor_id)}</span>`], ["Matched by", esc(d.method)],
      ["Name similarity", pct(d.name_score * 100, 0)], ["GSTIN on invoice", `${val(d.gstin_on_invoice)} ${d.gstin_on_invoice ? (d.gstin_valid ? '<span class="tick">✓</span>' : '<span class="tick no" title="Not a valid GSTIN format">✕</span>') : ""}`],
      ["Vendor status", esc(d.vendor.status)]]) : `<p>No approved vendor matches. GSTIN on invoice: ${val(d.gstin_on_invoice)}</p>`}
    ${d.candidates && d.candidates.length && d.status !== "approved" ? `<div><h4>Closest approved vendors</h4><table><tbody>${d.candidates.map(c => `<tr><td>${esc(c.name)}</td><td class="right">${pct(c.score * 100, 0)}</td></tr>`).join("")}</tbody></table></div>` : ""}`,

  po: d => `${kv([["Reference on invoice", val(d.reference, x => x) === none() ? none("none printed") : esc(d.reference || "") || none("none printed")],
      d.po ? ["Matched PO", `${esc(d.po.po_number)} <span class="muted">${esc(d.po.description)}</span>`] : ["Matched PO", none("no match")],
      ["Match type", `<span class="pill ${d.status === "explicit" ? "ok" : d.status === "implied" ? "pen" : "warn"}">${esc({ explicit: "explicit reference", implied: "inferred", ambiguous: "ambiguous", ref_not_found: "reference not in register", vendor_mismatch: "wrong vendor", none: "none" }[d.status] || d.status)}</span>`],
      d.po ? ["PO value (pre-tax)", inr(d.po.net_amount)] : null, d.po ? ["PO date", esc(d.po.po_date)] : null])}
    ${d.candidates && d.candidates.length ? `<div><h4>${d.status === "implied" ? "How the PO was inferred" : "Closest open POs"}</h4><table><thead><tr><th>PO</th><th class="right">Open balance</th><th class="right">Amount fit</th><th class="right">Description</th><th class="right">Score</th></tr></thead><tbody>
      ${d.candidates.map(c => `<tr><td>${esc(c.po_number)}<br><span class="muted">${esc(c.description)}</span></td><td class="right">${inr(c.remaining)}</td><td class="right">${pct(c.amount_score * 100, 0)}</td><td class="right">${pct(c.desc_score * 100, 0)}</td><td class="right"><b>${pct(c.score * 100, 0)}</b></td></tr>`).join("")}</tbody></table></div>` : ""}`,

  duplicates: d => d.matches.length
    ? `<table><thead><tr><th>Earlier submission</th><th>Why it matches</th></tr></thead><tbody>${d.matches.map(m => `<tr><td>${esc(m.filename)}<br><span class="muted">run ${esc(m.run_id)}</span></td><td>${esc(m.reason)}</td></tr>`).join("")}</tbody></table>`
    : `<p>Compared with ${d.checked_against} earlier submission${d.checked_against !== 1 ? "s" : ""} on file fingerprint, vendor and invoice number, and vendor with amount and date. No match.</p>`,

  budget: d => {
    const max = Math.max(d.po_net * 1.12, d.cumulative * 1.04), p = x => (x / max * 100).toFixed(2) + "%";
    const cls = { hold: "warn", reject: "bad" }[d.band] || "ok";
    return `<div class="bar" role="img" aria-label="Billing against the PO: ${pct(d.pct_billed)} of the PO value billed with this invoice">
        <i class="before" style="left:0;width:${p(d.billed_before)}"></i>
        <i class="this ${cls}" style="left:${p(d.billed_before)};width:${p(d.this_net)}"></i>
        <i class="tol" style="left:${p(d.po_net)};width:${p(d.po_net * d.tolerance_pct / 100)}"></i>
        <span class="po-line" style="left:${p(d.po_net)}"><span>PO value</span></span></div>
      <div class="legend"><span><b style="background:#C0B6FA"></b>Approved earlier</span><span><b style="background:var(--${cls === "ok" ? "ok" : cls === "warn" ? "warn" : "bad"})"></b>This invoice</span><span><b style="background:repeating-linear-gradient(45deg,#fff 0 2px,#7b8794 2px 3px)"></b>${d.tolerance_pct}% tolerance</span></div>
      ${kv([["PO value (pre-tax)", inr(d.po_net)], ["Approved so far", inr(d.billed_before)], ["This invoice (pre-tax)", inr(d.this_net)],
        ["Billed with this invoice", `${inr(d.cumulative)} <span class="muted">(${pct(d.pct_billed)} of the PO)</span>`],
        d.over_amount > 0 ? ["Over the PO", `<b>${inr(d.over_amount)} (${pct(d.over_pct)})</b>`] : ["Still open on the PO", inr(d.remaining_after)],
        ["Rule", `<span class="muted">up to ${d.tolerance_pct}% over is approved; ${d.tolerance_pct}% to ${d.hold_band_pct}% goes to a person; beyond ${d.hold_band_pct}% is rejected</span>`]])}`;
  },
};

/* ------------------------------------------------------------- run view -- */
class RunView {
  constructor(root, id, filename) {
    this.root = root; this.id = id; this.filename = filename; this.t = {}; this.expandAll = false;
    root.innerHTML = `
      <div class="run-head"><h1>${esc(filename)}</h1><span class="pill pen" data-role="state">Queued</span></div>
      <div class="run-sub" data-role="sub"><span>Run ${esc(id)}</span></div>
      <div class="run-tools"><button class="btn quiet" data-role="toggle" type="button">Expand all steps</button></div>
      <ol class="trail">${STAGES.map(([sid, t]) => `
        <li class="stage" data-stage="${sid}" data-status="pending"><div class="mark" aria-hidden="true"></div>
          <div><button class="head" type="button" aria-expanded="false" disabled><span class="name">${t}</span><span class="time"></span><span class="sum"></span></button>
          <div class="detail" hidden></div></div></li>`).join("")}</ol>
      <div data-role="verdict"></div>`;
    $("[data-role=toggle]", root).addEventListener("click", e => {
      this.expandAll = !this.expandAll;
      e.target.textContent = this.expandAll ? "Collapse all steps" : "Expand all steps";
      $$(".stage", root).forEach(li => { const b = $(".head", li); if (!b.disabled) this.open(li, this.expandAll); });
    });
    root.addEventListener("click", e => {
      const b = e.target.closest("button.head");
      if (b && !b.disabled) this.open(b.closest(".stage"), b.getAttribute("aria-expanded") !== "true");
    });
  }

  open(li, yes) { $(".head", li).setAttribute("aria-expanded", yes); $(".detail", li).hidden = !yes; }
  state(text, cls = "pen") { const s = $("[data-role=state]", this.root); s.textContent = text; s.className = `pill ${cls}`; }

  apply(ev) {
    const li = $(`[data-stage="${ev.stage}"]`, this.root);
    if (!li) return;
    if (ev.status === "running") {
      this.t[ev.stage] = ev.t;
      li.dataset.status = "running";
      $(".sum", li).textContent = "Working";
      this.state("Running");
      return;
    }
    li.dataset.status = ev.status;
    $(".mark", li).textContent = MARK[ev.status] || "";
    $(".sum", li).textContent = ev.summary || "";
    const dt = this.t[ev.stage] != null ? ev.t - this.t[ev.stage] : null;
    $(".time", li).textContent = dt != null ? `${dt.toFixed(1)}s` : "";
    if (ev.stage === "decision") {
      const d = DEC[ev.detail.decision];
      this.state(d ? d.label : "Done", d ? d.cls : "pen");
      this.loadVerdict();
      return;
    }
    const fn = DETAIL[ev.stage];
    if (fn && ev.status !== "skipped" && ev.detail) {
      $(".detail", li).innerHTML = fn(ev.detail);
      $(".head", li).disabled = false;
      this.open(li, this.expandAll || ev.stage === "extract");
    } else if (ev.status === "skipped") {
      $(".sum", li).textContent = ev.summary;
    }
  }

  async loadVerdict() {
    try { this.renderVerdict(await api(`/api/runs/${this.id}`)); } catch (e) { /* run view stays usable */ }
  }

  renderVerdict(run) {
    const res = run.result || {}, D = DEC[eff(run)] || DEC.HOLD;
    const v = $("[data-role=verdict]", this.root);
    v.innerHTML = `<div class="verdict">
      <div class="verdict-top"><div class="stamp ${D.cls}" role="status">${D.label}</div><p class="headline">${esc(res.headline || "")}</p></div>
      ${verdictBody(run)}${recheckPanels(run)}
      ${run.decision === "HOLD" && !run.final_decision ? `<div class="resolve"><p>A person decides this one</p>${learnHint(run)}<div class="row">
        <input type="text" aria-label="Note for the audit trail" placeholder="Add a note for the audit trail (optional)" data-role="note">
        <button class="btn ok" type="button" data-resolve="APPROVE">Approve anyway</button><button class="btn bad" type="button" data-resolve="REJECT">Reject</button></div></div>` : ""}
    </div>`;
    wireVerdict(v, this.id, run);
    $$("[data-resolve]", v).forEach(b => b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        await resolve(this.id, b.dataset.resolve, $("[data-role=note]", v).value);
        await this.loadVerdict();
      } catch (e) { b.disabled = false; alert(e.message); }
    }));
  }
}

/* -------------------------------------------- decision content, shared by both views -- */
const findingItem = f => `<li><span class="s ${f.severity}" aria-label="${f.severity}">${SEV_MARK[f.severity]}</span><span>${esc(f.message)}<code>${esc(CODE_LABEL[f.code] || f.code)}</code></span></li>`;

function verdictBody(run) {
  const res = run.result || {}, fs = res.findings || [], m = res.message;
  const blocking = fs.filter(f => f.severity === "hold" || f.severity === "reject"), rest = fs.filter(f => !blocking.includes(f));
  return `${run.final_decision ? `<div class="resolved"><b>A person resolved this hold: ${DEC[run.final_decision].label.toLowerCase()}.</b>${run.override_note ? ` “${esc(run.override_note)}”` : ""}
      ${(res.learned || []).length ? `<ul class="learned">${res.learned.map(l => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}</div>` : ""}
    ${run.inputs && run.inputs.note ? `<div class="resolved"><b>Note added:</b> ${esc(run.inputs.note)}</div>` : ""}
    <h3>${blocking.length ? "What stopped it" : "What the checks found"}</h3>
    <ul class="findings">${(blocking.length ? blocking : rest).map(findingItem).join("")}</ul>
    ${blocking.length && rest.length ? `<details class="fold"><summary>${rest.length} other note${rest.length > 1 ? "s" : ""} from the checks</summary><ul class="findings">${rest.map(findingItem).join("")}</ul></details>` : ""}
    <h3>What happens next</h3>
    <ol class="steps">${(res.next_steps || []).map(s => `<li>${esc(s)}</li>`).join("")}</ol>
    ${m ? `<details class="fold"><summary>Draft message to ${m.audience === "vendor" ? "the vendor" : "procurement"}</summary>
      <div class="draft"><div class="subj">${esc(m.subject)}</div><pre>${esc(m.body)}</pre><button class="btn" type="button" data-copy>Copy message</button></div></details>` : ""}`;
}

/* answers that send the run through the checks again: overrule "not an invoice", approve a new vendor, settle the PO */
function recheckPanels(run) {
  const fs = (run.result || {}).findings || [], has = code => fs.some(f => f.code === code);
  const open = run.decision === "HOLD" && !run.final_decision, f = (run.result || {}).fields || {};
  let out = has("NOT_AN_INVOICE") ? `<div class="resolve vendor-approve"><p>Is this actually an invoice?</p>
      <p class="sub">Checks it again as an invoice. It will be held for you to review, never approved automatically.</p>
      <div class="row"><button class="btn primary" type="button" data-treat-invoice>Yes, check it as an invoice</button></div></div>` : "";
  if (open && has("VENDOR_UNKNOWN")) out += `<div class="resolve vendor-approve" data-role="vendor-approve"><p>Approve this vendor</p>
      <p class="sub">Adds it to your approved list${f.vendor_gstin ? ` with GSTIN ${esc(f.vendor_gstin)}` : ""} and checks this invoice again. Later bills from it pass the vendor check on their own.</p>
      <div class="row"><input type="text" data-role="vendor-name" maxlength="200" value="${esc(f.vendor_name || "")}" aria-label="Vendor name" placeholder="Vendor name">
        <input type="text" data-role="vendor-note" maxlength="500" placeholder="How was this vendor verified? (optional)" aria-label="How was this vendor verified?"></div>
      <label class="wz-check"><input type="checkbox" data-role="vendor-no-po"><span><b>This vendor doesn't use purchase orders</b>
        <small>For a restaurant, clinic or shop. Its bills won't be held for having no PO.</small></span></label>
      <div class="row"><button class="btn primary" type="button" data-approve-vendor>Approve vendor and re-check</button></div></div>`;
  const po = ["PO_NOT_FOUND", "PO_AMBIGUOUS", "PO_NONE_OPEN"].filter(has);
  if (open && po.length && run.vendor_id) {
    const options = run.po_options || [], waivable = !has("PO_NOT_FOUND");
    out += `<div class="resolve vendor-approve" data-role="po-approve"><p>Purchase order</p>
      <p class="sub">${options.length ? "Match it to one of this vendor's open POs" : "This vendor has no open PO with a balance"}${waivable ? `${options.length ? ", or say it" : ". If it"} doesn't use POs, later bills from it won't be held for one either.` : ". The invoice cites a PO number, so it needs the right PO."}</p>
      ${options.length ? `<div class="row"><select data-role="po-pick" aria-label="Purchase order">${options.map(p =>
        `<option value="${esc(p.po_number)}">${esc(p.po_number)} · ${esc(p.description)} · ${inr(p.remaining)} left</option>`).join("")}</select>
        <button class="btn primary" type="button" data-po-use>Use this PO and re-check</button></div>` : ""}
      ${waivable ? `<div class="row"><button class="btn${options.length ? "" : " primary"}" type="button" data-po-none>No PO needed for this vendor</button></div>` : ""}</div>`;
  }
  return out;
}

function wireVerdict(root, id, run) {
  const m = (run.result || {}).message, copy = $("[data-copy]", root);
  if (copy) copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(`Subject: ${m.subject}\n\n${m.body}`); copy.textContent = "Copied"; } catch (e) { copy.textContent = "Select the text to copy"; }
  });
  const recheck = (button, url, body) => button && button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const d = await api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body()) });
      closeDrawer();
      location.hash = "#process";
      register([d.run]);                                       // same run, checked again and shown live
    } catch (e) { button.disabled = false; alert(e.message); }
  });
  recheck($("[data-treat-invoice]", root), `/api/runs/${id}/treat-as-invoice`, () => ({}));
  recheck($("[data-approve-vendor]", root), `/api/runs/${id}/approve-vendor`, () => ({
    name: $("[data-role=vendor-name]", root).value, note: $("[data-role=vendor-note]", root).value, no_po: $("[data-role=vendor-no-po]", root).checked }));
  recheck($("[data-po-use]", root), `/api/runs/${id}/po`, () => ({ po_number: $("[data-role=po-pick]", root).value }));
  recheck($("[data-po-none]", root), `/api/runs/${id}/po`, () => ({ no_po: true }));
}

/* what approving a held bill will teach the checks, mirroring the server's rules, so nobody is surprised */
function learnHint(run) {
  const fs = (run.result || {}).findings || [], has = code => fs.some(f => f.code === code), f = (run.result || {}).fields || {};
  const parts = [];
  if ((has("VENDOR_UNKNOWN") || has("VENDOR_AMBIGUOUS")) && f.vendor_name) parts.push(`add ${esc(f.vendor_name)} to your approved vendors`);
  if (has("PO_NOT_FOUND") && f.po_reference) parts.push(`accept PO number ${esc(f.po_reference)} for this vendor`);
  else if (has("PO_AMBIGUOUS") || has("PO_NONE_OPEN") || ((has("VENDOR_UNKNOWN") || (has("VENDOR_AMBIGUOUS") && f.vendor_gstin)) && !f.po_reference)) parts.push("mark the vendor as not using purchase orders");
  return parts.length ? `<p class="sub learn-hint">Approving will also ${parts.join(" and ")}, so its next bills pass these checks by themselves.</p>` : "";
}

async function resolve(id, decision, note) {
  await api(`/api/runs/${id}/override`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, note }) });
  const r = S.runs.get(id); if (r) r.decision = decision;
  if (decision === "APPROVE") loadFolders();                // it may have gone into its vendor's folder
  renderScenarios(); renderUploads(); refreshVisible();
}

/* -------------------------------------------------------------- the step-by-step view -- */
const WIZARD = ["New document", "Read document", "Extract fields", "Review details", "Run checks", "Decision", "Stamp"];
const CHECKS = ["validate", "vendor", "po", "duplicates", "budget"];
const TITLE = Object.fromEntries(STAGES);
const DWELL = 900;              // ms each page stays up while a document is processed live, so no step flashes past
const REVIEW_FIELDS = [["vendor_name", "Vendor"], ["vendor_gstin", "GSTIN"], ["invoice_number", "Invoice number"],
  ["invoice_date", "Invoice date", "DD-MM-YYYY"], ["po_reference", "PO number"], ["total", "Total amount"]];
const working = text => `<div class="wz-working" role="status"><span class="spinner" aria-hidden="true"></span><span>${esc(text)}</span></div>`;
const ARROW = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>`;
const shown = (k, v) => v == null || v === "" ? "" : k === "total" ? Number(v).toFixed(2) : String(v);

/* current and reached count the run's pages (1 New document ... 7 Stamp). Approvals sits between the first two: it is
   answered before anything is uploaded, so once a run exists it is done and can't be reopened */
function stepper(current, reached, onApprovals = false) {
  const items = WIZARD.map((title, i) => ({ title, n: i + 1 }));
  items.splice(1, 0, { title: "Approvals", n: null });
  return `<ol class="stepper" aria-label="Progress">${items.map(({ title, n }, i) => {
    const state = n === null ? (onApprovals ? "current" : current > 1 ? "done" : "")
      : onApprovals ? (n === 1 ? "done" : "") : n < current ? "done" : n === current ? "current" : "";
    const open = n !== null && (n === 1 || (!onApprovals && n <= reached));
    return `<li class="${state}"><button type="button" ${n ? `data-goto="${n}"` : ""} ${open ? "" : "disabled"} ${state === "current" ? 'aria-current="step"' : ""}>
      <span class="num">${state === "done" ? "✓" : i + 1}</span><span class="lbl">${title}</span></button></li>`;
  }).join("")}</ol>`;
}

/* Turns the stream of stage events into seven pages. A person's own upload moves on when they press Next, and
   waits at Review and at Decision for them. Sample scenarios play through on their own, one page at a time. */
class Wizard {
  constructor(root, id, filename, live) {
    const review = !!(S.runs.get(id) || {}).review;
    Object.assign(this, { root, id, filename, live, step: 2, reached: 2, seen: -1, ev: {}, run: null, timer: null,
      resuming: false, browsing: false, drawn: 0, review, manual: review });
    this.render();
  }

  state(text) { this.waiting = text; this.render(); }

  apply(ev) {
    if (ev.seq <= this.seen) return;                       // after a review the stream replays the earlier steps
    this.seen = ev.seq;
    this.ev[ev.stage] = ev;
    this.waiting = null;
    if (ev.stage === "decision" && ev.status !== "running") this.loadRun();
    this.advance();
    const page = ev.stage === "ingest" || ev.stage === "read" ? 2 : ev.stage === "extract" ? [3, 4] : ev.stage === "decision" ? 6 : 5;
    if ([].concat(page).includes(this.step)) this.render();
  }

  target() {
    const e = this.ev, done = s => e[s] && e[s].status !== "running";
    if (done("decision") && this.run) return 6;
    if (CHECKS.some(s => e[s]) || e.decision) return 5;
    if (done("extract")) return 4;
    return done("read") ? 3 : 2;
  }

  advance() {
    const t = this.target();
    this.reached = Math.max(this.reached, t);
    if (!this.live) { if (t > this.step) this.show(t); return; }
    if (this.manual || this.browsing || this.timer || this.step >= t || (this.step === 4 && this.review)) return;
    this.timer = setTimeout(() => { this.timer = null; this.show(this.step + 1); this.advance(); }, DWELL);
  }

  show(n, byUser = false) {
    if (n > this.reached && !(n === 7 && this.run)) return;
    this.browsing = byUser && n < this.target();
    this.step = n;
    this.reached = Math.max(this.reached, n);
    this.render();
    if (byUser && !this.browsing) this.advance();
  }

  async loadRun() {
    try { this.run = await api(`/api/runs/${this.id}`); } catch (e) { return; }
    if (this.run.folder_id) loadFolders();                 // it may have been filed automatically: folder counts change
    this.advance();
    if (this.step >= 5) this.render();                     // the Checks page gets its Next button
  }

  next() {                                                 // offered once the next page has something to show
    return this.target() > this.step ? `<div class="wz-actions"><button class="btn primary next" type="button" data-next>Next ${ARROW}</button></div>` : "";
  }

  streamEnded(run) {                                       // the stream stops at Review, or once the decision is made
    this.run = run.result ? run : this.run;
    if (run.status === "review") this.review = true;
    this.advance();
    if (this.step === 4) this.render();
  }

  finish(run) {                                            // opening a run that already ran
    this.run = run;
    if (run.status === "review") { this.review = true; this.show(4); return; }
    this.reached = 7;
    this.show(run.final_decision ? 7 : 6);
  }

  head(title, sub, cls = "lede") {
    return `<span class="wz-file">${esc(this.filename)}</span><h1>${title}</h1>${sub ? `<p class="${cls}">${esc(sub)}</p>` : ""}`;
  }

  preview() {
    const id = this.id;
    return `<figure class="wz-doc"><img alt="First page of ${esc(this.filename)}" src="/api/runs/${id}/preview"></figure>`;
  }

  render() {
    const pages = [null, null, this.readPage, this.extractPage, this.reviewPage, this.checksPage, this.resultPage, this.stampPage];
    const fresh = this.drawn !== this.step;
    this.fresh = fresh;
    this.drawn = this.step;
    const body = this.waiting ? this.head("Waiting in the queue", "Another document is being processed first.") + working(this.waiting) : pages[this.step].call(this);
    this.root.innerHTML = `<div class="wizard">${stepper(this.step, this.reached)}<div class="wz-card${fresh ? " enter" : ""}" data-step="${this.step}">${body}</div></div>`;
    if (fresh && this.root.getBoundingClientRect().top < 0) this.root.scrollIntoView({ block: "start", behavior: "smooth" });
    this.wire();
  }

  readPage() {
    const ingest = this.ev.ingest, read = this.ev.read, ready = read && read.status !== "running";
    return `${this.head("Read document", ready ? read.summary : "Opening the file and reading every page.")}
      <div class="wz-split"><div>${ready ? DETAIL.read(read.detail)
        : working(ingest && ingest.status !== "running" ? "Reading the text. Photos take a little longer." : "Receiving the file")}
        ${ingest && ingest.status !== "running" ? `<p class="muted wz-meta">${esc(ingest.summary)}</p>` : ""}</div>${this.preview()}</div>${this.next()}`;
  }

  extractPage() {
    const ex = this.ev.extract, ready = ex && ex.status !== "running";
    if (!ready) return this.head("Extract fields", "Finding the vendor, invoice number, date and amounts.") + working("Pulling out the details");
    const f = ex.detail.fields;
    return `${this.head("Extract fields", ex.summary)}
      <div class="wz-found">${[["vendor_name", "Vendor"], ["invoice_number", "Invoice number"], ["invoice_date", "Invoice date"], ["total", "Total"]].map(([k, label]) =>
        `<div class="${f[k] == null ? "missing" : ""}"><span>${label}</span><b>${f[k] == null ? "Not found" : esc(k === "total" ? inr(f[k]) : f[k])}</b></div>`).join("")}</div>
      ${this.next()}`;
  }

  vendorPanel(check, open) {
    if (!check) return "";
    if (check.status === "approved") return `<p class="wz-vendor ok"><span class="tick">✓</span> On your approved vendor list as <b>${esc(check.name)}</b>.</p>`;
    if (check.status === "blocked") return `<p class="wz-vendor bad"><b>${esc(check.name)}</b> is a blocked vendor, so this invoice will be rejected.</p>`;
    if (check.status !== "unknown" && check.status !== "no_identity") return `<p class="wz-vendor warn">The vendor doesn't cleanly match your approved list. The checks will say why.</p>`;
    if (check.status === "unknown" && ((S.runs.get(this.id) || {}).approvals || {}).vendors)
      return `<p class="wz-vendor ok"><span class="tick">✓</span> Not on your approved list yet. It will be added when the checks run, as approved on the Approvals step.</p>`;
    if (!open) return `<p class="wz-vendor warn">This vendor isn't on your approved list.</p>`;
    return `<div class="resolve vendor-approve wz-vendor-approve">
      <label class="wz-check"><input type="checkbox" data-role="approve-vendor"><span><b>Approve this vendor</b>
        <small>It isn't on your approved list. Tick to add it, using the vendor name and GSTIN above, before the checks run.</small></span></label>
      <input type="text" data-role="vendor-note" maxlength="500" placeholder="How was this vendor verified? (optional)" aria-label="How was this vendor verified?" disabled>
      <label class="wz-check"><input type="checkbox" data-role="vendor-no-po" disabled><span><b>This vendor doesn't use purchase orders</b>
        <small>For a restaurant, clinic or shop. Its bills won't be held for having no PO.</small></span></label>
    </div>`;
  }

  /* a folder's first invoice: its vendor and PO answers are given here once for the whole folder */
  batchPanel(po) {
    const rest = batchOf(this.id).filter(r => r.id !== this.id && r.status === "waiting").length;
    if (!rest) return "";
    if ((S.runs.get(this.id) || {}).approvals)                // answered on the Approvals step: nothing to ask again
      return `<p class="wz-vendor ok"><span class="tick">✓</span> First of ${rest + 1} invoices. Once you run the checks on this one, the other ${rest} are checked one at a time with the approvals you chose.</p>`;
    const choice = po.reference && !po.known
      ? `<label class="wz-check"><input type="checkbox" data-role="batch-po" value="accept"><span><b>Accept the PO numbers on this folder's invoices</b>
          <small>This one cites ${esc(po.reference)}, which isn't in your PO register. Ticking accepts the PO number each invoice in the folder cites, for this vendor, and remembers them for its later bills.</small></span></label>`
      : !po.reference
        ? `<label class="wz-check"><input type="checkbox" data-role="batch-po" value="no_po"><span><b>This vendor doesn't use purchase orders</b>
            <small>Its invoices in this folder, and later ones, won't be held for having no PO.</small></span></label>`
        : `<p class="sub">This one cites ${esc(po.reference)} from your PO register. Each invoice is matched to the PO it cites.</p>`;
    return `<div class="resolve vendor-approve wz-batch"><p>First of ${rest + 1} invoices from this folder</p>
      <p class="sub">Approve the vendor${po.reference && po.known ? "" : " and the PO"} here and it counts for the other ${rest}: they're checked straight through and approved automatically if every check passes. Anything that fails a check is held for you.</p>
      <label class="wz-check"><input type="checkbox" data-role="batch-all-vendors"><span><b>Approve all the new vendors</b>
        <small>Every vendor in this folder that isn't on your list, this one included, is added as its invoice is checked, so you aren't asked again. The PO answer below then covers them too. A name resembling a vendor already on file, or a blocked one, is still held for you.</small></span></label>
      ${choice}</div>`;
  }

  reviewPage() {
    const ex = this.ev.extract;
    if (!ex || ex.status === "running") return this.head("Review details", "Getting the fields ready.") + working("Preparing the review");
    const d = ex.detail, f = d.fields, open = this.review, items = d.line_items || [];
    const field = ([k, label, hint]) => {
      const v = shown(k, f[k]), grounded = d.grounding[k] === true, typed = (d.confidence || {})[k] === "user";
      return `<label class="wz-field${v ? "" : " missing"}"><span>${label}${grounded ? ' <span class="tick" title="Found word for word in the document">✓</span>' : ""}${typed ? ' <span class="pill pen">typed in</span>' : ""}</span>
        <input name="${k}" value="${esc(v)}" data-read="${esc(v)}" placeholder="${v ? "" : `Not found${hint ? ` (${hint})` : ""}`}" ${open ? "" : "disabled"}></label>`;
    };
    return `${this.head("Review details", open ? "Check what was read and fill in anything missing. What you type is marked as typed in, and a value that contradicts the document holds the invoice for a person."
        : "What was read from the document.")}
      <div class="wz-split"><form class="wz-form" data-role="review" novalidate>
        <div class="wz-fields">${REVIEW_FIELDS.map(field).join("")}
          <label class="wz-field wide"><span>Note <small>optional</small></span><input name="note" maxlength="500" placeholder="Anything a reviewer should know" ${open ? "" : "disabled"}></label></div>
        ${this.vendorPanel(d.vendor_check, open)}
        ${open ? this.batchPanel(d.po_check || {}) : ""}
        ${f.subtotal != null || f.tax_amount != null ? `<p class="muted wz-meta">${f.discount ? `Discount ${inr(f.discount)} · ` : ""}${f.subtotal != null ? `Pre-tax ${inr(f.subtotal)}` : ""}${f.tax_amount != null ? ` · Tax ${inr(f.tax_amount)}` : ""}</p>` : ""}
        ${items.length ? `<details class="fold"><summary>${items.length} line item${items.length > 1 ? "s" : ""}</summary><div class="detail"><table><thead><tr><th>Description</th><th class="right">Qty</th><th class="right">Amount</th></tr></thead>
          <tbody>${items.map(i => `<tr><td>${esc(i.description)}</td><td class="right">${i.quantity ?? "–"}</td><td class="right">${inr(i.amount)}</td></tr>`).join("")}</tbody></table></div></details>` : ""}
        <p class="auth-error" data-role="review-error" hidden></p>
        ${open ? `<div class="wz-actions"><button class="btn primary next" type="submit">Run the checks ${ARROW}</button></div>` : ""}
      </form>${this.preview()}</div>${open ? "" : this.next()}`;
  }

  checksPage() {
    const rows = CHECKS.map(s => {
      const ev = this.ev[s], st = ev ? ev.status : "pending";
      return `<li class="stage" data-status="${st}"><div class="mark" aria-hidden="true">${MARK[st] || ""}</div>
        <div class="head"><span class="name">${TITLE[s]}</span><span class="sum">${esc(st === "running" ? "Working" : ev ? ev.summary : "Waiting")}</span></div></li>`;
    }).join("");
    const deciding = this.ev.decision && this.ev.decision.status === "running";
    return `${this.head("Run checks", "Amounts, vendor, purchase order, duplicates and the PO balance.")}<ol class="trail wz-checks">${rows}</ol>${deciding ? working("Making the decision") : ""}${this.next()}`;
  }

  resultPage() {
    const run = this.run;
    if (!run || !run.result) return this.head("Decision", "Weighing what the checks found.") + working("Deciding");
    const D = DEC[run.decision] || DEC.HOLD, open = run.decision === "HOLD" && !run.final_decision;
    return `${this.head("Decision", run.result.headline, "headline")}
      <p class="wz-outcome"><span class="pill ${D.cls}">The system says: ${D.label.toLowerCase()}</span></p>
      ${verdictBody(run)}${recheckPanels(run)}
      ${eff(run) === "REJECT" ? `<p class="wz-no-folder">${FOLDER_ICON}<span>Rejected invoices can't be saved to a folder.</span></p>` : this.folderPanel()}
      <div data-role="batch-status">${batchStatus(batchOf(this.id))}</div>
      ${open ? `<div class="resolve wz-decide"><p>Your decision</p>${learnHint(run)}<div class="row"><input type="text" aria-label="Note for the audit trail" placeholder="Add a note for the audit trail (optional)" data-role="note"></div>
        <div class="wz-actions"><button class="btn bad" type="button" data-resolve="REJECT">Reject</button><button class="btn" type="button" data-keep>Keep on hold</button>
        <button class="btn ok" type="button" data-resolve="APPROVE">Approve</button></div></div>`
      : `<div class="wz-actions"><button class="btn primary next" type="button" data-stamp>Next ${ARROW}</button></div>`}`;
  }

  stampPage() {
    const run = this.run, D = DEC[eff(run)] || DEC.HOLD, f = (run.result || {}).fields || {};
    const by = run.final_decision ? "Decided by a person" : run.decision === "HOLD" ? "Waiting for a person" : "Decided automatically";
    return `<div class="wz-stamp"><div class="stamp big ${D.cls}${this.fresh ? "" : " still"}" role="status">${D.label}</div><p class="wz-by">${by}</p>
      ${kv([["File", esc(this.filename)], ["Vendor", val(f.vendor_name)], ["Invoice number", val(f.invoice_number)], ["Invoice date", val(f.invoice_date)], ["Total", val(f.total, inr)]])}
      ${folderName(run.folder_id) ? `<p class="wz-saved">${FOLDER_ICON}<span>Saved in <b>${esc(folderName(run.folder_id))}</b>${{ approvals: " automatically, as chosen on the Approvals step", vendor: " automatically, with this vendor's earlier bills" }[this.autoFiled()] || ""}</span></p>` : ""}
      <div data-role="batch-status">${batchStatus(batchOf(this.id))}</div>
      <div class="wz-actions center"><a class="btn" href="#dashboard">Open the dashboard</a><button class="btn primary" type="button" data-new>New document</button></div></div>`;
  }

  autoFiled() {                                            // why the system filed it ("approvals" | "vendor"), if it hasn't been moved since
    const auto = (this.run.result || {}).auto_folder;
    return auto && auto.id === this.run.folder_id ? auto.why || "vendor" : null;
  }

  folderPanel() {                                          // on the Decision page: file this invoice in a folder
    const name = folderName(this.run.folder_id);
    return `<div class="wz-folder"><div>
        <p class="wz-folder-title">${FOLDER_ICON}<span>Save to a folder</span><small>optional</small></p>
        <p class="muted">${name ? `Saved in <b>${esc(name)}</b>${{ approvals: ", as chosen on the Approvals step", vendor: ", with the earlier bills from this vendor" }[this.autoFiled()] || ""}.` : "Keep it with invoices like it. Each folder has its own dashboard."}</p></div>
      <button class="btn" type="button" data-folder-choose>${FOLDER_ICON}<span>${name ? "Change folder" : "Choose folder"}</span></button></div>`;
  }

  wireFolder(root) {
    const button = $("[data-folder-choose]", root);
    if (!button) return;
    button.addEventListener("click", () => folderDialog({ title: "Save to a folder", current: this.run.folder_id, allowNone: true, pick: async folderId => {
      const d = await api(`/api/runs/${this.id}/folder`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder_id: folderId }) });
      this.run.folder_id = folderId;
      if ((d.filed || []).length) toast(`The ${d.filed.length} other invoice${d.filed.length > 1 ? "s" : ""} from this folder that passed went in too.`);
      await loadFolders();
      this.render();
    } }));
  }

  wire() {
    const root = this.root, form = $("[data-role=review]", root);
    if (this.step === 6) this.wireFolder(root);
    if (form) form.addEventListener("submit", ev => { ev.preventDefault(); this.submitReview(form); });
    if (this.step === 6 && this.run) wireVerdict(root, this.id, this.run);
    $$("[data-resolve]", root).forEach(b => b.addEventListener("click", async () => {
      $$(".wz-decide .btn", root).forEach(x => { x.disabled = true; });
      try {
        await resolve(this.id, b.dataset.resolve, $("[data-role=note]", root).value);
        this.run = await api(`/api/runs/${this.id}`);
        this.show(7);
      } catch (e) { $$(".wz-decide .btn", root).forEach(x => { x.disabled = false; }); alert(e.message); }
    }));
    $$("[data-keep], [data-stamp]", root).forEach(b => b.addEventListener("click", () => this.show(7)));
    $$("[data-next]", root).forEach(b => b.addEventListener("click", () => this.show(this.step + 1, true)));
    const tick = $("[data-role=approve-vendor]", root);
    if (tick) tick.addEventListener("change", () => {
      $$("[data-role=vendor-note], [data-role=vendor-no-po]", root).forEach(i => { i.disabled = !tick.checked; });
    });
    const fresh = $("[data-new]", root);
    if (fresh) fresh.addEventListener("click", newDocument);
  }

  async submitReview(form) {
    const inputs = {}, error = $("[data-role=review-error]", form), button = $("[type=submit]", form);
    $$("input[name]", form).forEach(i => {                 // only what the person changed or added
      const v = i.value.trim();
      if (i.name === "note" ? v : v && v !== i.dataset.read) inputs[i.name] = v;
    });
    const tick = $("[data-role=approve-vendor]", form);
    const approve_vendor = tick && tick.checked ? { note: $("[data-role=vendor-note]", form).value, no_po: $("[data-role=vendor-no-po]", form).checked } : null;
    const po = $("[data-role=batch-po]", form), batch_po = po && po.checked ? po.value : null;
    const all = $("[data-role=batch-all-vendors]", form), approve_all_vendors = !!(all && all.checked);
    button.disabled = true;
    error.hidden = true;
    try {
      const d = await api(`/api/runs/${this.id}/continue`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ inputs, approve_vendor, batch_po, approve_all_vendors }) });
      this.review = false;
      this.resuming = true;
      this.reached = 5;
      this.show(5);
      register([d.run]);
      if ((d.batch || []).length) register(d.batch.map(r => ({ ...r, batch: S.runs.get(this.id).batch })), false, false);  // checked in the background
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
      button.disabled = false;
    }
  }
}

/* ------------------------------------------------------- run orchestration -- */
let VIEW = null;

function showRun(id, live) {
  const r = S.runs.get(id); if (!r) return;
  S.current = id;
  VIEW = new Wizard($("#run"), id, r.filename, live);
  renderScenarios(); renderUploads();
  if (!live) replay(VIEW, id);
}

function newDocument() {
  location.hash = "#process";
  S.current = null; VIEW = null;
  renderEmpty(); renderScenarios(); renderUploads();
}

async function replay(view, id) {
  const run = await api(`/api/runs/${id}`);
  if (!["done", "error", "review"].includes(run.status)) { view.state("Waiting in the queue"); return; }
  (run.events || []).forEach(ev => view.apply(ev));
  view.finish(run);
}

function pump() {
  if (S.streaming || !S.queue.length) return;
  const id = S.queue.shift(), r = S.runs.get(id);
  S.streaming = true; r.status = "running";
  if (VIEW && VIEW.id === id && VIEW.resuming) VIEW.resuming = false;   // the same pages carry on after a review
  else if ((S.follow && r.follow !== false) || !VIEW) showRun(id, true);
  const es = new EventSource(`/api/runs/${id}/stream`);
  const done = async () => {
    es.close(); S.streaming = false;
    let d = null;
    try { d = await api(`/api/runs/${id}`); r.status = d.status === "review" ? "review" : "done"; r.decision = eff(d); } catch (e) { r.status = "done"; }
    if (d && VIEW && VIEW.id === id) VIEW.streamEnded(d);
    if (d && d.folder_id) loadFolders();                     // a folder's later invoice may have been filed with its vendor
    renderScenarios(); renderUploads(); refreshVisible(); pump(); syncBatch();
  };
  es.onmessage = e => { const ev = JSON.parse(e.data); if (S.current === id && VIEW) VIEW.apply(ev); };  // another run on screen: the stream still drives status dots
  es.addEventListener("end", done);
  es.onerror = () => { if (es.readyState === EventSource.CLOSED || S.streaming) done(); };
  renderScenarios(); renderUploads();
}

/* follow: show each run on screen as it is processed. A folder's later invoices are checked in the background instead,
   so they don't take the page away from the first one's Decision */
function register(runs, review = false, follow = true) {
  if (follow) S.follow = true;
  runs.forEach(r => {
    const known = S.runs.get(r.id) || {};
    S.runs.set(r.id, { id: r.id, filename: r.filename, status: "queued", decision: null, review, batch: r.batch || known.batch, follow,
      approvals: r.approvals || known.approvals });
    S.queue.push(r.id);
  });
  renderScenarios(); renderUploads(); pump();
}

/* a folder's invoices that were waiting may have been queued by the server: the first one was decided without a review
   (so the next asks for the approval), or it was cancelled */
async function syncBatch() {
  if (![...S.runs.values()].some(r => r.status === "waiting")) return;
  let runs;
  try { ({ runs } = await api("/api/runs")); } catch (e) { return; }
  const moved = runs.filter(r => (S.runs.get(r.id) || {}).status === "waiting" && r.status !== "waiting");
  moved.forEach(r => register([{ id: r.id, filename: r.filename, batch: r.batch_id }], r.status === "queued" || r.status === "review", false));
  if (moved.length) renderUploads();
}

async function runScenarios(files) {
  try { register((await api("/api/runs/sample", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ files }) })).runs); }
  catch (e) { alert(e.message); }
}

/* files chosen or dropped: nothing is uploaded until the Approvals step is answered */
function uploadFiles(fileList) {
  const files = [...fileList].filter(f => READABLE.test(f.name) && !f.name.startsWith(".")).sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  if (!files.length) { alert("There are no PDFs, photos or ZIP files there."); return; }
  S.pending = { files, vendors: false, po: false, folder: null };
  S.current = null; VIEW = null;
  location.hash = "#process";
  renderApprovals();
}

const APPROVALS = [
  ["vendors", "Auto-approve all the vendors", "A vendor that isn't on your approved list is added when its invoice is checked, so you aren't asked. A name resembling a vendor already on file or a blocked one, or a GSTIN that disagrees with your records, is still held for you."],
  ["po", "Auto-approve all the PO references, whether present or not", "An invoice citing a PO number that isn't in your register, or citing none, isn't held for it. A PO in your register is still checked against its balance, and one that belongs to another vendor is still held."]];

/* step 2: what may be approved without asking, for every file in this upload, and where they all go */
function renderApprovals() {
  const p = S.pending, n = p.files.length;
  const names = p.files.slice(0, 5).map(f => esc(f.name)).join(", ") + (n > 5 ? ` and ${n - 5} more` : "");
  const zips = p.files.filter(f => /\.zip$/i.test(f.name)).length;
  $("#run").innerHTML = `<div class="wizard">${stepper(1, 1, true)}<div class="wz-card wz-approvals enter">
    <h1>Approvals</h1>
    <p class="lede">Choose what can be approved without asking you, for ${n > 1 || zips ? "every invoice in this upload" : "this invoice"}. Every other check still runs on each one.</p>
    <p class="muted wz-meta">${n > 1 ? `${n} files: ` : ""}${names}${zips ? `. ${zips > 1 ? "The ZIPs are" : "The ZIP is"} unpacked when you start, and each PDF or photo inside is checked in turn.` : ""}</p>
    <form class="wz-approval-form" data-role="approvals">
      ${APPROVALS.map(([key, title, help]) => `<label class="wz-check wz-approval"><input type="checkbox" name="${key}" ${p[key] ? "checked" : ""}>
        <span><b>${title}</b><small>${help}</small></span></label>`).join("")}
      <div class="wz-approval wz-approval-folder"><div>
          <p class="wz-folder-title">${FOLDER_ICON}<span>Add all the invoices to a folder</span><small>optional</small></p>
          <p class="muted">${p.folder ? `Into <b>${esc(folderName(p.folder) || "the folder")}</b>, each as soon as it's decided. Rejected invoices are left out.`
            : "Each invoice goes in as soon as it's decided. Rejected invoices are left out."}</p></div>
        <div class="row">${p.folder ? `<button class="btn" type="button" data-approval-unfile>Don't file</button>` : ""}
          <button class="btn" type="button" data-approval-folder>${FOLDER_ICON}<span>${p.folder ? "Change folder" : "Choose folder"}</span></button></div></div>
      <p class="auth-error" data-role="approvals-error" hidden></p>
      <div class="wz-actions"><button class="btn" type="button" data-goto="1">Back</button>
        <button class="btn primary next" type="submit">Start processing ${ARROW}</button></div>
    </form></div></div>`;
  const form = $("[data-role=approvals]");
  form.addEventListener("change", ev => { if (ev.target.name in p) p[ev.target.name] = ev.target.checked; });
  form.addEventListener("submit", ev => { ev.preventDefault(); startUpload(form); });
  $("[data-approval-folder]", form).addEventListener("click", () => folderDialog({ title: "Add all the invoices to a folder", current: p.folder,
    pick: async id => { p.folder = id; await loadFolders(); renderApprovals(); } }));
  const unfile = $("[data-approval-unfile]", form);
  if (unfile) unfile.addEventListener("click", () => { p.folder = null; renderApprovals(); });
}

async function startUpload(form) {
  const p = S.pending, error = $("[data-role=approvals-error]", form), button = $("[type=submit]", form);
  const fd = new FormData();
  p.files.forEach(f => fd.append("files", f));
  if (p.files.length > 1) fd.append("batch", "1");             // one upload: the first stops at Review, the rest go straight through
  const approvals = { vendors: p.vendors, po: p.po, folder_id: p.folder };
  fd.append("approvals", JSON.stringify(approvals));
  button.disabled = true; error.hidden = true;
  let runs;
  try { ({ runs } = await api("/api/runs", { method: "POST", body: fd })); }
  catch (e) { error.textContent = e.message; error.hidden = false; button.disabled = false; return; }
  S.pending = null;
  // the rest wait on the server until the first one's review is submitted, then are checked one at a time behind it
  const rest = runs.filter(r => r.waiting);
  rest.forEach(r => S.runs.set(r.id, { id: r.id, filename: r.filename, status: "waiting", decision: null, batch: r.batch, approvals }));
  register(runs.filter(r => !r.waiting).map(r => ({ ...r, approvals })), true);
  if (rest.length) toast(`${runs.length} invoices uploaded. Review the first one; then each of the other ${rest.length} is checked in turn with your approvals.`);
}

const READABLE = /\.(pdf|png|jpe?g|webp|tiff?|bmp|zip)$/i;   // a ZIP is unpacked on the server

/* what was dropped: files, and the files inside any dropped folder (its subfolders too) */
async function droppedFiles(dt) {
  const entries = [...(dt.items || [])].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!entries.some(e => e.isDirectory)) return [...dt.files];
  const out = [];
  const walk = async entry => {
    if (entry.isFile) { out.push(await new Promise((ok, fail) => entry.file(ok, fail))); return; }
    const reader = entry.createReader();
    for (;;) {                                               // a folder is read in chunks until one comes back empty
      const chunk = await new Promise((ok, fail) => reader.readEntries(ok, fail));
      if (!chunk.length) break;
      for (const e of chunk) await walk(e);
    }
  };
  for (const e of entries) await walk(e);
  return out;
}

/* the end of a folder upload: every checked invoice into one folder, picked or created in the usual popup */
function saveBatchToFolder(batch) {
  folderDialog({ title: "Save all to a folder", current: null, allowNone: false, pick: async folderId => {
    const d = await api(`/api/batches/${batch}/folder`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder_id: folderId }) });
    await loadFolders();
    if (VIEW && VIEW.run && d.filed.includes(VIEW.id)) { VIEW.run.folder_id = folderId; VIEW.render(); }
    toast(`${d.filed.length} invoice${d.filed.length === 1 ? "" : "s"} saved in ${folderName(folderId) || "the folder"}.${d.rejected ? ` ${d.rejected} rejected one${d.rejected === 1 ? " was" : "s were"} left out.` : ""}`);
  } });
}

const batchOf = id => { const b = (S.runs.get(id) || {}).batch; return b ? [...S.runs.values()].filter(r => r.batch === b) : []; };

/* ----------------------------------------------------------- inbox lists -- */
function latestFor(filename) {
  let found = null;
  S.runs.forEach(r => { if (r.filename === filename) found = r; });
  return found;
}
const dotFor = r => !r ? "" : r.status === "running" ? "running" : r.status === "queued" || r.status === "waiting" ? "" : r.status === "review" ? "review" : ({ APPROVE: "ok", HOLD: "warn", REJECT: "bad" }[r.decision] || "");
const dotText = r => !r ? "not run yet" : r.status === "running" ? "running" : r.status === "queued" ? "queued" : r.status === "waiting" ? "waiting for the first invoice's review"
  : r.status === "review" ? "waiting for your review" : (DEC[r.decision]?.label || "done");

/* the sample list is only on the demo's New document page (see tools/build_demo.py); the app itself has no list */
function renderScenarios() {
  const list = $("#scenarios");
  if (!list) return;
  list.innerHTML = S.scenarios.map((s, i) => {
    const r = latestFor(s.file);
    const again = r && r.status === "done" ? `<button class="again" type="button" data-again="${esc(s.file)}" title="Process this file again (it will be caught as a duplicate)">Run again</button>` : "";
    return `<li class="scn-row"><button class="scn" type="button" data-file="${esc(s.file)}" ${r && r.id === S.current ? 'aria-current="true"' : ""}>
      <span class="n">${i + 1}</span><span class="t">${esc(s.title)}<span class="e">Expected: ${DEC[s.expected].label.toLowerCase()}</span></span>
      <span class="dot ${dotFor(r)}" role="img" aria-label="${dotText(r)}" title="${dotText(r)}"></span></button>${again}</li>`;
  }).join("");
}

/* documents left at Review, offered on the New document page so none is forgotten. Finished ones are on the Dashboard */
function renderUploads() {
  const onPage = $("[data-role=batch-status]");            // a folder's progress, on its first invoice's last pages
  if (onPage && VIEW) onPage.innerHTML = batchStatus(batchOf(VIEW.id));
  const box = $("#awaiting");
  if (!box) return;
  const waiting = [...S.runs.values()].filter(r => r.status === "review").reverse();
  const batches = [...new Set([...S.runs.values()].filter(r => r.batch && r.status !== "done").map(r => r.batch))];
  box.hidden = !waiting.length && !batches.length;
  box.innerHTML = `${waiting.length ? `<p>Waiting for your review</p>${waiting.map(r => `<button class="wz-resume" type="button" data-run="${r.id}">
    <span class="dot review" aria-hidden="true"></span><span>${esc(r.filename)}</span>${ARROW}</button>`).join("")}` : ""}
    ${batches.map(b => batchStatus([...S.runs.values()].filter(r => r.batch === b))).join("")}`;
}

/* a folder's invoices with where each one is: shown on the New document page and the first invoice's last pages */
function batchStatus(runs) {
  if (runs.length < 2) return "";
  runs = runs.slice().sort((a, b) => a.filename.localeCompare(b.filename, undefined, { numeric: true }));  // upload order
  const done = runs.filter(r => r.status === "done"), count = d => done.filter(r => r.decision === d).length;
  const fileable = done.length - count("REJECT"), finished = done.length === runs.length;
  return `<div class="wz-batch-status"><p>Folder of ${runs.length} invoices · ${done.length} checked${done.length ? ` (${count("APPROVE")} approved, ${count("HOLD")} held, ${count("REJECT")} rejected)` : ""}</p>
    ${runs.map(r => `<button class="wz-resume" type="button" data-run="${r.id}" ${r.status === "waiting" ? "disabled" : ""}>
      <span class="dot ${dotFor(r)}" aria-hidden="true"></span><span>${esc(r.filename)}</span><small>${esc(dotText(r))}</small></button>`).join("")}
    ${finished && fileable ? `<div class="wz-actions"><button class="btn" type="button" data-batch-folder="${esc(runs[0].batch)}">${FOLDER_ICON}<span>Save all ${fileable} to a folder</span></button></div>
      ${count("REJECT") ? `<p class="muted wz-meta">Rejected invoices can't be saved to a folder.</p>` : ""}` : ""}</div>`;
}

function renderEmpty() {
  $("#run").innerHTML = `<div class="wizard">${stepper(1, 1)}<div class="wz-card wz-new enter">
    <h1>New document</h1>
    <p class="lede">Add an invoice or bill. You'll watch it being read, check what was found, then get a decision.</p>
    <label class="drop" for="file" tabindex="0"><strong>Choose files or drop them here</strong><span>PDFs or photos: one, several, a whole folder, or a ZIP</span></label>
    <div class="drop-folder"><button class="btn" type="button" data-pick-folder>${FOLDER_ICON}<span>Or choose a whole folder</span></button></div>
    <div class="demo-only demo-samples">
      <p>This demo replays recorded results, so it can't read a new document. Pick a sample to watch it go through every step. Each builds on the last, so run them in order for the full story.</p>
      <button class="btn primary" type="button" data-run-all>Run all in order</button>
      <ol class="scenarios" id="scenarios"></ol>
    </div>
    <div class="wz-awaiting" id="awaiting" hidden></div>
  </div></div>`;
  renderScenarios(); renderUploads();
}

/* --------------------------------------------------------------- dashboard -- */
const dayName = iso => new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
const monthName = ym => new Date(`${ym}-01T00:00:00`).toLocaleDateString("en-IN", { month: "long", year: "numeric" });
const period = () => S.view.kind === "all" ? "" : S.view.pick;
const scope = () => {                                      // the Dashboard's period and folder, as a query string
  const q = new URLSearchParams();
  if (period()) q.set("period", period());
  if (S.view.folder != null) q.set("folder", S.view.folder);
  return q.toString() ? `?${q}` : "";
};
const folderName = id => (S.folders.find(f => f.id === id) || {}).name;

/* key: what a row is grouped on. name: the group's heading. newest: sort latest first. Rows with no key go last. */
const GROUPS = {
  date: { key: r => r.invoice_date, name: k => k ? dayName(k) : "No invoice date", newest: true },
  month: { key: r => (r.invoice_date || "").slice(0, 7), name: k => k ? monthName(k) : "No invoice date", newest: true },
  year: { key: r => (r.invoice_date || "").slice(0, 4), name: k => k || "No invoice date", newest: true },
  vendor: { key: r => r.vendor_name, name: k => k || "Vendor not read" },
  invoice: { key: r => r.invoice_number, name: k => k || "No invoice number" },
  po: { key: r => r.po_number, name: k => k || "No PO matched" },
  outcome: { key: r => eff(r), name: k => (DEC[k] || DEC.HOLD).label },
  folder: { key: r => r.folder_id == null ? "" : String(r.folder_id), name: k => k ? folderName(+k) || "Folder" : "Not in a folder" },
  none: { key: () => "", name: () => "" },
};

const viewKey = () => `invoice-flow-view:${S.me}`;
function saveView() {
  try {
    localStorage.setItem(viewKey(), JSON.stringify({ group: S.view.group, kind: S.view.kind, dashTab: S.view.dashTab, grain: S.view.grain }));
  } catch (e) { /* the choice just isn't remembered */ }
}
function loadView() {
  try {
    const saved = JSON.parse(localStorage.getItem(viewKey())) || {};
    if (GROUPS[saved.group]) S.view.group = saved.group;
    if (["all", "month", "year"].includes(saved.kind)) S.view.kind = saved.kind;
    if (["overview", "analytics"].includes(saved.dashTab)) S.view.dashTab = saved.dashTab;
    if (GRAINS[saved.grain]) S.view.grain = saved.grain;
  } catch (e) { /* defaults stand */ }
  $("#group-by").value = S.view.group;
  $("#period-kind").value = S.view.kind;
}

function renderPeriods() {
  const yearly = S.view.kind === "year", pick = $("#period-pick");
  const dated = S.history.filter(r => r.status === "done" && r.invoice_date);
  const options = [...new Set(dated.map(r => r.invoice_date.slice(0, yearly ? 4 : 7)))].sort().reverse();
  if (!options.includes(S.view.pick)) S.view.pick = options[0] || "";
  pick.hidden = S.view.kind === "all";
  pick.disabled = !options.length;
  pick.innerHTML = options.length ? options.map(o => `<option value="${o}">${yearly ? o : monthName(o)}</option>`).join("") : `<option value="">No dated invoices yet</option>`;
  pick.value = S.view.pick;
  $("#export").href = `/api/export.xlsx${scope()}`;
  updateFilterCount();
}

async function loadDashboard() {
  const rs = await api("/api/runs"), f = S.view.folder;
  // rejected documents stay off the Dashboard; a folder's Dashboard covers only what was filed in it
  S.history = rs.runs.filter(r => eff(r) !== "REJECT" && (f == null || r.folder_id === f));
  if (S.view.dashTab === "analytics") dashTab("analytics");
  $("#dash-title").textContent = f == null ? "All invoices" : folderName(f) || "Folder";
  renderPeriods();
  const st = await api(`/api/stats${scope()}`);
  const c = st.counts;
  const toHeld = label => c.HOLD ? `<button class="tile-go" type="button" data-show-held aria-label="${label}" title="Show held invoices">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>` : "";
  const heldN = `${c.HOLD} held invoice${c.HOLD === 1 ? "" : "s"}`;
  const money = v => { const s = inr0(v); return `<div class="v${s.length > 11 ? " v-long" : ""}">${s}</div>`; };   // tens of crores get a smaller figure
  $("#tally").innerHTML = `
    <div class="tile-count"><div class="v">${st.total}</div><div class="l">Invoices processed</div><div class="s">${st.avg_seconds}s average per invoice</div></div>
    <div class="tile-auto"><div class="v">${st.straight_through}%</div><div class="l">Approved with no human touch</div><div class="s">${c.APPROVE} approved in total</div></div>
    <div class="tile-paid">${money(st.approved_value)}<div class="l">Approved for payment</div><div class="s">Gross, including GST</div></div>
    <div class="tile-held-amount tile-go-host">${money(st.pending_value)}<div class="l">Held for payment</div>
      <div class="s">${c.HOLD ? `In ${heldN} · Gross, incl. GST` : "Nothing is on hold"}</div>${toHeld(`Show the ${heldN}, ${inr0(st.pending_value)} in all`)}</div>
    <div class="tile-held tile-go-host"><div class="v">${c.HOLD}</div><div class="l">Waiting for a person</div><div class="s">${inr0(st.pending_value)} on hold</div>
      ${toHeld(`Show the ${heldN}`)}</div>`;
  const tot = Math.max(st.total, 1);
  $("#split-note").textContent = st.total ? `${st.total} invoice${st.total === 1 ? "" : "s"} processed` : "";
  $("#split").innerHTML = st.total ? `<div class="split" role="img" aria-label="${c.APPROVE} approved, ${c.HOLD} held">
      ${c.APPROVE ? `<i class="ok" style="flex-grow:${c.APPROVE}"></i>` : ""}${c.HOLD ? `<i class="warn" style="flex-grow:${c.HOLD}"></i>` : ""}</div>
    <div class="split-legend"><div class="ok"><b>${c.APPROVE}</b>Approved</div><div class="warn"><b>${c.HOLD}</b>Held for review</div></div>
    ${c.HOLD ? `<button class="split-call" type="button" data-show-held><span>${c.HOLD} invoice${c.HOLD === 1 ? " needs" : "s need"} a closer look</span>
      <span class="split-go">Review invoices <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg></span></button>` : ""}`
    : `<p class="muted">${S.view.folder != null && !period() ? "Nothing in this folder yet. File an invoice from its stamp page."
      : period() ? "No invoices in this period." : "Nothing processed yet. Process a document to see it here."}</p>`;
  const mx = Math.max(...st.reasons.map(r => r.count), 1);
  $("#reasons").innerHTML = st.reasons.length ? st.reasons.map(r => `<div class="rbar"><span>${esc(CODE_LABEL[r.code] || r.code)}</span><span class="t"><i style="width:${r.count / mx * 100}%"></i></span><b>${r.count}</b></div>`).join("")
    : `<p class="muted">No held or rejected invoices yet.</p>`;
  renderHistory();
}

function historyRow(r) {
  const d = DEC[eff(r)] || DEC.HOLD, why = r.codes.length ? esc(CODE_LABEL[r.codes[0]] || r.codes[0]) + (r.codes.length > 1 ? ` <span class="muted">+${r.codes.length - 1}</span>` : "") : '<span class="muted">All checks passed</span>';
  const received = r.created_at ? `Received ${new Date(r.created_at).toLocaleString("en-IN")}` : "";
  return `<tr class="click" tabindex="0" data-open="${r.id}"><td title="${received}">${r.invoice_date ? esc(dayName(r.invoice_date)) : none()}</td><td>${esc(r.filename)}</td><td>${val(r.vendor_name)}</td><td>${val(r.invoice_number)}</td>
    <td>${esc(r.po_number || "–")}</td><td class="right">${r.total != null ? inr(r.total) : "–"}</td>
    <td><span class="mini ${d.cls}">${d.label}</span>${r.final_decision ? ' <span class="muted" title="Resolved by a person">by a person</span>' : ""}</td><td>${why}</td><td class="muted">${esc((r.extractor || "").split(":")[0])}</td>
    <td class="row-actions"><button class="icon-btn row-remove" type="button" data-remove="${r.id}" title="${DELETE_TITLE}"
      aria-label="Delete ${esc(r.filename)} permanently">${REMOVE_ICON}</button></td></tr>`;
}

function historyGroup(key, rows) {
  const g = S.view.group, id = `${g}:${key}`, open = !S.collapsed.has(id);
  const total = rows.reduce((t, r) => t + (r.total || 0), 0), list = rows.map(historyRow).join("");
  if (g === "none") return `<tbody>${list}</tbody>`;
  return `<tbody><tr class="group-row"><td colspan="10"><button class="group-toggle" type="button" data-group="${esc(id)}" aria-expanded="${open}">
      <span class="caret" aria-hidden="true">${open ? "▾" : "▸"}</span><span>${esc(GROUPS[g].name(key))}</span>
      <span class="count">${rows.length} invoice${rows.length !== 1 ? "s" : ""}</span><span class="sum">${inr(total)}</span></button></td></tr>${open ? list : ""}</tbody>`;
}

function renderHistory() {
  const done = S.history.filter(r => r.status === "done"), term = S.view.search.trim().toLowerCase(), p = period();
  const found = r => !term || [r.invoice_number, r.vendor_name, r.po_number, r.filename].some(v => (v || "").toLowerCase().includes(term));
  const rows = done.filter(r => (S.filter === "ALL" || eff(r) === S.filter) && (r.invoice_date || "").startsWith(p) && found(r))
    .sort((a, b) => (b.invoice_date || "").localeCompare(a.invoice_date || ""));  // newest first, undated last
  $$("#filters .chip").forEach(b => b.classList.toggle("on", b.dataset.f === S.filter));
  updateFilterCount();
  $("#history-count").textContent = `${rows.length} invoice${rows.length === 1 ? "" : "s"}${S.filter === "ALL" ? "" : `, ${DEC[S.filter].label.toLowerCase()}`}`;

  const undated = p ? done.filter(r => !r.invoice_date).length : 0, note = $("#undated");
  note.hidden = !undated;
  note.textContent = `${undated} invoice${undated !== 1 ? "s have" : " has"} no invoice date, so ${undated !== 1 ? "they appear" : "it appears"} only under All time.`;

  const group = GROUPS[S.view.group], byKey = new Map();
  rows.forEach(r => { const k = group.key(r) || ""; byKey.set(k, [...(byKey.get(k) || []), r]); });
  const keys = [...byKey.keys()].sort((a, b) => !a - !b || (group.newest ? b.localeCompare(a) : a.localeCompare(b)));
  $("#history").innerHTML = `<thead><tr><th>Invoice date</th><th>File</th><th>Vendor</th><th>Invoice no.</th><th>PO</th><th class="right">Total</th><th>Outcome</th><th>Why</th><th>Read by</th><th aria-label="Remove"></th></tr></thead>
    ${rows.length ? keys.map(k => historyGroup(k, byKey.get(k))).join("")
      : `<tbody><tr><td class="empty-row" colspan="10">${S.history.length ? "No invoices match these filters." : "No invoices yet."}</td></tr></tbody>`}`;
  if (document.body.classList.contains("sheet-open")) renderSheet();   // the phone's filter page shows the new counts and total
}

/* --------------------------------------------------------------- analytics -- */
/* The Dashboard's second tab, for the folder and period chosen above it. Charts follow one set of rules: thin marks,
   hairline grid, a legend whenever there's more than one series, a tooltip on hover or keyboard focus, and every chart
   also readable as a table, so no value is reachable only by hovering. */
const GRAINS = { day: { label: "Daily", key: d => d }, month: { label: "Monthly", key: d => d.slice(0, 7) }, year: { label: "Yearly", key: d => d.slice(0, 4) } };
const inrShort = v => v >= 1e7 ? `₹${(v / 1e7).toFixed(1).replace(/\.0$/, "")}Cr` : v >= 1e5 ? `₹${(v / 1e5).toFixed(1).replace(/\.0$/, "")}L`
  : v >= 1e3 ? `₹${(v / 1e3).toFixed(0)}k` : `₹${Math.round(v)}`;
const barW = pct => pct > 0 ? `width:max(2px, ${pct}%)` : "width:0";   // a real zero draws no bar, a tiny value still shows
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const shortDay = iso => new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short" });

async function loadAnalytics() {
  const box = $("#dash-analytics");
  box.classList.add("refreshing");                           // keep the last render, dimmed, instead of a blank flash
  let a;
  try { a = await api(`/api/analytics${scope()}`); } catch (e) { box.innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  box.classList.remove("refreshing");
  if (!a.total) {
    box.innerHTML = `<p class="muted viz-empty">${S.view.folder != null ? "Nothing in this folder yet." : period() ? "No invoices in this period." : "Nothing processed yet."} The charts fill in as invoices are decided.</p>`;
    return;
  }
  box.innerHTML = [outcomeCard(a), spendCard(a), flagsCard(a), vendorCard(a), batchCard(a)].join("");
}

const vizCard = (title, sub, body, table, wide = true, label = title) => `<section class="viz-card${wide ? " wide" : ""}" aria-label="${esc(label)}">
  ${title || sub ? `<div class="viz-head">${title ? `<h2>${title}</h2>` : ""}${sub ? `<p class="muted">${sub}</p>` : ""}</div>` : ""}${body}
  <details class="viz-table"><summary>Show as table</summary><div class="table-wrap">${table}</div></details></section>`;
const tbl = (head, rows) => `<table class="greenbar compact"><thead><tr>${head.map((h, i) => `<th${i ? ' class="right"' : ""}>${h}</th>`).join("")}</tr></thead>
  <tbody>${rows.map(r => `<tr>${r.map((c, i) => `<td${i ? ' class="right"' : ""}>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;

/* 1. what needs a person now: value and count by outcome, and the money on hold by how long it has waited */
function outcomeCard(a) {
  const o = a.outcome, ages = a.hold_ages, mx = Math.max(...ages.map(x => x.value), 1);
  const tile = (k, note) => `<div class="viz-tile"><span class="mini ${DEC[k].cls}"><span aria-hidden="true">${SEV_MARK[{ APPROVE: "info", HOLD: "hold", REJECT: "reject" }[k]]}</span> ${DEC[k].label}</span>
    <div class="v">${inr0(o[k].value)}</div><div class="s">${plural(o[k].count, "invoice")}${note ? ` · ${note}` : ""}</div></div>`;
  const body = `<div class="viz-tiles">${tile("HOLD", "waiting for you")}${tile("APPROVE", "for payment")}${tile("REJECT", "")}</div>
    ${o.HOLD.count ? `<div class="viz-bars">${ages.map(x => `<div class="viz-bar-row" tabindex="0" data-tip="${esc(`${x.label}: ${inr0(x.value)} on hold, ${plural(x.count, "invoice")}`)}">
      <span>${x.label}</span><span class="track"><i class="warn" style="${barW(x.value / mx * 100)}"></i></span><b>${x.count ? `${inr0(x.value)} <span class="muted">· ${x.count}</span>` : '<span class="muted">none</span>'}</b></div>`).join("")}</div>`
      : `<p class="muted">Nothing is on hold.</p>`}`;
  return vizCard("", "", body,
    tbl(["", "Invoices", "Value"], [...["HOLD", "APPROVE", "REJECT"].map(k => [DEC[k].label, o[k].count, inr0(o[k].value)]),
      ...ages.map(x => [`On hold ${x.label.toLowerCase()}`, x.count, inr0(x.value)])]), true, "Outcomes and money on hold");
}

/* 2. money approved for payment over time, by invoice date, at the grain picked in the card */
function spendSeries(a) {
  const g = GRAINS[S.view.grain || "month"], sums = new Map();
  a.spend.forEach(([d, v]) => sums.set(g.key(d), (sums.get(g.key(d)) || 0) + v));
  const keys = [...sums.keys()].sort();
  if (keys.length > 1) {                                     // empty periods inside the range count as nothing spent, not as missing
    const all = [], next = { day: k => { const d = new Date(`${k}T00:00:00Z`); d.setUTCDate(d.getUTCDate() + 1); return d.toISOString().slice(0, 10); },
      month: k => { const [y, m] = k.split("-").map(Number); return m === 12 ? `${y + 1}-01` : `${y}-${String(m + 1).padStart(2, "0")}`; },
      year: k => String(+k + 1) }[S.view.grain || "month"];
    for (let k = keys[0]; k <= keys[keys.length - 1] && all.length < 800; k = next(k)) all.push(k);
    return all.map(k => [k, sums.get(k) || 0]);
  }
  return keys.map(k => [k, sums.get(k)]);
}
const grainLabel = (k, grain) => grain === "day" ? dayName(k) : grain === "month" ? monthName(k) : k;

function spendCard(a) {
  const grain = S.view.grain || "month", pts = spendSeries(a);
  const picker = `<div class="seg" role="group" aria-label="Show spend">${Object.entries(GRAINS).map(([k, g]) =>
    `<button type="button" data-grain="${k}" aria-pressed="${k === grain}">${g.label}</button>`).join("")}</div>`;
  const body = pts.length ? `${picker}${lineChart(pts, grain)}` : `${picker}<p class="muted">No approved invoices with a date yet.</p>`;
  return vizCard("Spend over time", "", body, tbl([grain === "day" ? "Day" : grain === "month" ? "Month" : "Year", "Approved"], pts.map(([k, v]) => [grainLabel(k, grain), inr0(v)])));
}

function lineChart(pts, grain) {
  const W = 760, H = 230, L = 62, R = 16, T = 14, B = 30, n = pts.length;
  const top = Math.max(...pts.map(p => p[1]), 1), nice = (() => { const e = 10 ** Math.floor(Math.log10(top)); return Math.ceil(top / e / 2) * 2 * e; })();
  const x = i => n === 1 ? L + (W - L - R) / 2 : L + i * (W - L - R) / (n - 1), y = v => T + (H - T - B) * (1 - v / nice);
  const grid = [0, 0.25, 0.5, 0.75, 1].map(f => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(nice * f)}" y2="${y(nice * f)}"/>
    <text class="tick" x="${L - 8}" y="${y(nice * f) + 4}" text-anchor="end">${inrShort(nice * f)}</text>`).join("");
  const every = Math.max(1, Math.ceil(n / 6)), short = k => grain === "day" ? shortDay(k) : grain === "month" ? new Date(`${k}-01T00:00:00`).toLocaleDateString("en-IN", { month: "short", year: "2-digit" }) : k;
  const xs = pts.map((p, i) => i % every === 0 || i === n - 1 ? `<text class="tick" x="${x(i)}" y="${H - 8}" text-anchor="middle">${short(p[0])}</text>` : "").join("");
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const dots = n <= 40 ? pts.map((p, i) => `<circle class="dot" cx="${x(i)}" cy="${y(p[1])}" r="4"/>`).join("") : "";
  const data = esc(JSON.stringify(pts.map((p, i) => [grainLabel(p[0], grain), Math.round(p[1]), +x(i).toFixed(1), +y(p[1]).toFixed(1)])));
  return `<div class="viz-line"><svg viewBox="0 0 ${W} ${H}" role="img" tabindex="0" aria-label="Approved spend per ${grain}, ${n} points. Use the arrow keys to read each point, or open the table."
      data-line="${data}">${grid}<line class="axis" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>${xs}
    <path class="series" d="${line}"/>${dots}<line class="cross" y1="${T}" y2="${H - B}" hidden/><circle class="cross-dot" r="5" hidden/>
    <rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}"/></svg></div>`;
}

/* 3. fraud and control flags, kept out of the general hold chart so the dangerous ones aren't hidden */
function flagsCard(a) {
  const group = (kind, title, what, cls, mark) => {
    const list = a.flags[kind], total = list.reduce((s, x) => s + x.count, 0), mx = Math.max(...list.map(x => x.count), 1);
    return `<div class="viz-flags ${cls}"><p class="viz-flag-title"><span class="mini ${cls}"><span aria-hidden="true">${mark}</span> ${title}</span><b>${total}</b></p>
      <p class="muted">${what}</p>
      ${list.length ? `<div class="viz-bars">${list.map(x => `<div class="viz-bar-row" tabindex="0" data-tip="${esc(`${CODE_LABEL[x.code] || x.code}: ${plural(x.count, "invoice")}`)}">
        <span>${esc(CODE_LABEL[x.code] || x.code)}</span><span class="track"><i class="${cls}" style="${barW(x.count / mx * 100)}"></i></span><b>${x.count}</b></div>`).join("")}</div>`
        : `<p class="muted">None.</p>`}</div>`;
  };
  const rows = ["call", "resend"].flatMap(k => a.flags[k].map(x => [k === "call" ? "Call the vendor" : "Ask for a resend", esc(CODE_LABEL[x.code] || x.code), x.count]));
  return vizCard("", "",
    `<div class="viz-two">${group("call", "Call the vendor", "Possible fraud: an identity, PO or amount that doesn't fit what you have on record.", "bad", "✕")}
      ${group("resend", "Ask for a resend", "Document problems: a GSTIN or GST shown wrongly, sums that don't add up, details missing.", "warn", "!")}</div>`,
    `<table class="greenbar compact"><thead><tr><th>Action</th><th>Flag</th><th class="right">Invoices</th></tr></thead>
      <tbody>${rows.map(r => `<tr><td>${r[0]}</td><td>${r[1]}</td><td class="right">${r[2]}</td></tr>`).join("") || '<tr><td colspan="3" class="muted">No flags.</td></tr>'}</tbody></table>`, true, "Fraud and control flags");
}

/* 4. vendor scorecard: who sends the most, how often it stops, and why, so one message can replace many */
function vendorCard(a) {
  const mxN = Math.max(...a.vendors.map(v => v.invoices), 1);
  const rows = a.vendors.map(v => `<tr><td>${esc(v.name)}</td>
    <td class="right"><span class="inline-bar" tabindex="0" data-tip="${esc(`${v.name}: ${plural(v.invoices, "invoice")}, ${inr0(v.value)}`)}"><i style="${barW(v.invoices / mxN * 100)}"></i></span>${v.invoices}</td>
    <td class="right"><span class="inline-bar" tabindex="0" data-tip="${esc(`${v.name}: ${v.stopped} of ${v.invoices} stopped`)}"><i class="warn" style="${barW(v.hold_rate)}"></i></span>${v.hold_rate}%</td>
    <td>${v.top_reason ? `${esc(CODE_LABEL[v.top_reason] || v.top_reason)} <span class="muted">× ${v.top_reason_count}</span>` : '<span class="muted">Never stopped</span>'}</td>
    <td class="right">${inr0(v.value)}</td></tr>`).join("");
  const table = `<table class="greenbar viz-score"><thead><tr><th>Vendor</th><th class="right">Invoices</th><th class="right">Stopped</th><th>Top reason</th><th class="right">Value</th></tr></thead><tbody>${rows}</tbody></table>`;
  return vizCard("Vendor scorecard", "",
    `<div class="table-wrap">${table}</div>`,
    tbl(["Vendor", "Invoices", "Stopped", "Stop rate", "Top reason", "Value"], a.vendors.map(v => [esc(v.name), v.invoices, v.stopped, `${v.hold_rate}%`, v.top_reason ? esc(CODE_LABEL[v.top_reason] || v.top_reason) : "–", inr0(v.value)])));
}

/* 5. batch throughput: invoices per upload, seconds each, and how many cleared without a person */
function batchCard(a) {
  const bs = a.batches, multi = bs.filter(b => b.batch), n = multi.reduce((s, b) => s + b.invoices, 0);
  const cleared = multi.reduce((s, b) => s + b.cleared, 0), secs = n ? multi.reduce((s, b) => s + b.avg_seconds * b.invoices, 0) / n : 0;
  const label = b => b.batch ? `${esc(b.first)}${b.invoices > 1 ? ` <span class="muted">+ ${b.invoices - 1} more</span>` : ""}` : "Single files";
  const when = b => b.batch ? new Date(b.started).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
  const tiles = multi.length ? `<div class="viz-tiles small">
      <div class="viz-tile"><div class="v">${(n / multi.length).toFixed(1).replace(/\.0$/, "")}</div><div class="s">invoices per upload, ${plural(multi.length, "upload")}</div></div>
      <div class="viz-tile"><div class="v">${secs.toFixed(1)}s</div><div class="s">of checking per invoice</div></div>
      <div class="viz-tile"><div class="v">${n ? Math.round(cleared / n * 100) : 0}%</div><div class="s">cleared without a person (${cleared} of ${n})</div></div></div>`
    : `<p class="muted">No multi-file uploads yet. Choose several files, or drop a folder, on the New document page.</p>`;
  const rows = bs.map(b => `<tr><td>${label(b)}</td><td class="muted">${when(b)}</td><td class="right">${b.invoices}</td><td class="right">${b.avg_seconds}s</td>
    <td class="right"><span class="inline-bar" tabindex="0" data-tip="${esc(`${b.cleared} of ${b.invoices} cleared without a person`)}"><i class="ok" style="${barW(b.cleared_pct)}"></i></span>${b.cleared_pct}%</td></tr>`).join("");
  return vizCard("Batch throughput", "",
    `${tiles}<div class="table-wrap"><table class="greenbar viz-score"><thead><tr><th>Upload</th><th>Started</th><th class="right">Invoices</th><th class="right">Seconds each</th><th class="right">Cleared alone</th></tr></thead><tbody>${rows}</tbody></table></div>`,
    tbl(["Upload", "Invoices", "Seconds each", "Cleared without a person"], bs.map(b => [b.batch ? esc(b.first) : "Single files", b.invoices, `${b.avg_seconds}s`, `${b.cleared} (${b.cleared_pct}%)`])));
}

/* the tooltip, the line chart's crosshair, the grain buttons and the stop bars */
function vizTip(target, text, ev) {
  const tip = $("#viz-tip");
  if (!target) { tip.hidden = true; return; }
  tip.textContent = text;
  tip.hidden = false;
  const r = target.getBoundingClientRect(), px = ev && ev.clientX ? ev.clientX : r.left + r.width / 2, py = ev && ev.clientY ? ev.clientY : r.top;
  tip.style.left = `${Math.min(window.innerWidth - tip.offsetWidth - 8, Math.max(8, px - tip.offsetWidth / 2))}px`;
  tip.style.top = `${Math.max(8, py - tip.offsetHeight - 12)}px`;
}

function lineAt(svg, i, ev) {
  const pts = JSON.parse(svg.dataset.line), p = pts[Math.max(0, Math.min(pts.length - 1, i))];
  svg.dataset.at = pts.indexOf(p);
  const cross = $(".cross", svg), dot = $(".cross-dot", svg);
  cross.setAttribute("x1", p[2]); cross.setAttribute("x2", p[2]); cross.hidden = false;
  dot.setAttribute("cx", p[2]); dot.setAttribute("cy", p[3]); dot.hidden = false;
  const box = svg.getBoundingClientRect(), scale = box.width / svg.viewBox.baseVal.width;
  vizTip(svg, `${p[0]}: ${inr0(p[1])} approved`, { clientX: box.left + p[2] * scale, clientY: box.top + p[3] * scale });
}

function wireAnalytics() {
  const box = $("#dash-analytics");
  box.addEventListener("mouseover", ev => { const t = ev.target.closest("[data-tip]"); if (t) vizTip(t, t.dataset.tip, ev); });
  box.addEventListener("mouseout", ev => {
    if (ev.target.closest("[data-tip]")) vizTip(null);
    const svg = ev.target.closest("svg[data-line]");
    if (svg && !svg.contains(ev.relatedTarget)) { $$(".cross, .cross-dot", svg).forEach(el => { el.hidden = true; }); vizTip(null); }
  });
  box.addEventListener("focusin", ev => { const t = ev.target.closest("[data-tip]"); if (t) vizTip(t, t.dataset.tip); });
  box.addEventListener("focusout", () => vizTip(null));
  box.addEventListener("mousemove", ev => {
    const svg = ev.target.closest("svg[data-line]"); if (!svg) return;
    const pts = JSON.parse(svg.dataset.line), box2 = svg.getBoundingClientRect(), vx = (ev.clientX - box2.left) / box2.width * svg.viewBox.baseVal.width;
    let best = 0; pts.forEach((p, i) => { if (Math.abs(p[2] - vx) < Math.abs(pts[best][2] - vx)) best = i; });
    lineAt(svg, best, ev);
  });
  box.addEventListener("keydown", ev => {
    const svg = ev.target.closest("svg[data-line]"); if (!svg || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(ev.key)) return;
    ev.preventDefault();
    const n = JSON.parse(svg.dataset.line).length, at = svg.dataset.at == null ? -1 : +svg.dataset.at;
    lineAt(svg, { ArrowLeft: at - 1, ArrowRight: at + 1, Home: 0, End: n - 1 }[ev.key]);
  });
  box.addEventListener("click", ev => {
    const g = ev.target.closest("[data-grain]");
    if (g) { S.view.grain = g.dataset.grain; saveView(); loadAnalytics(); }
  });
}

/* light or dark: every colour is a token in app.css, so this flips one attribute. theme.js reapplies it on the next load */
function setTheme(theme, save = true) {
  if (theme === "dark") document.documentElement.dataset.theme = "dark";
  else delete document.documentElement.dataset.theme;
  if (save) try { localStorage.setItem("invoice-flow-theme", theme); } catch (e) { /* this visit only */ }
  $$("[data-theme-pick]").forEach(b => b.setAttribute("aria-checked", String(b.dataset.themePick === theme)));
}

function dashTab(name) {
  S.view.dashTab = name; saveView();
  $$("[data-dash-tab]").forEach(b => b.setAttribute("aria-selected", String(b.dataset.dashTab === name)));
  $("#dash-overview").hidden = name !== "overview";
  $("#filter-bar").classList.toggle("on-analytics", name === "analytics");   // the History filters have nothing to filter there
  updateFilterCount();
  $("#dash-analytics").hidden = name !== "analytics";
  if (name === "analytics") loadAnalytics();
}

/* ------------------------------------------------------------------ ledger -- */
let LEDGER = { pos: [], vendors: [] };                     // what the PO ledger page last showed, for its two forms

async function loadLedger() {
  const { pos, vendors } = LEDGER = await api("/api/pos");
  $("#vendor-table").innerHTML = `<thead><tr><th>ID</th><th>Name</th><th>GSTIN</th><th>Status</th><th>Source</th><th>Approval note</th><th></th></tr></thead><tbody>
    ${vendors.map(v => `<tr><td>${esc(v.vendor_id)}</td><td>${esc(v.name)}</td><td>${esc(v.gstin) || '<span class="muted">none</span>'}</td>
      <td><span class="mini ${v.status === "approved" ? "ok" : "bad"}">${esc(v.status)}</span></td><td class="muted">${esc(v.source)}</td><td>${esc(v.note)}</td>
      <td>${v.source === "approved in app" ? `<button class="btn quiet" type="button" data-remove-vendor="${esc(v.vendor_id)}">Remove</button>` : ""}</td></tr>`).join("")}</tbody>`;
  $("#po-table").innerHTML = `<thead><tr><th>PO</th><th>Vendor</th><th>For</th><th class="right">PO value</th><th>Billed so far</th><th class="right">Open</th><th>Approved invoices</th></tr></thead><tbody>
    ${pos.map(p => {
      const cls = p.pct_billed > 100.01 ? "over" : p.pct_billed >= 99.9 ? "full" : "";
      const mine = p.source === "added in app";
      return `<tr><td><b>${esc(p.po_number)}</b>${mine ? `<span class="po-mine">added by you${p.invoices.length ? "" : ` · <button class="link-btn" type="button" data-remove-po="${esc(p.po_number)}">Remove</button>`}</span>` : ""}</td>
        <td>${esc(p.vendor_name)}</td><td>${esc(p.description)}</td><td class="right">${inr(p.net_amount)}<span class="po-gst">+ ${pct(p.gst_rate, 2)} GST</span></td>
        <td><div class="pobar ${cls}" role="img" aria-label="${pct(p.pct_billed)} billed"><i style="width:${Math.min(p.pct_billed, 100)}%"></i></div><span class="muted">${inr(p.billed)} (${pct(p.pct_billed)})</span></td>
        <td class="right">${inr(Math.max(p.remaining, 0))}</td>
        <td><div class="po-inv">${p.invoices.length ? p.invoices.map(i => `<span class="pill">${esc(i.invoice_number || "no number")}: ${inr0(i.net_amount)}</span>`).join("") : '<span class="muted">none yet</span>'}</div></td></tr>`;
    }).join("")}</tbody>`;
}

/* the PO ledger's two forms: a new purchase order, and a new vendor (which a new PO can open on the way, then come back) */
const GST_RATES = [0, 0.25, 3, 5, 12, 18, 28, 40];
const PO_ICON = ICON(`<path d="M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10A.5.5 0 0 1 6 20V4a.5.5 0 0 1 .5-.5z"/><path d="M14 3.5V8h4M9 12h6M9 15.5h6"/>`);
const VENDOR_ICON = ICON(`<path d="M4 9.5 5.5 4h13L20 9.5M4 9.5h16M4 9.5a2.7 2.7 0 0 0 5.3 0 2.7 2.7 0 0 0 5.4 0 2.7 2.7 0 0 0 5.3 0M5.5 12v8h13v-8M10 20v-4.5h4V20"/>`);
const localDay = () => { const d = new Date(); return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };

function nextPoNumber() {                                  // PO-<year>-<next number>, following the register's own pattern
  const y = new Date().getFullYear();
  const n = Math.max(1000, ...LEDGER.pos.map(p => /^PO-(\d{4})-(\d+)$/i.exec(p.po_number)).filter(m => m && +m[1] === y).map(m => +m[2]));
  return `PO-${y}-${n + 1}`;
}

function ledgerDialog(kind, keep = {}) {
  const dlg = $("#ledger-dialog"), approved = LEDGER.vendors.filter(v => v.status === "approved");
  const head = (icon, title, sub) => `<div class="modal-head">${icon}<h2>${title}</h2><button class="icon-btn" type="button" data-close aria-label="Close">${REMOVE_ICON}</button></div>
    <p class="muted lf-sub">${sub}</p>`;
  const actions = label => `<p class="auth-error" data-error hidden></p>
    <div class="lf-actions"><button class="btn" type="button" data-close>Cancel</button><button class="btn primary" type="submit">${label}</button></div>`;
  dlg.innerHTML = kind === "po"
    ? `<form class="modal-body ledger-form" novalidate>${head(PO_ICON, "New purchase order", "Invoices that cite this PO are matched and billed against it, like any PO in your register.")}
        <label>PO number<input name="po_number" required maxlength="40" autocomplete="off" value="${esc(nextPoNumber())}"></label>
        <label>Vendor<span class="lf-row"><select name="vendor_id" required><option value="">Choose a vendor</option>
          ${approved.map(v => `<option value="${esc(v.vendor_id)}">${esc(v.name)}</option>`).join("")}</select>
          <button class="btn" type="button" data-new-vendor>＋ New</button></span></label>
        <label>What it's for<input name="description" required maxlength="200" placeholder="Office chairs for the Jodhpur branch"></label>
        <div class="lf-two"><label>PO value before GST (₹)<input name="net_amount" type="number" inputmode="decimal" min="0.01" step="0.01" required placeholder="50000"></label>
          <label>GST<select name="gst_rate">${GST_RATES.map(r => `<option value="${r}"${r === 18 ? " selected" : ""}>${r}%</option>`).join("")}</select></label></div>
        <label>PO date<input name="po_date" type="date" value="${localDay()}"></label>
        <p class="lf-total" data-total></p>${actions("Add purchase order")}</form>`
    : `<form class="modal-body ledger-form" novalidate>${head(VENDOR_ICON, "New vendor", "Their bills are matched to this vendor by name and GSTIN, so they aren't held as an unknown vendor.")}
        <label>Vendor name<input name="name" required maxlength="200" autocomplete="off" placeholder="Belle Boutique"></label>
        <label>GSTIN <small>optional, 15 characters</small><input name="gstin" maxlength="15" autocomplete="off" placeholder="08AABCS1234F1Z5" class="lf-code"></label>
        <label>Other names on their bills <small>optional, separated by commas</small><input name="aliases" maxlength="600" placeholder="Belle, Belle Tailors"></label>
        <label class="wz-check"><input type="checkbox" name="no_po"><span><b>This vendor doesn't use purchase orders</b>
          <small>For a shop, restaurant or clinic. Its bills won't be held for having no PO.</small></span></label>
        <label>Note <small>optional</small><input name="note" maxlength="500" placeholder="How was this vendor verified?"></label>
        ${actions(keep.back ? "Add vendor and go back" : "Add vendor")}</form>`;
  const form = $("form", dlg), error = $("[data-error]", dlg);
  Object.entries(keep.values || {}).forEach(([k, v]) => { const el = form.elements[k]; if (el) el.type === "checkbox" ? (el.checked = !!v) : (el.value = v); });
  const total = $("[data-total]", dlg);
  const sum = () => { if (!total) return; const n = parseFloat(form.elements.net_amount.value), r = parseFloat(form.elements.gst_rate.value);
    total.textContent = n > 0 ? `With GST: ${inr(Math.round(n * (1 + r / 100) * 100) / 100)}` : ""; };
  form.addEventListener("input", sum); sum();
  dlg.onclick = ev => {
    if (kind === "vendor" && keep.back && ev.target.closest("[data-close]")) ledgerDialog("po", { values: keep.back });   // back to the PO, as typed
    else if (ev.target === dlg || ev.target.closest("[data-close]")) dlg.close();
    else if (ev.target.closest("[data-new-vendor]")) ledgerDialog("vendor", { back: Object.fromEntries(new FormData(form)) });
  };
  form.onsubmit = async ev => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(form)), button = $("button[type=submit]", form);
    if (kind === "vendor") f.no_po = !!form.elements.no_po.checked;
    error.hidden = true; button.disabled = true;
    try {
      const d = await api(kind === "po" ? "/api/pos" : "/api/vendors", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(f) });
      await loadLedger();
      if (kind === "vendor" && keep.back) { ledgerDialog("po", { values: { ...keep.back, vendor_id: d.vendor_id } }); return; }
      dlg.close();
      toast(kind === "po" ? `${d.po_number} added to your PO register.` : `${f.name.trim()} added to your vendors as ${d.vendor_id}.`);
    } catch (e) { error.textContent = e.message; error.hidden = false; button.disabled = false; }
  };
  if (!dlg.open) dlg.showModal();
  (form.querySelector("input:not([type=checkbox])") || form).focus();
}

/* ------------------------------------------------------------------- rules -- */
async function loadRules() {
  const c = S.cfg, r = c.rules, e = c.extractor;
  const who = e.provider === "Gemini" ? "Google Gemini" : esc(e.provider || "Claude");
  const reading = e.llm && e.mode !== "rules" ? `A PDF's own text layer is used when it has one, and scans and photos are read on this computer with OCR (PP-OCRv6).
       ${who} (${esc(e.model)}) then looks at the page image together with that text, handwriting included, and returns the fields. Every value is checked against the document text, and you check them on the Review page.
       If the model can't be reached, the built-in rules take over.`
    : `A PDF's own text layer is used when it has one. Scans and photos are read on this computer with OCR (PP-OCRv6), and words it is unsure of, usually handwriting, get a second, closer read. Built-in rules then pick out the fields, and you check them on the Review page.
       ${e.key_rejected ? `${esc(e.key_problem || "The model key can't be used")}, so documents are read with the built-in rules and OCR.` : `For difficult handwriting or unusual layouts, add a Google Gemini key (GEMINI_API_KEY in .env) to have Gemini read the page too.`}`;
  $("#rules").innerHTML = `
    <p class="lede">Every decision is the worst finding across nine checks. Nothing is scored in secret: the reasons shown for each invoice are the rules below, firing.</p>
    <div class="rules-grid" style="margin-top:22px">
      <div class="rules-col"><h2><span class="mini ok">Approved</span></h2><ul>
        <li>It is an invoice, and every key field was found</li><li>The vendor is on the approved list</li><li>Amounts add up and GST is accounted for</li>
        <li>It matches a PO, named or clearly implied</li><li>Cumulative billing is within ${r.tolerance_pct}% of the PO value</li><li>It is not a duplicate</li></ul></div>
      <div class="rules-col"><h2><span class="mini warn">Held</span></h2><ul>
        <li>Invoice number, date, vendor or total is missing</li><li>Amounts don't add up</li><li>Vendor is unknown, or the GSTIN doesn't fit</li>
        <li>A value typed at upload disagrees with the document</li><li>It may not be an invoice: some invoice details, but no title</li>
        <li>PO is missing, unknown, or ambiguous</li><li>Billing is ${r.tolerance_pct}% to ${r.hold_band_pct}% over the PO</li><li>It looks like a possible duplicate</li></ul></div>
      <div class="rules-col"><h2><span class="mini bad">Rejected</span></h2><ul>
        <li>It is a quotation, receipt or other non-invoice, or shows almost nothing an invoice would (a person can overrule this)</li><li>The vendor is blocked</li><li>Same vendor, invoice number and amount as an earlier one</li>
        <li>Billing is more than ${r.hold_band_pct}% over the PO</li><li>Rejected documents stay off the Dashboard</li></ul></div>
    </div>
    <div class="rules-more">
      <div><h2>Thresholds</h2><dl>
        <dt>Auto-approve up to this much over the PO</dt><dd>${r.tolerance_pct}%</dd>
        <dt>Send to a person up to</dt><dd>${r.hold_band_pct}%</dd>
        <dt>Rounding allowed in arithmetic</dt><dd>${inr(r.arith_abs_tol)} or ${pct(r.arith_rel_tol * 100)}</dd>
        <dt>Vendor name match without a GSTIN</dt><dd>${pct(r.vendor_name_strong * 100, 0)}</dd>
        <dt>Confidence needed to infer a PO</dt><dd>${pct(r.implied_po_min_score * 100, 0)}, with a ${pct(r.implied_po_min_margin * 100, 0)} lead</dd>
        <dt>Warn if an invoice is older than</dt><dd>${r.stale_days} days</dd></dl></div>
      <div><h2>How documents are read</h2><p>${reading}</p>
        <p class="muted" style="margin-top:10px">Dates are checked as of ${esc(c.as_of)}. Billing is always judged against the PO's pre-tax value, so GST is backed out of totals that include it.</p></div>
    </div>`;
}

/* ------------------------------------------------------------------ drawer -- */
async function openDrawer(id) {
  const run = await api(`/api/runs/${id}`);
  if (run.status !== "done" && run.status !== "error") { location.hash = "#process"; return; }
  const body = $("#drawer-body");
  // two columns: the bill (it stays in view while the steps scroll) with its folder, and the steps and decision
  body.innerHTML = `<div class="drawer-grid">
      <aside class="drawer-bill">
        <button class="bill-thumb" type="button" data-enlarge title="Enlarge the bill">
          <img src="/api/runs/${id}/preview" alt="First page of ${esc(run.filename)}">
          <span class="bill-zoom">${ZOOM_ICON}<span>Enlarge</span></span></button>
        <div class="drawer-folder" data-role="drawer-folder">${drawerFolder(run)}</div>
        <a class="btn quiet drawer-open" href="/api/runs/${id}/file" target="_blank" rel="noopener">Open the original document</a>
      </aside>
      <div class="drawer-main"></div>
    </div>`;
  const view = new RunView($(".drawer-main", body), id, run.filename);
  (run.events || []).forEach(ev => view.apply(ev));
  if (!(run.events || []).some(e => e.stage === "decision")) view.renderVerdict(run);
  DRAWER_RUN = run;
  $("#drawer").hidden = false; $("#scrim").hidden = false;
  $("#drawer-close").focus();
}
function closeDrawer() { closeLightbox(); $("#drawer").hidden = true; $("#scrim").hidden = true; DRAWER_RUN = null; if (location.hash === "#dashboard") loadDashboard(); }

let DRAWER_RUN = null;                                     // the invoice open in the detail panel
const ZOOM_ICON = ICON('<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.4-4.4M11 8.5v5M8.5 11h5"/>');

/* the detail panel's folder box: where the invoice is filed, and a way to file it (never a rejected one) */
function drawerFolder(run) {
  const name = folderName(run.folder_id);
  if (eff(run) === "REJECT") return `<p class="wz-folder-title">${FOLDER_ICON}<span>Folder</span></p><p class="muted">Rejected invoices can't be saved to a folder.</p>`;
  return `<p class="wz-folder-title">${FOLDER_ICON}<span>Folder</span></p>
    <p class="muted">${name ? `Saved in <b>${esc(name)}</b>.` : "Not in a folder yet."}</p>
    <button class="btn" type="button" data-drawer-folder>${FOLDER_ICON}<span>${name ? "Change folder" : "Add to folder"}</span></button>`;
}

function openLightbox(src, alt) {
  const box = $("#lightbox"), img = $("#lightbox-img");
  img.src = src; img.alt = alt; img.classList.remove("actual");
  box.hidden = false;
  $("#lightbox-close").focus();
}
function closeLightbox() {
  const box = $("#lightbox");
  if (box.hidden) return false;
  box.hidden = true;
  const back = $("[data-enlarge]", $("#drawer-body"));
  if (back) back.focus();
  return true;
}

/* ------------------------------------------------------------------ routing -- */
function route() {
  const tab = (location.hash || "#process").slice(1);
  const name = ["process", "dashboard", "ledger", "rules"].includes(tab) ? tab : "process";
  $$(".view").forEach(v => v.hidden = v.id !== `view-${name}`);
  $$(".tabs a").forEach(a => a.toggleAttribute("aria-current", a.dataset.tab === name));
  $$(".tabs a").forEach(a => a.dataset.tab === name ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
  renderFolders();                                         // the open folder is highlighted while its Dashboard shows
  refreshVisible();
}
function refreshVisible() {
  const tab = (location.hash || "#process").slice(1);
  if (tab === "dashboard") loadDashboard(); else if (tab === "ledger") loadLedger(); else if (tab === "rules") loadRules();
}

/* ------------------------------------------------------------ sort and filter sheet -- */
/* On a phone the Dashboard's filters take two lines, as on Flipkart: the search, then one line that slides sideways and
   starts with Sort and Filters, followed by every filter (Folder, Dated, the outcome chips). Sort, Filters and the Folder /
   Dated chips open a full-screen filter page: categories on the left, their options with counts on the right. The page
   only sets the bar's own controls and fires their change events, so every filter behaves as it does on a wide screen */
const onAnalytics = () => $("#filter-bar").classList.contains("on-analytics");
const FS_TABS = [["sort", "Sort"], ["folder", "Folder"], ["dated", "Dated"], ["outcome", "Outcome"]];
let fsTab = "folder";
function updateFilterCount() {
  const badge = $("#filter-count"); if (!badge) return;
  const n = [S.view.folder != null, S.view.kind !== "all", !onAnalytics() && S.filter !== "ALL"].filter(Boolean).length;
  badge.hidden = !n; badge.textContent = n || "";
  const folder = $("#folder-pick"), kind = $("#period-kind"), pick = $("#period-pick");
  $("#fm-folder").textContent = folder.selectedOptions[0] ? folder.selectedOptions[0].textContent : "All invoices";
  $("#fm-dated").textContent = kind.value === "all" || pick.disabled || !pick.selectedOptions[0] ? "All time" : pick.selectedOptions[0].textContent;
  $$(".fm-pick").forEach(b => b.classList.toggle("on", b.dataset.sheet === "folder" ? S.view.folder != null : S.view.kind !== "all"));
}
function fsOptions(name, options, current) {      // options: [value, label, count or null]
  return options.map(([value, label, count]) => `<label class="fs-opt"><input type="radio" name="${name}" value="${esc(value)}" ${String(value) === String(current) ? "checked" : ""}>
    <span class="fs-box" aria-hidden="true"></span><span class="fs-label">${esc(label)}</span>${count == null ? "" : `<span class="fs-n">${count}</span>`}</label>`).join("");
}
function fsRows() {                                // the invoices in view for the chosen period: what the counts are of
  const p = period();
  return S.history.filter(r => r.status === "done" && (!p || (r.invoice_date || "").startsWith(p)));
}
function renderSheet(tab = fsTab) {
  const active = document.activeElement, inSheet = active && active.closest && active.closest("#sheet");
  const refocus = inSheet && active.name ? `#sheet-body input[name="${active.name}"]:checked` : inSheet && active.dataset.fsTab ? `[data-fs-tab="${active.dataset.fsTab}"]` : null;
  drawSheet(tab);
  if (refocus && $(refocus)) $(refocus).focus();          // a redraw keeps keyboard focus where it was
}
function drawSheet(tab) {
  const tabs = FS_TABS.filter(([k]) => !onAnalytics() || k === "folder" || k === "dated");
  if (!tabs.some(([k]) => k === tab)) tab = tabs[0][0];
  fsTab = tab;
  $("#fs-nav").innerHTML = tabs.map(([k, label]) => `<button type="button" role="tab" id="fs-tab-${k}" data-fs-tab="${k}" aria-selected="${k === tab}"
    aria-controls="sheet-body" tabindex="${k === tab ? 0 : -1}">${label}${k === "folder" && S.view.folder != null || k === "dated" && S.view.kind !== "all"
      || k === "outcome" && S.filter !== "ALL" ? '<i class="fs-dot" aria-label="(set)"></i>' : ""}</button>`).join("");
  $("#sheet-body").setAttribute("aria-labelledby", `fs-tab-${tab}`);
  const body = $("#sheet-body"), pick = $("#period-pick"), rows = fsRows();
  if (tab === "sort") {
    body.innerHTML = `<p class="fs-h">Group the history by</p>${fsOptions("group", [...$("#group-by").options].map(o => [o.value, o.textContent, null]), $("#group-by").value)}`;
  } else if (tab === "folder") {
    const counts = Object.fromEntries(S.folders.map(f => [String(f.id), f.count]));
    body.innerHTML = fsOptions("folder", [...$("#folder-pick").options].map(o => [o.value, o.textContent, o.value ? counts[o.value] ?? 0 : null]), $("#folder-pick").value);
  } else if (tab === "dated") {
    const dated = S.history.filter(r => r.status === "done" && r.invoice_date), yearly = S.view.kind === "year";
    const inPick = v => dated.filter(r => r.invoice_date.startsWith(v)).length;
    body.innerHTML = fsOptions("kind", [...$("#period-kind").options].map(o => [o.value, o.textContent, o.value === "all" ? S.history.filter(r => r.status === "done").length : null]), $("#period-kind").value)
      + (!pick.hidden && !pick.disabled ? `<p class="fs-h">${yearly ? "Year" : "Month"}</p>${fsOptions("pick", [...pick.options].map(o => [o.value, o.textContent, inPick(o.value)]), pick.value)}` : "");
  } else {
    const n = k => rows.filter(r => eff(r) === k).length;
    body.innerHTML = fsOptions("outcome", $$("#filters .chip").map(c => [c.dataset.f, c.textContent, c.dataset.f === "ALL" ? rows.length : n(c.dataset.f)]), S.filter);
  }
  $("#fs-total").textContent = onAnalytics() ? "" : $("#history-count").textContent;
}
function openSheet(tab) {
  renderSheet(tab);
  $("#sheet").hidden = false;
  requestAnimationFrame(() => document.body.classList.add("sheet-open"));
  ($("#sheet-body input:checked") || $("#sheet-body input") || $("[data-sheet-close]")).focus();
}
function closeSheet() {
  if ($("#sheet").hidden) return;
  document.body.classList.remove("sheet-open");
  setTimeout(() => { if (!document.body.classList.contains("sheet-open")) $("#sheet").hidden = true; }, 250);
  const opener = $(`.f-mobile [data-sheet="${fsTab}"]`) || $(".f-mobile [data-sheet]"); if (opener) opener.focus();
}
function setControl(sel, value) {       // as if the person had picked it in the bar
  const el = $(sel); if (!el || el.value === value) return;
  el.value = value; el.dispatchEvent(new Event("change", { bubbles: true }));
}
function wireSheet() {
  document.body.appendChild($("#sheet"));    // a full-screen page of its own: no page-level layout rule can offset it
  $("#filter-bar").addEventListener("click", ev => { const b = ev.target.closest("[data-sheet]"); if (b) openSheet(b.dataset.sheet); });
  const refresh = (focusName) => setTimeout(() => {      // the dashboard reloads after a change; then show the new counts
    if (!document.body.classList.contains("sheet-open")) return;
    renderSheet(); const again = focusName && $(`#sheet-body input[name="${focusName}"]:checked`); if (again) again.focus();
  }, 380);
  $("#sheet").addEventListener("click", ev => {
    if (ev.target.closest("[data-sheet-close]")) { closeSheet(); return; }
    const t = ev.target.closest("[data-fs-tab]");
    if (t) { renderSheet(t.dataset.fsTab); $(`[data-fs-tab="${fsTab}"]`).focus(); return; }
    if (ev.target.closest("[data-sheet-clear]")) {
      setControl("#folder-pick", ""); setControl("#period-kind", "all");
      if (S.filter !== "ALL") $('#filters [data-f="ALL"]').click();
      refresh();
    }
  });
  $("#fs-nav").addEventListener("keydown", ev => {       // up / down move between categories, as in a vertical tab list
    if (!["ArrowDown", "ArrowUp"].includes(ev.key)) return;
    ev.preventDefault();
    const tabs = $$("[data-fs-tab]"), i = tabs.findIndex(b => b.dataset.fsTab === fsTab);
    const next = tabs[(i + (ev.key === "ArrowDown" ? 1 : tabs.length - 1)) % tabs.length];
    renderSheet(next.dataset.fsTab); $(`[data-fs-tab="${fsTab}"]`).focus();
  });
  $("#sheet").addEventListener("change", ev => {
    const r = ev.target; if (r.type !== "radio") return;
    if (r.name === "group") setControl("#group-by", r.value);
    if (r.name === "folder") setControl("#folder-pick", r.value);
    if (r.name === "kind") setControl("#period-kind", r.value);
    if (r.name === "pick") setControl("#period-pick", r.value);
    if (r.name === "outcome") $(`#filters [data-f="${r.value}"]`).click();
    updateFilterCount();
    refresh(r.name);
  });
  document.addEventListener("keydown", ev => { if (ev.key === "Escape") closeSheet(); });
}

/* ------------------------------------------------------------------ sidebar menu -- */
/* the ☰ button, as on YouTube. On a wide screen it switches the sidebar sideways between full (labels and folders) and a
   narrow rail of icons, which leaves the page, and the upload, as much width as possible; the rail is where it starts, and
   the choice is remembered in this browser. On a phone it slides the folders in from the left. Wired before init's first
   request, so the button works at once, even while a slow connection is still loading the page */
const narrow = () => matchMedia("(max-width: 860px)").matches;
function syncMenu() {
  const body = document.body, open = narrow() ? body.classList.contains("drawer-open") : !body.classList.contains("rail");
  $("#menu").setAttribute("aria-expanded", String(open));
  $("#menu").setAttribute("aria-label", narrow() ? (open ? "Close folders" : "Open folders") : (open ? "Collapse sidebar" : "Expand sidebar"));
}
function setRail(on, remember = true) {
  document.body.classList.toggle("rail", on);
  syncMenu();
  if (remember) try { localStorage.setItem("invoice-flow-sidebar", on ? "rail" : "full"); } catch (e) { /* storage blocked */ }
}
function setDrawer(open) {
  document.body.classList.toggle("drawer-open", open);
  syncMenu();
}
function wireMenu() {
  let saved = null;
  try { saved = localStorage.getItem("invoice-flow-sidebar"); } catch (e) { /* storage blocked */ }
  setRail(saved === "rail", false);                          // the full sidebar unless the person shrank it to icons
  $("#menu").addEventListener("click", () => narrow() ? setDrawer(!document.body.classList.contains("drawer-open"))
                                                      : setRail(!document.body.classList.contains("rail")));
  document.addEventListener("keydown", ev => { if (ev.key === "Escape" && document.body.classList.contains("drawer-open")) setDrawer(false); });
  document.addEventListener("click", ev => {     // a tap on the dimmed page closes the drawer
    if (document.body.classList.contains("drawer-open") && !ev.target.closest("#side-folders, #menu, .modal")) setDrawer(false);
  });
  matchMedia("(max-width: 860px)").addEventListener("change", () => { setDrawer(false); syncMenu(); });
}

/* --------------------------------------------------------------------- init -- */
async function init() {
  wireMenu();
  wireSheet();
  [S.cfg, { samples: S.scenarios }] = await Promise.all([api("/api/config"), api("/api/samples")]);
  Object.assign(CODE_LABEL, S.cfg.labels);
  const e = S.cfg.extractor, llm = e.llm && e.mode !== "rules";
  $("#engine").innerHTML = `Reading with ${llm ? `<b>${e.provider === "Gemini" ? "Google Gemini" : esc(e.provider || "Claude")}</b> (${esc(e.model)})` : "<b>built-in rules</b>"}${e.ocr ? " and OCR" : ""}<br>Dates checked as of ${esc(S.cfg.as_of)}`
    + (e.key_problem ? `<br><span class="engine-warn" title="${esc(e.key_problem)}">${/GEMINI/.test(e.key_problem) ? "Gemini key rejected: fix it to read handwriting" : "Model key can't be used"}</span>` : "");
  const { runs } = await api("/api/runs");           // survive a page reload
  runs.slice().reverse().forEach(r => S.runs.set(r.id, { id: r.id, filename: r.filename, status: r.status === "done" || r.status === "error" ? "done" : r.status,
    decision: eff(r), batch: r.batch_id, review: r.status === "review" }));
  await loadFolders();
  renderEmpty();

  $("#logout").addEventListener("click", async () => { await fetch("/api/logout", { method: "POST" }); location.reload(); });
  $$('.tabs a[data-tab="dashboard"]').forEach(a => a.addEventListener("click", () => {     // the Dashboard tab means all invoices
    S.view.folder = null;
    if (location.hash === "#dashboard") { renderFolders(); refreshVisible(); }
  }));
  $("#new-folder").addEventListener("click", () => folderDialog({ title: "Folders", current: S.view.folder, pick: async id => openFolder(id) }));
  $("#folders").addEventListener("click", ev => {
    const open = ev.target.closest("[data-folder-open]"), reset = ev.target.closest("[data-folder-reset]");
    if (open) { openFolder(+open.dataset.folderOpen); setDrawer(false); }   // a phone's drawer gets out of the way
    else if (reset) resetFolder(reset);
  });
  $("#folder-pick").addEventListener("change", ev => { S.view.folder = ev.target.value ? +ev.target.value : null; renderFolders(); loadDashboard(); });
  $("#vendor-table").addEventListener("click", async ev => {
    const b = ev.target.closest("[data-remove-vendor]"); if (!b) return;
    if (!confirm(`Remove ${b.dataset.removeVendor} from the approved list? Its invoices already processed stay as they are.`)) return;
    try { await api(`/api/vendors/${b.dataset.removeVendor}`, { method: "DELETE" }); loadLedger(); } catch (e) { alert(e.message); }
  });
  $("#add-po").addEventListener("click", () => ledgerDialog("po"));
  $$("#add-vendor, [data-add-vendor]").forEach(b => b.addEventListener("click", () => ledgerDialog("vendor")));
  $("#po-table").addEventListener("click", async ev => {
    const b = ev.target.closest("[data-remove-po]"); if (!b) return;
    if (!confirm(`Remove ${b.dataset.removePo} from your PO register? Nothing has been billed against it.`)) return;
    try { await api(`/api/pos/${b.dataset.removePo.split("/").map(encodeURIComponent).join("/")}`, { method: "DELETE" }); loadLedger(); } catch (e) { alert(e.message); }
  });
  // the drop zone and the progress bar are redrawn with each page, so their events are caught on the page around them
  const input = $("#file"), stage = $("#run"), dropZone = ev => ev.target.closest && ev.target.closest(".drop");
  input.addEventListener("change", () => { if (input.files.length) uploadFiles(input.files); input.value = ""; });
  const folderInput = $("#folder");   // the folder picker: every PDF and photo in the chosen folder (and its subfolders), as one batch
  folderInput.addEventListener("change", () => { if (folderInput.files.length) uploadFiles(folderInput.files); folderInput.value = ""; });
  stage.addEventListener("keydown", ev => { if (dropZone(ev) && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); input.click(); } });
  ["dragenter", "dragover"].forEach(n => stage.addEventListener(n, ev => { const d = dropZone(ev); if (d) { ev.preventDefault(); d.classList.add("over"); } }));
  ["dragleave", "drop"].forEach(n => stage.addEventListener(n, ev => { const d = dropZone(ev); if (d) { ev.preventDefault(); d.classList.remove("over"); } }));
  stage.addEventListener("drop", async ev => { if (dropZone(ev)) { const files = await droppedFiles(ev.dataTransfer); if (files.length) uploadFiles(files); } });
  stage.addEventListener("click", ev => {
    if (ev.target.closest("[data-pick-folder]")) { folderInput.click(); return; }
    const saveAll = ev.target.closest("[data-batch-folder]");
    if (saveAll) { saveBatchToFolder(saveAll.dataset.batchFolder); return; }
    const waiting = ev.target.closest("[data-run]");
    if (waiting) { S.follow = false; showRun(waiting.dataset.run, false); return; }
    if (ev.target.closest("[data-run-all]")) { runScenarios(S.scenarios.map(s => s.file)); return; }
    const sample = ev.target.closest("[data-file]");
    if (sample) {
      const r = latestFor(sample.dataset.file);
      if (!r) runScenarios([sample.dataset.file]);                                  // never run: run it
      else if (r.status === "done") { S.follow = false; showRun(r.id, false); }     // already run: show what happened
      return;
    }
    const step = ev.target.closest("[data-goto]");
    if (!step) return;
    if (step.dataset.goto === "1") newDocument();
    else if (VIEW) VIEW.show(+step.dataset.goto, true);
  });

  $$("[data-theme-pick]").forEach(b => b.addEventListener("click", () => setTheme(b.dataset.themePick)));
  setTheme(document.documentElement.dataset.theme === "dark" ? "dark" : "light", false);
  $$("[data-dash-tab]").forEach(b => b.addEventListener("click", () => dashTab(b.dataset.dashTab)));
  wireAnalytics();
  $("#dash-overview").addEventListener("click", ev => {    // the tiles' arrows and "Review invoices": all held invoices, in History
    if (!ev.target.closest("[data-show-held]")) return;
    S.filter = "HOLD";
    renderHistory();
    $("#history-section").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("#filters").addEventListener("click", ev => { const b = ev.target.closest("[data-f]"); if (!b) return; S.filter = b.dataset.f; renderHistory(); });
  $("#group-by").addEventListener("change", ev => { S.view.group = ev.target.value; saveView(); renderHistory(); });
  $("#history-search").addEventListener("input", ev => { S.view.search = ev.target.value; renderHistory(); });
  $("#period-kind").addEventListener("change", ev => { S.view.kind = ev.target.value; S.view.pick = ""; saveView(); loadDashboard(); });
  $("#period-pick").addEventListener("change", ev => { S.view.pick = ev.target.value; loadDashboard(); });
  $("#history").addEventListener("click", ev => {
    const remove = ev.target.closest("[data-remove]");
    if (remove) { deleteRun(remove); return; }
    const g = ev.target.closest("[data-group]");
    if (g) {
      if (!S.collapsed.delete(g.dataset.group)) S.collapsed.add(g.dataset.group);
      renderHistory();
      return;
    }
    const tr = ev.target.closest("[data-open]"); if (tr) openDrawer(tr.dataset.open);
  });
  $("#history").addEventListener("keydown", ev => {
    if (ev.key !== "Enter" || ev.target.closest("button")) return;   // Enter on a button inside the row is the button's
    const tr = ev.target.closest("[data-open]"); if (tr) openDrawer(tr.dataset.open);
  });
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", ev => {
    if (ev.key !== "Escape") return;
    if (!closeLightbox() && !$("#drawer").hidden) closeDrawer();   // the enlarged bill closes first, then the panel
  });
  $("#drawer-body").addEventListener("click", ev => {
    if (!DRAWER_RUN) return;
    const enlarge = ev.target.closest("[data-enlarge]");
    if (enlarge) {                                           // a sharper copy from the server; the offline demo's embedded picture as it is
      const src = $("img", enlarge).getAttribute("src");
      openLightbox(src.startsWith("/api/") ? `${src}?large=1` : src, `The bill: ${DRAWER_RUN.filename}`);
      return;
    }
    if (!ev.target.closest("[data-drawer-folder]")) return;
    const run = DRAWER_RUN;
    folderDialog({ title: "Add to a folder", current: run.folder_id, allowNone: true, pick: async folderId => {
      await api(`/api/runs/${run.id}/folder`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder_id: folderId }) });
      run.folder_id = folderId;
      await loadFolders();
      const box = $("[data-role=drawer-folder]");
      if (box && DRAWER_RUN === run) box.innerHTML = drawerFolder(run);
      toast(folderId == null ? "Taken out of its folder." : `Saved in ${folderName(folderId)}.`);
    } });
  });
  $("#lightbox").addEventListener("click", ev => {
    const img = ev.target.closest("#lightbox-img");
    if (img) { img.classList.toggle("actual"); return; }      // click the picture: actual size, scrollable; again: fit
    closeLightbox();                                           // the dark area or the Close button
  });
  window.addEventListener("hashchange", route);
  loadView();
  route();
  if (window.Guide) Guide.maybeStart();   // Shiven's tour, the first time someone signs in to an empty account
}

/* --------------------------------------------------------------------- login -- */
function showAuth() {
  const login = $("#auth-form"), signup = $("#signup-form");
  $("#auth").hidden = false;
  $("#landing").hidden = false;                              // what the app does comes first; the sign-in card is a scroll below
  /* two tabs: Log in, and Create account (username, password, confirm password), which signs straight in */
  const tab = (name, focus = true) => {
    const creating = name === "signup";
    login.hidden = creating; signup.hidden = !creating;
    $$('[role="tab"][data-auth-tab]').forEach(b => b.setAttribute("aria-selected", String(b.dataset.authTab === name)));
    $("#auth-title").textContent = creating ? "Create your account" : "Sign in to Invoice Flow";
    if (focus) $("[name=username]", creating ? signup : login).focus({ preventScroll: true });
  };
  const reveal = (name, smooth = true) => {                  // "Get started" / "Log in" on the landing: open that tab and scroll to the card
    tab(name, false);
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("#auth").scrollIntoView({ behavior: smooth && !still ? "smooth" : "auto", block: "start" });
    setTimeout(() => $("[name=username]", name === "signup" ? signup : login).focus({ preventScroll: true }), smooth && !still ? 450 : 0);
  };
  $$("[data-auth-tab]").forEach(b => b.addEventListener("click", ev => { ev.preventDefault(); tab(b.dataset.authTab); }));
  $$("[data-lp-go]").forEach(b => b.addEventListener("click", ev => { ev.preventDefault(); reveal(b.dataset.lpGo); }));
  const send = async (form, error, url, body) => {
    error.hidden = true;
    const button = $("button[type=submit]", form); button.disabled = true;
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (r.ok) {
        if (url === "/api/signup") try { sessionStorage.setItem("invoice-flow-new-account", body.username); } catch (e) { /* the tour still starts: no runs yet */ }
        location.hash = ""; location.reload(); return;          // a fresh page: nothing from another login stays in memory
      }
      error.textContent = (await r.json().catch(() => ({}))).detail || "Something went wrong. Try again.";
    } catch (e) { error.textContent = "Couldn't reach the server. Check your connection and try again."; }
    error.hidden = false; button.disabled = false;
  };
  login.addEventListener("submit", ev => {
    ev.preventDefault();
    if (!login.reportValidity()) return;
    const f = Object.fromEntries(new FormData(login));
    send(login, $("#auth-error"), "/api/login", { username: f.username, password: f.password });
  });
  signup.addEventListener("submit", ev => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(signup)), error = $("#signup-error");
    if (!signup.reportValidity()) return;
    if (f.password !== f.confirm) { error.textContent = "The passwords don't match."; error.hidden = false; $("[name=confirm]", signup).focus(); return; }
    send(signup, error, "/api/signup", { username: f.username, password: f.password, confirm: f.confirm });
  });
  // a link to …/#signup or …/#login opens straight on that card; so does a Google sign-in that came back with an error
  if (location.hash === "#signup" || location.hash === "#login") reveal(location.hash.slice(1), false);
  else if (!$("#auth-error").hidden) reveal("login", false);
  else tab("login", false);
}

async function boot() {
  const r = await fetch("/api/me");
  if (r.status === 401) { showAuth(); return; }
  document.body.classList.remove("signed-out");
  S.me = (await r.json()).username;
  $("#who-name").textContent = S.me;
  $("#who-avatar").textContent = S.me.charAt(0).toUpperCase();
  $("#who").hidden = false;
  await init();
}
boot().catch(err => { document.body.insertAdjacentHTML("afterbegin", `<p style="padding:20px;color:#B3261E">Could not start: ${esc(err.message)}</p>`); });
