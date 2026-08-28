"""Tests for logging helpers (100% coverage)."""

from __future__ import annotations

from pathlib import Path

from botflow.common.logger import setup_logging, get_logger


def test_setup_logging(tmp_path: Path):
    # Must not raise; creates log dir and adds handlers.
    setup_logging(tmp_path, level="DEBUG")
    setup_logging(tmp_path, level="info")


def test_get_logger():
    log = get_logger("proxy")
    assert log is not None
    # Bind returns a usable logger; logging must not raise.
    log.bind(user="x").info("hello {}", "world")
    log.warning("warn msg")
    log.error("err msg")
    log.debug("debug msg")
    log.opt().log("INFO", "opt log")
    log.bind(request_id="r").debug("bound debug")
