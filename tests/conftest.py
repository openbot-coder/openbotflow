"""全仓测试隔离（ZG-2 硬规格，docs/tasks/setup_token_features.md §3）。

setup token 功能会在「项目根/.setup_token」写入明文凭据（0600）。任何触发
lifespan / ``ensure_setup_token`` 的测试都绝不能往仓库根写这个文件——否则
跑完测试仓库变脏、且有把测试期明文凭据误提交的风险。

settings 单例可能在本 fixture 之前就构造（字段支会停在空串），故
``BotflowSettings.setup_token_path`` 的解析顺序定档为
「字段 → 调用时 ``os.environ.get("BOTFLOW_SETUP_TOKEN_FILE")`` → 项目根默认」：
这里只需 setenv，每个测试的 setup token 文件都会落到本用例的 ``tmp_path``。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def setup_token_file_isolated(tmp_path, monkeypatch):
    """强制 ``BOTFLOW_SETUP_TOKEN_FILE`` → 本用例 ``tmp_path/.setup_token``。"""
    monkeypatch.setenv("BOTFLOW_SETUP_TOKEN_FILE", str(tmp_path / ".setup_token"))
