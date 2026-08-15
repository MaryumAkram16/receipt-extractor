"""Renders the spending summary into a PDF and saves it to disk.

The job result carries a URL to the file (`/reports/{filename}`), never the
PDF bytes themselves — the same "store and link" principle as any large
artifact: passing megabytes through a job status endpoint doesn't scale and
isn't what that endpoint is for.
"""
import uuid
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from .. import db

REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"


def generate_report(start_date: str | None = None, end_date: str | None = None) -> dict:
    """Queries the aggregation, renders a PDF, and returns {report_url, summary}.

    Every number in the PDF comes straight from `db.query_summary` — the
    report layer formats, it doesn't compute.
    """
    summary = db.query_summary(start_date, end_date)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"spending-report-{uuid.uuid4().hex[:10]}.pdf"
    filepath = REPORTS_DIR / filename

    _render_pdf(filepath, summary)

    return {
        "report_url": f"/reports/{filename}",
        "summary": summary,
    }


def _fmt_date(iso_str: str | None) -> str:
    if not iso_str:
        return "—"
    try:
        return datetime.fromisoformat(iso_str).strftime("%Y-%m-%d")
    except ValueError:
        return iso_str


def _render_pdf(filepath: Path, summary: dict) -> None:
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"], fontSize=20, spaceAfter=4
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle", parent=styles["Normal"], textColor=colors.grey, spaceAfter=20
    )
    heading_style = ParagraphStyle(
        "SectionHeading", parent=styles["Heading2"], spaceBefore=18, spaceAfter=8
    )

    doc = SimpleDocTemplate(str(filepath), pagesize=letter,
                             topMargin=0.75 * inch, bottomMargin=0.75 * inch)
    story = []

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph("Spending Summary Report", title_style))
    date_range = f"{_fmt_date(summary['earliest'])} to {_fmt_date(summary['latest'])}"
    story.append(Paragraph(f"Generated {generated_at} · data range: {date_range}", subtitle_style))

    # --- Overview ---
    story.append(Paragraph("Overview", heading_style))
    review_pct = f"{summary['needs_review_rate'] * 100:.1f}%"
    overview_data = [
        ["Total receipts processed", str(summary["total_records"])],
        ["Flagged for review", f"{summary['needs_review_count']} ({review_pct})"],
    ]
    overview_table = Table(overview_data, colWidths=[3 * inch, 3 * inch])
    overview_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
    ]))
    story.append(overview_table)

    # --- Spend by currency ---
    story.append(Paragraph("Spend by currency", heading_style))
    if summary["by_currency"]:
        rows = [["Currency", "Total", "Receipts"]]
        for row in summary["by_currency"]:
            total = row["total"] if row["total"] is not None else 0
            rows.append([row["currency"], f"{total:,.2f}", str(row["n"])])
        t = Table(rows, colWidths=[2 * inch, 2 * inch, 2 * inch])
        t.setStyle(_table_style())
        story.append(t)
    else:
        story.append(Paragraph("No data yet.", styles["Normal"]))

    # --- Top vendors ---
    story.append(Paragraph("Top vendors by spend", heading_style))
    if summary["top_vendors"]:
        rows = [["Vendor", "Total", "Receipts"]]
        for row in summary["top_vendors"]:
            total = row["total"] if row["total"] is not None else 0
            rows.append([row["vendor"], f"{total:,.2f}", str(row["n"])])
        t = Table(rows, colWidths=[3 * inch, 1.5 * inch, 1.5 * inch])
        t.setStyle(_table_style())
        story.append(t)
    else:
        story.append(Paragraph("No data yet.", styles["Normal"]))

    story.append(Spacer(1, 20))
    story.append(Paragraph(
        "Generated automatically by the receipt-extractor reporting pipeline.",
        ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8, textColor=colors.grey),
    ))

    doc.build(story)


def _table_style() -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ])