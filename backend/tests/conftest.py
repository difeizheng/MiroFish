"""
pytest 全局配置

backend/app/config.py 以 override=True 加载根目录 .env，
若 .env 中开启了 GRAPH_LOCAL_ONLY（本地离线降级模式），
会泄漏进测试进程，使契约测试绕开 Zep 路径而失败。
这里在每个测试前屏蔽该开关，保证测试永远走真实 Zep 调用路径。
"""

import pytest


@pytest.fixture(autouse=True)
def _neutralize_local_only_flags(monkeypatch):
    """测试默认走 zep 后端、关闭离线模式，避免 .env / 容器 env 污染。"""
    monkeypatch.delenv('GRAPH_LOCAL_ONLY', raising=False)
    monkeypatch.setenv('GRAPH_BACKEND', 'zep')
