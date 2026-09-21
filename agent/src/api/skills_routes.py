"""Installed-skills HTTP routes.

Mounted by ``agent/api_server.py`` via ``register_skills_routes(app)``.

Read-only: the bundled and user skills discovered by ``SkillsLoader`` are
exposed so the Web UI can show a single skill's full body.

The *list* route deliberately lives in ``system_routes.py`` alongside the other
capability inventories rather than here. Registering a second ``GET /skills``
from this module silently lost to the earlier registration -- FastAPI resolves
the first match, so the richer payload never reached a client. Add to the
existing handler instead of adding another one.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException

from src.api.security import require_auth


def register_skills_routes(app: FastAPI) -> None:
    """Mount the installed-skills detail route onto ``app``."""

    @app.get("/skills/{name}", dependencies=[Depends(require_auth)])
    def get_skill(name: str) -> dict[str, object]:
        """Return a single skill's metadata and full SKILL.md body."""
        from src.agent.skills import SkillsLoader

        loader = SkillsLoader()
        content = loader.get_content(name)
        if content.startswith("Error:"):
            raise HTTPException(status_code=404, detail=content)

        skill = next((item for item in loader.skills if item.name == name), None)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
        return {
            "name": skill.name,
            "description": skill.description,
            "category": skill.category,
            "body": skill.body,
            "metadata": skill.metadata,
        }
