"""
Graphiti 后端切换回归测试（阶段 3）。

覆盖范围：
1. is_graph_local_only() 在 GRAPH_BACKEND=graphiti 时返回 False
2. is_graph_local_only() 在 GRAPH_BACKEND=zep 时尊重 GRAPH_LOCAL_ONLY
3. get_zep_client() 在 graphiti 模式下返回 shim 实例
4. get_zep_client() 在 zep 模式下返回 Zep 实例
5. get_zep_client() 错误处理（缺 API key / 缺密码）
6. zep_tools._graph_local_only 委托正确性

注意：config.py 的 load_dotenv(override=True) 会在 import 时把 .env 的
GRAPH_LOCAL_ONLY 写入 os.environ。pytest 的 monkeypatch.delenv 在某些
fixture 组合下会被 dotenv 重新触发。因此测试采用显式 setenv 覆盖策略
（而非 delenv），确保值确定。
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


# ============================================================
# 1. is_graph_local_only 测试
# ============================================================

class TestIsGraphLocalOnly:
    def test_zep_default_returns_false(self, monkeypatch):
        monkeypatch.setenv("GRAPH_BACKEND", "zep")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "")
        from app.utils.zep import is_graph_local_only
        assert is_graph_local_only() is False

    def test_local_only_env_true(self, monkeypatch):
        monkeypatch.setenv("GRAPH_BACKEND", "zep")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "1")
        from app.utils.zep import is_graph_local_only
        assert is_graph_local_only() is True

    def test_local_only_env_yes(self, monkeypatch):
        monkeypatch.setenv("GRAPH_BACKEND", "zep")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "yes")
        from app.utils.zep import is_graph_local_only
        assert is_graph_local_only() is True

    def test_local_only_env_false(self, monkeypatch):
        monkeypatch.setenv("GRAPH_BACKEND", "zep")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "0")
        from app.utils.zep import is_graph_local_only
        assert is_graph_local_only() is False

    def test_graphiti_overrides_local_only(self, monkeypatch):
        """GRAPH_BACKEND=graphiti 时即使 GRAPH_LOCAL_ONLY=1 也返回 False。"""
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "1")
        monkeypatch.setenv("GRAPH_BACKEND", "graphiti")
        from app.utils.zep import is_graph_local_only
        assert is_graph_local_only() is False

    def test_graphiti_without_local_only(self, monkeypatch):
        monkeypatch.setenv("GRAPH_BACKEND", "graphiti")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "1")  # 即使开着
        from app.utils.zep import is_graph_local_only
        assert is_graph_local_only() is False

    def test_zep_explicit_with_local_only(self, monkeypatch):
        """GRAPH_BACKEND=zep 明确时尊重 GRAPH_LOCAL_ONLY。"""
        monkeypatch.setenv("GRAPH_BACKEND", "zep")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "true")
        from app.utils.zep import is_graph_local_only
        assert is_graph_local_only() is True


# ============================================================
# 2. get_zep_client 路由测试
# ============================================================

class TestGetZepClientRouting:
    def test_graphiti_returns_shim(self, monkeypatch):
        """graphiti 模式下 get_zep_client 返回 GraphitiShimClient。"""
        monkeypatch.setenv("GRAPH_BACKEND", "graphiti")
        monkeypatch.setenv("NEO4J_PASSWORD", "testpass")
        monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")

        from app.utils import zep
        zep.clear_zep_client_cache()

        mock_instance = MagicMock(name="GraphitiShimClient")
        mock_class = MagicMock(return_value=mock_instance)
        with patch("app.services.graphiti_shim.GraphitiShimClient", mock_class):
            client = zep.get_zep_client()
            assert client is mock_instance
            mock_class.assert_called_once()

    def test_zep_returns_zep_client(self, monkeypatch):
        """zep 模式下 get_zep_client 返回 Zep 客户端。"""
        monkeypatch.setenv("GRAPH_BACKEND", "zep")
        monkeypatch.setenv("ZEP_API_KEY", "z_fake_key_for_test")
        monkeypatch.setenv("ZEP_API_URL", "")

        from app.utils import zep
        zep.clear_zep_client_cache()

        mock_zep_instance = MagicMock(name="Zep")
        mock_zep_class = MagicMock(return_value=mock_zep_instance)
        with patch("app.utils.zep.Zep", mock_zep_class):
            client = zep.get_zep_client()
            assert client is mock_zep_instance

    def test_graphiti_missing_password_raises(self, monkeypatch):
        """graphiti 模式 + 无 NEO4J_PASSWORD → get_zep_client 抛 ValueError。"""
        monkeypatch.setenv("GRAPH_BACKEND", "graphiti")
        monkeypatch.setenv("NEO4J_PASSWORD", "")

        from app.utils import zep
        zep.clear_zep_client_cache()
        with pytest.raises(ValueError, match="NEO4J_PASSWORD"):
            zep.get_zep_client()

    def test_zep_missing_api_key_raises(self, monkeypatch):
        """zep 模式 + 空 ZEP_API_KEY → get_zep_client 抛 ValueError。"""
        monkeypatch.setenv("GRAPH_BACKEND", "zep")
        monkeypatch.setenv("ZEP_API_KEY", "")
        monkeypatch.setenv("ZEP_API_URL", "")

        from app.utils import zep
        zep.clear_zep_client_cache()
        with pytest.raises(ValueError, match="ZEP_API_KEY"):
            zep.get_zep_client()

    def test_zep_api_url_rejected(self, monkeypatch):
        """zep 模式 + ZEP_API_URL 存在 → 抛 ValueError（安全策略）。"""
        monkeypatch.setenv("GRAPH_BACKEND", "zep")
        monkeypatch.setenv("ZEP_API_KEY", "z_fake")
        monkeypatch.setenv("ZEP_API_URL", "http://evil.example.com")

        from app.utils import zep
        zep.clear_zep_client_cache()
        with pytest.raises(ValueError, match="ZEP_API_URL"):
            zep.get_zep_client()

    def test_graphiti_mode_ignores_zep_api_url(self, monkeypatch):
        """graphiti 模式下 ZEP_API_URL 不应触发拒绝。"""
        monkeypatch.setenv("GRAPH_BACKEND", "graphiti")
        monkeypatch.setenv("NEO4J_PASSWORD", "testpass")
        monkeypatch.setenv("ZEP_API_URL", "http://whatever")

        from app.utils import zep
        zep.clear_zep_client_cache()

        mock_instance = MagicMock()
        mock_class = MagicMock(return_value=mock_instance)
        with patch("app.services.graphiti_shim.GraphitiShimClient", mock_class):
            client = zep.get_zep_client()
            assert client is mock_instance  # 不抛 ValueError


# ============================================================
# 2.5 should_use_local_write 测试（写路径降级策略）
# ============================================================

class TestShouldUseLocalWrite:
    def test_graphiti_mode_no_extraction_config_returns_true(self, monkeypatch):
        """graphiti 模式但未配 EXTRACTION/EMBED 时，写路径走本地 JSONL（降级）。"""
        monkeypatch.setenv("GRAPH_BACKEND", "graphiti")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "")
        monkeypatch.delenv("EXTRACTION_API_KEY", raising=False)
        monkeypatch.delenv("EMBED_API_KEY", raising=False)
        from app.utils.zep import should_use_local_write
        assert should_use_local_write() is True

    def test_graphiti_mode_with_extraction_returns_false(self, monkeypatch):
        """graphiti 模式且配了 EXTRACTION+EMBED 时，写路径走 graphiti.add_episode。"""
        monkeypatch.setenv("GRAPH_BACKEND", "graphiti")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "")
        monkeypatch.setenv("EXTRACTION_API_KEY", "sk-test")
        monkeypatch.setenv("EMBED_API_KEY", "sk-test")
        from app.utils.zep import should_use_local_write
        assert should_use_local_write() is False

    def test_graphiti_partial_config_returns_true(self, monkeypatch):
        """只配了 EXTRACTION 但没配 EMBED 时，仍降级（两者都需要）。"""
        monkeypatch.setenv("GRAPH_BACKEND", "graphiti")
        monkeypatch.setenv("EXTRACTION_API_KEY", "sk-test")
        monkeypatch.delenv("EMBED_API_KEY", raising=False)
        from app.utils.zep import should_use_local_write
        assert should_use_local_write() is True

    def test_zep_mode_no_local_only_returns_false(self, monkeypatch):
        monkeypatch.setenv("GRAPH_BACKEND", "zep")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "")
        from app.utils.zep import should_use_local_write
        assert should_use_local_write() is False

    def test_zep_mode_with_local_only_returns_true(self, monkeypatch):
        monkeypatch.setenv("GRAPH_BACKEND", "zep")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "1")
        from app.utils.zep import should_use_local_write
        assert should_use_local_write() is True


# ============================================================
# 3. zep_tools._graph_local_only 委托测试
# ============================================================

class TestZepToolsLocalOnlyDelegation:
    def test_delegates_to_is_graph_local_only(self, monkeypatch):
        """zep_tools._graph_local_only() 在 graphiti 模式返回 False。"""
        monkeypatch.setenv("GRAPH_BACKEND", "graphiti")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "1")
        from app.services.zep_tools import _graph_local_only
        assert _graph_local_only() is False

    def test_zep_mode_respects_local_only(self, monkeypatch):
        monkeypatch.setenv("GRAPH_BACKEND", "zep")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "1")
        from app.services.zep_tools import _graph_local_only
        assert _graph_local_only() is True

    def test_zep_mode_no_local_only(self, monkeypatch):
        monkeypatch.setenv("GRAPH_BACKEND", "zep")
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "")
        from app.services.zep_tools import _graph_local_only
        assert _graph_local_only() is False


# ============================================================
# 4. 缓存行为测试
# ============================================================

class TestClientCacheBehavior:
    def test_graphiti_client_cached(self, monkeypatch):
        """同一配置多次调用返回同一实例（lru_cache）。"""
        monkeypatch.setenv("GRAPH_BACKEND", "graphiti")
        monkeypatch.setenv("NEO4J_PASSWORD", "testpass")

        from app.utils import zep
        zep.clear_zep_client_cache()

        instances = []
        def factory(*a, **kw):
            inst = MagicMock()
            instances.append(inst)
            return inst

        with patch("app.services.graphiti_shim.GraphitiShimClient", side_effect=factory):
            c1 = zep.get_zep_client()
            c2 = zep.get_zep_client()
            assert c1 is c2
            assert len(instances) == 1

    def test_clear_cache_creates_new_instance(self, monkeypatch):
        """clear_zep_client_cache 后再调用会构造新实例。"""
        monkeypatch.setenv("GRAPH_BACKEND", "graphiti")
        monkeypatch.setenv("NEO4J_PASSWORD", "testpass")

        from app.utils import zep

        instances = []
        def factory(*a, **kw):
            inst = MagicMock()
            instances.append(inst)
            return inst

        with patch("app.services.graphiti_shim.GraphitiShimClient", side_effect=factory):
            zep.clear_zep_client_cache()
            c1 = zep.get_zep_client()
            zep.clear_zep_client_cache()
            c2 = zep.get_zep_client()
            assert c1 is not c2
            assert len(instances) == 2
