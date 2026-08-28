"""Authentication dependencies for botflow LLM Proxy (LLM key + admin key)."""

from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBearer

from botflow.config import get_config
from botflow.storage.db import Database, get_db
from botflow.storage.models import ApiKey

# Security scheme reused by Swagger UI for both LLM and admin auth.
security = HTTPBearer(auto_error=False)


def _extract_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        # Tolerate raw key without "Bearer " prefix.
        return authorization.strip() or None
    return token


async def resolve_api_key(db: Database, token: str) -> ApiKey | None:
    """Resolve a presented key to an ApiKey row.

    Resolution order:
      1. If any client API keys are registered in the DB, the token must match
         one of them (by sha256 hash) and be enabled.
      2. Otherwise fall back to the legacy single BOTFLOW_LLM_KEY from config,
         preserving backward compatibility for single-key deployments.
    """
    configured = await db.list_api_keys()
    if configured:
        key_hash = db.hash_key(token)
        for row in configured:
            if row.is_enabled and row.key_hash == key_hash:
                return row
        return None
    # Legacy single-key mode.
    legacy = get_config().llm_key
    if legacy and secrets.compare_digest(token, legacy):
        # Synthesize a pseudo ApiKey so callers always get an id (0 = legacy).
        return ApiKey(id=0, key_hash=db.hash_key(token), label="legacy", is_enabled=True)
    return None


async def verify_llm_key(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    credentials: Optional[HTTPAuthorizationCredentials] = None,
    db: Database = None,  # injected by FastAPI (deprecated positional fallback below)
) -> ApiKey:
    """LLM Proxy auth: any valid client API key (or legacy single key)."""
    if db is None:
        db = get_db()
    token = _extract_token(authorization)
    if credentials and credentials.credentials:
        token = credentials.credentials
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide Authorization: Bearer <key>.",
        )
    api_key = await resolve_api_key(db, token)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or disabled API key.",
        )
    # Stash for downstream logging / per-key isolation.
    request.state.api_key_id = api_key.id
    request.state.api_key = api_key
    return api_key


async def verify_admin_key(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    credentials: Optional[HTTPAuthorizationCredentials] = None,
) -> None:
    """Admin REST API auth: must match BOTFLOW_ADMIN_KEY."""
    admin_key = get_config().admin_key
    if not admin_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server admin key is not configured (BOTFLOW_ADMIN_KEY).",
        )
    token = _extract_token(authorization)
    if credentials and credentials.credentials:
        token = credentials.credentials
    if not token or not secrets.compare_digest(token, admin_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin key.",
        )
    request.state.is_admin = True
