"use strict";
/* Shiven, the guide: a step-by-step tour for a new user, in the manner of a game tutorial. The screen dims except for
   the thing being explained, Shiven explains it in a speech bubble, and for the steps that matter the person taps the
   highlighted control themselves (a hand points at it) and the tour moves on when they do. The tour includes a first
   demo: a real sample invoice processed from upload to stamp.

   It starts by itself the first time someone signs in to an account with nothing in it, and "Ask Shiven" in the sidebar
   replays it. Uses app.js's own functions (runScenarios, newDocument, setRail, setDrawer, narrow) and its state (S). */
window.Guide = (() => {
  const AVATAR = `<svg viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="31" fill="#654CF4"/>
    <path d="M17 60c3-8 8.5-11.5 15-11.5S44 52 47 60z" fill="#F4CB3D"/><path d="M28 47.5h8v4.5a4 4 0 0 1-8 0z" fill="#E9B48C"/>
    <ellipse cx="32" cy="35.5" rx="14.5" ry="15.5" fill="#F2C29B"/>
    <path d="M17.2 33.5C16.8 22 23.5 15.5 32.5 15.5c8.8 0 15.3 6.2 14.5 18-2.2-5.4-6.6-8.3-12.6-8.6-2.3 2.6-6.6 4.2-11.6 4.4-2.6.2-4.4 1.6-5.6 4.2z" fill="#0E0A20"/>
    <circle cx="26" cy="37" r="4.7" fill="#FFFFFF" fill-opacity=".35" stroke="#0E0A20" stroke-width="2"/>
    <circle cx="38" cy="37" r="4.7" fill="#FFFFFF" fill-opacity=".35" stroke="#0E0A20" stroke-width="2"/>
    <path d="M30.7 37h2.6" stroke="#0E0A20" stroke-width="2"/><circle cx="26" cy="37.4" r="1.5" fill="#0E0A20"/><circle cx="38" cy="37.4" r="1.5" fill="#0E0A20"/>
    <path d="M27.4 44.2c2.6 2.4 6.6 2.4 9.2 0" fill="none" stroke="#0E0A20" stroke-width="2" stroke-linecap="round"/>
    <circle cx="51" cy="51" r="8" fill="#66C79E" stroke="#FFFFFF" stroke-width="2"/><path d="M47.4 51.2l2.5 2.5 4.6-5" fill="none" stroke="#0E0A20" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  const PREFERRED = ["01_happy_path_clean_exact.pdf", "02_tax_embedded_bundled.pdf", "03_po_implied_no_reference.pdf",
                     "07_near_miss_within_tolerance.pdf", "12_scanned_image_clean.pdf", "04_split_invoice_1_of_2.pdf"];
  const $ = (s, r = document) => r.querySelector(s);
  const visible = el => el && el.getClientRects().length && getComputedStyle(el).visibility !== "hidden";
  const key = () => `invoice-flow-guide:${(typeof S !== "undefined" && S.me) || "anon"}`;
  let root, i = 0, steps = [], waitTimer = null, raf = null, demo = null;

  const STEPS = () => [
    { title: "Hi, I'm Shiven!", text: "I'll show you around Invoice Flow and help you process your first invoice. It takes about a minute.",
      next: "Let's go" },
    { target: "#menu", title: "The menu", tap: true,
      text: () => narrow() ? "Tap the menu to see your folders." : "Tap the menu to open the full sidebar. Tap it again later to shrink it to icons, so the page gets more room.",
      done: () => narrow() ? document.body.classList.contains("drawer-open") : !document.body.classList.contains("rail") },
    { target: "#side-folders", title: "Folders", text: "Folders keep invoices together, like Food bills or Rent. Each folder gets its own dashboard, and New folder makes one.",
      after: () => { if (narrow()) setDrawer(false); } },
    { target: ".theme-switch", title: "Light or dark", text: "Pick whichever is easier on your eyes. Invoice Flow remembers your choice." },
    { target: ".drop", title: "Your invoices go here", before: () => { location.hash = "#process"; if (S.current || typeof VIEW !== "undefined" && VIEW) newDocument(); },
      text: "Drop PDFs or photos here, choose several at once, a whole folder, or a ZIP. Clean PDFs, scans and phone photos all work." },
    { title: "Let's process your first invoice", place: "center", action: "Process a sample invoice",
      text: "I've got a sample invoice ready. Tap the button and watch it go from upload to a decision.",
      run: async () => { demo = pickSample(); await runScenarios([demo]); } },
    { target: "#run .stepper", title: "Watch it work", wait: true,
      text: () => `Right now: <b>${stageNow()}</b>. First the document is read, then the vendor, number, date and total are picked out, then the checks run: amounts, vendor, purchase order, duplicates and the PO balance.`,
      done: () => !!$('#run .wz-card[data-step="6"] .wz-outcome') },
    { target: () => $("#run .wz-card .headline") || $("#run .wz-outcome"), title: "The decision, and why",
      text: () => `${verdictLine()} The reasons are listed underneath, so nothing is a black box. Green means approved, amber means held for a person, red means rejected.` },
    { target: () => $("#run [data-stamp]") || $("#run .wz-decide"), title: () => $("#run .wz-decide") ? "Your call" : "Stamp it", tap: true,
      text: () => $("#run .wz-decide") ? "This one is held for a person: that's you. Tap Approve, Keep on hold or Reject." : "Tap Next to stamp the decision.",
      done: () => !!$("#run .wz-stamp") },
    { target: "#run .wz-stamp .stamp", title: "Done!", text: "That's your first invoice processed. Every invoice ends with a stamp like this, showing who decided: the system or a person." },
    { target: '.dock a[data-tab="dashboard"]', title: "The Dashboard", tap: true, text: "Tap Dashboard to see the big picture.",
      done: () => location.hash === "#dashboard" },
    { target: "#tally", title: "Your numbers", text: "At a glance: invoices processed, how many passed with no human touch, the money approved and the money held, and what's waiting for you.",
      before: () => { if (typeof dashTab === "function") dashTab("overview"); } },
    { target: () => $("#filter-bar"), title: "Find anything", text: "Filter by folder, month or outcome, group the history, or search for an invoice, vendor or PO." },
    { target: () => $("#history-section") || $("#history"), title: "History", text: "Every invoice lives here. Tap a row to open its full trail: the page, what was read, every check." },
    { target: '[data-dash-tab="analytics"]', title: "Analytics", text: "Spend over time, money on hold by age, why invoices stop, and a scorecard for each vendor." },
    { target: '.dock a[data-tab="ledger"]', title: "PO ledger", text: "How much of each purchase order has been billed so far, and your approved vendors." },
    { target: '.dock a[data-tab="rules"]', title: "Rules", text: "Every check and threshold, written out, so you always know why an invoice was approved or held." },
    { target: "#guide-start", title: "That's the tour!", next: "Finish", before: () => { if (narrow()) return; setRail(false, false); },
      text: "Upload your own invoices whenever you're ready. If you ever need me again, tap Ask Shiven." },
  ];

  function pickSample() {
    const files = (S.scenarios || []).map(s => s.file);
    return PREFERRED.find(f => files.includes(f) && !latestFor(f)) || files.find(f => !latestFor(f)) || PREFERRED[0];
  }
  function stageNow() {
    const cur = $("#run .stepper li.current .lbl") || $("#run .wz-card h1");
    return cur ? cur.textContent.trim() : "starting";
  }
  function verdictLine() {
    const pill = $("#run .wz-outcome .pill");
    return pill ? `${pill.textContent.trim()}.` : "";
  }
  const val = v => typeof v === "function" ? v() : v;
  const targetOf = step => { const t = val(step.target); const el = typeof t === "string" ? $(t) : t; return visible(el) ? el : null; };

  function build() {
    root = document.createElement("div");
    root.className = "guide";
    root.innerHTML = `<div class="g-block g-top"></div><div class="g-block g-left"></div><div class="g-block g-right"></div><div class="g-block g-bottom"></div>
      <div class="g-spot" aria-hidden="true"></div><div class="g-hand" aria-hidden="true">👆</div>
      <section class="g-panel" role="dialog" aria-modal="false" aria-labelledby="g-title" aria-describedby="g-text">
        <div class="g-avatar">${AVATAR}<span class="g-name">Shiven</span></div>
        <div class="g-bubble"><p class="g-step" id="g-step"></p><h2 id="g-title"></h2><p id="g-text"></p>
          <div class="g-actions"><button class="g-skip" type="button" data-g="skip">Skip tour</button><span class="g-grow"></span>
            <button class="btn" type="button" data-g="back">Back</button><button class="btn primary" type="button" data-g="next">Next</button></div></div>
      </section>`;
    document.body.appendChild(root);
    root.addEventListener("click", async ev => {
      const b = ev.target.closest("[data-g]"); if (!b) return;
      if (b.dataset.g === "skip" || b.dataset.g === "finish") return stop(true);
      if (b.dataset.g === "back") return go(i - 1);
      if (b.dataset.g === "run") { b.disabled = true; try { await steps[i].run(); } catch (e) { alert(e.message); b.disabled = false; return; } return go(i + 1); }
      go(i + 1);
    });
    document.addEventListener("keydown", ev => { if (root && root.isConnected && ev.key === "Escape") stop(true); });
  }

  function place() {                                    // spotlight, click-blockers, hand and panel follow the target
    if (!root || !root.isConnected) return;
    const step = steps[i], el = step && targetOf(step), W = innerWidth, H = innerHeight, pad = 8;
    const r = el ? el.getBoundingClientRect() : null;
    const spot = $(".g-spot", root), hand = $(".g-hand", root), panel = $(".g-panel", root);
    const box = r ? { l: Math.max(r.left - pad, 0), t: Math.max(r.top - pad, 0), r: Math.min(r.right + pad, W), b: Math.min(r.bottom + pad, H) } : null;
    spot.hidden = !box;
    if (box) Object.assign(spot.style, { left: `${box.l}px`, top: `${box.t}px`, width: `${box.r - box.l}px`, height: `${box.b - box.t}px` });
    root.classList.toggle("g-dim-all", !box);
    // the person can tap only the highlighted control, and only on the steps that ask them to
    const hole = box && step.tap ? box : { l: W, t: H, r: W, b: H };
    const set = (cls, s) => Object.assign($(cls, root).style, s);
    set(".g-top", { left: "0", top: "0", width: "100%", height: `${hole.t}px` });
    set(".g-bottom", { left: "0", top: `${hole.b}px`, width: "100%", height: `${Math.max(H - hole.b, 0)}px` });
    set(".g-left", { left: "0", top: `${hole.t}px`, width: `${hole.l}px`, height: `${Math.max(hole.b - hole.t, 0)}px` });
    set(".g-right", { left: `${hole.r}px`, top: `${hole.t}px`, width: `${Math.max(W - hole.r, 0)}px`, height: `${Math.max(hole.b - hole.t, 0)}px` });
    hand.hidden = !(box && step.tap);
    if (!hand.hidden) Object.assign(hand.style, { left: `${Math.min(box.r - 18, W - 44)}px`, top: `${Math.min(box.b - 10, H - 50)}px` });
    // the panel sits in a corner clear of the target; centred when there is nothing to point at
    const pw = panel.offsetWidth, ph = panel.offsetHeight, m = 16;
    const spots = val(step.place) === "center" || !box ? [[(W - pw) / 2, (H - ph) / 2]]
      : narrow() ? [[m / 2, H - ph - m / 2], [m / 2, m / 2]]
      : [[W - pw - m, H - ph - m], [m, H - ph - m], [W - pw - m, m], [m, m]];
    const clear = ([x, y]) => !box || x > box.r || x + pw < box.l || y > box.b || y + ph < box.t;
    const [x, y] = spots.find(clear) || spots[0];
    Object.assign(panel.style, { left: `${Math.max(x, 4)}px`, top: `${Math.max(y, 4)}px` });
    raf = requestAnimationFrame(place);
  }

  function render() {
    const step = steps[i];
    $("#g-step", root).textContent = `Step ${i + 1} of ${steps.length}`;
    $("#g-title", root).textContent = val(step.title);
    $("#g-text", root).innerHTML = val(step.text);
    const next = $('[data-g="next"], [data-g="run"], [data-g="finish"]', root), back = $('[data-g="back"]', root);
    back.hidden = i === 0 || !!step.wait || !!step.tap;
    next.hidden = !!step.wait || (!!step.tap && !step.done());
    next.disabled = false;
    if (step.run) { next.dataset.g = "run"; next.textContent = step.action; }
    else if (i === steps.length - 1) { next.dataset.g = "finish"; next.textContent = step.next || "Finish"; }
    else { next.dataset.g = "next"; next.textContent = step.next || "Next"; }
    $(".g-skip", root).hidden = i === steps.length - 1;
    if (!next.hidden) next.focus({ preventScroll: true });
  }

  async function go(n) {
    clearInterval(waitTimer);
    if (steps[i] && n > i && steps[i].after) steps[i].after();
    if (n < 0) n = 0;
    if (n >= steps.length) return stop(true);
    i = n;
    const step = steps[i];
    if (step.before) step.before();
    await new Promise(r => setTimeout(r, 120));          // let the page draw what the step points at
    const el = targetOf(step);
    // to the middle of the screen: at the bottom a phone's section bar would sit over it (the bar itself stays put)
    if (el && !el.closest(".dock, .sidebar")) el.scrollIntoView({ block: "center", behavior: "smooth" });
    render();
    if (step.done) {                                      // a step that waits on the person, or on the work
      waitTimer = setInterval(() => {
        if (!root || !root.isConnected) return clearInterval(waitTimer);
        if (step.wait) $("#g-text", root).innerHTML = val(step.text);
        if (step.done()) { clearInterval(waitTimer); setTimeout(() => go(i + 1), step.tap ? 350 : 600); }
      }, 300);
    }
  }

  function start() {
    if (root && root.isConnected) return;
    if (!root) build(); else document.body.appendChild(root);
    steps = STEPS();
    document.body.classList.add("guide-on");
    cancelAnimationFrame(raf); place();
    go(0);
  }

  function stop(finished) {
    clearInterval(waitTimer); cancelAnimationFrame(raf);
    if (root) root.remove();
    document.body.classList.remove("guide-on");
    if (finished) try { localStorage.setItem(key(), "done"); } catch (e) { /* storage blocked */ }
    const b = $("#guide-start"); if (b) b.focus({ preventScroll: true });
  }

  /* first sign-in to an empty account: the tour starts by itself, once per account in this browser */
  function maybeStart() {
    let seen = null;
    try { seen = localStorage.getItem(key()); } catch (e) { /* storage blocked: treat as seen, never nag */ seen = "blocked"; }
    if (!seen && S.runs.size === 0 && !location.hash.startsWith("#signup")) setTimeout(start, 700);
  }

  document.addEventListener("click", ev => { if (ev.target.closest("#guide-start")) start(); });
  return { start, stop, maybeStart };
})();
