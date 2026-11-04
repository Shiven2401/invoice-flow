"""Vendor approval, typed details and the Excel report."""
import json
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pytest
from fastapi import HTTPException
from PIL import Image

from app import config, db, export, extract, main, pipeline, rules

SAMPLES = ROOT / "samples/synthetic"
UID = 901
USER = dict(id=UID, username="tester")

GUIDE = """AI Solutions Analyst - Case Study
Everything you need to know to do your best work
Congratulations - you've moved to the next round
We work with companies on hard, messy operational problems - invoice processing, vendor onboarding, GTM execution.
Problem 1: build a process that reads an invoice, extracts the invoice number, date, vendor and total, and matches a purchase order.
Submit a link to your process and a demo video by 15-01-2026.
"""
CONTRACT = """Service Agreement
This agreement is dated 12-03-2026 between the parties below.
The supplier shall invoice monthly and each bill is payable within 30 days.
Total fee: Rs 50,000.00
GSTIN: 08AABCS1234F1Z5
"""
HANDWRITTEN_OCR = """INVOICE    9079081766
LMR    LMR PHYSIO CARE
& REHAB CENTRE
Bil No.: 106    Date:3.-o6-26
S.No.    Particulars    Qty.    Rate    Amount
TOTALAMOUNT    495eo
"""
NO_TITLE_INVOICE = """Sharma Office Supplies Pvt Ltd
Invoice No: SOS/26-27/0431
Date: 12-10-2026
Grand Total    1,18,000.00
"""
# OCR of a motor insurance schedule that doubles as a tax invoice: "Invoice" is unreadable in the small print
INSURANCE_OCR = """SBI General Insurance Company Limited.
SURAKSHA AUR BHAROSA DONO    CertificateCumPoliey Schedule(FORMS1OFTHECENTRALMOTORVEHICLERULES,1989)
PAN:AAMCSS857LGSTIN:08AAMCS8857L1ZCCIN:U66000MH2009PLC190546IRDAIReg:144
Pellcy No:    HYNDAIH1IB/1291232    Proposal No.&Date:    HI1811935017,29-OCT-2024
Sub Total (Basic Premlum)    14,165
Sub Total (Deduetibles)    3,117    Notes:1.ConsolidatedStamp duty paid to state exchoquer
Total Own Damage Premium    11,048    Certifieate is belion e date of commcncement of the Policy
Net Own Damage Premium (A)    23,244    4.Policyissuaee is subject torealization of chequeif premiumis paid by cheque
SGST(9%)    2,092    5.Thepolicy isubjeet to compuisory deductible of INR IDO
CGST(9%)    2092    6.This policyis toberead inconjunction with the Policy
Gross Premiuu Paid    27,428    vehiele.    7.TCovergdertholiyinlyfrODederabityncnctn withthe
"""
# OCR of a restaurant bill: discount, GST plus VAT and surcharge, wrapped rows, values under labels, the time glued to the date
RESTAURANT_OCR = """15446
IIGSTVATINVOICEIL
BOGEN CAFE & BISTRO
GSTNo.08ABXFM9475F1ZC
Date    Table No.    lnvoice No.
28-11-202510:57PM    0-1    9697
Description    Qly    RateAmount
BLUEBERRY MARTINI    1    279.00    279.00
CHARGRILLED PANEER
2    489.00    978.00
MINERALWATERMRP(1    60.00
3    180.00
SULABRUTBOTTLE    1    4000.004000.00
TUBORG STRONG CAN    6    350.002100.00
Gross Amount    14500.00
Discount @25.00%(-)    3625.00
CGST@2.5%    136.70
SGST@2.5%    136.70
VAT@20%    1081.50
Sur Charge on VAT@
216.30
20.00%
Round Off (-)    -0.20
Net Amount12446.00
Tin:08869410
"""
NO_TITLE_NO_NUMBER_INVOICE = """Sharma Office Supplies Pvt Ltd
GSTIN: 08AABCS1234F1Z5
Invoice dated 12-10-2026
CGST @ 9%    9,000.00
SGST @ 9%    9,000.00
Grand Total    1,18,000.00
"""


def process(filename, inputs=None):
    rid = main._enqueue(SAMPLES / filename, filename, UID, inputs)
    wait(rid)
    return rid


def wait(rid):
    for _ in range(600):
        if main.BUS[rid]["done"]:
            return
        time.sleep(0.05)
    raise TimeoutError(rid)


@pytest.fixture(autouse=True)
def clean():
    main.reset(USER)


def codes(rid):
    return {x["code"] for x in json.loads(db.get_run(rid, UID)["result_json"])["findings"]}


# ------------------------------------------------------------ vendor approval --

def test_approving_an_unknown_vendor_clears_the_vendor_hold():
    rid = process("11_unapproved_vendor.pdf")
    assert "VENDOR_UNKNOWN" in codes(rid)
    main.approve_vendor(rid, dict(note="checked with procurement"), USER)
    wait(rid)
    assert "VENDOR_UNKNOWN" not in codes(rid) and db.get_run(rid, UID)["vendor_id"] == "V006"
    assert "DUPLICATE_EXACT" not in codes(rid)  # a re-check must not match the invoice to itself


def test_corrected_vendor_name_still_matches_the_invoice_on_recheck():
    rid = process("11_unapproved_vendor.pdf")
    main.approve_vendor(rid, dict(name="Apex Stationery Wholesale", gstin=""), USER)
    wait(rid)
    assert "VENDOR_UNKNOWN" not in codes(rid)


def test_only_unknown_vendors_can_be_approved():
    rid = process("01_happy_path_clean_exact.pdf")  # approved, nothing to resolve
    with pytest.raises(HTTPException) as e:
        main.approve_vendor(rid, {}, USER)
    assert e.value.status_code == 400


@pytest.mark.parametrize("name, gstin", [
    ("Kiran Courier Services", ""),             # blocked vendor by name
    ("Totally New Traders", "08AAKFK4321R1Z3"),  # blocked vendor's GSTIN under a new name
    ("Sharma Office Supplies", ""),             # already on the list
    ("Totally New Traders", "NOT-A-GSTIN"),
    ("", ""),
])
def test_approval_cannot_get_round_the_vendor_list(name, gstin):
    assert rules.vendor_approval_problem(name, gstin, rules.load_vendors(UID))


def test_blocked_vendor_cannot_be_smuggled_in_through_the_form():
    rid = process("11_unapproved_vendor.pdf")
    with pytest.raises(HTTPException):
        main.approve_vendor(rid, dict(name="Kiran Courier Services", gstin="08AAKFK4321R1Z3"), USER)
    assert not db.added_vendors(UID)


def test_approved_vendor_can_be_removed_but_the_master_list_cannot():
    rid = process("11_unapproved_vendor.pdf")
    vid = main.approve_vendor(rid, {}, USER)["vendor_id"]
    wait(rid)
    assert vid in {v["vendor_id"] for v in rules.load_vendors(UID)}
    main.remove_vendor(vid, USER)
    assert vid not in {v["vendor_id"] for v in rules.load_vendors(UID)}
    with pytest.raises(HTTPException):
        main.remove_vendor("V001", USER)


def test_reset_clears_app_approved_vendors():
    rid = process("11_unapproved_vendor.pdf")
    main.approve_vendor(rid, {}, USER)
    wait(rid)
    main.reset(USER)
    assert not db.added_vendors(UID)


def test_existing_database_gets_the_new_column():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE runs(id TEXT PRIMARY KEY)")
    db.init(c)
    assert {"inputs_json", "user_id"} <= {r[1] for r in c.execute("PRAGMA table_info(runs)")}


def test_a_dropped_browser_connection_is_not_logged_as_an_error(monkeypatch):
    import logging
    from app import logs
    sent = []
    monkeypatch.setattr(logs.logger, "patch", lambda *a, **k: sent.append(a) or logs.logger)
    try:
        raise ConnectionResetError(10054, "An existing connection was forcibly closed")
    except ConnectionResetError:
        import sys as _sys
        noise = logging.LogRecord("asyncio", logging.ERROR, __file__, 1, "Exception in callback", None, _sys.exc_info())
    logs._Intercept().emit(noise)
    assert not sent
    logs._Intercept().emit(logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "a real problem", None, None))
    assert sent


# --------------------------------------------------------------- typed details --

def test_clean_inputs_normalises_and_drops_blanks():
    assert extract.clean_inputs(dict(total="Rs. 1,18,000.50", invoice_date="2-Dec-25", po_reference=" po-2026-1001 ", vendor_name=" ")) == dict(
        total=118000.5, invoice_date="2025-12-02", po_reference="PO-2026-1001")


@pytest.mark.parametrize("raw", [dict(total="abc"), dict(total="-5"), dict(invoice_date="soon"), dict(vendor_gstin="123"), "not a dict"])
def test_clean_inputs_refuses_what_it_cannot_use(raw):
    with pytest.raises(ValueError):
        extract.clean_inputs(raw)


def test_typed_value_fills_a_gap_and_says_so():
    rid = process("13_scanned_missing_date.pdf", dict(invoice_date="2026-10-19"))
    run = main.run_detail(rid, USER)
    assert run["invoice_date"] == "2026-10-19" and "USER_ENTERED" in codes(rid) and "MISSING_FIELD" not in codes(rid)


def test_typed_value_that_contradicts_the_document_holds_the_invoice():
    rid = process("01_happy_path_clean_exact.pdf", dict(total=1.0))
    assert "USER_INPUT_CONFLICT" in codes(rid) and db.get_run(rid, UID)["decision"] == "HOLD"


def test_typed_value_that_agrees_with_the_document_changes_nothing():
    f = dict(total=118000.0, confidence={}, grounding={"total": True})
    assert extract.apply_inputs(f, dict(total=118000.0)) == ([], [])
    assert f["grounding"]["total"] is True


def test_upload_refuses_field_details_for_several_files():
    class Upload:
        filename = "a.pdf"
    import asyncio
    with pytest.raises(HTTPException) as e:
        asyncio.run(main.upload([Upload(), Upload()], json.dumps(dict(total="100")), USER))
    assert e.value.status_code == 400


# --------------------------------------------------------------- not an invoice --

@pytest.mark.parametrize("text", [GUIDE, "Invoice Processing Policy\nAll invoices must be approved within 30 days.\n"])
def test_text_about_invoices_is_not_an_invoice(text):
    assert extract.rules_extract(text)["doc_type"] == "other"


def test_some_invoice_details_but_no_proof_goes_to_a_person_not_the_bin():
    f = extract.rules_extract(CONTRACT)
    assert f["doc_type"] == "unconfirmed"
    _checks, fs = rules.validate(f, rules.derive_amounts(f))
    assert "INVOICE_UNCONFIRMED" in {x["code"] for x in fs} and "NOT_AN_INVOICE" not in {x["code"] for x in fs}


def test_insurance_bill_with_an_unreadable_title_is_read_correctly():
    f = extract.rules_extract(INSURANCE_OCR)
    assert f["doc_type"] == "invoice"
    assert f["vendor_gstin"] == "08AAMCS8857L1ZC"
    assert (f["total"], f["subtotal"], f["tax_amount"]) == (27428.0, 23244.0, 4184.0)
    _checks, fs = rules.validate(f, rules.derive_amounts(f))
    assert "ARITH_MISMATCH" not in {x["code"] for x in fs}


def test_restaurant_bill_with_discount_and_vat_is_read_correctly():
    f = extract.rules_extract(RESTAURANT_OCR)
    f["grounding"] = extract.ground(f, RESTAURANT_OCR)
    assert (f["vendor_name"], f["vendor_gstin"], f["invoice_number"], f["invoice_date"]) == ("BOGEN CAFE & BISTRO", "08ABXFM9475F1ZC", "9697", "2025-11-28")
    assert (f["subtotal"], f["discount"], f["tax_amount"], f["total"]) == (10875.0, 3625.0, 1571.2, 12446.0)
    assert f["tax_rate"] is None  # 5% GST plus 20% VAT is no single rate
    assert [li["description"] for li in f["line_items"]][:2] == ["BLUEBERRY MARTINI", "CHARGRILLED PANEER"]
    _checks, fs = rules.validate(f, rules.derive_amounts(f))
    assert not {"ARITH_MISMATCH", "UNGROUNDED_FIELD", "MISSING_FIELD"} & {x["code"] for x in fs}


def test_net_and_gross_mean_different_things_either_side_of_the_tax():
    before = extract.rules_extract("TAX INVOICE\nNet Amount    1000.00\nCGST 9%    90.00\nSGST 9%    90.00\nGrand Total    1180.00\n")
    assert (before["subtotal"], before["total"]) == (1000.0, 1180.0)
    after = extract.rules_extract("TAX INVOICE\nGross Amount    1000.00\nCGST 9%    90.00\nSGST 9%    90.00\nNet Amount    1180.00\n")
    assert (after["subtotal"], after["total"]) == (1000.0, 1180.0)
    no_tax = extract.rules_extract("INVOICE\nGross Amount    1000.00\n")
    assert no_tax["total"] == 1000.0


def test_a_number_inside_other_text_is_never_taken_as_an_amount():
    f = extract.rules_extract("TAX INVOICE\nNet Amount\nTin:08869410\n")
    assert f["total"] is None and f["subtotal"] is None


def test_discount_is_not_applied_when_the_sums_do_not_support_it():
    f = extract.rules_extract("TAX INVOICE\nSub Total    900.00\nDiscount    100.00\nCGST 9%    81.00\nSGST 9%    81.00\nGrand Total    1062.00\n")
    assert f["subtotal"] == 900.0 and "discount" not in f  # the subtotal is already after the discount


def test_full_item_table_with_a_discount_still_has_its_sum_checked():
    f = extract.rules_extract(RESTAURANT_OCR.replace("MINERALWATERMRP(1    60.00\n3    180.00\n", ""))
    assert not f.get("items_incomplete")
    checks, fs = rules.validate(f, rules.derive_amounts(f))
    assert "ARITH_MISMATCH" in {x["code"] for x in fs}  # the rows here don't reach the printed gross, and every row was read


def test_small_document_in_a_big_photo_is_cropped_to_the_paper():
    img = Image.new("RGB", (1000, 1000))
    box = lambda x, y: ([[x, y], [x + 100, y], [x + 100, y + 20], [x, y + 20]], "text", 0.9)
    crop = extract._paper_crop(img, (1000, 1000), [box(400, 200), box(500, 700)])
    assert crop.size[0] < 400 and crop.size[1] < 650
    assert extract._paper_crop(img, (1000, 1000), [box(0, 0), box(900, 950)]) is None  # already fills the frame


def test_reconciling_the_subtotal_cannot_explain_away_a_real_mismatch():
    f = extract.rules_extract("TAX INVOICE\nDescription    Amount\nWidgets    110.00\nSub Total    100.00\nCGST 9%    18.00\nGrand Total    128.00\n")
    assert f["subtotal"] == 100.0  # 110 fits the sums, but it isn't labelled as a pre-tax total


def test_person_can_overrule_not_an_invoice_but_it_is_only_ever_held(tmp_path):
    path = tmp_path / "plain.png"
    Image.new("RGB", (800, 600), (120, 160, 90)).save(path)
    rid = main._enqueue(path, "plain.png", UID)
    wait(rid)
    main.treat_as_invoice(rid, USER)
    wait(rid)
    assert db.get_run(rid, UID)["decision"] == "HOLD" and "NOT_AN_INVOICE" not in codes(rid)
    assert main.stats(USER)["counts"]["HOLD"] == 1  # back on the Dashboard for a person


def test_overruling_is_only_for_documents_rejected_as_not_an_invoice():
    rid = process("01_happy_path_clean_exact.pdf")
    with pytest.raises(HTTPException):
        main.treat_as_invoice(rid, USER)


def test_is_invoice_cannot_be_set_from_the_upload_form():
    assert "is_invoice" not in extract.clean_inputs(dict(is_invoice=True, note="x"))


@pytest.mark.parametrize("text, kind", [(HANDWRITTEN_OCR, "invoice"), (NO_TITLE_INVOICE, "invoice"), (NO_TITLE_NO_NUMBER_INVOICE, "invoice"),
                                        ("TAX INVOICE\nnothing else was legible\n", "invoice"),
                                        ("BILL OF SUPPLY\nAcme Traders\n", "bill"), ("Payment successful\nReceipt 123\n", "receipt")])
def test_real_invoices_stay_invoices_however_little_was_read(text, kind):
    assert extract.rules_extract(text)["doc_type"] == kind


def test_non_invoice_is_rejected_with_a_clear_reason():
    f = extract.rules_extract(GUIDE)
    _checks, fs = rules.validate(f, rules.derive_amounts(f))
    x = next(x for x in fs if x["code"] == "NOT_AN_INVOICE")
    assert x["severity"] == "reject" and x["message"].startswith("This document is not an invoice")
    assert rules.NEXT["INVOICE_UNCONFIRMED"] and rules.CODE_LABEL["INVOICE_UNCONFIRMED"]


def test_photo_with_no_text_is_rejected_as_not_an_invoice(tmp_path):
    path = tmp_path / "plain.png"
    Image.new("RGB", (800, 600), (120, 160, 90)).save(path)
    rid = main._enqueue(path, "plain.png", UID)
    wait(rid)
    assert db.get_run(rid, UID)["decision"] == "REJECT" and "NOT_AN_INVOICE" in codes(rid)
    assert [e["stage"] for e in main.BUS[rid]["events"] if e["status"] == "skipped"] == ["validate", "vendor", "po", "duplicates", "budget"]


# ------------------------------------------------- rejected stays off the Dashboard --

def test_rejected_documents_are_left_out_of_the_dashboard_figures():
    held = process("11_unapproved_vendor.pdf")
    process("01_happy_path_clean_exact.pdf")
    process("01_happy_path_clean_exact.pdf")  # exact duplicate: rejected
    st = main.stats(USER)
    assert st["total"] == 2 and st["counts"] == dict(APPROVE=1, HOLD=1)
    assert "DUPLICATE_EXACT" not in {r["code"] for r in st["reasons"]}
    main.override(held, dict(decision="REJECT"), USER)  # a person's rejection counts too
    assert main.stats(USER)["counts"] == dict(APPROVE=1, HOLD=0)


def test_report_keeps_rejected_documents_on_their_own_sheet():
    process("01_happy_path_clean_exact.pdf")
    process("01_happy_path_clean_exact.pdf")
    z = zipfile.ZipFile(BytesIO(build()))
    assert "Rejected" in z.read("xl/workbook.xml").decode()
    data = next(z.read(n).decode() for n in sorted(z.namelist()) if n.startswith("xl/charts/chart"))
    assert data.count("<c:pt idx") and "Rejected" not in data


# ---------------------------------------------------------------- Excel report --

def build():
    rows = db.list_runs(UID, limit=-1)
    return export.workbook(main._stats(rows), rows, main.pos(USER)["pos"], rules.load_vendors(UID))


def test_report_has_all_sheets_and_four_charts():
    for name in ("01_happy_path_clean_exact.pdf", "11_unapproved_vendor.pdf", "09_duplicate_resubmission.pdf"):
        process(name)
    z = zipfile.ZipFile(BytesIO(build()))
    book = z.read("xl/workbook.xml").decode()
    assert all(s in book for s in ("Summary", "Invoices", "PO ledger", "Approved vendors", "Blocked vendors", "Rejected", "Chart data"))
    assert len([n for n in z.namelist() if n.startswith("xl/charts/chart")]) == 4


def test_horizontal_charts_label_their_categories():
    for name in ("01_happy_path_clean_exact.pdf", "11_unapproved_vendor.pdf"):
        process(name)
    z = zipfile.ZipFile(BytesIO(build()))
    xml = next(z.read(n).decode() for n in z.namelist() if n.startswith("xl/charts/chart") and b'barDir val="bar"' in z.read(n))
    cat, val = xml.split("<c:catAx>")[1].split("</c:catAx>")[0], xml.split("<c:valAx>")[1].split("</c:valAx>")[0]
    assert '<c:delete val="1"/>' not in cat and 'orientation val="maxMin"' in cat  # names shown, largest on top
    assert '<c:delete val="1"/>' in val  # bars carry their own numbers


def test_report_with_nothing_processed_still_opens():
    z = zipfile.ZipFile(BytesIO(build()))
    assert not [n for n in z.namelist() if n.startswith("xl/charts/chart")]


def test_text_from_a_document_never_becomes_a_formula():
    rid = process("01_happy_path_clean_exact.pdf")
    db.update_run(rid, vendor_name='=HYPERLINK("http://evil.example","x")')
    z = zipfile.ZipFile(BytesIO(build()))
    assert not any(b"<f>" in z.read(n) for n in z.namelist() if n.startswith("xl/worksheets/sheet"))


@pytest.mark.parametrize("raw, want", [
    ("Date:30..-06-26", "Date:30-06-26"),
    ("Date:30..06-26", "Date:30-06-26"),
    ("Date 30-06..26", "Date 30-06-26"),
    ("M/s......Ravi Traders", "M/s Ravi Traders"),
    ("Total 1,500.00", "Total 1,500.00"),
])
def test_dotted_fill_in_lines_are_tidied(raw, want):
    assert extract.tidy_ocr(raw) == want


def test_tax_is_read_from_its_own_column_not_the_notes_beside_it():
    text = ("Net Own Damage Premium (A)    23,244    4. Policy issuance is subject to realization of cheque\n"
            "SGST(9%)    2,092    5.The policy is subject to compulsory deductible of INR 1000 (IMT22)\n"
            "CGST(9%)    2092    6. This policy is to be read with the wordings\n"
            "Gruss Premiuw Paid    27,428    vehicle.\n")
    f = extract.rules_extract(text)
    assert (f["tax_amount"], f["subtotal"], f["total"]) == (4184.0, 23244.0, 27428.0)


def test_two_taxes_in_one_cell_are_both_counted():
    f = extract.rules_extract("Acme Traders\nTax Invoice\nSub Total 1,000.00\nCGST 9% 90.00 SGST 9% 90.00\nGrand Total 1,180.00\n")
    assert f["tax_amount"] == 180.0 and f["tax_rate"] == 18.0


@pytest.mark.parametrize("key, usable", [("sk-ant-api03-abc", True), ("AQ.example-google-style-key", False), ("", False)])
def test_only_an_anthropic_key_turns_on_claude(monkeypatch, key, usable):
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    assert extract.llm_available() is usable
    assert (extract.llm_key_problem() is not None) == (bool(key) and not usable)


def test_gemini_reads_when_its_key_is_set_and_the_key_stays_out_of_the_url(monkeypatch):
    import requests
    seen = {}

    class Reply:
        ok, status_code = True, 200
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": '{"doc_type": "invoice", "vendor_name": "LMR Physio Care", '
                                                                     '"invoice_number": "106", "invoice_date": "2026-06-30", "total": 49500}'}]}}]}

    def post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, body=json)
        return Reply()

    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(requests, "post", post)
    assert extract.llm_provider() == ("Gemini", config.GEMINI_MODEL)
    f, meta = extract.extract_fields(dict(text="Bill No. 106", method="ocr", page_images=["aGVsbG8="], notes=[]), "llm")
    assert meta["used"] == f"llm:{config.GEMINI_MODEL}" and (f["vendor_name"], f["invoice_number"], f["total"]) == ("LMR Physio Care", "106", 49500.0)
    assert "test-key" not in seen["url"] and seen["headers"]["x-goog-api-key"] == "test-key"
    assert seen["body"]["contents"][0]["parts"][0]["inline_data"]["mime_type"] == "image/jpeg"


def test_a_busy_gemini_falls_back_to_the_rules(monkeypatch):
    import requests

    class Busy:
        ok, status_code, text = False, 503, "high demand"

    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(requests, "post", lambda *a, **k: Busy())
    monkeypatch.setattr(extract.time, "sleep", lambda s: None)
    f, meta = extract.extract_fields(dict(text="Acme Traders\nTax Invoice\nInvoice No: A-1\nTotal 500.00", method="ocr", page_images=[], notes=[]), "llm")
    assert meta["used"] == "rules" and f["invoice_number"] == "A-1" and any("503" in n for n in meta["notes"])


def test_the_gemini_daily_limit_falls_back_to_the_rules(monkeypatch):
    import requests
    calls = []

    class Reply:
        ok, status_code = True, 200
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": '{"doc_type": "invoice", "invoice_number": "G-1", "total": 100}'}]}}]}

    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "GEMINI_DAILY_LIMIT", 2)
    monkeypatch.setattr(extract, "_gemini_today", [None, 0])
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or Reply())
    doc = dict(text="Acme Traders\nTax Invoice\nInvoice No: A-1\nTotal 500.00", method="ocr", page_images=["aGVsbG8="], notes=[])
    used = [extract.extract_fields(dict(doc, notes=[]), "llm")[1]["used"] for _ in range(3)]
    assert used == [f"llm:{config.GEMINI_MODEL}"] * 2 + ["rules"] and len(calls) == 2
    monkeypatch.setattr(extract, "_gemini_today", ["1999-01-01", 2])  # a new day starts the count again
    assert extract.extract_fields(dict(doc, notes=[]), "llm")[1]["used"].startswith("llm:")


def test_the_sign_in_page_has_no_model_notice(monkeypatch):
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    assert "auth-note" not in c.get("/").text
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")  # removed at the owner's request, with a key set too
    assert "auth-note" not in c.get("/").text and "sent to Google Gemini" not in c.get("/").text
