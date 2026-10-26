from __future__ import annotations

import asyncio
from io import BytesIO

import pytest

from src.channels.bus.events import OutboundMessage
from src.channels.bus.queue import MessageBus
from src.channels.email import EmailChannel
from src.channels.rich_text import render_email_html, render_email_pdf


def _channel(*, pdf_password: str = "") -> EmailChannel:
    return EmailChannel(
        {
            "consent_granted": True,
            "smtp_host": "smtp.example.test",
            "smtp_username": "bot@example.test",
            "smtp_password": "secret",
            "pdf_password": pdf_password,
            "from_address": "bot@example.test",
        },
        MessageBus(),
    )


def test_renderer_sanitises_html_and_links() -> None:
    rendered = render_email_html(
        "# Report\n\n**Return:** 1.2%\n\n<script>alert(1)</script>\n"
        "[unsafe](javascript:alert(1))"
    )
    assert "<h1>Report</h1>" in rendered
    assert "<script>" not in rendered
    assert "javascript:" not in rendered


def test_html_delivery_keeps_plain_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _channel()
    sent = []
    monkeypatch.setattr(channel, "_smtp_send", lambda message: sent.append(message))
    asyncio.run(channel.send(OutboundMessage(
        channel="email", chat_id="reader@example.test", content="# Report",
        metadata={"delivery_format": "html"},
    )))
    message = sent[0]
    assert message.get_body(preferencelist=("plain",)).get_content().startswith("# Report")
    assert "<h1>Report</h1>" in message.get_body(preferencelist=("html",)).get_content()


def test_pdf_delivery_attaches_pdf_and_hides_content(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _channel()
    sent = []
    monkeypatch.setattr(channel, "_smtp_send", lambda message: sent.append(message))
    monkeypatch.setattr("src.channels.email.render_email_pdf", lambda text: render_email_pdf(text))
    asyncio.run(channel.send(OutboundMessage(
        channel="email", chat_id="reader@example.test", content="# Secret report",
        metadata={"delivery_format": "pdf"},
    )))
    message = sent[0]
    assert "Secret report" not in message.get_body(preferencelist=("plain",)).get_content()
    pdf = next(part for part in message.iter_attachments() if part.get_content_type() == "application/pdf")
    assert pdf.get_payload(decode=True).startswith(b"%PDF")


def test_protected_pdf_uses_configured_password(monkeypatch: pytest.MonkeyPatch) -> None:
    from pypdf import PdfReader
    from reportlab.pdfgen.canvas import Canvas

    raw = BytesIO()
    canvas = Canvas(raw)
    canvas.drawString(72, 720, "Protected report")
    canvas.save()
    channel = _channel(pdf_password="secret-password")
    sent = []
    monkeypatch.setattr(channel, "_smtp_send", lambda message: sent.append(message))
    monkeypatch.setattr("src.channels.email.render_email_pdf", lambda _text: raw.getvalue())
    asyncio.run(channel.send(OutboundMessage(
        channel="email", chat_id="reader@example.test", content="Protected report",
        metadata={"delivery_format": "pdf", "protect_pdf": True},
    )))
    pdf = next(part for part in sent[0].iter_attachments() if part.get_content_type() == "application/pdf")
    reader = PdfReader(BytesIO(pdf.get_payload(decode=True)))
    assert reader.is_encrypted
    assert reader.decrypt("wrong") == 0
    assert reader.decrypt("secret-password") != 0


def test_protected_pdf_without_password_fails_closed() -> None:
    channel = _channel()
    with pytest.raises(RuntimeError, match="no PDF password"):
        asyncio.run(channel.send(OutboundMessage(
            channel="email", chat_id="reader@example.test", content="Secret",
            metadata={"delivery_format": "pdf", "protect_pdf": True},
        )))


@pytest.mark.parametrize("engine_unavailable", [True, False])
def test_pdf_fallback_embeds_readable_chinese_in_all_text_styles(monkeypatch, engine_unavailable):
    from pypdf import PdfReader
    from reportlab.pdfbase import pdfmetrics
    from src.channels import rich_text

    class BrokenEngine:
        def __init__(self, **kwargs):
            pass

        def write_pdf(self):
            raise OSError("native rendering library unavailable")

    monkeypatch.setattr(rich_text, "_WEASYPRINT_HTML", False if engine_unavailable else BrokenEngine)
    text = "# 中文研究报告\n\n**南向资金**与繁體數據摘要。\n\n`证券代码000001`\n\n```\n回测收益率=12.5%\n```"
    pdf = render_email_pdf(text)
    reader = PdfReader(BytesIO(pdf))
    extracted = "\n".join(page.extract_text() for page in reader.pages)
    for expected in ["中文研究报告", "南向资金", "繁體數據摘要", "证券代码000001", "回测收益率=12.5%"]:
        assert expected in extracted
    # Unicode extraction alone can pass even when the actual glyph is missing.
    face = pdfmetrics.getFont(rich_text._PDF_FONT_NAME).face
    assert all(face.charToGlyph.get(ord(char), 0) != 0 for char in text if ord(char) > 127)
    descriptors = [
        font.get_object().get("/FontDescriptor")
        for page in reader.pages for font in page["/Resources"]["/Font"].values()
    ]
    embedded = [item.get_object() for item in descriptors if item]
    assert any("/FontFile2" in item and "VibeReportCJK" in str(item["/FontName"]) for item in embedded)


def test_html_email_does_not_reference_packaged_font_path():
    html = render_email_html("# 中文报告")
    assert "file:" not in html
    assert ".ttf" not in html
