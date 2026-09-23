"""Authentication dependencies for botflow LLM Proxy (LLM key + admin key)."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from botflow.config import get_config
from botflow.storage.db import Database, get_db
from botflow.storage.models import ApiKey

# Security scheme reused by Swagger UI for both LLM and admin auth.
#
# It MUST be consumed through ``Depends(security)`` / ``Security(security)``.
# Writing ``credentials: HTTPAuthorizationCredentials = None`` instead makes
# FastAPI classify the plain default as a *request body* field (any Pydantic
# model is a body param), which silently swallows the JSON body of every write
# endpoint: a lone body param binds the whole body, so PATCHes either 422'd
# ("body.credentials / body.scheme missing") or returned 200 while ignoring the
# payload entirely. See tests/test_admin_api.py::TestWriteEndpointsUseJsonBody.
security = HTTPBearer(auto_error=False)


def _extract_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        # Tolerate raw key without "Bearer " prefix.
        return authorization.strip() or None
    return token


# PBKDF2 迭代数：OWASP 对 pbkdf2-sha256 的推荐下限。迭代数写进存档字符串，
# 将来调大不影响旧密码校验；配合端点的密码长度上限（≤128）把未鉴权端点
# 单次请求的 CPU 开销钉死（P1-6：超长密码在模型层就 422，进不到这里）。
PBKDF2_ITERATIONS = 600_000

# 会话 7 天过期。过期行由 login 时 cleanup_config_by_prefix("admin_sess:%", 本常量)
# 按 updated_at 清理 —— TTL 和清理阈值必须是同一个值，否则永远清不干净。
SESSION_TTL_SECONDS = 7 * 24 * 3600


def hash_password(password: str) -> str:
    """存档格式 ``pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>``。

    salt 随机 → 同一密码两次存档字符串不同，比对只能靠 verify_password，
    不能靠字符串相等。
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码与存档。存档损坏（缺段 / 迭代数非数字 / hex 非法）视为不匹配而非抛错。"""
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    # 比较走 bytes + compare_digest：定长比较不因长度差泄漏，也不吃非 ASCII TypeError。
    return secrets.compare_digest(dk, expected)


async def resolve_api_key(db: Database, token: str) -> ApiKey | None:
    """Resolve a presented key to an ApiKey row.

    Resolution order:
      1. If any client API keys are registered in the DB, the token must match
         one of them (by sha256 hash) and be enabled.
      2. Otherwise fall back to the legacy single key stored in the DB config
         table (key="llm_key"), preserving backward compatibility for deployments
         that used ``botflow set llm-key`` instead of the multi-key apikey commands.
    """
    configured = await db.list_api_keys()
    if configured:
        key_hash = db.hash_key(token)
        for row in configured:
            if row.is_enabled and row.key_hash == key_hash:
                return row
        return None
    # Legacy single-key mode — read from DB config table (set via ``botflow set llm-key``).
    legacy = await db.get_config("llm_key")
    if legacy and secrets.compare_digest(token, legacy):
        return ApiKey(id=0, key_hash=db.hash_key(token), label="legacy", is_enabled=True)
    return None


async def verify_llm_key(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Database = None,  # injected by FastAPI (deprecated positional fallback below)
) -> ApiKey:
    """LLM Proxy auth: any valid client API key (or legacy single key)."""
    if db is None:
        db = get_db()
    token = _extract_token(authorization)
    # ``credentials`` is filled by FastAPI from the Authorization header; the
    # isinstance guard keeps direct (unit-test) invocation working, where the
    # default is the Depends() marker rather than a parsed credentials object.
    if isinstance(credentials, HTTPAuthorizationCredentials) and credentials.credentials:
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


def session_config_key(token: str) -> str:
    """会话在 config 表里的 key。存 token 的 sha256 前 32 位而非原文——库被读走
    也还原不出能直接拿去请求的会话 token；前缀 ``admin_sess:`` 供 LIKE 批量清理。"""
    return "admin_sess:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


async def create_session(db: Database, username: str) -> str:
    """签发会话。返回的 token 是唯一一次可见的凭据，库里只留它的哈希 key。"""
    token = secrets.token_urlsafe(32)
    exp = time.time() + SESSION_TTL_SECONDS
    await db.set_config(
        session_config_key(token), json.dumps({"username": username, "exp": exp})
    )
    return token


async def resolve_session(db: Database, token: str) -> Optional[dict]:
    """按 token 解析会话；无记录 / JSON 损坏 / 字段缺失 / 已过期 → None。

    损坏行只判无效、不顺手删（R10：避免过度工程）——真正的清理由 login 时的
    ``cleanup_config_by_prefix("admin_sess:%", SESSION_TTL_SECONDS)`` 按时间兜底。
    """
    raw = await db.get_config(session_config_key(token))
    if not raw:
        return None
    try:
        session = json.loads(raw)
        exp = float(session["exp"])
    except (ValueError, TypeError, KeyError):
        return None
    if exp < time.time():
        return None
    return session


def _require_admin_key() -> str:
    """取 BOTFLOW_ADMIN_KEY。未配置是部署问题，按既有行为回 500 而非 401。"""
    admin_key = get_config().admin_key
    if not admin_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server admin key is not configured (BOTFLOW_ADMIN_KEY).",
        )
    return admin_key


async def verify_admin_key(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> None:
    """Admin REST API auth: BOTFLOW_ADMIN_KEY 直接出示，或持有 login 签发的会话 token。

    签名必须保持这三个参数（P0-1）：FastAPI 在路由注册时会把依赖的每个参数变成
    请求字段，给它加 ``db`` 参数会让全部 ``Depends(verify_admin_key)`` 路由注册即
    抛 FastAPIError —— 所以 session 分支在函数体内自行 ``get_db()``。
    """
    admin_key = _require_admin_key()
    token = _extract_token(authorization)
    if isinstance(credentials, HTTPAuthorizationCredentials) and credentials.credentials:
        token = credentials.credentials
    if not token:
        # 空 token 显式短路：compare_digest(None, ...) 会 TypeError 把 401 变 500
        # （P1-10 的第一道防线），空串也不该进 session 分支白查一次库。
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin key.",
        )
    # 分支①：直接出示 admin key。一律转 utf-8 bytes 再比 —— secrets.compare_digest
    # 收到非 ASCII str 会抛 TypeError（P1-10），不转则 key/token 含中文即 500。
    if secrets.compare_digest(token.encode("utf-8"), admin_key.encode("utf-8")):
        request.state.is_admin = True
        return
    # 分支②：login 签发的会话 token。查不到（含损坏/过期）即无效；401 文案与分支①
    # 统一，不泄露走的是哪条鉴权路径。
    session = await resolve_session(get_db(), token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin key.",
        )
    request.state.is_admin = True
