import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pytest
from PIL import Image

from app import extract
from run_samples import run_all

# what OCR gives for a phone photo of a boxed GST invoice, after layout_rows()
OCR_GST_INVOICE = """(ORIGINAL FOR RECIPIENT)
Tax Invoice
TAXINVOICE    Invoice No.    Dated
CHARU MARKETING FY 23-25    MMC/16832/25-26    2-Dec-25
GSTIN/UIN:08AWSPB0932G1ZM
Buyer(Bill to)
SHREE GANESHAYE NAMAH    Dispatched through    Destination
Description of Goods    HSN/SAC    GST    Quantity    Rate    Rate    per    Disc.%    Amount
VEEBA    09041200    5 %    1PCS    112.50    107.14    PCS    107.14
GoDice Mozz    04062000    5%    2.000KG    440.00    419.05KG    838.10
OUTPUT CGST    71.63
OUTPUTSGST    71.63
"""


@pytest.fixture(scope="module")
def rows():
    return run_all("rules")


def test_every_scenario_matches_expected_decision(rows):
    wrong = [(r["m"]["file"], r["res"]["decision"], r["m"]["expected"]) for r in rows if r["res"]["decision"] != r["m"]["expected"]]
    assert not wrong, wrong


def test_expected_reason_codes_are_raised(rows):
    wrong = [(r["m"]["file"], [c for c in r["m"]["codes"] if c not in r["codes"]]) for r in rows
             if any(c not in r["codes"] for c in r["m"]["codes"])]
    assert not wrong, wrong


def test_nine_stages_stream_for_every_run(rows):
    for r in rows:
        done = [e[0] for e in r["events"] if e[1] != "running"]
        assert done[-1] == "decision" and len(set(done)) == 9, (r["m"]["file"], done)


def test_split_po_is_judged_cumulatively(rows):
    by = {r["m"]["order"]: r["res"] for r in rows}
    assert by[4]["budget"]["pct_billed"] == 60.0
    assert by[5]["budget"]["billed_before"] == 90000.0 and by[5]["budget"]["band"] == "exact"
    assert by[6]["budget"]["billed_before"] == 150000.0 and by[6]["budget"]["over_pct"] == 8.0


def test_decisions_always_carry_an_explanation(rows):
    for r in rows:
        assert r["res"]["headline"] and r["res"]["findings"]
        if r["res"]["decision"] != "APPROVE":
            assert r["res"]["next_steps"] and r["res"]["message"]


def test_enhance_gives_enlarged_grayscale():
    out = extract.enhance(Image.new("RGB", (400, 300), (200, 180, 120)))
    assert out.ndim == 2 and max(out.shape) == 1200  # capped at 3x


def test_layout_rows_groups_a_tilted_row_and_orders_cells():
    box = lambda x, y, text: ([[x, y], [x + 100, y + 2], [x + 100, y + 22], [x, y + 20]], text, 0.99)
    # the right-hand end of each row sits 16 px lower, more than half a line's height
    boxes = [box(800, 116, "107.14"), box(0, 100, "VEEBA"), box(800, 156, "838.10"), box(0, 140, "Go Dice")]
    assert extract.layout_rows(boxes) == "VEEBA    107.14\nGo Dice    838.10"


def test_layout_rows_handles_a_blank_page():
    assert extract.layout_rows(None) == ""


def test_short_year_month_name_date():
    assert extract.parse_date("2-Dec-25") == "2025-12-02"


def test_boxed_gst_invoice_fields_are_extracted():
    f = extract.rules_extract(OCR_GST_INVOICE)
    assert f["vendor_name"] == "CHARU MARKETING FY 23-25"
    assert f["vendor_gstin"] == "08AWSPB0932G1ZM"
    assert f["buyer_name"] == "SHREE GANESHAYE NAMAH"
    assert f["invoice_number"] == "MMC/16832/25-26"
    assert f["invoice_date"] == "2025-12-02"
    assert f["tax_amount"] == 143.26
    assert [(li["description"], li["quantity"], li["unit_price"], li["amount"]) for li in f["line_items"]] == [
        ("VEEBA", 1.0, 107.14, 107.14), ("GoDice Mozz", 2.0, 419.05, 838.10)]


def test_gstin_glued_to_next_word_beats_the_buyers():
    f = extract.rules_extract("Acme Pvt Ltd\nGSTIN:08AAFFR5678K1Z2Phone:0291-2512288\nBill To:\nGSTIN:08AAACO1234P1Z9\n")
    assert f["vendor_gstin"] == "08AAFFR5678K1Z2"
