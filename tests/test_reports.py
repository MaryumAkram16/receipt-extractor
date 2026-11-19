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
