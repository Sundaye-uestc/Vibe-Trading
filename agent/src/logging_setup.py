"""Application logging setup.

Nothing ever configured a root handler, so every ``logger.warning(...)`` fell
through to Python's ``lastResort`` handler: a bare stderr writer with no
formatter and no level control. That is harmless by itself, but it also made
stderr the only place a warning could appear — a supervisor that reads stdout
(``start.py``) saw none of them, and a warning-heavy run could fill the stderr
pipe.

This installs a real stdout handler, but only when nothing else already has: a
test runner, a host application, or an explicit ``dictConfig`` must keep its own
configuration.
"""

from __future__ import annotations

import logging
import os
import sys

LOG_LEVEL_ENV = "VIBE_TRADING_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def _resolve_level() -> int:
    """Resolve the root log level from ``VIBE_TRADING_LOG_LEVEL``.

    Reads through the project accessor so the value can also come from
    ``~/.vibe-trading/.env``. The import is deferred: this module is loaded very
    early by ``api_server``, before the config package is necessarily safe to
    import.

    Returns:
        The configured level, or :data:`logging.INFO` when unset or invalid.
    """
    try:
        from src.config.accessor import get_env_value

        raw = get_env_value(LOG_LEVEL_ENV, "").strip().upper()
    except Exception:  # noqa: BLE001 - fall back to the process environment
        raw = os.environ.get(LOG_LEVEL_ENV, "").strip().upper()
    if not raw:
        return logging.INFO
    level = logging.getLevelName(raw)
    return level if isinstance(level, int) else logging.INFO


def configure_logging() -> bool:
    """Install a stdout handler on the root logger.

    Returns:
        True when this call installed the handler; False when the root logger
        was already configured, in which case nothing is changed.
    """
    root = logging.getLogger()
    if root.handlers:
        return False
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    root.setLevel(_resolve_level())
    return True
