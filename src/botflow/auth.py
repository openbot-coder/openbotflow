"""Authentication dependencies for botflow LLM Proxy (LLM key + admin key)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from botflow.common.logger import get_logger
from botflow.config import get_config
from botflow.storage.db import Database, get_db
from botflow.storage.models import ApiKey

# Setup token 生命周期（生成 → 开通为止）相关日志走现成 loguru。
_log = get_logger("auth")

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


# ---------------------------------------------------------------------------
# Setup token（系统自动生成的一次性开通凭证）
#
# 与 BOTFLOW_ADMIN_KEY 是两个东西：setup token 只管 POST /admin/auth/setup 的
# 首次开通/重置，用毕（成功开通）即从 KV 与文件双删；admin key 的 Bearer 通道
# 不受这里影响。明文只存在两处，均在服务器侧：.setup_token 文件（0600）+ 启动
# 日志一行；DB 只存 sha256 哈希 + 生成时间。
# ---------------------------------------------------------------------------

# config KV 中 setup token 记录的 key（value = {"hash", "created_at"}，无明文）。
SETUP_TOKEN_KEY = "admin_setup_token"


def generate_setup_token() -> str:
    """32 位 hex（128 bit 随机）—— ``secrets`` 密码学安全随机源。"""
    return secrets.token_hex(16)


def _is_valid_setup_token(content: str) -> bool:
    """文件内容判据（分支③④共用，T2.2 被测对象）：恰 32 位小写 hex、无换行。

    只认 ``generate_setup_token`` 的产物形状：0 字节 / 截断 / 污染 / 带换行
    一律 False → 落分支③轮换，关掉「KV 在、文件坏 → 永远开不了通」的死锁。
    """
    return len(content) == 32 and all(c in "0123456789abcdef" for c in content)


def write_setup_token_file(path: Path, token: str) -> None:
    """把明文 token 写进 0600 文件。任一步失败向上抛 OSError，由调用方降级告警。

    ``fchmod`` 必须无条件执行（R2）：① ``O_CREAT`` 的 mode 会被 umask 掩码
    （umask 022 时落成 0644），只有显式 fchmod 才钉死 0600；② 文件已存在时
    ``O_CREAT`` 不会改旧文件的 mode，同样靠这一步收紧。
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, token.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


async def ensure_setup_token(db: Database) -> None:
    """FastAPI 启动钩子：按「生成一次、用到开通为止」维护 setup token（四分支）。

    这是 LLM 网关的开通便利功能，不配让全量网关陪葬（ZG-1/R7）：文件/KV 的
    写删失败一律 ``log.error`` 降级，本函数任何路径不向上抛，启动不中断。
    动作序「先写文件、再写 KV」保证 KV 记录 ⟹ 文件已完整写入，不会出现
    「KV 有哈希、明文永失」的死锁；失败留到下次启动走分支②/③重新生成自愈。
    """
    path = get_config().setup_token_path

    # 分支①：账号已配置 → 幂等清理（删 KV + 删文件），不生成。
    if await db.get_config("admin_account"):
        await db.execute_write("DELETE FROM config WHERE key = ?", (SETUP_TOKEN_KEY,))
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass  # 天然幂等
        except OSError as e:
            # 清理非致命：下次启动分支①重试（T2.11）。
            _log.error(
                "setup token file cleanup failed: path={} errno={} error={}",
                path, getattr(e, "errno", None), e,
            )
        return

    raw = await db.get_config(SETUP_TOKEN_KEY)
    rotate_reason: Optional[str] = None
    if raw is not None:
        # 分支③④：KV 有记录 → 以文件内容定去留。
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            rotate_reason = "file missing"
        except (OSError, UnicodeDecodeError):
            rotate_reason = "file invalid"  # 读不出来 → 视同损坏走轮换
        else:
            if _is_valid_setup_token(content):
                # 分支④：双在不动（重启不轮换），仅收紧权限到 0600。
                # 收紧 ≠ 轮换：O_WRONLY 不带 O_TRUNC，内容与哈希不变。
                # 与 write_setup_token_file 统一走「fd + 无条件 fchmod」一条路径
                # （R6 win32 spy fchmod 轨一处断言覆盖全部收紧点）；
                # open/fchmod 任一失败同样仅告警不抛。
                try:
                    fd = os.open(path, os.O_WRONLY)
                    try:
                        os.fchmod(fd, 0o600)
                    finally:
                        os.close(fd)
                except OSError as e:
                    _log.warning(
                        "setup token file chmod failed: path={} error={}", path, e
                    )
                return
            rotate_reason = "file invalid"

    # 分支②（无 KV 记录）/ 分支③（文件缺失或损坏）：生成新 token。
    token = generate_setup_token()
    try:
        write_setup_token_file(path, token)
    except Exception as e:
        # 写文件任一步失败：不抛、不写 KV，下次启动重新生成自愈（T2.10）。
        _log.error(
            "setup token file write failed: path={} errno={} error={}",
            path, getattr(e, "errno", None), e,
        )
        return
    try:
        await db.set_config(
            SETUP_TOKEN_KEY,
            json.dumps(
                {"hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                 "created_at": time.time()}
            ),
        )
    except Exception as e:
        # 文件已成功、KV 失败：只告警。下次启动无 KV → 分支②重新生成覆盖文件。
        _log.error("setup token KV write failed: error={}", e)
        return
    if rotate_reason:
        # 日志含 "rotated: file missing" / "rotated: file invalid"（A12/R4 预案锚点）。
        _log.info(
            "setup token rotated: {} -> new token {} (file: {})",
            rotate_reason, token, path,
        )
    else:
        # 明文进日志是决策 1 有意双写（R3：与 .setup_token 同信任域）。
        _log.info("setup token generated: {} (file: {})", token, path)
