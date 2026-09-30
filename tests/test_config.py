"""Tests for global configuration management (100% coverage)."""

from __future__ import annotations

from pathlib import Path

from botflow.config import BotflowSettings, load_config, set_config, get_config


def test_defaults(tmp_path, monkeypatch):
    # .env is gitignored and may legitimately exist in the repo root (local
    # secrets). Run from an empty dir so this test asserts the shipped
    # defaults rather than whatever the developer's .env happens to hold.
    monkeypatch.chdir(tmp_path)
    s = BotflowSettings()
    assert s.host == "0.0.0.0"
    assert s.port == 8080
    assert s.call_log_detail_days == 1
    assert s.raw_session_retention_days == 7
    assert s.daily_summary_hour == 0
    assert s.summary_group == ""
    assert s.llm_key == ""
    assert s.admin_key == ""
    assert s.rate_limit_max_requests == 300
    assert s.rate_limit_window_seconds == 60
    assert s.rate_limit_max_keys == 10000
    assert s.dedup_ttl_seconds == 300


def test_rate_limit_and_dedup_env_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOTFLOW_RATE_LIMIT_MAX_REQUESTS", "42")
    monkeypatch.setenv("BOTFLOW_RATE_LIMIT_WINDOW_SECONDS", "7")
    monkeypatch.setenv("BOTFLOW_RATE_LIMIT_MAX_KEYS", "123")
    monkeypatch.setenv("BOTFLOW_DEDUP_TTL_SECONDS", "9")
    s = BotflowSettings()
    assert s.rate_limit_max_requests == 42
    assert s.rate_limit_window_seconds == 7
    assert s.rate_limit_max_keys == 123
    assert s.dedup_ttl_seconds == 9


def test_load_config_no_workspace():
    s = load_config()
    assert isinstance(s, BotflowSettings)


def test_load_config_with_workspace_dotenv(tmp_path: Path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("BOTFLOW_HOST=127.0.0.1\nBOTFLOW_PORT=9000\n")
    s = load_config(tmp_path)
    assert s.host == "127.0.0.1"
    assert s.port == 9000


def test_load_config_without_dotenv(tmp_path: Path):
    # No .env exists in this workspace -> falls back to defaults.
    s = load_config(tmp_path)
    assert s.host == "0.0.0.0"


def test_set_and_get_config():
    s = BotflowSettings(host="1.2.3.4", port=7777)
    set_config(s)
    assert get_config() is s
    assert get_config().host == "1.2.3.4"


def test_get_config_lazy_load():
    # Unset module state so get_config() loads a default on first call.
    import botflow.config as cfg
    saved = cfg._config
    cfg._config = None
    try:
        s = get_config()
        assert isinstance(s, BotflowSettings)
    finally:
        cfg._config = saved
