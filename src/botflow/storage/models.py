"""Pydantic data models for botflow entities."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field


class Provider(BaseModel):
    """LLM provider configuration."""

    id: int = 0
    name: str
    provider_type: str  # openai, azure, anthropic, google, ollama, vllm
    api_key: str = ""
    base_url: str = ""
    extra_config: dict[str, Any] = Field(default_factory=dict)
    is_enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Model(BaseModel):
    """LLM model configuration."""

    id: int = 0
    name: str  # model name passed to provider, e.g. "gpt-4o"
    provider_id: int
    display_name: str = ""
    api_format: str = ""  # override provider's SDK class per-model (openai/deepseek/anthropic/google/azure/ollama/vllm); empty = use provider_type
    max_retries: int = 3
    cooldown_seconds: int = 60
    cooldown_failure_threshold: int = 3
    extra_config: dict[str, Any] = Field(default_factory=dict)
    is_enabled: bool = True
    context_window: int = 0  # 0 means unknown/no truncation
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ModelGroup(BaseModel):
    """Model group for weighted routing."""

    id: int = 0
    name: str
    description: str = ""
    is_enabled: bool = True
    fallback_group_id: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GroupModel(BaseModel):
    """Association between a group and a model with weight."""

    id: int = 0
    group_id: int
    model_id: int
    weight: float = 1.0
    is_enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GroupModelWithDetails(BaseModel):
    """GroupModel joined with Model and Provider details."""

    id: int
    group_id: int
    model_id: int
    weight: float
    is_enabled: bool
    model_name: str
    display_name: str
    api_format: str = ""  # per-model SDK override; empty = use provider_type
    provider_id: int
    provider_name: str
    provider_type: str
    max_retries: int
    cooldown_seconds: int
    cooldown_failure_threshold: int
    context_window: int = 0
    proxy: str = ""  # per-model proxy from model.extra_config["proxy"]
    extra_config: dict = {}  # full model.extra_config dict


class CallLog(BaseModel):
    """Audit log for an LLM API call."""

    id: int = 0
    api_key_id: Optional[int] = None  # FK to api_keys; NULL when key system unused
    group_id: Optional[int] = None
    model_id: Optional[int] = None
    provider_id: Optional[int] = None
    request_body: Optional[str] = None
    response_body: Optional[str] = None
    status: str = ""  # success, error, timeout, cooldown, cancelled
    error_type: Optional[str] = None  # e.g. ProviderError / TimeoutError / AllModelsCooldownError
    error_message: Optional[str] = None
    traceback: Optional[str] = None  # limited stack trace on failure
    request_id: Optional[str] = None  # correlates retries/streams of one request
    duration_ms: Optional[int] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cache_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    tool_calls: Optional[str] = None  # JSON string of tool calls
    cost: Optional[float] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApiKey(BaseModel):
    """Client API key. Calls authenticated by these keys are logged separately."""

    id: int = 0
    key_hash: str  # sha256 of the plaintext key (raw key is never stored)
    label: str = ""
    is_enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DailySummary(BaseModel):
    """Per-day LLM-generated wiki summary of all conversations."""

    id: int = 0
    day: str  # YYYY-MM-DD
    summary_md: str = ""  # LLM wiki-formatted summary
    stats_json: str = "{}"  # aggregated usage/error stats
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RawSession(BaseModel):
    """Compressed raw conversation sessions for a day (gzip blob)."""

    id: int = 0
    day: str  # YYYY-MM-DD
    blob: bytes = b""  # gzip-compressed JSON of all call_logs for the day
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ModelStats(BaseModel):
    """Aggregated model statistics."""

    model_id: int
    model_name: str
    total_calls: int = 0
    success_calls: int = 0
    error_calls: int = 0
    avg_duration_ms: Optional[float] = None
    min_duration_ms: Optional[float] = None
    max_duration_ms: Optional[float] = None
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cache_tokens: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0


class GroupStats(BaseModel):
    """Aggregated group statistics."""

    group_id: int
    group_name: str
    total_calls: int = 0
    success_calls: int = 0
    error_calls: int = 0
    avg_duration_ms: Optional[float] = None
    total_cost: float = 0.0
