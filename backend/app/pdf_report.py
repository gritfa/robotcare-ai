from __future__ import annotations

import hashlib
import hmac
from html import escape
import os
from pathlib import Path
from uuid import uuid4

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.pdfmetrics import getRegisteredFontNames, registerFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import ServiceReport
from .resource_locks import resource_lock

PDF_FONT_NAME = "RobotCareCJK"


def register_pdf_font() -> str:
    if PDF_FONT_NAME in getRegisteredFontNames():
        return PDF_FONT_NAME

    configured = os.getenv("ROBOTCARE_PDF_FONT_PATH")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            registerFont(TTFont(PDF_FONT_NAME, str(candidate), subfontIndex=0))
            return PDF_FONT_NAME

    fallback = "STSong-Light"
    if fallback not in getRegisteredFontNames():
        registerFont(UnicodeCIDFont(fallback))
    return fallback


def report_pdf_path(report_dir: Path, report: ServiceReport, secret: str) -> Path:
    """Return a deterministic, non-user-controlled and unguessable PDF path."""
    digest = hmac.new(
        secret.encode("utf-8"),
        f"service-report:{report.id}:{report.report_number}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    root = report_dir.resolve()
    target = (root / f"{digest}.pdf").resolve()
    if target.parent != root:
        raise RuntimeError("Invalid PDF report path")
    return target


def is_complete_pdf(target: Path) -> bool:
    if not target.is_file() or target.stat().st_size < 10:
        return False
    with target.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            return False
        stream.seek(max(0, target.stat().st_size - 2048))
        return b"%%EOF" in stream.read()


def ensure_report_pdf(
    report_dir: Path,
    report: ServiceReport,
    secret: str,
) -> Path:
    """Create a complete PDF once per process; replacement is atomic cross-process."""

    target = report_pdf_path(report_dir, report, secret)
    with resource_lock("report-pdf", str(target)):
        if not is_complete_pdf(target):
            render_report_pdf(report, target)
        if not is_complete_pdf(target):
            target.unlink(missing_ok=True)
            raise RuntimeError("Generated PDF failed the completeness check")
    return target


def _page_footer(canvas, document) -> None:
    canvas.saveState()
    canvas.setFont(document.pdf_font_name, 8)
    canvas.setFillColor(colors.HexColor("#667085"))
    canvas.drawCentredString(A4[0] / 2, 12 * mm, f"第 {document.page} 页")
    canvas.restoreState()


def render_report_pdf(report: ServiceReport, target: Path) -> None:
    """Render the stored text report as a Chinese-readable PDF.

    Uploaded images are intentionally not decoded or diagnosed. Their original
    filenames are already included in the stored report content.
    """
    font_name = register_pdf_font()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{uuid4().hex}.tmp")

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ChineseTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=18,
        leading=24,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#1D2939"),
        spaceAfter=10 * mm,
    )
    body_style = ParagraphStyle(
        "ChineseBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10.5,
        leading=17,
        textColor=colors.HexColor("#344054"),
        wordWrap="CJK",
        spaceAfter=2.5 * mm,
    )
    label_style = ParagraphStyle(
        "ChineseLabel",
        parent=body_style,
        textColor=colors.HexColor("#101828"),
    )

    lines = [line.strip() for line in report.content.splitlines() if line.strip()]
    title = lines[0] if lines else "RobotCare AI 第三方售后诊断报告"
    story = [Paragraph(escape(title), title_style)]
    metadata = Table(
        [
            [Paragraph("报告编号", label_style), Paragraph(escape(report.report_number), body_style)],
            [
                Paragraph("生成时间", label_style),
                Paragraph(escape(report.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")), body_style),
            ],
        ],
        colWidths=[30 * mm, 125 * mm],
    )
    metadata.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2F4F7")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D5DD")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#EAECF0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend([metadata, Spacer(1, 8 * mm)])
    story.extend(Paragraph(escape(line), body_style) for line in lines[1:])

    document = SimpleDocTemplate(
        str(temporary),
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=22 * mm,
        title=title,
        author="RobotCare AI",
        subject="第三方售后诊断报告",
    )
    document.pdf_font_name = font_name
    try:
        document.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
