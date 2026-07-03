"""Authentication middleware for HTTP and MCP services."""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger


def verify_llm_key(request: Request, valid_key: str) -> Optional[JSONResponse]:
    """Verify the LLM API key from the Authorization header.

    Args:
        request: FastAPI request object.
        valid_key: The expected valid key.

    Returns:
        JSONResponse with 401 if invalid, None if valid.
    """
    if not valid_key:
        return None  # No key configured, skip auth

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"error": "Missing or invalid Authorization header"})

    provided_key = auth_header.removeprefix("Bearer ").strip()
    # 使用常量时间比较防止时序攻击
    if not hmac.compare_digest(provided_key, valid_key):
        logger.warning("Invalid LLM key attempt from {}", request.client.host if request.client else "unknown")
        return JSONResponse(status_code=401, content={"error": "Invalid API key"})

    return None


def verify_mcp_key(provided_key: str, valid_key: str) -> Optional[Exception]:
    """Verify the MCP key.

    Args:
        provided_key: The key provided by the client.
        valid_key: The expected valid key.

    Returns:
        Exception if invalid, None if valid.
    """
    if not valid_key:
        return None

    # 使用常量时间比较防止时序攻击
    if not hmac.compare_digest(provided_key, valid_key):
        logger.warning("Invalid MCP key attempt")
        return HTTPException(status_code=401, detail="Invalid MCP key")

    return None
