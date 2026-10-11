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
