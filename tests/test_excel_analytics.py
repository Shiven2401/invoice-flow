"""The Excel report carries the Analytics tab: an Analytics sheet with every card's figures, scoped like the rest of the report."""
import re
import zipfile
from io import BytesIO

from app import export, main
from test_accounts import SAMPLE, UNKNOWN_VENDOR, clean, client, folder, run_sample  # noqa: F401  (clean is an autouse fixture)


def sheets(content):
    z = zipfile.ZipFile(BytesIO(content))
    names = re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
    return names, z.read("xl/sharedStrings.xml").decode(), z


def test_the_report_has_an_analytics_sheet_with_every_card():
    c = client("asha")
    run_sample(c, SAMPLE)
    run_sample(c, UNKNOWN_VENDOR)
    names, strings, z = sheets(c.get("/api/export.xlsx").content)
    assert names[:2] == ["Summary", "Analytics"]          # right after the summary, before the invoice list
    assert "Invoices" in names
    for card in ("Outcomes and money on hold", "Money on hold, by how long it has waited", "Spend over time",
                 "Fraud and control flags", "Vendor scorecard", "Batch throughput"):
        assert card in strings, card
    assert "Call the vendor" in strings and "Ask for a resend" in strings
    charts = [n for n in z.namelist() if n.startswith("xl/charts/chart")]
    assert len(charts) >= 5                               # the Summary's charts plus the Analytics sheet's


def test_the_analytics_sheet_follows_the_chosen_folder():
    c = client("asha")
    office = folder(c, "Office")
    a = run_sample(c, SAMPLE)
    run_sample(c, UNKNOWN_VENDOR)
    c.post(f"/api/runs/{a}/folder", json=dict(folder_id=office))
    names, strings, _z = sheets(c.get("/api/export.xlsx", params=dict(folder=office)).content)
    assert "Folder: Office." in strings
    assert SAMPLE in strings and UNKNOWN_VENDOR not in strings   # the folder's invoice list only


def test_an_empty_report_still_builds():
    data = export.workbook(main._stats([]), [], [], [], analytics=main._analytics([], top=None))
    names, strings, _z = sheets(data)
    assert "Analytics" in names and "No multi-file uploads yet." in strings


def test_months_fill_the_gaps_like_the_page():
    assert export._months([("2026-08-03", 10), ("2026-10-01", 5), ("2026-10-20", 1.5)]) == [("Aug 2026", 10), ("Sep 2026", 0), ("Oct 2026", 6.5)]
    assert export._months([]) == []
