"""The installed-skills HTTP surface the Web UI reads.

``GET /skills`` was registered twice — once in ``system_routes.py`` and once in
``skills_routes.py`` — and FastAPI serves whichever registered first. The
shadowed handler never ran, so its extra fields silently never reached the UI.
Nothing compared the two, so the regression was invisible until the UI was
opened.
"""

from __future__ import annotations

import collections
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def test_no_two_handlers_serve_the_same_method_and_path() -> None:
    """A duplicate registration is a silent shadow, never an override."""
    seen = collections.Counter(
        (method, route.path)
        for route in api_server.app.routes
        for method in (getattr(route, "methods", None) or [])
    )

    duplicates = {key: count for key, count in seen.items() if count > 1}

    assert not duplicates, f"duplicate route registrations: {duplicates}"


def test_skill_list_exposes_the_category_the_ui_groups_by(client: TestClient) -> None:
    response = client.get("/skills")

    assert response.status_code == 200
    skills = response.json()
    assert skills, "no skills returned"

    missing = [skill["name"] for skill in skills if not skill.get("category")]
    assert not missing, f"skills without a category: {missing[:5]}"


def test_skill_detail_returns_the_body_and_category(client: TestClient) -> None:
    name = client.get("/skills").json()[0]["name"]

    response = client.get(f"/skills/{name}")

    assert response.status_code == 200
    detail = response.json()
    assert detail["name"] == name
    assert detail["category"]
    assert detail["body"]


def test_unknown_skill_detail_is_a_404(client: TestClient) -> None:
    response = client.get("/skills/definitely-not-a-real-skill")

    assert response.status_code == 404


def test_the_bundled_skill_count_matches_the_documented_one() -> None:
    """Guards the README/``SKILL.md`` counts this fork hand-maintains."""
    from src.agent.skills import SkillsLoader

    repo_root = Path(__file__).resolve().parents[2]
    manifest = (repo_root / "agent" / "SKILL.md").read_text(encoding="utf-8")
    bundled = len(SkillsLoader().skills)

    assert f"### Finance Skills ({bundled})" in manifest
    assert f"| `list_skills` | List all {bundled} finance skills | None |" in manifest
