"""Generate the synthetic demo invoices (14 scenarios) + manifest.json.

Run:  python tools/make_samples.py
Output: samples/synthetic/*.pdf and samples/synthetic/manifest.json

Three layouts, so "different vendors format differently" is real:
  classic  - itemised table, CGST/SGST shown separately, Indian digit grouping
  bundled  - one lump-sum line, GST embedded in the total, Western grouping
  scanned  - classic layout rasterised, skewed and noised -> image-only PDF (needs OCR / vision)
"""
import json
import re
import sys
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "samples" / "synthetic"
W, H = A4

BUYER = {"name": "Orbit Retail Pvt Ltd", "gstin": "08AAACO1234P1Z9",
         "addr": "Plot 14, Industrial Area, Jodhpur 342003"}

VENDORS = {
    "V001": dict(name="Sharma Office Supplies Pvt Ltd", gstin="08AABCS1234F1Z5", phone="0291-2431100",
                 addr1="12 Sardarpura, 1st B Road", addr2="Jodhpur, Rajasthan 342003"),
    "V002": dict(name="Rathore IT Services LLP", gstin="08AAFFR5678K1Z2", phone="0291-2512288",
                 addr1="C-45, Shastri Nagar", addr2="Jodhpur, Rajasthan 342003"),
    "V003": dict(name="Marwar Logistics Pvt Ltd", gstin="08AACCM9012L1Z8", phone="0291-2744001",
                 addr1="Transport Nagar, Pali Road", addr2="Jodhpur, Rajasthan 342005"),
    "V004": dict(name="Thar Facility Management Co", gstin="08AAHCT3456M1Z1", phone="0291-2650777",
                 addr1="B-9, Basni Industrial Area", addr2="Jodhpur, Rajasthan 342005"),
    "X001": dict(name="Apex Stationers & Co", gstin="08AAPFA7777Q1ZK", phone="0291-2300456",
                 addr1="Nai Sarak", addr2="Jodhpur, Rajasthan 342001"),
}


def inr(x):
    """Indian digit grouping: 1,18,000.00"""
    whole, dec = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        whole = head + "," + tail
    return f"{whole}.{dec}"


def west(x):
    return f"{x:,.2f}"


FAMILY = {  # regular -> (bold, italic)
    "Helvetica": ("Helvetica-Bold", "Helvetica-Oblique"),
    "Courier": ("Courier-Bold", "Courier-Oblique"),
    "Times-Roman": ("Times-Bold", "Times-Italic"),
}


def classic(path, v, inv, font="Helvetica"):
    bold, italic = FAMILY[font]
    c = canvas.Canvas(str(path), pagesize=A4)
    # vendor block (left) and title (right) deliberately share a baseline
    c.setFont(bold, 15); c.drawString(40, H - 50, v["name"])
    c.setFont(bold, 13); c.drawRightString(W - 40, H - 50, "TAX INVOICE")
    c.setFont(font, 9)
    c.drawString(40, H - 64, v["addr1"])
    c.drawString(40, H - 76, v["addr2"])
    c.drawString(40, H - 88, f"GSTIN: {v['gstin']}    Phone: {v['phone']}")
    # meta block (right) next to Bill-To (left)
    c.setFont(bold, 9); c.drawString(40, H - 120, "Bill To:")
    c.setFont(font, 9)
    c.drawString(40, H - 132, BUYER["name"])
    c.drawString(40, H - 144, BUYER["addr"])
    c.drawString(40, H - 156, f"GSTIN: {BUYER['gstin']}")
    y = H - 120
    rows = []
    if inv.get("number"):
        rows.append(("Invoice No:", inv["number"]))
    if inv.get("date"):
        rows.append(("Invoice Date:", inv["date"]))
    if inv.get("po"):
        rows.append(("PO Ref:", inv["po"]))
    rows.append(("Payment Terms:", "30 days"))
    for lab, val in rows:
        c.setFont(bold, 9); c.drawString(350, y, lab)
        c.setFont(font, 9); c.drawString(440, y, val)
        y -= 13
    # table
    ty = H - 200
    c.line(40, ty + 12, W - 40, ty + 12)
    c.setFont(bold, 9)
    for x, t in ((42, "S.No"), (75, "Description"), (330, "HSN/SAC"), (400, "Qty"), (440, "Rate")):
        c.drawString(x, ty, t)
    c.drawRightString(W - 42, ty, "Amount")
    c.line(40, ty - 6, W - 40, ty - 6)
    c.setFont(font, 9)
    ty -= 22
    line_sum = 0.0
    for i, (desc, hsn, qty, rate) in enumerate(inv["lines"], 1):
        amt = round(qty * rate, 2)
        line_sum += amt
        c.drawString(46, ty, str(i)); c.drawString(75, ty, desc)
        c.drawString(330, ty, hsn); c.drawString(402, ty, str(qty))
        c.drawString(440, ty, inr(rate)); c.drawRightString(W - 42, ty, inr(amt))
        ty -= 14
        if inv.get("note") and i == 1:
            c.setFont(italic, 8)
            c.drawString(75, ty, inv["note"]); c.setFont(font, 9); ty -= 14
    c.line(40, ty + 4, W - 40, ty + 4)
    sub = inv.get("subtotal_override", round(line_sum, 2))
    rate = inv.get("gst", 18)
    half = round(sub * rate / 200, 2)
    total = inv.get("total_override", round(sub + 2 * half, 2))
    ty -= 14
    for lab, val, b in (("Sub Total", sub, False), (f"CGST @ {rate/2:g}%", half, False),
                        (f"SGST @ {rate/2:g}%", half, False), ("Grand Total", total, True)):
        c.setFont(bold if b else font, 10 if b else 9)
        c.drawString(380, ty, lab); c.drawRightString(W - 42, ty, "Rs. " + inr(val) if b else inr(val))
        ty -= 14
    c.setFont(font, 8)
    c.drawString(40, 90, "Bank: HDFC Bank, Jodhpur   A/C: 50200012345678   IFSC: HDFC0001234")
    c.drawString(40, 78, "Payment due within 30 days of invoice date. Subject to Jodhpur jurisdiction.")
    if inv.get("footer"):
        c.drawString(40, 66, inv["footer"])
    c.save()
    return dict(subtotal=sub, total=total)


def bundled(path, v, inv, font="Times-Roman"):
    bold = "Times-Bold"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont(bold, 18); c.drawCentredString(W / 2, H - 55, v["name"])
    c.setFont(font, 10)
    c.drawCentredString(W / 2, H - 70, f"{v['addr1']}, {v['addr2']}")
    c.drawCentredString(W / 2, H - 83, f"GSTIN {v['gstin']}  |  Tel {v['phone']}")
    c.setFont(bold, 14); c.drawCentredString(W / 2, H - 120, "INVOICE")
    y = H - 155
    if inv.get("number"):
        c.setFont(font, 11); c.drawString(60, y, f"Invoice #: {inv['number']}"); y -= 16
    c.setFont(font, 11); c.drawString(60, y, f"Date: {inv['date']}"); y -= 16
    c.drawString(60, y, f"Billed to: {BUYER['name']}"); y -= 40
    c.line(60, y + 14, W - 60, y + 14)
    c.setFont(bold, 11); c.drawString(65, y, "Description"); c.drawRightString(W - 65, y, "Amount (INR)")
    c.line(60, y - 6, W - 60, y - 6)
    y -= 26
    c.setFont(font, 11)
    desc, amount = inv["bundle"]
    c.drawString(65, y, f"1   {desc}"); c.drawRightString(W - 65, y, west(amount))
    y -= 40
    c.line(60, y + 24, W - 60, y + 24)
    c.setFont(bold, 12)
    c.drawString(300, y, f"Total (incl. {inv.get('gst', 18)}% GST)"); c.drawRightString(W - 65, y, west(amount))
    c.setFont(font, 9)
    if inv.get("po"):
        c.drawString(60, 110, f"Against PO No. {inv['po']}")
    c.drawString(60, 96, "Remit to: ICICI Bank, Jodhpur  A/C 123400056789  IFSC ICIC0000123")
    c.save()
    return dict(total=amount)


def scan(src_pdf, dst_pdf):
    """Rasterise -> skew -> noise -> image-only PDF (simulates a phone scan)."""
    doc = fitz.open(src_pdf)
    pix = doc[0].get_pixmap(dpi=170)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    img = img.rotate(0.9, expand=True, fillcolor=(250, 250, 248), resample=Image.BICUBIC)
    noise = Image.effect_noise(img.size, 14).convert("RGB")
    img = Image.blend(img, noise, 0.07).filter(ImageFilter.GaussianBlur(0.55))
    img.save(dst_pdf, "PDF", resolution=170.0)


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.pdf"):
        f.unlink()
    V = VENDORS
    items = []

    def add(fname, title, scenario, expected, codes, fn):
        fn(OUT / fname)
        items.append(dict(file=fname, title=title, scenario=scenario, expected=expected, codes=codes))

    # 01 - happy path
    inv01 = dict(number="SOS/26-27/0412", date="12-Oct-2026", po="PO-2026-1001", gst=18,
                 lines=[("A4 copier paper 70gsm (500 sheets)", "48025690", 150, 280.00),
                        ("Toner cartridge 85A compatible", "84439959", 20, 1900.00),
                        ("Whiteboard markers (box of 10)", "96082000", 50, 400.00)])
    add("01_happy_path_clean_exact.pdf", "Happy path - clean PDF, explicit PO, exact match",
        "Machine-readable PDF, itemised, GST shown separately, PO reference printed. Everything lines up.",
        "APPROVE", [], lambda p: classic(p, V["V001"], inv01))

    # 02 - tax embedded, bundled
    inv02 = dict(number="RIT/2610/088", date="15-Oct-2026", po="PO-2026-1002", gst=18,
                 bundle=("Annual maintenance contract - laptops (Q1 instalment)", 295000.00))
    add("02_tax_embedded_bundled.pdf", "Bundled line, GST embedded in the total",
        "One lump-sum line, no subtotal or tax breakdown. The engine must back out 18% GST to compare against the PO's net value.",
        "APPROVE", ["TAX_EMBEDDED", "PARTIAL_BILLING"], lambda p: bundled(p, V["V002"], inv02))

    # 03 - implied PO
    inv03 = dict(number="SOS/26-27/0431", date="18-Oct-2026", po=None, gst=18,
                 note="Supplied as per order dated 05-Sep-2026 (mesh back, black)",
                 lines=[("Ergonomic office chairs", "94013000", 20, 10000.00)])
    add("03_po_implied_no_reference.pdf", "PO reference implied, not stated",
        "No PO number anywhere. The engine must infer the PO from vendor + amount + description and say how confident it is.",
        "APPROVE", ["IMPLIED_PO_MATCH"], lambda p: classic(p, V["V001"], inv03))

    # 04-06 - split PO
    inv04 = dict(number="TFM/1041", date="20-Oct-2026", po="PO-2026-1004", gst=18,
                 lines=[("Housekeeping services - October 2026", "998533", 1, 90000.00)])
    add("04_split_invoice_1_of_2.pdf", "Split PO - invoice 1 of 2 (60%)",
        "Vendor bills 60% of a PO. Approved as a partial; the PO ledger records the balance.",
        "APPROVE", ["PARTIAL_BILLING"], lambda p: classic(p, V["V004"], inv04, "Courier"))
    inv05 = dict(number="TFM/1057", date="27-Oct-2026", po="PO-2026-1004", gst=18,
                 lines=[("Housekeeping services - November 2026 (balance)", "998533", 1, 60000.00)])
    add("05_split_invoice_2_of_2.pdf", "Split PO - invoice 2 of 2 (closes the PO)",
        "The remaining 40%. Cumulative billing now equals the PO exactly, so it is approved and the PO closes.",
        "APPROVE", ["PO_FULLY_BILLED"], lambda p: classic(p, V["V004"], inv05, "Courier"))
    inv06 = dict(number="TFM/1066", date="28-Oct-2026", po="PO-2026-1004", gst=18,
                 lines=[("Additional deep-cleaning - common areas", "998533", 1, 12000.00)])
    add("06_split_third_invoice_overbilled.pdf", "Split PO - a third invoice pushes billing past the PO",
        "Each invoice looks fine alone; cumulative billing is 108% of the PO. Only the cumulative ledger catches it.",
        "HOLD", ["OVER_TOLERANCE"], lambda p: classic(p, V["V004"], inv06, "Courier"))

    # 07-08 - near miss
    inv07 = dict(number="MLP/10-0917", date="21-Oct-2026", po="PO-2026-1003", gst=18,
                 lines=[("Line-haul freight JDH-DEL (32 trips)", "996511", 32, 2530.00)])
    add("07_near_miss_within_tolerance.pdf", "Near-miss amount - 1.2% over PO, inside tolerance",
        "Net 80,960 vs PO 80,000. Not exact, but within the 2% tolerance - approved with the variance recorded.",
        "APPROVE", ["WITHIN_TOLERANCE"], lambda p: classic(p, V["V003"], inv07, "Times-Roman"))
    inv08 = dict(number="MLP/10-0952", date="24-Oct-2026", po="PO-2026-1007", gst=18,
                 lines=[("Warehouse racking installation", "995461", 1, 53000.00)])
    add("08_over_tolerance_6pct.pdf", "Near-miss amount - 6% over PO, outside tolerance",
        "Net 53,000 vs PO 50,000. Close enough to look right, too far to pay without a person looking.",
        "HOLD", ["OVER_TOLERANCE"], lambda p: classic(p, V["V003"], inv08, "Times-Roman"))

    # 09 - duplicate (same invoice number as 01, different file)
    inv09 = dict(inv01, footer="REMINDER: payment overdue - please remit at the earliest.")
    add("09_duplicate_resubmission.pdf", "Duplicate - the same invoice sent again",
        "Same vendor and invoice number as scenario 01, but a different file (a 'reminder' copy). Must not be paid twice.",
        "REJECT", ["DUPLICATE_EXACT"], lambda p: classic(p, V["V001"], inv09, "Helvetica"))

    # 10 - possible duplicate, no invoice number
    inv10 = dict(number=None, date="15-Oct-2026", po="PO-2026-1002", gst=18,
                 bundle=("Annual maintenance contract - laptops (Q1 instalment)", 295000.00))
    add("10_possible_duplicate_no_number.pdf", "Missing invoice number + looks like a duplicate",
        "No invoice number at all, but vendor, date and amount match scenario 02. Can't be proven a duplicate, can't be safely paid.",
        "HOLD", ["MISSING_FIELD", "POSSIBLE_DUPLICATE"], lambda p: bundled(p, V["V002"], inv10))

    # 11 - unapproved vendor
    inv11 = dict(number="AS/2026/771", date="22-Oct-2026", po=None, gst=18,
                 lines=[("Office furniture - 8 workstations", "94033000", 8, 5000.00)])
    add("11_unapproved_vendor.pdf", "Vendor not on the approved list",
        "Valid-looking invoice from a supplier procurement has never onboarded. Held and routed to vendor onboarding.",
        "HOLD", ["VENDOR_UNKNOWN"], lambda p: classic(p, V["X001"], inv11, "Helvetica"))

    # 12 - scanned, clean
    inv12 = dict(number="RIT/2610/091", date="19-Oct-2026", po="PO-2026-1006", gst=18,
                 lines=[("Network switches 24-port", "85176290", 4, 30000.00)])

    def f12(p):
        tmp = OUT / "_tmp12.pdf"
        classic(tmp, V["V002"], inv12)
        scan(tmp, p); tmp.unlink()
    add("12_scanned_image_clean.pdf", "Scanned image - no text layer, all fields present",
        "Image-only PDF with skew and noise. Needs OCR / vision to read. Once read, it is a clean match.",
        "APPROVE", [], f12)

    # 13 - scanned, missing date
    inv13 = dict(number="RIT/2610/094", date=None, po="PO-2026-1009", gst=18,
                 lines=[("Wi-Fi access points", "85176290", 6, 7500.00)])

    def f13(p):
        tmp = OUT / "_tmp13.pdf"
        classic(tmp, V["V002"], inv13)
        scan(tmp, p); tmp.unlink()
    add("13_scanned_missing_date.pdf", "Scanned image - invoice date missing",
        "Scan with no invoice date. The engine must not guess: it holds and drafts a query to the vendor.",
        "HOLD", ["MISSING_FIELD"], f13)

    # 14 - arithmetic mismatch (would pass tolerance if arithmetic weren't checked)
    inv14 = dict(number="SOS/26-27/0455", date="26-Oct-2026", po="PO-2026-1008", gst=18,
                 subtotal_override=75500.00,
                 lines=[("Printer cartridge CF217A", "84439959", 25, 2100.00),
                        ("Drum unit CF219A", "84439959", 11, 2000.00)])
    add("14_arithmetic_mismatch.pdf", "Lines don't add up to the stated subtotal",
        "Line items sum to 74,500 but the subtotal says 75,500 (+Rs 1,000). The stated total is within PO tolerance - only the arithmetic check catches it.",
        "HOLD", ["ARITH_MISMATCH"], lambda p: classic(p, V["V001"], inv14))

    manifest = [dict(order=i + 1, **it) for i, it in enumerate(items)]
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"wrote {len(items)} invoices to {OUT}")


if __name__ == "__main__":
    build()
    sys.exit(0)
