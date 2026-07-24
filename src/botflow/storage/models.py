"""Pydantic data models for botflow entities."""

from __future__ import annotations

from datetime import datetime
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
    created_at: datetime = Field(default_factory=lambda: datetime.now())
    updated_at: datetime = Field(default_factory=lambda: datetime.now())


class Model(BaseModel):
    """LLM model configuration."""

    id: int = 0
    name: str  # model name passed to provider, e.g. "gpt-4o"
    provider_id: int
    display_name: str = ""
    max_retries: int = 3
    cooldown_seconds: int = 60
    cooldown_failure_threshold: int = 3
    extra_config: dict[str, Any] = Field(default_factory=dict)
    is_enabled: bool = True
    context_window: int = 0  # 0 means unknown/no truncation
    created_at: datetime = Field(default_factory=lambda: datetime.now())
    updated_at: datetime = Field(default_factory=lambda: datetime.now())


class ModelGroup(BaseModel):
    """Model group for weighted routing."""

    id: int = 0
    name: str
    description: str = ""
    is_enabled: bool = True
    fallback_group_id: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now())
    updated_at: datetime = Field(default_factory=lambda: datetime.now())


class GroupModel(BaseModel):
    """Association between a group and a model with weight."""

    id: int = 0
    group_id: int
    model_id: int
    weight: float = 1.0
    is_enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now())


class GroupModelWithDetails(BaseModel):
    """GroupModel joined with Model and Provider details."""

    id: int
    group_id: int
    model_id: int
    weight: float
    is_enabled: bool
    model_name: str
    display_name: str
    provider_id: int
    provider_name: str
    provider_type: str
    max_retries: int
    cooldown_seconds: int
    cooldown_failure_threshold: int
    context_window: int = 0


class CallLog(BaseModel):
    """Audit log for an LLM API call."""

    id: int = 0
    group_id: Optional[int] = None
    model_id: Optional[int] = None
    provider_id: Optional[int] = None
    request_body: Optional[str] = None
    response_body: Optional[str] = None
    status: str = ""  # success, error, timeout, cancelled
    duration_ms: Optional[int] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cache_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    tool_calls: Optional[str] = None  # JSON string of tool calls
    cost: Optional[float] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now())


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
