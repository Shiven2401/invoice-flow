"use strict";
/* Stands in for the server in invoice-flow-demo.html, so the real app.js runs unchanged.
   fetch and EventSource replay what tools/build_demo.py recorded from the real pipeline. */
window.DEMO = (() => {
  const data = JSON.parse(document.getElementById("demo-data").textContent);
  const records = new Map(data.runs.map(r => [r.filename, r]));
  const runs = new Map();                                   // id -> run started in this page, oldest first
  let generation = 0;                                       // Reset ends any replay still playing

  const send = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const needsServer = () => send({ detail: "This demo only replays recorded results. That needs the full app." }, 400);
  const eff = r => r.final_decision || r.decision;
  const started = () => [...runs.values()].reverse();
  const round2 = n => Math.round(n * 100) / 100;

  function view(r) {                                        // a run still in the queue has no outcome to show yet
    return r.status === "done" ? r : { ...r, decision: null, result: null, events: [], codes: [] };
  }

  function stats(period) {
    const all = started(), rs = all.filter(r => r.status === "done" && eff(r) !== "REJECT" && (r.invoice_date || "").startsWith(period)), n = rs.length;
    const sum = (list, key) => list.reduce((t, r) => t + (r[key] || 0), 0);
    const of = k => rs.filter(r => eff(r) === k), reasons = {};
    rs.forEach(r => new Set(r.codes).forEach(c => { reasons[c] = (reasons[c] || 0) + 1; }));
    return {
      total: n, counts: { APPROVE: of("APPROVE").length, HOLD: of("HOLD").length },
      straight_through: n ? Math.round(rs.filter(r => r.decision === "APPROVE").length / n * 100) : 0,
      approved_value: round2(sum(of("APPROVE"), "total")), pending_value: round2(sum(of("HOLD"), "total")),
      avg_seconds: n ? round2(sum(rs, "duration_ms") / n / 1000) : 0,
      reasons: Object.entries(reasons).map(([code, count]) => ({ code, count })).sort((a, b) => b.count - a.count).slice(0, 8),
      queued: all.filter(r => r.status !== "done").length,
    };
  }

  function pos() {
    const posted = [...runs.values()].filter(r => r.status === "done" && eff(r) === "APPROVE" && r.po_number && r.result && r.result.budget);
    return {
      vendors: data.vendors,
      pos: data.pos.map(p => {
        const lines = posted.filter(r => r.po_number === p.po_number);
        const billed = round2(lines.reduce((t, r) => t + r.result.budget.this_net, 0));
        return { ...p, billed, remaining: round2(p.net_amount - billed), pct_billed: Math.round(billed / p.net_amount * 1000) / 10,
          invoices: lines.map(r => ({ run_id: r.id, net_amount: r.result.budget.this_net, invoice_number: r.invoice_number })) };
      }),
    };
  }

  function start(filename) {
    const run = { ...records.get(filename), status: "queued", final_decision: null, override_note: null, created_at: new Date().toISOString() };
    runs.delete(run.id);
    runs.set(run.id, run);
    return { id: run.id, filename };
  }

  window.fetch = async (url, opts = {}) => {
    const method = (opts.method || "GET").toUpperCase();
    if (url === "/api/me") return send({ username: "demo" });
    if (url === "/api/folders") return send({ folders: [] });                 // folders need the full app
    if (url === "/api/config") return send(data.config);
    if (url === "/api/samples") return send({ samples: data.samples });
    if (url.startsWith("/api/stats")) return send(stats(new URLSearchParams(url.split("?")[1]).get("period") || ""));
    if (url === "/api/pos") return send(pos());
    if (url === "/api/reset") { generation++; runs.clear(); return send({ ok: true }); }
    if (url === "/api/runs/sample") return send({ runs: JSON.parse(opts.body).files.map(start) });
    if (url === "/api/runs" && method === "GET") return send({ runs: started().map(view) });
    const m = String(url).match(/^\/api\/runs\/(\w+)(?:\/([\w-]+))?$/), run = m && runs.get(m[1]);
    if (run && !m[2]) return send(view(run));
    if (run && m[2] === "override") {
      const body = JSON.parse(opts.body);
      Object.assign(run, { final_decision: body.decision, override_note: (body.note || "").trim() });
      return send({ ok: true, final_decision: body.decision });
    }
    return needsServer();
  };

  window.EventSource = class {
    static CLOSED = 2;
    constructor(url) {
      this.readyState = 1;
      this.listeners = {};
      this.play(runs.get(url.split("/")[3]));
    }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    close() { this.readyState = 2; }
    async play(run) {
      const mine = generation, t0 = performance.now(), wait = ms => new Promise(go => setTimeout(go, ms));
      await wait(0);                                        // app.js attaches its handlers after constructing this
      run.status = "running";
      for (const [i, ev] of run.events.entries()) {
        await wait(ev.status === "running" ? 300 : 60);
        if (generation !== mine) return;
        ev.t = round2((performance.now() - t0) / 1000);
        if (i === run.events.length - 1) run.status = "done";
        if (this.onmessage) this.onmessage({ data: JSON.stringify(ev) });
      }
      if (this.listeners.end) this.listeners.end();
    }
  };

  document.addEventListener("click", ev => {                // the report is prebuilt: a static file can't generate one
    const link = ev.target.closest('a[href^="/api/export.xlsx"], a[href="/api/vendors.xlsx"]');
    if (!link) return;
    ev.preventDefault();
    const vendorList = link.getAttribute("href") === "/api/vendors.xlsx";
    const bytes = Uint8Array.from(atob(vendorList ? data.vendor_workbook : data.workbook), c => c.charCodeAt(0));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([bytes], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }));
    a.download = vendorList ? "approved-vendors-sample.xlsx" : "invoice-flow-sample-report.xlsx";
    a.click();
  });

  return { preview: id => data.previews[id] };
})();
