"""The Dashboard's Excel report: a summary sheet with native charts, plus the data behind them.

workbook() takes plain data and returns the file's bytes, so it knows nothing about
the web layer. Invoice text comes from uploaded documents and can start with '=', so
every string is written as text and never becomes a live formula.
"""
import io
from datetime import datetime

import xlsxwriter

from .rules import CODE_LABEL

INK, INK_2, RULE, PEN = "#0E0A20", "#5D5A6E", "#E4E2EE", "#654CF4"   # the app's palette: ink text, violet charts
# bar fills are stepped from the app's status colours: its amber and red can't be told apart with red-green colour blindness
OUTCOMES = [("APPROVE", "Approved", "#1E7A4C", "#DDF0E5"), ("HOLD", "Held", "#C98512", "#FBEBD0"), ("REJECT", "Rejected", "#A61C14", "#F9DEDA")]
GOOD, AMBER, BAD = "#1E7A4C", "#C98512", "#A61C14"
INR = '[>=10000000]"₹"##\\,##\\,##\\,##0.00;[>=100000]"₹"##\\,##\\,##0.00;"₹"##,##0.00'
INR_WHOLE = INR.replace(".00", "")
BOOK = dict(in_memory=True, strings_to_formulas=False, strings_to_urls=False)
HEAD = dict(bold=True, font_color=INK, bottom=2, bottom_color=INK, text_wrap=True, valign="top")
CHART_W, CHART_H = 560, 320
TOP_N = 8


def _put(ws, row, col, value, fmt=None):
    if value is None or value == "":
        ws.write_blank(row, col, None, fmt)
    elif isinstance(value, datetime):
        ws.write_datetime(row, col, value, fmt)
    elif isinstance(value, (int, float)):
        ws.write_number(row, col, value, fmt)
    else:
        ws.write_string(row, col, str(value)[:32767], fmt)


def _sheet(wb, name, head, columns, rows):
    """A filterable data sheet. columns: (title, width, format or None, row -> value)."""
    ws = wb.add_worksheet(name)
    for c, (title, width, *_rest) in enumerate(columns):
        ws.set_column(c, c, width)
        ws.write_string(0, c, title, head)
    for r, row in enumerate(rows, 1):
        for c, (_title, _width, fmt, get) in enumerate(columns):
            _put(ws, r, c, get(row), fmt(row) if callable(fmt) else fmt)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, max(len(rows), 1), len(columns) - 1)
    return ws


def _bar(wb, title, sheet, first, last, value_format=None, colors=None, kind="bar", height=CHART_H, longest=None, cat_col=0, val_col=1, labels=True):
    """One-series chart: values labelled directly, so the value axis and grid are left out."""
    chart = wb.add_chart({"type": kind})
    series = dict(categories=[sheet, first, cat_col, last, cat_col], values=[sheet, first, val_col, last, val_col], fill={"color": PEN}, gap=55)
    if labels:
        series["data_labels"] = dict(value=True, font=dict(color=INK, size=10), **({"num_format": value_format} if value_format else {}))
    if kind == "line":
        series.pop("fill"), series.pop("gap")
        series.update(line=dict(color=PEN, width=2.25), marker=dict(type="circle", size=6, fill=dict(color=PEN), border=dict(color=PEN)))
        if labels:
            series["data_labels"]["position"] = "above"
    if colors:
        series["points"] = [{"fill": {"color": c}} for c in colors]
    chart.add_series(series)
    chart.set_title(dict(name=title, overlay=False, name_font=dict(size=12, bold=True, color=INK)))
    chart.set_legend(dict(none=True))
    cat_axis = dict(num_font=dict(color=INK_2, size=10), line=dict(color=RULE), major_tick_mark="none")
    value_axis = dict(visible=False, major_gridlines=dict(visible=False), min=0)
    if longest:
        value_axis["max"] = longest * 1.3  # room for a long label past the end of the longest bar
    if kind == "bar":  # xlsxwriter's bar charts swap the axes: y is the category axis
        chart.set_y_axis(dict(cat_axis, reverse=True))  # keep the table's order, largest at the top
        chart.set_x_axis(value_axis)
    else:
        chart.set_x_axis(cat_axis)
        chart.set_y_axis(value_axis)
    chart.set_chartarea(dict(border=dict(color=RULE)))
    chart.set_size(dict(width=CHART_W, height=height))
    return chart


FLAG_ACTION = {"call": ("Call the vendor", "Possible fraud: an identity, PO or amount that doesn't fit what you have on record.", BAD),
               "resend": ("Ask for a resend", "Document problems: a GSTIN or GST shown wrongly, sums that don't add up, details missing.", AMBER)}


def _months(spend):
    """Approved value per month, with the empty months inside the range shown as zero, as on the Analytics tab."""
    sums = {}
    for day, value in spend:
        sums[day[:7]] = sums.get(day[:7], 0) + value
    if not sums:
        return []
    keys, (y, m) = [], map(int, min(sums).split("-"))
    while f"{y}-{m:02d}" <= max(sums) and len(keys) < 800:
        keys.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return [(datetime.strptime(k, "%Y-%m").strftime("%b %Y"), round(sums.get(k, 0), 2)) for k in keys]


def _analytics_sheet(wb, a, head, money, scope):
    """Everything on the Analytics tab, as tables with a native chart beside each one."""
    ws = wb.add_worksheet("Analytics")
    ws.hide_gridlines(2)
    ws.set_column(0, 0, 2)
    ws.set_column(1, 1, 34)
    ws.set_column(2, 6, 15)
    ws.set_column(7, 7, 3)
    title = wb.add_format(dict(font_size=20, bold=True, font_color=INK))
    h2 = wb.add_format(dict(font_size=13, bold=True, font_color=INK, bottom=2, bottom_color=PEN))
    muted = wb.add_format(dict(font_color=INK_2))
    wrap = wb.add_format(dict(text_wrap=True, valign="top"))
    stamp = wb.add_format(dict(num_format="dd-mmm-yyyy hh:mm"))
    pct = wb.add_format(dict(num_format="0%"))
    secs = wb.add_format(dict(num_format='0.0"s"'))
    big = wb.add_format(dict(font_size=16, bold=True, font_color=INK, align="left"))
    big_money = wb.add_format(dict(font_size=16, bold=True, font_color=INK, align="left", num_format=INR_WHOLE))
    small = wb.add_format(dict(font_color=INK_2, text_wrap=True, valign="top", top=2, top_color=INK))
    ws.set_row(1, 30)
    ws.write_string(1, 1, "Analytics", title)
    ws.write_string(2, 1, f"The same figures as the Analytics tab of the Dashboard. {scope}Rejected invoices count here.", muted)
    at = [4]   # the next free row

    def section(name, sub=""):
        ws.merge_range(at[0], 1, at[0], 6, name, h2)
        at[0] += 1
        if sub:
            ws.merge_range(at[0], 1, at[0], 6, sub, muted)
            at[0] += 1
        return at[0]

    def table(heads, rows, fmts):
        for c, text in enumerate(heads):
            ws.write_string(at[0], 1 + c, text, head)
        first = at[0] + 1
        for r in rows:
            at[0] += 1
            for c, value in enumerate(r):
                _put(ws, at[0], 1 + c, value, fmts[c])
        if not rows:
            at[0] += 1
            ws.write_string(at[0], 1, "None.", muted)
        last = at[0]
        at[0] += 2
        return first, last

    def chart(top, c):
        ws.insert_chart(top, 8, c, dict(object_position=3))
        at[0] = max(at[0], top + 18)   # a chart is about 16 rows tall: never let the next one overlap it

    # 1. outcomes, and the money on hold by how long it has waited
    o = a["outcome"]
    top = section("Outcomes and money on hold", "What needs a person now: invoices and value by outcome.")
    tiles = [("HOLD", "Held", "waiting for you"), ("APPROVE", "Approved", "for payment"), ("REJECT", "Rejected", "")]
    ws.set_row(at[0], 26)
    for i, (k, name, note) in enumerate(tiles):
        ws.merge_range(at[0], 1 + i * 2, at[0], 2 + i * 2, o[k]["value"], big_money)
        ws.merge_range(at[0] + 1, 1 + i * 2, at[0] + 1, 2 + i * 2, f"{name}: {o[k]['count']} invoice{'' if o[k]['count'] == 1 else 's'}" + (f" · {note}" if note else ""), small)
    at[0] += 3
    first, last = table(["Outcome", "Invoices", "Value"], [(name, o[k]["count"], o[k]["value"]) for k, name, _n in tiles], [None, None, money])
    if any(o[k]["value"] for k, *_ in tiles):
        chart(top, _bar(wb, "Value by outcome", "Analytics", first, last, INR_WHOLE, [AMBER, GOOD, BAD], "column", cat_col=1, val_col=3))
    ages_top = at[0]
    ws.write_string(at[0], 1, "Money on hold, by how long it has waited", wb.add_format(dict(bold=True, font_color=INK)))
    at[0] += 1
    first, last = table(["Waiting", "Invoices", "Value"], [(x["label"], x["count"], x["value"]) for x in a["hold_ages"]], [None, None, money])
    if o["HOLD"]["count"]:
        chart(ages_top, _bar(wb, "Money on hold by age", "Analytics", first, last, INR_WHOLE, [AMBER] * len(a["hold_ages"]), cat_col=1, val_col=3))

    # 2. spend over time
    months = _months(a["spend"])
    top = section("Spend over time", "Value approved for payment, by invoice month." +
                  (f" {a['undated_spend']} approved invoice{'' if a['undated_spend'] == 1 else 's'} had no date and are left out." if a["undated_spend"] else ""))
    first, last = table(["Month", "Approved"], months, [None, money])
    if months:
        chart(top, _bar(wb, "Approved spend per month", "Analytics", first, last, INR_WHOLE, kind="line" if len(months) > 1 else "column",
                        cat_col=1, val_col=2, labels=len(months) <= 12))

    # 3. fraud and control flags
    top = section("Fraud and control flags", "Counted apart from the holds, so the dangerous ones aren't hidden.")
    rows, colors = [], []
    for kind, (action, what, color) in FLAG_ACTION.items():
        total = sum(x["count"] for x in a["flags"].get(kind, []))
        ws.write_string(at[0], 1, f"{action}: {total}", wb.add_format(dict(bold=True, font_color=color)))
        ws.merge_range(at[0], 2, at[0], 6, what, muted)
        at[0] += 1
        for x in a["flags"].get(kind, []):
            rows.append((CODE_LABEL.get(x["code"], x["code"]), action, x["count"]))
            colors.append(color)
    at[0] += 1
    first, last = table(["Flag", "Action", "Invoices"], rows, [None, None, None])
    if rows:
        chart(top, _bar(wb, "Fraud and control flags", "Analytics", first, last, colors=colors, cat_col=1, val_col=3))

    # 4. vendor scorecard
    vs = a["vendors"]
    top = section("Vendor scorecard", f"Who sends the most, how often it stops, and why. {a['vendor_count']} vendor{'' if a['vendor_count'] == 1 else 's'}.")
    first, last = table(["Vendor", "Invoices", "Stopped", "Stop rate", "Top reason", "Value"],
                        [(v["name"], v["invoices"], v["stopped"], v["hold_rate"] / 100,
                          f"{CODE_LABEL.get(v['top_reason'], v['top_reason'])} × {v['top_reason_count']}" if v["top_reason"] else "Never stopped", v["value"]) for v in vs],
                        [None, None, None, pct, wrap, money])
    if vs:
        shown = vs[:TOP_N]
        chart(top, _bar(wb, f"Invoices per vendor (top {len(shown)})", "Analytics", first, first + len(shown) - 1, cat_col=1, val_col=2))

    # 5. batch throughput
    bs = a["batches"]
    multi = [b for b in bs if b["batch"]]
    n = sum(b["invoices"] for b in multi)
    cleared = sum(b["cleared"] for b in multi)
    section("Batch throughput", "Invoices per upload, seconds of checking each, and how many cleared without a person.")
    if multi:
        ws.set_row(at[0], 24)
        for i, (value, fmt, note) in enumerate([(round(n / len(multi), 1), big, f"invoices per upload, {len(multi)} upload{'' if len(multi) == 1 else 's'}"),
                                                (round(sum(b["avg_seconds"] * b["invoices"] for b in multi) / n, 1) if n else 0, wb.add_format(dict(font_size=16, bold=True, font_color=INK, align="left", num_format='0.0"s"')), "of checking per invoice"),
                                                (cleared / n if n else 0, wb.add_format(dict(font_size=16, bold=True, font_color=INK, align="left", num_format="0%")), f"cleared without a person ({cleared} of {n})")]):
            ws.merge_range(at[0], 1 + i * 2, at[0], 2 + i * 2, value, fmt)
            ws.merge_range(at[0] + 1, 1 + i * 2, at[0] + 1, 2 + i * 2, note, small)
        at[0] += 3
    else:
        ws.write_string(at[0], 1, "No multi-file uploads yet.", muted)
        at[0] += 2
    moment = lambda s: datetime.fromisoformat(s) if s else None
    table(["Upload", "Started", "Invoices", "Seconds each", "Cleared without a person", "Cleared %"],
          [(f"{b['first']}" + (f" + {b['invoices'] - 1} more" if b["invoices"] > 1 else "") if b["batch"] else "Single files",
            moment(b["started"]) if b["batch"] else None, b["invoices"], b["avg_seconds"], b["cleared"], b["cleared_pct"] / 100) for b in bs],
          [wrap, stamp, None, secs, None, pct])
    return ws


def _vendor_sheet(wb, name, vendors, runs):
    """One row per vendor, with what has been approved for it so far."""
    head, wrap = wb.add_format(HEAD), wb.add_format(dict(text_wrap=True, valign="top"))
    money, stamp = wb.add_format(dict(num_format=INR)), wb.add_format(dict(num_format="dd-mmm-yyyy hh:mm"))
    approved = {}
    for r in runs:
        if r["status"] == "done" and (r["final_decision"] or r["decision"]) == "APPROVE" and r["vendor_id"]:
            approved.setdefault(r["vendor_id"], []).append(r["total"] or 0)
    moment = lambda s: datetime.fromisoformat(s) if s else None
    return _sheet(wb, name, head, [
        ("Vendor ID", 9, None, lambda v: v["vendor_id"]), ("Name", 36, None, lambda v: v["name"]), ("GSTIN", 18, None, lambda v: v["gstin"]),
        ("Also known as", 30, wrap, lambda v: ", ".join(v["aliases"])), ("Status", 10, None, lambda v: v["status"]), ("Source", 16, None, lambda v: v["source"]),
        ("Invoices approved", 10, None, lambda v: len(approved.get(v["vendor_id"], []))), ("Approved value (gross)", 16, money, lambda v: sum(approved.get(v["vendor_id"], []))),
        ("Approved on", 17, stamp, lambda v: moment(v.get("approved_at"))), ("Approval note", 36, wrap, lambda v: v.get("note")),
    ], vendors)


def _approved(vendors):
    return [v for v in vendors if v["status"].lower() == "approved"]


def approved_vendors(vendors, runs):
    """The approved vendor list as a workbook of its own."""
    out = io.BytesIO()
    wb = xlsxwriter.Workbook(out, BOOK)
    _vendor_sheet(wb, "Approved vendors", _approved(vendors), runs)
    wb.close()
    return out.getvalue()


def workbook(stats, runs, pos, vendors, period="", analytics=None, folder=None):
    """The report. analytics is the Analytics tab's data for the same runs; folder names the folder they come from."""
    out = io.BytesIO()
    wb = xlsxwriter.Workbook(out, BOOK)
    head = wb.add_format(HEAD)
    money, date, stamp, pct = (wb.add_format(dict(num_format=f)) for f in (INR, "dd-mmm-yyyy", "dd-mmm-yyyy hh:mm", "0.0%"))
    wrap = wb.add_format(dict(text_wrap=True, valign="top"))
    outcome_fmt = {k: wb.add_format(dict(bg_color=wash, font_color=INK)) for k, _l, _c, wash in OUTCOMES}
    label = {k: lbl for k, lbl, _c, _w in OUTCOMES}

    eff = lambda r: r["final_decision"] or r["decision"]
    done = [r for r in runs if r["status"] in ("done", "error")]
    shown = [r for r in done if eff(r) != "REJECT"]  # same as the Dashboard; rejected documents get their own sheet
    moment = lambda s: datetime.fromisoformat(s) if s else None

    summary = wb.add_worksheet("Summary")
    summary.hide_gridlines(2)
    summary.set_landscape()
    summary.fit_to_pages(1, 1)
    scope = (f"Folder: {folder}. " if folder else "") + (f"Invoices dated {period}. " if period else "")
    if analytics is not None:
        _analytics_sheet(wb, analytics, head, money, scope)

    invoice_columns = [
        ("Received", 17, stamp, lambda r: moment(r["created_at"])), ("File", 30, None, lambda r: r["filename"]),
        ("Vendor", 30, None, lambda r: r["vendor_name"]), ("Vendor ID", 9, None, lambda r: r["vendor_id"]),
        ("Invoice no.", 18, None, lambda r: r["invoice_number"]), ("Invoice date", 12, date, lambda r: moment(r["invoice_date"])),
        ("PO", 14, None, lambda r: r["po_number"]), ("Pre-tax", 15, money, lambda r: r["net"]), ("Total", 15, money, lambda r: r["total"]),
        ("Outcome", 10, lambda r: outcome_fmt.get(eff(r)), lambda r: label.get(eff(r))),
        ("System decision", 10, None, lambda r: label.get(r["decision"])), ("Resolved by a person", 10, None, lambda r: "Yes" if r["final_decision"] else "No"),
        ("Why it stopped", 44, wrap, lambda r: "; ".join(CODE_LABEL.get(c, c) for c in r["codes"])),
        ("Resolution note", 30, wrap, lambda r: r["override_note"]), ("Upload note", 30, wrap, lambda r: r["inputs"].get("note")),
        ("Read by", 12, None, lambda r: (r["extractor"] or "").split(":")[0]), ("Seconds", 9, None, lambda r: round(r["duration_ms"] / 1000, 1) if r["duration_ms"] else None),
    ]
    _sheet(wb, "Invoices", head, invoice_columns, shown)
    _sheet(wb, "PO ledger", head, [
        ("PO", 15, None, lambda p: p["po_number"]), ("Vendor", 30, None, lambda p: p["vendor_name"]), ("For", 42, None, lambda p: p["description"]),
        ("PO value (pre-tax)", 16, money, lambda p: p["net_amount"]), ("Billed so far", 15, money, lambda p: p["billed"]),
        ("Still open", 15, money, lambda p: max(p["remaining"], 0)), ("% billed", 9, pct, lambda p: p["pct_billed"] / 100),
        ("Approved invoices", 36, wrap, lambda p: ", ".join(i["invoice_number"] or "no number" for i in p["invoices"])),
    ], pos)
    _vendor_sheet(wb, "Approved vendors", _approved(vendors), runs)
    blocked = [v for v in vendors if v not in _approved(vendors)]
    if blocked:
        _vendor_sheet(wb, "Blocked vendors", blocked, runs)
    _sheet(wb, "Rejected", head, invoice_columns, [r for r in done if eff(r) == "REJECT"])

    # ---- the tables the charts draw from, so every chart has a plain-numbers view
    data = wb.add_worksheet("Chart data")
    data.set_column(0, 0, 40)
    data.set_column(1, 1, 18)

    def table(row, title, unit, rows, fmt=None):
        data.write_string(row, 0, title, head)
        data.write_string(row, 1, unit, head)
        for i, (name, value) in enumerate(rows, row + 1):
            data.write_string(i, 0, name)
            data.write_number(i, 1, value, fmt)
        return row + 1, row + len(rows)

    by_vendor = {}
    for r in shown:
        if eff(r) == "APPROVE" and r["total"]:
            key = r["vendor_name"] or "Unknown vendor"
            by_vendor[key] = by_vendor.get(key, 0) + r["total"]
    top_vendors = sorted(by_vendor.items(), key=lambda x: -x[1])[:TOP_N]
    reasons = [(CODE_LABEL.get(x["code"], x["code"]), x["count"]) for x in stats["reasons"]]
    billed = sorted(((p["po_number"], p["pct_billed"] / 100) for p in pos if p["billed"]), key=lambda x: -x[1])

    outcomes = [o for o in OUTCOMES if o[0] in stats["counts"]]
    counts = table(0, "Invoices by outcome", "Invoices", [(lbl, stats["counts"][k]) for k, lbl, _c, _w in outcomes])
    why = table(counts[1] + 2, "Why invoices stop", "Invoices", reasons)
    spend = table(why[1] + 2, "Approved value by vendor (gross)", "Rupees", top_vendors, money)
    ledger = table(spend[1] + 2, "PO value billed so far", "% of PO value", billed, pct)

    # ---- summary sheet
    summary.set_column(0, 0, 2)
    summary.set_column(1, 8, 13)
    summary.set_row(1, 30)
    summary.write_string(1, 1, "Invoice Flow report", wb.add_format(dict(font_size=20, bold=True, font_color=INK)))
    muted = wb.add_format(dict(font_color=INK_2))
    ledger_note = "The PO ledger is always the current position. " if period or folder else ""
    summary.write_string(2, 1, f"Generated {datetime.now():%d %b %Y, %H:%M}. {scope}{ledger_note}Charts are live: they read from the Chart data sheet."
                         + (" The Analytics sheet has everything on the Analytics tab." if analytics is not None else ""), muted)

    tile_value = wb.add_format(dict(font_size=18, bold=True, font_color=INK, align="left"))
    tile_money = wb.add_format(dict(font_size=18, bold=True, font_color=INK, align="left", num_format=INR))
    tile_label = wb.add_format(dict(font_color=INK_2, text_wrap=True, valign="top", top=2, top_color=INK))
    tiles = [("Invoices processed", stats["total"], tile_value), ("Approved with no human touch", stats["straight_through"] / 100, wb.add_format(dict(font_size=18, bold=True, font_color=INK, align="left", num_format="0%"))),
             ("Approved for payment (gross)", stats["approved_value"], tile_money), ("On hold (gross)", stats["pending_value"], tile_money)]
    summary.set_row(4, 32)
    summary.set_row(5, 28)
    for i, (text, value, fmt) in enumerate(tiles):
        col = 1 + i * 2
        summary.merge_range(4, col, 4, col + 1, value, fmt)
        summary.merge_range(5, col, 5, col + 1, text, tile_label)

    if not stats["total"]:
        summary.write_string(7, 1, "No invoices have been processed yet, so there is nothing to chart.", muted)
    charts = []
    if stats["total"]:
        charts.append(_bar(wb, "Invoices by outcome", "Chart data", *counts, colors=[c for _k, _l, c, _w in outcomes], kind="column"))
    if reasons:
        charts.append(_bar(wb, "Why invoices stop", "Chart data", *why))
    if top_vendors:
        charts.append(_bar(wb, "Approved value by vendor (gross, top 8)", "Chart data", *spend, value_format=INR_WHOLE, longest=top_vendors[0][1]))
    if billed:
        charts.append(_bar(wb, "PO value billed so far", "Chart data", *ledger, value_format="0%",
                           colors=[BAD if v > 1.0001 else PEN for _n, v in billed]))
    for i, chart in enumerate(charts):
        summary.insert_chart(7 + (i // 2) * 17, 1, chart, dict(x_offset=(i % 2) * (CHART_W + 16), object_position=3))

    wb.close()
    return out.getvalue()
