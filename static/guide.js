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

  /* one page of the processing, explained while it is on screen. The page waits for the person: the tour holds the
     wizard (no automatic move to the next page) and moves on when they tap the page's own Next button */
  const page = (n, title, text, ready, waiting) => ({
    target: () => $(`#run .wz-card[data-step="${n}"]`) || $("#run .wz-card"), point: "#run [data-next]", title, tap: true, hold: true, live: true,
    text: () => `${text}<span class="g-now">${$("#run [data-next]") ? `${ready} <b>Tap Next on the page</b> when you're ready.` : waiting}</span>`,
    done: () => +(($("#run .wz-card") || {}).dataset || {}).step > n,
  });
  const card = (label, title, text) => ({ target: `#dash-analytics .viz-card[aria-label="${label}"]`, title, text, wait4: true });

  const STEPS = () => [
    { title: "Hey, I'm Shiven! Welcome to Invoice Flow", next: "Let's go",
      text: "Invoice Flow checks your bills so you don't have to. I'll show you every part of the app and process your first invoice with you, " +
        "one step at a time. <b>Nothing moves on until you tap</b>, so take as long as you like to read." },
    { target: "#menu", title: "The menu", tap: true,
      text: () => narrow() ? "Tap the menu to see your folders." : "Tap the menu to open the full sidebar. Tap it again later to shrink it to icons, so the page gets more room.",
      done: () => narrow() ? document.body.classList.contains("drawer-open") : !document.body.classList.contains("rail") },
    { target: "#side-folders", title: "Folders", text: "Folders keep invoices together, like Food bills or Rent. Each folder gets its own dashboard, and New folder makes one.",
      after: () => { if (narrow()) setDrawer(false); } },
    { target: ".theme-switch", title: "Light or dark", text: "Pick whichever is easier on your eyes. Invoice Flow remembers your choice." },
    { target: ".drop", title: "Your invoices go here", before: () => { location.hash = "#process"; if (S.current || typeof VIEW !== "undefined" && VIEW) newDocument(); },
      text: "Drop PDFs or photos here, choose several at once, a whole folder, or a ZIP. Clean PDFs, scans and phone photos all work." },
    { title: "Let's process your first invoice", place: "center", action: "Process a sample invoice",
      text: "I've got a sample invoice ready: a real-looking tax invoice from a supplier. Tap the button and I'll walk you through each of the six pages it goes through, from reading it to the final stamp.",
      run: async () => { demo = pickSample(); await runScenarios([demo]); hold(); } },
    page(2, "1 · Read the document",
      "First Invoice Flow opens the file and reads every page. A clean PDF already has its words inside, so they're copied out exactly. " +
      "A scan or a phone photo is a picture, so it goes through <b>OCR</b> (optical character recognition), which turns the picture into text. " +
      "The page it's reading is shown beside the summary.",
      "Done: the text is in.", "Reading now. Photos take a few seconds longer."),
    page(3, "2 · Pull out the details",
      "Now it finds what an accounts team needs from all that text: <b>who the vendor is</b>, the <b>invoice number</b>, the <b>date</b> and the <b>total</b>, " +
      "plus the GSTIN, the PO number, the tax and every line item. Each value is checked against the document word for word, so nothing is made up. " +
      "Anything it can't find says <b>Not found</b> instead of guessing.",
      "Got the details.", "Picking out the details now."),
    page(4, "3 · Review what was read",
      "Here's everything side by side with the page itself. A <b>✓</b> next to a field means it was found word for word in the document. " +
      "When you upload your own invoices, this is where you stop to <b>fix or fill in</b> anything, add a note, or approve a vendor you haven't used before. " +
      "For this sample everything was read cleanly, so there's nothing to change.",
      "Looks right.", "Getting the review ready."),
    page(5, "4 · Run the checks",
      "Now the rules run, one after another, and each line turns green, amber or red: " +
      "<b>Validate</b> (do the lines, GST and total add up, at the right tax rate?), <b>Vendor</b> (is it on your approved list, with a matching GSTIN?), " +
      "<b>Purchase order</b> (does it match a PO you raised?), <b>Duplicates</b> (has this bill been seen before?) and " +
      "<b>PO balance</b> (is there enough left on the PO to pay it?).",
      "All the checks are done.", "The checks are still running…"),
    { target: () => $("#run .wz-card .headline") || $("#run .wz-outcome"), title: "5 · The decision, and why", hold: true,
      text: () => `${verdictLine()} The system weighs everything the checks found and makes one call: <b>Approved</b> (all clear, ready to pay), ` +
        "<b>Held</b> (something needs a person, and the reason says what) or <b>Rejected</b> (it can't be paid, like a blocked vendor or a duplicate). " +
        "The reasons are listed underneath, so nothing is a black box. Below them you can save the invoice to a folder." },
    { target: () => $("#run [data-stamp]") || $("#run .wz-decide"), title: () => $("#run .wz-decide") ? "6 · Your call" : "6 · Stamp it", tap: true, hold: true,
      text: () => $("#run .wz-decide") ? "This one is held for a person: that's you. Tap Approve, Keep on hold or Reject, and add a note for the audit trail if you like."
        : "Happy with the decision? Tap <b>Next</b> to stamp it.",
      done: () => !!$("#run .wz-stamp") },
    { target: "#run .wz-stamp .stamp", title: "Done! Your first invoice", text: "Every invoice ends with a stamp like this: the decision, " +
      "who made it (the system or a person) and the key details. It's saved in your history and counted on the Dashboard straight away." },
    { target: '.dock a[data-tab="dashboard"]', title: "The Dashboard", tap: true, text: "Tap <b>Dashboard</b> to see the big picture.",
      done: () => location.hash === "#dashboard" },
    { target: "#tally", title: "Your numbers", text: "At a glance: invoices processed, how many passed with no human touch, the money approved for payment, " +
      "the money held, and how many are waiting for you.",
      before: () => { if (typeof dashTab === "function") dashTab("overview"); } },
    { target: () => $("#filter-bar"), title: "Find anything", text: "Filter by folder, month or outcome, group the history, or search for an invoice, vendor or PO. " +
      "On a phone, <b>Sort</b> and <b>Filters</b> open a full-screen page." },
    { target: () => $("#history-section") || $("#history"), title: "History", text: "Every invoice lives here. Tap a row to open its full trail: the page, what was read and every check." },
    { target: '[data-dash-tab="analytics"]', title: "Analytics", tap: true, text: "Now tap <b>Analytics</b> for the deeper view.",
      done: () => { const p = $("#dash-analytics"); return !!p && !p.hidden; } },
    card("Outcomes and money on hold", "Outcomes and money on hold",
      "How much money is <b>held</b> waiting for you, <b>approved</b> for payment and <b>rejected</b>. The bars underneath show how long held money has been waiting, " +
      "so old holds don't get forgotten."),
    card("Spend over time", "Spend over time",
      "Money approved for payment, by invoice date. Switch between <b>Daily</b>, <b>Monthly</b> and <b>Yearly</b>, and hover or tap a point to read it."),
    card("Fraud and control flags", "Fraud and control flags",
      "Problems are split by what to do about them. <b>Call the vendor</b>: possible fraud, like a GSTIN or PO that doesn't fit. " +
      "<b>Ask for a resend</b>: document problems, like sums that don't add up or a missing GSTIN."),
    card("Vendor scorecard", "Vendor scorecard",
      "Every vendor: how many invoices they send, how often theirs stop, the most common reason and the total value. One message to a vendor can fix many holds."),
    card("Batch throughput", "Batch throughput",
      "For each upload of several files or a folder: how many invoices, how many seconds each took to check, and how many cleared without a person."),
    { target: "#export", title: "Download the Excel report", point: "#export",
      text: "One click builds an Excel workbook for exactly what you're looking at: the chosen <b>folder</b> and <b>month</b>. " +
        "Inside: a <b>Summary</b> with charts, an <b>Analytics</b> sheet with everything on this tab, that folder's <b>invoice list</b>, the PO ledger, your vendors and the rejected invoices. " +
        "The charts are real Excel charts, so they're ready for a report." },
    { target: '.dock a[data-tab="ledger"]', title: "PO ledger", text: "Every purchase order: its value, how much has been billed so far and what's still open, plus your approved vendors. " +
      "Raise your own with <b>New purchase order</b>, and add a supplier before its first bill with <b>New vendor</b>." },
    { target: '.dock a[data-tab="rules"]', title: "Rules", text: "Every check and threshold, written out in plain words, so you always know why an invoice was approved or held." },
    { target: "#guide-start", title: "That's the tour!", next: "Finish", before: () => { if (narrow()) return; setRail(false, false); },
      text: "Upload your own invoices whenever you're ready. If you ever need me again, tap Ask Shiven." },
  ];

  function pickSample() {
    const files = (S.scenarios || []).map(s => s.file);
    return PREFERRED.find(f => files.includes(f) && !latestFor(f)) || files.find(f => !latestFor(f)) || PREFERRED[0];
  }
  /* the demo's pages stay put until the person taps Next: no automatic move while they read */
  function hold() {
    if (typeof VIEW === "undefined" || !VIEW || (demo && VIEW.filename !== demo)) return;
    if (!VIEW.manual) { clearTimeout(VIEW.timer); VIEW.timer = null; VIEW.manual = true; if (VIEW.step < 7) VIEW.render(); }
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
    // the hand points at the control to tap: the page's own button when the step names one, else the highlighted control
    const pointEl = step.point ? $(step.point) : null, pr = visible(pointEl) ? pointEl.getBoundingClientRect() : null;
    const aim = pr && pr.bottom > 0 && pr.top < H ? { l: pr.left, t: pr.top, r: pr.right, b: pr.bottom } : step.point ? null : box;
    hand.hidden = !(aim && step.tap);
    if (!hand.hidden) Object.assign(hand.style, { left: `${Math.min(aim.r - 18, W - 44)}px`, top: `${Math.min(aim.b - 10, H - 50)}px` });
    // the panel sits in a corner clear of the target (or at least clear of the button to tap); centred when there is nothing to point at
    const pw = panel.offsetWidth, ph = panel.offsetHeight, m = 16;
    const spots = val(step.place) === "center" || !box ? [[(W - pw) / 2, (H - ph) / 2]]
      : narrow() ? [[m / 2, H - ph - m / 2], [m / 2, m / 2]]
      : [[W - pw - m, H - ph - m], [m, H - ph - m], [W - pw - m, m], [m, m]];
    const off = b => ([x, y]) => !b || x > b.r || x + pw < b.l || y > b.b || y + ph < b.t;
    const [x, y] = spots.find(off(box)) || spots.find(off(aim && { l: aim.l - 12, t: aim.t - 12, r: aim.r + 12, b: aim.b + 12 })) || spots[0];
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
    const step = steps[i], me = n;
    if (step.before) step.before();
    if (step.hold) hold();
    await new Promise(r => setTimeout(r, 120));          // let the page draw what the step points at
    for (let t = 0; step.wait4 && !targetOf(step) && t < 25; t++) await new Promise(r => setTimeout(r, 200));   // a chart still loading
    if (i !== me || !root || !root.isConnected) return;
    const el = targetOf(step);
    // a tall processing page starts at its top so it can be read from the beginning; anything else goes to the middle of the
    // screen: at the bottom a phone's section bar would sit over it (the bar itself stays put)
    if (el && !el.closest(".dock, .sidebar")) {
      const tall = el.getBoundingClientRect().height > innerHeight * 0.7;
      el.scrollIntoView({ block: tall ? "start" : "center", behavior: "smooth" });
    }
    render();
    let pointed = false;
    if (step.done || step.live || step.hold) {            // a step that waits on the person, or on the work
      waitTimer = setInterval(() => {
        if (!root || !root.isConnected) return clearInterval(waitTimer);
        if (step.hold) hold();
        if (step.live || step.wait) $("#g-text", root).innerHTML = val(step.text);
        const p = step.point && $(step.point);              // the page's Next button just appeared: bring it into view once
        if (p && !pointed) { pointed = true; const r = p.getBoundingClientRect(); if (r.bottom > innerHeight - 90 || r.top < 0) p.scrollIntoView({ block: "center", behavior: "smooth" }); }
        if (step.done && step.done()) { clearInterval(waitTimer); setTimeout(() => go(i + 1), step.tap ? 350 : 600); }
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
    // tour closed mid-demo: the invoice's pages carry on by themselves again
    if (demo && typeof VIEW !== "undefined" && VIEW && VIEW.filename === demo && VIEW.manual && !VIEW.review) { VIEW.manual = false; VIEW.advance(); }
    if (finished) try { localStorage.setItem(key(), "done"); } catch (e) { /* storage blocked */ }
    const b = $("#guide-start"); if (b) b.focus({ preventScroll: true });
  }

  /* a brand-new account: Shiven says hello straight away. Otherwise the first sign-in to an empty account starts the tour,
     once per account in this browser */
  function maybeStart() {
    let seen = null, fresh = false;
    try { fresh = sessionStorage.getItem("invoice-flow-new-account") === S.me; sessionStorage.removeItem("invoice-flow-new-account"); } catch (e) { /* no flag */ }
    try { seen = localStorage.getItem(key()); } catch (e) { /* storage blocked: treat as seen, never nag */ seen = "blocked"; }
    if (fresh || (!seen && S.runs.size === 0 && !location.hash.startsWith("#signup"))) setTimeout(start, 700);
  }

  document.addEventListener("click", ev => { if (ev.target.closest("#guide-start")) start(); });
  return { start, stop, maybeStart };
})();
