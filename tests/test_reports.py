"""SQLite persistence, aggregation, PDF generation and safe report downloads."""
import os

import pytest

from app import db
from app.reports import generator


def _insert(vendor="Cafe Aroma", total=10.0, currency="PKR", needs_review=False, created_at=None):
    db.insert_extraction(vendor=vendor, date="2025-06-12", total_amount=total, currency=currency,
                         confidence=0.9, needs_review=needs_review, source="api")
    if created_at:
        with db.get_connection() as conn:
            conn.execute("UPDATE extractions SET created_at = ? WHERE id = (SELECT MAX(id) FROM extractions)",
                         (created_at,))


def test_insert_and_read_back():
    _insert(vendor="Greenleaf", total=25.5, currency="USD", needs_review=True)
    with db.get_connection() as conn:
        [row] = conn.execute("SELECT * FROM extractions").fetchall()
    assert (row["vendor"], row["total_amount"], row["currency"], row["needs_review"]) == ("Greenleaf", 25.5, "USD", 1)
    assert row["created_at"].endswith("+00:00")


def test_persistence_survives_new_connections():
    _insert()
    db.init_db()  # idempotent schema creation must not drop data
    assert db.query_summary()["total_records"] == 1


def test_empty_summary():
    summary = db.query_summary()
    assert summary["total_records"] == 0
    assert summary["needs_review_rate"] == 0.0
    assert summary["by_currency"] == [] and summary["top_vendors"] == []


def test_aggregation_groups_by_currency_and_never_mixes_them():
    _insert(vendor="A", total=1000, currency="PKR")
    _insert(vendor="A", total=82, currency="PKR", needs_review=True)
    _insert(vendor="B", total=25.5, currency="USD")
    _insert(vendor=None, total=None, currency=None, needs_review=True)

    summary = db.query_summary()
    assert summary["total_records"] == 4
    assert summary["needs_review_count"] == 2
    assert summary["needs_review_rate"] == 0.5
    by_currency = {r["currency"]: (r["total"], r["n"]) for r in summary["by_currency"]}
    assert by_currency == {"PKR": (1082.0, 2), "USD": (25.5, 1), "unknown": (None, 1)}
    vendors = {r["vendor"]: r["n"] for r in summary["top_vendors"]}
    assert vendors == {"A": 2, "B": 1, "unknown": 1}


def test_top_vendors_limited_to_ten():
    for i in range(12):
        _insert(vendor=f"V{i}", total=i)
    assert len(db.query_summary()["top_vendors"]) == 10


def test_date_filters_are_inclusive_and_whole_day_for_bare_end_date():
    _insert(vendor="before", created_at="2025-06-10T23:59:59.000000+00:00")
    _insert(vendor="on-start", created_at="2025-06-11T00:00:00.000000+00:00")
    _insert(vendor="on-end-day", created_at="2025-06-12T18:30:00.000000+00:00")
    _insert(vendor="after", created_at="2025-06-13T00:00:00.000000+00:00")

    start = db.normalize_bound("2025-06-11", end=False)
    end = db.normalize_bound("2025-06-12", end=True)
    vendors = {r["vendor"] for r in db.query_summary(start, end)["top_vendors"]}
    assert vendors == {"on-start", "on-end-day"}


@pytest.mark.parametrize("value, end, expected", [
    ("2025-06-12", False, "2025-06-12T00:00:00.000000+00:00"),
    ("2025-06-12", True, "2025-06-12T23:59:59.999999+00:00"),
    ("2025-06-12T10:00:00", False, "2025-06-12T10:00:00.000000+00:00"),
    ("2025-06-12T15:00:00+05:00", False, "2025-06-12T10:00:00.000000+00:00"),
    (None, False, None),
    ("", True, None),
])
def test_normalize_bound(value, end, expected):
    assert db.normalize_bound(value, end) == expected


@pytest.mark.parametrize("value", ["yesterday", "2025-02-30", "12/06/2025", "2025-06-12 junk"])
def test_normalize_bound_rejects_garbage(value):
    with pytest.raises(ValueError):
        db.normalize_bound(value, end=False)


def test_generate_report_writes_real_pdf():
    _insert(vendor="Cafe Aroma", total=1082, currency="PKR")
    _insert(vendor="<b>Markup & Co</b>", total=5, currency="USD")  # odd vendor names must not break rendering
    result = generator.generate_report()
    filename = result["report_url"].removeprefix("/reports/")
    path = generator.REPORTS_DIR / filename
    assert path.is_file()
    assert path.read_bytes().startswith(b"%PDF")
    assert result["summary"]["total_records"] == 2


def test_report_filenames_are_unguessable():
    names = {generator.generate_report()["report_url"] for _ in range(3)}
    assert len(names) == 3
    for url in names:
        assert len(url.removeprefix("/reports/spending-report-").removesuffix(".pdf")) == 32


def test_download_generated_report(client):
    url = generator.generate_report()["report_url"]
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")


def test_legacy_ten_char_report_names_still_download(client):
    generator.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (generator.REPORTS_DIR / "spending-report-db9ffe4832.pdf").write_bytes(b"%PDF-1.4 legacy")
    assert client.get("/reports/spending-report-db9ffe4832.pdf").status_code == 200


def test_missing_report_is_404(client):
    resp = client.get("/reports/spending-report-0123456789.pdf")
    assert resp.status_code == 404


@pytest.mark.parametrize("path", [
    "/reports/evil.pdf",                              # wrong shape
    "/reports/spending-report-XYZ.pdf",               # not hex
    "/reports/spending-report-0123456789.PDF",
    "/reports/spending-report-0123456789.pdf.exe",
    "/reports/.env",
    "/reports/..%5C..%5Cetc%5Cpasswd",
    "/reports/spending-report-0123456789.pdf%00.txt",
])
def test_invalid_report_filenames_are_rejected(client, path):
    assert client.get(path).status_code == 400


@pytest.mark.parametrize("path", [
    "/reports/../app/main.py",
    "/reports/..%2F..%2Fetc%2Fpasswd",
    "/reports/%2e%2e/%2e%2e/etc/passwd",
])
def test_path_traversal_never_serves_files(client, path):
    resp = client.get(path)
    assert resp.status_code in (400, 404)
    assert b"root:" not in resp.content
    assert b"FastAPI" not in resp.content


def test_symlink_escaping_reports_dir_is_refused(client, tmp_path):
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"%PDF secret")
    generator.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    link = generator.REPORTS_DIR / "spending-report-abcdefabcd.pdf"
    os.symlink(secret, link)
    assert client.get("/reports/spending-report-abcdefabcd.pdf").status_code == 404


# --- regression: vendor totals must never mix currencies ----------------------

def test_vendor_with_receipts_in_multiple_currencies_is_split_per_currency():
    _insert(vendor="Global Mart", total=1000, currency="PKR")
    _insert(vendor="Global Mart", total=500, currency="PKR")
    _insert(vendor="Global Mart", total=20, currency="USD")

    rows = db.query_summary()["top_vendors"]
    by_key = {(r["vendor"], r["currency"]): (r["total"], r["n"]) for r in rows}
    assert by_key == {("Global Mart", "PKR"): (1500.0, 2), ("Global Mart", "USD"): (20.0, 1)}
    # the buggy query produced a single row totalling 1520 "units" of nothing
    assert all(r["total"] != 1520 for r in rows)


def test_vendor_rows_keep_existing_fields_and_add_currency():
    _insert(vendor="Cafe Aroma", total=1082, currency="PKR")
    [row] = db.query_summary()["top_vendors"]
    assert row == {"vendor": "Cafe Aroma", "currency": "PKR", "total": 1082.0, "n": 1}


def test_vendor_with_unknown_currency_is_its_own_group():
    _insert(vendor="Mystery Shop", total=800, currency=None)
    _insert(vendor="Mystery Shop", total=5, currency="USD")
    by_key = {(r["vendor"], r["currency"]): r["total"] for r in db.query_summary()["top_vendors"]}
    assert by_key == {("Mystery Shop", "unknown"): 800.0, ("Mystery Shop", "USD"): 5.0}


def test_top_vendors_are_ranked_within_each_currency_not_across_currencies():
    # Large PKR amounts must not crowd every USD vendor out of the top list.
    for i in range(12):
        _insert(vendor=f"PKR vendor {i}", total=10_000 + i, currency="PKR")
    _insert(vendor="Small USD vendor", total=3, currency="USD")

    rows = db.query_summary()["top_vendors"]
    pkr = [r for r in rows if r["currency"] == "PKR"]
    usd = [r for r in rows if r["currency"] == "USD"]
    assert len(pkr) == 10                      # still capped at 10 per currency
    assert [r["vendor"] for r in usd] == ["Small USD vendor"]
    assert [r["total"] for r in pkr] == sorted((r["total"] for r in pkr), reverse=True)


def test_vendor_date_filter_applies_to_currency_split():
    _insert(vendor="Global Mart", total=100, currency="PKR", created_at="2025-06-01T10:00:00.000000+00:00")
    _insert(vendor="Global Mart", total=7, currency="USD", created_at="2025-07-01T10:00:00.000000+00:00")
    start = db.normalize_bound("2025-06-15", end=False)
    rows = db.query_summary(start_date=start)["top_vendors"]
    assert [(r["vendor"], r["currency"], r["total"]) for r in rows] == [("Global Mart", "USD", 7.0)]


def test_pdf_vendor_table_shows_currency_per_row(monkeypatch):
    _insert(vendor="Global Mart", total=1000, currency="PKR")
    _insert(vendor="Global Mart", total=20, currency="USD")

    captured = {}
    real_table = generator.Table

    def spy_table(data, *args, **kwargs):
        if data and data[0] and data[0][0] == "Vendor":
            captured["rows"] = data
        return real_table(data, *args, **kwargs)

    monkeypatch.setattr(generator, "Table", spy_table)
    result = generator.generate_report()

    assert captured["rows"][0] == ["Vendor", "Currency", "Total", "Receipts"]
    assert sorted(captured["rows"][1:]) == [
        ["Global Mart", "PKR", "1,000.00", "1"],
        ["Global Mart", "USD", "20.00", "1"],
    ]
    path = generator.REPORTS_DIR / result["report_url"].removeprefix("/reports/")
    assert path.read_bytes().startswith(b"%PDF")


def test_report_job_result_exposes_currency_per_vendor(client):
    from .conftest import run_queued_jobs

    _insert(vendor="Global Mart", total=1000, currency="PKR")
    _insert(vendor="Global Mart", total=20, currency="USD")
    job_id = client.post("/jobs/report", json={}).json()["job_id"]
    run_queued_jobs()
    vendors = client.get(f"/jobs/{job_id}").json()["result"]["summary"]["top_vendors"]
    assert {(v["vendor"], v["currency"], v["total"]) for v in vendors} == {
        ("Global Mart", "PKR", 1000.0), ("Global Mart", "USD", 20.0),
    }
