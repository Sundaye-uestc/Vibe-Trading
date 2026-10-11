"""Safe rich-text rendering for scheduled email reports.

Scheduled reports are authored as Markdown.  This module deliberately keeps
the renderer separate from the Email channel so the same sanitisation rules
are used by HTML mail and PDF attachments.  WeasyPrint is preferred when its
native libraries are available; a small reportlab renderer keeps PDF delivery
working on hosts where Pango/Cairo cannot be installed.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from xml.sax.saxutils import escape

import nh3
from mistune import create_markdown

logger = logging.getLogger(__name__)

_WEASYPRINT_HTML = None
_MARKDOWN = create_markdown(escape=True, plugins=["table", "strikethrough", "url"])
_ALLOWED_TAGS = {
    "p", "a", "strong", "em", "del", "code", "pre", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "br", "table", "thead", "tbody", "tr", "th", "td",
}
_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href"}, "code": {"class"}, "ol": {"start"},
}
_EMAIL_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
       color: #1f2937; line-height: 1.55; max-width: 860px; margin: 0 auto; padding: 24px; }
h1, h2, h3 { color: #111827; line-height: 1.25; }
h1 { font-size: 26px; } h2 { font-size: 21px; margin-top: 28px; }
h3 { font-size: 17px; margin-top: 22px; }
a { color: #2563eb; }
table { border-collapse: collapse; width: 100%; margin: 16px 0; }
th, td { border: 1px solid #d1d5db; padding: 8px 10px; text-align: left; }
th { background: #f3f4f6; }
blockquote { border-left: 4px solid #d1d5db; margin-left: 0; padding-left: 14px; color: #4b5563; }
code { background: #f3f4f6; padding: 2px 4px; border-radius: 4px; }
pre { background: #f3f4f6; padding: 12px; overflow-wrap: anywhere; white-space: pre-wrap; }
"""


def render_markdown_fragment(text: str) -> str:
    """Render Markdown as a conservative, sanitised HTML fragment."""
    return nh3.clean(
        _MARKDOWN(text or ""),
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes={"https", "http", "mailto"},
        strip_comments=True,
        link_rel="noopener noreferrer",
    ).strip()


def render_email_html(text: str) -> str:
    """Render Markdown as a standalone HTML document suitable for email/PDF."""
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<style>{_EMAIL_CSS}</style></head><body>"
        f"{render_markdown_fragment(text)}</body></html>"
    )


def _render_pdf_reportlab(text: str) -> bytes:
    """Render a simple, paginated PDF without native rendering libraries."""
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    body = ParagraphStyle("vibe-report-body", parent=styles["BodyText"], fontSize=10,
                          leading=14, spaceAfter=5, wordWrap="CJK")
    heading = {level: ParagraphStyle(
        f"vibe-report-h{level}", parent=styles[f"Heading{min(level, 3)}"],
        fontSize=max(12, 22 - level * 2), leading=max(15, 26 - level * 2),
        spaceBefore=8, spaceAfter=5, keepWithNext=True,
    ) for level in range(1, 7)}
    code = ParagraphStyle("vibe-report-code", parent=body, backColor=colors.HexColor("#f3f4f6"),
                          leftIndent=8, rightIndent=8, fontName="Courier")

    def inline(value: str) -> str:
        value = escape(value)
        value = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", value)
        value = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", value)
        value = re.sub(r"__([^_]+)__", r"<b>\1</b>", value)
        value = re.sub(r"`([^`]+)`", r"<font name='Courier'>\1</font>", value)
        return value

    story = []
    in_code = False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            story.append(Paragraph(escape(line).replace(" ", "&#160;"), code))
            continue
        if not line.strip():
            story.append(Spacer(1, 2 * mm))
            continue
        match = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if match:
            story.append(Paragraph(inline(match.group(2)), heading[len(match.group(1))]))
            continue
        line = re.sub(r"^\s*[-*+]\s+", "• ", line.strip())
        line = re.sub(r"^\s*>\s?", "", line)
        story.append(Paragraph(inline(line), body))
    if not story:
        story.append(Paragraph("&#160;", body))
    target = BytesIO()
    SimpleDocTemplate(target, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                      topMargin=16 * mm, bottomMargin=16 * mm).build(story)
    return target.getvalue()


def render_email_pdf(text: str) -> bytes:
    """Render a real PDF, preferring WeasyPrint and falling back to reportlab."""
    global _WEASYPRINT_HTML
    if _WEASYPRINT_HTML is None:
        try:
            from weasyprint import HTML
            _WEASYPRINT_HTML = HTML
        except (ImportError, OSError):
            _WEASYPRINT_HTML = False
    if _WEASYPRINT_HTML is not False:
        try:
            return _WEASYPRINT_HTML(string=render_email_html(text)).write_pdf()
        except Exception:
            logger.exception("WeasyPrint failed; falling back to reportlab")
    return _render_pdf_reportlab(text)
