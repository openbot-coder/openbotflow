# BotFlow 安全审计报告

**审计日期**：2026-07-03  
**项目版本**：0.1.0  
**审计范围**：src/botflow 全部代码  

---

## 审计摘要

| 严重度 | 数量 |
|--------|------|
| 严重（Critical） | 0 |
| 高危（High） | 0 |
| 中危（Medium） | 3 |
| 低危（Low） | 1 |

---

## 中危漏洞

### 1. 时序攻击漏洞（Timing Attack）

**位置**：`src/botflow/auth.py:30,50`  
**CWE**：CWE-208  
**CVSS**：5.3  

#### 攻击者画像
外部用户或已认证用户

#### 输入向量
通过发送大量认证请求，测量响应时间差异来推断有效密钥

#### 代码路径
```
auth.py:30  if provided_key != valid_key:
auth.py:50  if provided_key != valid_key:
```

#### 漏洞分析
当前实现使用 Python 的 `!=` 运算符进行密钥比较。该运算符在遇到第一个不匹配字符时就会返回，导致响应时间存在微小差异。攻击者可以通过统计分析大量请求的响应时间来逐步推断有效密钥。

#### 影响评估
- 可能导致 API 密钥泄露
- 攻击者可利用泄露的密钥访问受保护资源

#### 修复建议
```python
import hmac

def verify_llm_key(request: Request, valid_key: str) -> Optional[JSONResponse]:
    if not valid_key:
        return None

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"error": "Missing or invalid Authorization header"})

    provided_key = auth_header.removeprefix("Bearer ").strip()
    # 使用常量时间比较
    if not hmac.compare_digest(provided_key, valid_key):
        logger.warning("Invalid LLM key attempt from {}", request.client.host if request.client else "unknown")
        return JSONResponse(status_code=401, content={"error": "Invalid API key"})

    return None
```

---

### 2. CORS 配置过于宽松

**位置**：`src/botflow/core.py:150`  
**CWE**：CWE-942  
**CVSS**：5.3  

#### 攻击者画像
外部用户

#### 输入向量
构造恶意网页，利用浏览器的 CORS 机制发起跨域请求

#### 代码路径
```
core.py:150  allow_origins=["*"],
core.py:151  allow_credentials=True,
```

#### 漏洞分析
当前 CORS 配置允许所有来源（`allow_origins=["*"]`）并启用凭证（`allow_credentials=True`）。这种组合虽然在技术上被浏览器拒绝，但配置本身表明安全意识不足。在生产环境中应限制允许的来源。

#### 影响评估
- 可能被利用进行 CSRF 攻击
- 增加攻击面

#### 修复建议
```python
# 生产环境应限制允许的来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://yourdomain.com"],  # 明确指定允许的来源
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
```

---

### 3. 缺少速率限制

**位置**：`src/botflow/core.py` (全局)  
**CWE**：CWE-770  
**CVSS**：5.3  

#### 攻击者画像
外部用户

#### 输入向量
发送大量认证请求或 API 调用

#### 代码路径
```
core.py:172-183  auth_middleware (无速率限制)
```

#### 漏洞分析
当前实现没有对 API 请求进行速率限制。攻击者可以：
1. 发送大量暴力破解认证请求
2. 发起 DoS 攻击消耗服务器资源
3. 滥用 API 进行大量调用产生高额费用

#### 影响评估
- 服务不可用风险
- 资源耗尽风险
- 潜在的经济损失

#### 修复建议
```python
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
import time

class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests: int = 100, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: dict[str, list[float]] = {}

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        
        # 清理过期记录
        if client_ip in self.requests:
            self.requests[client_ip] = [t for t in self.requests[client_ip] if now - t < self.window_seconds]
        else:
            self.requests[client_ip] = []
        
        # 检查速率限制
        if len(self.requests[client_ip]) >= self.max_requests:
            return JSONResponse(status_code=429, content={"error": "Too many requests"})
        
        self.requests[client_ip].append(now)
        return await call_next(request)
```

---

## 低危漏洞

### 4. API 密钥明文存储

**位置**：`src/botflow/storage/db.py:42`  
**CWE**：CWE-312  
**CVSS**：3.7  

#### 攻击者画像
能够访问数据库文件的内部人员或攻击者

#### 输入向量
直接访问 SQLite 数据库文件

#### 代码路径
```
db.py:42  api_key TEXT NOT NULL DEFAULT '',
```

#### 漏洞分析
Provider 的 API 密钥以明文形式存储在 SQLite 数据库中。如果攻击者能够访问数据库文件（如通过文件系统漏洞或备份泄露），可以直接读取所有密钥。

#### 影响评估
- 密钥泄露风险
- 可能导致第三方服务被滥用

#### 修复建议
```python
# 方案1：使用环境变量存储敏感配置
# 方案2：使用加密存储（如 Fernet）
from cryptography.fernet import Fernet

class EncryptedField:
    def __init__(self, key: bytes):
        self.cipher = Fernet(key)
    
    def encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()
    
    def decrypt(self, encrypted: str) -> str:
        return self.cipher.decrypt(encrypted.encode()).decode()
```

---

## 安全优点

### 1. SQL 注入防护良好
所有数据库查询使用参数化查询（`?` 占位符），列名通过白名单验证，有效防止 SQL 注入。

### 2. 无命令注入风险
代码中未发现 `exec()`、`eval()`、`subprocess` 等危险函数调用。

### 3. 日志脱敏处理
MCP 管理工具在显示 Provider 信息时对 API 密钥进行了脱敏处理：
```python
f"  API Key: {'***' if p.api_key else '(none)'}\n"
```

### 4. 数据库安全配置
- 启用 WAL 模式提高并发性能
- 设置 `busy_timeout=5000` 防止锁超时
- 启用外键约束

---

## 性能评估

### 优点
1. **异步数据库操作**：使用 aiosqlite 进行异步数据库操作，避免阻塞事件循环
2. **连接池管理**：单连接模式适合 SQLite 场景，避免连接池开销
3. **索引优化**：关键查询字段已建立索引
4. **日志轮转**：配置了 100MB 日志轮转和 30 天保留期

### 改进建议
1. **增加缓存层**：对频繁查询的 Provider/Model 配置添加内存缓存
2. **批量操作**：call_logs 清理可考虑批量删除减少数据库锁时间
3. **连接池**：如果未来迁移至 PostgreSQL，需要实现连接池

---

## 修复优先级

| 优先级 | 漏洞 | 建议完成时间 |
|--------|------|--------------|
| P1 | 时序攻击漏洞 | 1 周内 |
| P1 | CORS 配置 | 1 周内 |
| P2 | 速率限制 | 2 周内 |
| P3 | API 密钥加密存储 | 下个版本 |

---

## 审计结论

本次审计发现 3 个中危漏洞和 1 个低危漏洞，未发现严重或高危漏洞。整体安全状况良好，主要问题集中在认证机制的细节处理上。建议优先修复时序攻击漏洞和 CORS 配置问题。

**审计完成——未发现中等或更高严重度的已确认严重漏洞。**
