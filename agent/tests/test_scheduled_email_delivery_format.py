from __future__ import annotations

from src.scheduled_research.models import JobStatus, ScheduledResearchJob


def test_scheduled_job_round_trips_email_format() -> None:
    job = ScheduledResearchJob(
        id="email-html",
        prompt="prepare report",
        schedule="60000",
        next_run_at=1,
        created_at=1,
        status=JobStatus.PENDING,
        delivery_channel="email",
        delivery_target="reader@example.test",
        delivery_format="html",
    )
    encoded = job.to_dict()
    assert encoded["delivery_format"] == "html"
    assert ScheduledResearchJob.from_dict(encoded).delivery_format == "html"


def test_protected_pdf_round_trip_never_stores_password() -> None:
    job = ScheduledResearchJob(
        id="email-pdf",
        prompt="prepare report",
        schedule="60000",
        next_run_at=1,
        created_at=1,
        status=JobStatus.PENDING,
        delivery_channel="email",
        delivery_target="reader@example.test",
        delivery_format="pdf",
        protect_pdf=True,
    )
    encoded = job.to_dict()
    assert encoded["protect_pdf"] is True
    assert "pdf_password" not in repr(encoded)


def test_job_rejects_secret_in_config() -> None:
    job = ScheduledResearchJob(
        id="unsafe",
        prompt="prepare report",
        schedule="60000",
        next_run_at=1,
        created_at=1,
        config={"nested": [{"pdf_password": "secret"}]},
    )
    try:
        job.to_dict()
    except ValueError as exc:
        assert "pdf_password" in str(exc)
    else:
        raise AssertionError("secret config must be rejected")
