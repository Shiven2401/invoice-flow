"""Hand-filled bill books (a printed shop header, Hindi handwriting, handwritten figures). The text below is what OCR made
of real boutique bills: the printed parts come through, the Hindi doesn't, some figures do."""
from app import extract

BILL_2953 = """BELLEBOUTIQUE
B-75, Parshavnath Marg, Subhash Nagar, Jaipur-16    M:9352136920,9309036920
Bill No.    2953    Date23.9.26
M/s.    219535210<01241
PARTICULARS    Qty.    Rate    Rs.    Amount    P.
ollasnasi2da    2100
L-31
Coustomer Sign
Rs.    G. TOTAL
Auth. Sig."""

BILL_2970 = """BOUTQUE
B-75, Parshavnath Marg, Subhash Nagar, Jaipur-16    M:9352136920,9309036920
Bill No. 2970    Date) 1026
PARTICULARS    Qty.    Rate    Rs.    Amount    P.
125gmaama    300
29c222    500
Coustomer Sign    (120
G. TOTAL    3870
Auth. Sig."""

BILL_2951 = """BELLEBOUTIQUE
Bill No.    2951    Date
PARTICULARS    Qty.    Rate    Rs:    Amount    P.
21可1    811    af    1200
5    550    2750
Rs.    G. TOTAL
Auth. Sig."""


def test_a_bill_book_is_a_bill_not_rejected():
    f = extract.rules_extract(BILL_2951)          # number and the printed table only: the handwriting didn't read
    assert f["doc_type"] == "bill" and f["invoice_number"] == "2951"


def test_a_date_written_straight_after_its_label():
    assert extract.rules_extract(BILL_2953)["invoice_date"] == "2026-09-23"


def test_g_total_is_the_grand_total():
    assert extract.rules_extract(BILL_2970)["total"] == 3870


def test_boutique_is_a_business_name_even_glued():
    f = extract.rules_extract(BILL_2953)
    assert f["vendor_name"] == "BELLEBOUTIQUE" and f["confidence"]["vendor_name"] == "high"


def test_devanagari_digits_are_numbers():
    assert extract.parse_date("२२.९.२६") == "2026-09-22"
    assert extract.to_num("३९५०") == 3950
    assert extract.tidy_ocr("Date २३.९.२६") == "Date 23.9.26"


def test_a_handwritten_total_counts_as_found_when_its_line_amounts_were():
    fields = dict(total=3950.0, line_items=[dict(description="a", amount=1200.0), dict(description="b", amount=2750.0)])
    assert extract.ground(fields, BILL_2951)["total"] is True     # 3950 itself isn't in the text, 1200 and 2750 are
    assert "1200 + 2750" in fields["evidence"]["total"]


def test_a_total_that_its_amounts_dont_explain_is_still_not_found():
    fields = dict(total=4000.0, line_items=[dict(description="a", amount=1200.0), dict(description="b", amount=2750.0)])
    assert extract.ground(fields, BILL_2951)["total"] is False
    fields = dict(total=3950.0, line_items=[dict(description="a", amount=1200.0), dict(description="b", amount=2750.0)])
    assert extract.ground(fields, "Bill No. 2951    1200")["total"] is False   # 2750 isn't in this text: no proof


def test_a_refused_gemini_key_is_reported_and_not_used(monkeypatch):
    from app import config

    class Reply:
        status_code, text = 401, '{"error": {"status": "UNAUTHENTICATED"}}'

    import requests
    monkeypatch.setattr(config, "GEMINI_API_KEY", "a-bad-key")
    monkeypatch.setattr(extract, "_gemini_rejected", [None])
    assert extract.llm_provider()[0] == "Gemini"
    monkeypatch.setattr(requests, "get", lambda *a, **k: Reply())
    extract.check_gemini_key()
    assert extract.llm_provider() == (None, None) and "GEMINI_API_KEY" in extract.llm_key_problem()
    fields, meta = extract.extract_fields(dict(text=BILL_2970, page_images=["x"], notes=[]), "auto")
    assert meta["used"] == "rules" and any("rejected" in n for n in meta["notes"]) and fields["total"] == 3870


def test_the_model_is_told_about_bill_books_and_hindi():
    for hint in ("G. TOTAL", "M/s.", "Devanagari", "day.month.year", "Advance"):
        assert hint in extract.LLM_SYSTEM
