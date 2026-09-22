"""Tests for the application logging setup."""

from __future__ import annotations

import logging
import sys

from src.logging_setup import configure_logging, DEFAULT_LOG_LEVEL, LOG_LEVEL_ENV


class TestConfigureLogging:
    """The handler is installed once, and never over someone else's config."""

    @staticmethod
    def _without_root_handlers():
        """Detach the root handlers, returning them for restoration."""
        root = logging.getLogger()
        saved = list(root.handlers)
        for handler in saved:
            root.removeHandler(handler)
        return root, saved

    def test_installs_a_stdout_handler_when_unconfigured(self) -> None:
        root, saved = self._without_root_handlers()
        try:
            assert configure_logging() is True

            assert len(root.handlers) == 1
            handler = root.handlers[0]
            assert isinstance(handler, logging.StreamHandler)
            assert handler.stream is sys.stdout
            assert root.level == logging.INFO
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
            for handler in saved:
                root.addHandler(handler)

    def test_leaves_an_existing_configuration_alone(self) -> None:
        root = logging.getLogger()
        existing = logging.NullHandler()
        root.addHandler(existing)
        before = list(root.handlers)
        try:
            assert configure_logging() is False
            # Nothing added, nothing removed — a runner that already configured
            # logging keeps its handlers.
            assert root.handlers == before
            assert existing in root.handlers
        finally:
            root.removeHandler(existing)

    def test_reads_the_level_from_the_environment(self, monkeypatch) -> None:
        root, saved = self._without_root_handlers()
        monkeypatch.setenv(LOG_LEVEL_ENV, "debug")
        try:
            assert configure_logging() is True
            assert root.level == logging.DEBUG
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
            for handler in saved:
                root.addHandler(handler)

    def test_falls_back_to_info_for_an_invalid_level(self, monkeypatch) -> None:
        root, saved = self._without_root_handlers()
        monkeypatch.setenv(LOG_LEVEL_ENV, "chatty")
        try:
            assert configure_logging() is True
            assert root.level == logging.INFO
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
            for handler in saved:
                root.addHandler(handler)

    def test_default_level_is_info(self) -> None:
        assert DEFAULT_LOG_LEVEL == "INFO"
