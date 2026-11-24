"""How accurately does Invoice Flow read invoices? A labelled test set, read the way the app reads an upload.

    python tools/eval_extraction.py [--n 120] [--seed 2026] [--modes auto,rules] [--env path/to/.env]

1. Makes --n invoices with known answers (deterministic for a seed): 24 fictional vendors with valid GSTINs, 6 invoice-
   number styles, 7 date styles, 1-6 line items from a 30-item catalogue, GST at 5/12/18/28%, with or without a PO,
   in three layouts (CGST+SGST table, IGST table, one GST-inclusive lump sum) and three forms: a clean PDF with a text
   layer, a skewed noisy scan with no text layer, and a phone photo (JPEG, tilted, shadowed, compressed).
2. Reads each one with app.extract exactly as an upload is read: read_document (text layer, or OCR with RapidOCR),
   then extract_fields. Mode "auto" is OCR + Gemini (GEMINI_API_KEY); "rules" is OCR + the built-in rules alone.
3. Scores 8 fields per invoice (vendor name, GSTIN, invoice number, invoice date, PO number, pre-tax amount, tax,
   total; the lump-sum layout prints no pre-tax amount or tax, so it is scored on 6) and writes docs/extraction-eval.md
   and docs/extraction-eval.json.

A field is right when: vendor name matches at 90%+ similarity (case and punctuation aside); GSTIN is exact; invoice
and PO numbers match ignoring spaces and punctuation (an absent PO must come back empty); the date is the same day;
amounts are within Rs 1.
"""
import argparse
import difflib
import json
import os
import random
import re
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=120)
ap.add_argument("--seed", type=int, default=2026)
ap.add_argument("--modes", default="auto,rules")
ap.add_argument("--env", default=str(ROOT / ".env"))
ap.add_argument("--keep", help="folder to keep the generated invoices in")
args = ap.parse_args()
from dotenv import load_dotenv  # noqa: E402

load_dotenv(args.env)
os.environ["GEMINI_DAILY_LIMIT"] = "0"
import fitz  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app import config, extract  # noqa: E402
from make_samples import classic, inr, scan, west  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
W, H = A4
R = random.Random(args.seed)

# ----------------------------------------------------------------- the data --
GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin(state):
    """A well-formed GSTIN with a correct check character (the GSTN mod-36 scheme)."""
    letters = "".join(R.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(3)) + R.choice("CFHP") + R.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    body = f"{state:02d}{letters}{R.randint(1000, 9999)}{R.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}{R.choice('123')}Z"
    total = 0
    for i, ch in enumerate(body):
        v = GST_CHARS.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return body + GST_CHARS[(36 - total % 36) % 36]


NAMES = ["Sharma Office Supplies Pvt Ltd", "Rathore IT Services LLP", "Marwar Logistics Pvt Ltd", "Thar Facility Management Co",
         "Deccan Poultry Farms", "Godavari Fresh Fish", "Kaveri Engineering Works", "Nilgiri Tea Traders", "Bharat Electricals & Co",
         "Sahyadri Packaging Pvt Ltd", "Ganga Textiles", "Vindhya Steel Fabricators", "Konark Printers", "Malabar Spices Exports",
         "Aravalli Stationery Mart", "Narmada Agro Industries", "Coromandel Chemicals Ltd", "Himalaya Water Solutions",
         "Satpura Furniture House", "Indus Courier Services", "Lotus Hospitality Supplies", "Chambal Cement Agencies",
         "Pushkar Handicrafts", "Brahmaputra Tech Solutions Pvt Ltd"]
CITIES = [("Jodhpur, Rajasthan 342003", 8), ("Hyderabad, Telangana 500032", 36), ("Pune, Maharashtra 411001", 27),
          ("Chennai, Tamil Nadu 600017", 33), ("Kolkata, West Bengal 700001", 19), ("Bengaluru, Karnataka 560001", 29)]
VENDORS = []
for name in NAMES:
    city, state = R.choice(CITIES)
    VENDORS.append(dict(name=name, gstin=gstin(state), phone=f"0{R.randint(20, 99)}-{R.randint(2000000, 2999999)}",
                        addr1=f"{R.randint(1, 120)}, {R.choice(['MG Road', 'Station Road', 'Industrial Area', 'Ring Road', 'Market Yard'])}", addr2=city))
BUYERS = [dict(name="Orbit Retail Pvt Ltd", addr="Plot 14, Industrial Area, Jodhpur 342003", gstin="08AAACO1234P1Z9"),
          dict(name="Invoice Flow", addr="Hyderabad, Telangana", gstin=None),
          dict(name="Sunrise Hotels Pvt Ltd", addr="Banjara Hills, Hyderabad 500034", gstin="36AAECS2211L1Z4")]
CATALOGUE = [("A4 copier paper 70gsm (500 sheets)", "4802", 280), ("Toner cartridge 85A", "8443", 1900), ("Whiteboard markers (box of 10)", "9608", 400),
             ("Laptop AMC - quarterly", "998713", 62500), ("Network switch 24-port", "8517", 14800), ("Courier charges - October", "996812", 3450),
             ("Housekeeping services - monthly", "998533", 60000), ("Chicken curry cut (kg)", "0207", 180), ("Prawns medium (kg)", "0306", 420),
             ("Corrugated boxes 5-ply", "4819", 38), ("LED tube light 20W", "9405", 210), ("Office chair ergonomic", "9401", 7400),
             ("Drinking water 20L jar", "2201", 60), ("Printer servicing", "998719", 2500), ("Steel almirah 4-door", "9403", 11200),
             ("Basmati rice 25kg bag", "1006", 2150), ("Cotton bedsheets double", "6302", 640), ("Hand sanitiser 5L", "3808", 950),
             ("CAT6 cable box 305m", "8544", 8900), ("Security guard services", "998525", 18500), ("Diesel generator rental (day)", "997319", 4200),
             ("Tea leaves premium (kg)", "0902", 520), ("Cement OPC 53 (bag)", "2523", 395), ("Visiting cards (box of 100)", "4911", 350),
             ("Pest control service", "998531", 3200), ("Masala blend (kg)", "0910", 780), ("Bubble wrap roll", "3920", 690),
             ("Monitor 24-inch IPS", "8528", 11900), ("Pallet racking bay", "9403", 16500), ("Annual software licence", "997331", 45000)]
NUMBER_STYLES = [lambda: f"INV/26-27/{R.randint(1, 9999):04d}", lambda: f"{R.choice(['SOS', 'TFM', 'KEW', 'MSE', 'BTS'])}-{R.randint(100, 9999)}",
                 lambda: f"2026/TX/{R.randint(1, 99999):05d}", lambda: f"A{R.randint(1000, 99999)}", lambda: f"GST/{R.randint(1, 9999):04d}/26",
                 lambda: f"{R.randint(10000, 999999)}"]
DATE_STYLES = [lambda d: d.strftime("%d-%b-%Y"), lambda d: d.strftime("%d/%m/%Y"), lambda d: d.isoformat(), lambda d: d.strftime("%d.%m.%Y"),
               lambda d: d.strftime("%B %d, %Y"), lambda d: d.strftime("%d %b %Y"), lambda d: d.strftime("%d-%m-%Y")]


def igst_layout(path, v, inv, buyer):
    """A second table style: "Bill No." / "Dated", taxable value, IGST in one line (an inter-state sale)."""
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 12); c.drawRightString(W - 40, H - 48, "TAX INVOICE (ORIGINAL FOR RECIPIENT)")
    c.setFont("Helvetica-Bold", 16); c.drawString(40, H - 80, v["name"].upper())
    c.setFont("Helvetica", 9); c.drawString(40, H - 94, f"{v['addr1']}, {v['addr2']}")
    c.drawString(40, H - 106, f"GSTIN/UIN: {v['gstin']}   Ph: {v['phone']}")
    c.rect(40, H - 190, W - 80, 70)
    c.setFont("Helvetica", 9)
    c.drawString(48, H - 136, "Buyer (Bill to)"); c.setFont("Helvetica-Bold", 9); c.drawString(48, H - 150, buyer["name"])
    c.setFont("Helvetica", 9); c.drawString(48, H - 162, buyer["addr"])
    if buyer.get("gstin"):
        c.drawString(48, H - 174, f"GSTIN/UIN: {buyer['gstin']}")
    y = H - 136
    for lab, val in (("Bill No.", inv["number"]), ("Dated", inv["date"]), ("Buyer's Order No.", inv.get("po") or "-"), ("Mode of Payment", "NEFT")):
        c.drawString(330, y, lab); c.drawString(430, y, f": {val}"); y -= 13
    ty = H - 215
    c.setFont("Helvetica-Bold", 9)
    for x, t in ((44, "Sl"), (66, "Description of Goods/Services"), (300, "HSN/SAC"), (370, "Quantity"), (430, "Rate")):
        c.drawString(x, ty, t)
    c.drawRightString(W - 44, ty, "Amount")
    c.line(40, ty - 5, W - 40, ty - 5)
    c.setFont("Helvetica", 9)
    ty -= 20
    sub = 0.0
    for i, (desc, hsn, qty, rate) in enumerate(inv["lines"], 1):
        amt = round(qty * rate, 2); sub += amt
        c.drawString(46, ty, str(i)); c.drawString(66, ty, desc); c.drawString(300, ty, hsn); c.drawString(372, ty, f"{qty} Nos")
        c.drawString(430, ty, west(rate)); c.drawRightString(W - 44, ty, west(amt)); ty -= 14
    sub = round(sub, 2)
    tax = round(sub * inv["gst"] / 100, 2)
    total = round(sub + tax, 2)
    c.line(40, ty + 2, W - 40, ty + 2); ty -= 14
    for lab, val in (("Taxable Value", sub), (f"IGST @ {inv['gst']}%", tax), ("Total Invoice Value", total)):
        c.setFont("Helvetica-Bold" if lab.startswith("Total") else "Helvetica", 9.5)
        c.drawString(350, ty, lab); c.drawRightString(W - 44, ty, "INR " + west(val)); ty -= 14
    c.setFont("Helvetica", 8); c.drawString(40, 80, "Declaration: We declare that this invoice shows the actual price of the goods described.")
    c.drawString(40, 68, f"for {v['name']}  -  Authorised Signatory")
    c.save()
    return dict(subtotal=sub, tax=tax, total=total)


def lump_layout(path, v, inv, buyer):
    """One line, GST included in the amount, no pre-tax figure or tax shown."""
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Times-Bold", 18); c.drawCentredString(W / 2, H - 55, v["name"])
    c.setFont("Times-Roman", 10); c.drawCentredString(W / 2, H - 70, f"{v['addr1']}, {v['addr2']}")
    c.drawCentredString(W / 2, H - 83, f"GSTIN {v['gstin']}  |  Tel {v['phone']}")
    c.setFont("Times-Bold", 14); c.drawCentredString(W / 2, H - 120, "INVOICE")
    c.setFont("Times-Roman", 11)
    c.drawString(60, H - 155, f"Invoice #: {inv['number']}"); c.drawString(60, H - 171, f"Date: {inv['date']}")
    c.drawString(60, H - 187, f"Billed to: {buyer['name']}")
    desc = ", ".join(d for d, *_ in inv["lines"])[:70]
    total = round(sum(q * r for _, _, q, r in inv["lines"]) * (1 + inv["gst"] / 100), 2)
    y = H - 240
    c.line(60, y + 14, W - 60, y + 14); c.setFont("Times-Bold", 11); c.drawString(65, y, "Description"); c.drawRightString(W - 65, y, "Amount (INR)")
    c.line(60, y - 6, W - 60, y - 6); c.setFont("Times-Roman", 11); y -= 26
    c.drawString(65, y, f"1   {desc}"); c.drawRightString(W - 65, y, west(total)); y -= 40
    c.setFont("Times-Bold", 12); c.drawString(300, y, f"Total (incl. {inv['gst']}% GST)"); c.drawRightString(W - 65, y, west(total))
    c.setFont("Times-Roman", 9)
    if inv.get("po"):
        c.drawString(60, 110, f"Against PO No. {inv['po']}")
    c.drawString(60, 96, "Remit to: ICICI Bank  A/C 123400056789  IFSC ICIC0000123")
    c.save()
    return dict(subtotal=None, tax=None, total=total)


def photo(src_pdf, dst_jpg):
    """A phone photo of the printout: tilted, unevenly lit, slightly soft, JPEG-compressed."""
    pix = fitz.open(src_pdf)[0].get_pixmap(dpi=150)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    img = img.rotate(R.uniform(-3.5, 3.5), expand=True, fillcolor=(118, 112, 104), resample=Image.BICUBIC)
    shade = Image.new("L", img.size, 0)
    ImageDraw.Draw(shade).rectangle([0, 0, img.size[0], img.size[1]], fill=0)
    grad = Image.linear_gradient("L").resize(img.size).rotate(R.choice([0, 90, 180, 270]))
    img = Image.composite(img, Image.new("RGB", img.size, (60, 55, 50)), grad.point(lambda p: 255 - p // 4))
    img = img.filter(ImageFilter.GaussianBlur(0.8)).resize((int(img.size[0] * 0.85), int(img.size[1] * 0.85)))
    img.save(dst_jpg, "JPEG", quality=55)


def make(i, folder):
    v, buyer = R.choice(VENDORS), R.choice(BUYERS)
    d = date(2026, 4, 1) + timedelta(days=R.randint(0, 230))
    gst = R.choice([5, 12, 18, 18, 18, 28])
    lines = []
    for item in R.sample(CATALOGUE, R.choice([1, 1, 2, 2, 3, 4, 5, 6])):
        desc, hsn, base = item
        rate = round(base * R.uniform(0.85, 1.2), R.choice([0, 0, 2]))
        lines.append((desc, hsn, R.choice([1, 1, 2, 3, 5, 10, 12, 20, 25, 50]) if base < 20000 else 1, rate))
    po = f"PO-2026-{R.randint(1001, 1999)}" if R.random() < 0.8 else None
    inv = dict(number=R.choice(NUMBER_STYLES)(), date=R.choice(DATE_STYLES)(d), po=po, gst=gst, lines=lines)
    layout = R.choices(["classic", "igst", "lump"], weights=[45, 40, 15])[0]
    pdf = folder / f"inv_{i:03d}_{layout}.pdf"
    if layout == "classic":
        amounts = classic(pdf, v, inv, font=R.choice(["Helvetica", "Times-Roman", "Courier"]), buyer=buyer)
        amounts["tax"] = round(amounts["total"] - amounts["subtotal"], 2)
    elif layout == "igst":
        amounts = igst_layout(pdf, v, inv, buyer)
    else:
        amounts = lump_layout(pdf, v, inv, buyer)
    form = R.choices(["pdf", "scan", "photo"], weights=[40, 40, 20])[0]
    path = pdf
    if form == "scan":
        path = pdf.with_name(pdf.stem + "_scan.pdf"); scan(pdf, path)
    elif form == "photo":
        path = pdf.with_suffix(".jpg"); photo(pdf, path)
    truth = dict(vendor_name=v["name"], vendor_gstin=v["gstin"], invoice_number=inv["number"], invoice_date=d.isoformat(),
                 po_reference=po, subtotal=amounts["subtotal"], tax_amount=amounts["tax"], total=amounts["total"])
    return dict(file=path.name, path=str(path), layout=layout, form=form, truth=truth)


# -------------------------------------------------------------- the scoring --
FIELDS = ["vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "po_reference", "subtotal", "tax_amount", "total"]
alnum = lambda s: re.sub(r"[^0-9A-Z]", "", str(s or "").upper())
name_key = lambda s: re.sub(r"[^a-z0-9 ]", "", re.sub(r"\b(pvt|private|ltd|limited|llp|co|and)\b|&", " ", str(s or "").lower())).split()


def right(field, want, got):
    if field == "vendor_name":
        return difflib.SequenceMatcher(None, " ".join(name_key(want)), " ".join(name_key(got))).ratio() >= 0.9
    if field in ("subtotal", "tax_amount", "total"):
        return got is not None and abs(float(got) - want) <= 1.0
    if field == "po_reference":
        return alnum(want) == alnum(got)                      # both empty counts: no PO, none read
    return alnum(want) == alnum(got) if want else not got


def main():
    folder = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="if-eval-"))
    folder.mkdir(parents=True, exist_ok=True)
    cases = [make(i + 1, folder) for i in range(args.n)]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    print(f"{len(cases)} invoices in {folder}; reading with {modes}; Gemini model {config.GEMINI_MODEL if config.GEMINI_API_KEY else '(none)'}")
    for k, c in enumerate(cases, 1):
        t0 = time.time()
        doc = extract.read_document(c["path"])
        c["read"] = doc["method"]
        for mode in modes:
            for attempt in range(6):   # a busy model (503) or a dropped connection is retried: this measures the model, not the network
                fields, meta = extract.extract_fields(dict(doc), mode=mode)
                if mode == "rules" or meta["used"] != "rules" or "quota" in " ".join(meta["notes"]).lower():
                    break
                time.sleep(10 * (attempt + 1))
            if mode != "rules" and meta["used"] == "rules":
                print(f"   ! {c['file']}: the model wasn't used ({'; '.join(meta['notes'])[:160]})")
            got = {f: fields.get(f) for f in FIELDS}
            c[mode] = dict(used=meta["used"], got=got,
                           right={f: right(f, c["truth"][f], got[f]) for f in FIELDS if not (c["layout"] == "lump" and f in ("subtotal", "tax_amount"))})
        print(f"{k:>3}/{len(cases)} {c['file']:<28} {c['read']:<10} " + "  ".join(
            f"{m}: {sum(c[m]['right'].values())}/{len(c[m]['right'])}" for m in modes) + f"  ({time.time() - t0:.1f}s)", flush=True)
    report(cases, modes)


def pct(a, b):
    return f"{100 * a / b:.1f}%" if b else "-"


def report(cases, modes):
    out = dict(n=len(cases), seed=args.seed, model=config.GEMINI_MODEL if config.GEMINI_API_KEY else None, modes={}, cases=[
        {k: v for k, v in c.items() if k != "path"} for c in cases])
    md = [f"# Extraction accuracy: {len(cases)} labelled invoices", "",
          f"Generated by `python tools/eval_extraction.py --n {len(cases)} --seed {args.seed}` on {date.today().isoformat()}. "
          f"Every invoice has a known answer; each is read exactly as an upload is (text layer, or RapidOCR on a scan or photo, "
          f"then the extractor). \"auto\" is OCR + Google Gemini (`{out['model']}`); \"rules\" is OCR + the built-in rules alone.", "",
          "| | " + " | ".join(modes) + " |", "|---|" + "---|" * len(modes)]
    rows = {}
    for m in modes:
        cells = [r for c in cases for r in c[m]["right"].values()]
        docs_all = sum(all(c[m]["right"].values()) for c in cases)
        per_field = {f: [c[m]["right"][f] for c in cases if f in c[m]["right"]] for f in FIELDS}
        by = lambda key: {g: [r for c in cases if c[key] == g for r in c[m]["right"].values()] for g in sorted({c[key] for c in cases})}
        out["modes"][m] = dict(field_accuracy=sum(cells) / len(cells), fields_scored=len(cells), invoices_all_right=docs_all,
                               per_field={f: sum(v) / len(v) for f, v in per_field.items()},
                               by_form={g: sum(v) / len(v) for g, v in by("form").items()},
                               by_layout={g: sum(v) / len(v) for g, v in by("layout").items()})
        by_model = sum(1 for c in cases if str(c[m]["used"]).startswith("llm"))
        out["modes"][m]["read_by_model"] = by_model
        rows.setdefault("Read by the model (the rest fell back to rules)", []).append("-" if m == "rules" else f"{by_model}/{len(cases)}")
        rows.setdefault("**Field accuracy**", []).append(f"**{pct(sum(cells), len(cells))}** ({sum(cells)}/{len(cells)} fields)")
        rows.setdefault("Invoices with every field right", []).append(f"{pct(docs_all, len(cases))} ({docs_all}/{len(cases)})")
        for f, v in per_field.items():
            rows.setdefault(f"Field: {f}", []).append(pct(sum(v), len(v)))
        for g, v in by("form").items():
            rows.setdefault(f"Form: {g} ({sum(c['form'] == g for c in cases)} invoices)", []).append(pct(sum(v), len(v)))
        for g, v in by("layout").items():
            rows.setdefault(f"Layout: {g} ({sum(c['layout'] == g for c in cases)} invoices)", []).append(pct(sum(v), len(v)))
    md += [f"| {k} | " + " | ".join(v) + " |" for k, v in rows.items()]
    misses = [(c["file"], f, c["truth"][f], c[modes[0]]["got"][f]) for c in cases for f, ok in c[modes[0]]["right"].items() if not ok]
    md += ["", f"## What \"{modes[0]}\" got wrong ({len(misses)} fields)", "", "| Invoice | Field | Right answer | Read as |", "|---|---|---|---|"]
    md += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in misses[:80]]
    md += ["", "These are synthetic invoices: realistic layouts, scans and photos, but printed text. Real-world bills (handwriting, "
           "stamps over figures, multi-page) are harder; treat this as the accuracy on clean-to-noisy printed invoices."]
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "extraction-eval.json").write_text(json.dumps(out, indent=1, default=str))
    (ROOT / "docs" / "extraction-eval.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    for m in modes:
        s = out["modes"][m]
        print(f"\n{m}: field accuracy {s['field_accuracy']:.1%} over {s['fields_scored']} fields; every field right on "
              f"{s['invoices_all_right']}/{len(cases)} invoices; by form {({k: round(v * 100, 1) for k, v in s['by_form'].items()})}")


if __name__ == "__main__":
    main()
