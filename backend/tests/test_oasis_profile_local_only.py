"""
回归测试：oasis_profile_generator 的 GRAPH_LOCAL_ONLY 离线降级

背景（bug）：prepare 阶段 _search_zep_for_entity 没有遵循 GRAPH_LOCAL_ONLY，
导致 Zep 云端额度耗尽后，每个实体重试 3 次（46s+59s），150 个实体串行 → 无限卡死。
修复（commit d19d040）：开头短路 + Zep 失败降级到本地缓存。

这些测试防止该 bug 复发 —— 严格验证三条路径：
  R1: GRAPH_LOCAL_ONLY=1 时不碰 zep_client（防死循环）
  R2: 无 zep_client 时返回空结构不崩
  R3: Zep 抛可重试错误时降级到本地缓存（兜底）

注意：用 __new__ 跳过 __init__，避免构造时真的去连 Zep（get_zep_client）。
"""

from unittest.mock import patch

import pytest

from app.services.oasis_profile_generator import OasisProfileGenerator
from app.services.zep_entity_reader import EntityNode


# ---------- 测试用实体与缓存数据 ----------

def _make_entity():
    """长鑫存储（图谱中心节点，有大量边）"""
    return EntityNode(
        uuid="19292a9f-cc06-443e-8a52-df5a51ffe6e4",
        name="长鑫存储",
        labels=["Entity", "SemiconductorCompany"],
        summary="国产 DRAM 龙头",
        attributes={},
    )


def _make_cache_data():
    """伪造本地图谱缓存：1 节点 + 1 边，保证 _search_local_cache_for_entity 有命中"""
    return {
        "nodes": [
            {
                "uuid": "aaaaaaaa-0000-0000-0000-000000000001",
                "name": "DDR5产品",
                "summary": "长鑫的 DDR5 已量产",
                "labels": ["Entity"],
            },
        ],
        "edges": [
            {
                "fact": "长鑫存储 developed DDR5产品",
                "source_node_uuid": "19292a9f-cc06-443e-8a52-df5a51ffe6e4",
                "target_node_uuid": "aaaaaaaa-0000-0000-0000-000000000001",
                "source_node_name": "长鑫存储",
                "target_node_name": "DDR5产品",
            },
        ],
    }


def _make_generator(graph_id="mirofish_test"):
    """用 __new__ 跳过 __init__（避免 get_zep_client 真的连 Zep）"""
    gen = OasisProfileGenerator.__new__(OasisProfileGenerator)
    gen.graph_id = graph_id
    gen.zep_client = None  # 默认无 client
    return gen


# ---------- R1: GRAPH_LOCAL_ONLY 短路（核心回归） ----------


class TestGraphLocalOnlyShortCircuit:
    """BUG-R1 回归：GRAPH_LOCAL_ONLY=1 时必须走本地缓存，绝不调 Zep"""

    def test_local_only_does_not_touch_zep_client(self, monkeypatch):
        """本地模式下，即使 zep_client 存在也不调用它的 graph.search"""
        monkeypatch.setenv("GRAPH_LOCAL_ONLY", "1")

        gen = _make_generator()
        # 故意给一个会抛异常的假 zep_client —— 如果短路失效就会抛错暴露
        gen.zep_client = type("FakeZep", (), {})()
        gen.zep_client.graph = type("FakeGraph", (), {})()
        gen.zep_client.graph.search = lambda **kw: (_ for _ in ()).throw(
            AssertionError("GRAPH_LOCAL_ONLY 下不应调用 zep_client.graph.search")
        )

        with patch(
            "app.services.zep_entity_reader.ZepEntityReader._read_graph_cache_data",
            return_value=_make_cache_data(),
        ):
            result = gen._search_zep_for_entity(_make_entity())

        # 走了本地缓存：有 facts 命中
        assert len(result["facts"]) >= 1
        assert any("DDR5" in f for f in result["facts"])

    def test_local_only_accepts_truthy_values(self, monkeypatch):
        """1 / true / yes 都应触发短路（大小写不敏感）"""
        for val in ("1", "true", "TRUE", "yes", "Yes"):
            monkeypatch.setenv("GRAPH_LOCAL_ONLY", val)
            gen = _make_generator()
            gen.zep_client = None  # 无 client，若没短路会返回空

            with patch(
                "app.services.zep_entity_reader.ZepEntityReader._read_graph_cache_data",
                return_value=_make_cache_data(),
            ):
                result = gen._search_zep_for_entity(_make_entity())
            # 每种 truthy 写法都应命中本地缓存
            assert result["facts"], f"GRAPH_LOCAL_ONLY={val} 未触发短路"

    def test_local_only_off_falls_through(self, monkeypatch):
        """GRAPH_LOCAL_ONLY 未设/为 0 时不短路（走正常 Zep 路径）"""
        monkeypatch.delenv("GRAPH_LOCAL_ONLY", raising=False)

        gen = _make_generator()
        gen.zep_client = None  # 无 client → 正常路径返回空结构

        result = gen._search_zep_for_entity(_make_entity())
        assert result == {"facts": [], "node_summaries": [], "context": ""}


# ---------- R2: 无 zep_client 时优雅降级 ----------


class TestNoZepClient:
    def test_returns_empty_structure_when_no_client(self, monkeypatch):
        """无 zep_client（ZEP_API_KEY 未配）时返回标准空结构，不崩"""
        monkeypatch.delenv("GRAPH_LOCAL_ONLY", raising=False)
        gen = _make_generator()
        gen.zep_client = None

        result = gen._search_zep_for_entity(_make_entity())
        assert set(result.keys()) == {"facts", "node_summaries", "context"}
        assert result["facts"] == []
        assert result["node_summaries"] == []
        assert result["context"] == ""


# ---------- R3: Zep 可重试错误降级到本地缓存（兜底） ----------


class TestZepFailureFallback:
    """BUG-R1 兜底：Zep 抛可重试错误（如额度耗尽）时降级到本地缓存"""

    def test_retryable_error_falls_back_to_local_cache(self, monkeypatch):
        monkeypatch.delenv("GRAPH_LOCAL_ONLY", raising=False)
        gen = _make_generator()

        # 造一个会抛"可重试错误"的 zep_client
        gen.zep_client = type("FakeZep", (), {})()
        gen.zep_client.graph = type("FakeGraph", (), {})()

        def _boom(**kw):
            # 模拟 Zep 限流/额度耗尽类错误
            raise Exception("429 Too Many Requests / quota exhausted")

        gen.zep_client.graph.search = _boom

        # 让 is_retryable_zep_error 认为这个错可重试
        with patch(
            "app.services.oasis_profile_generator.is_retryable_zep_error",
            return_value=True,
        ), patch(
            "app.services.oasis_profile_generator.call_zep_read_with_retry",
            side_effect=Exception("429 Too Many Requests / quota exhausted"),
        ), patch(
            "app.services.zep_entity_reader.ZepEntityReader._read_graph_cache_data",
            return_value=_make_cache_data(),
        ):
            result = gen._search_zep_for_entity(_make_entity())

        # 降级成功：从本地缓存捞到 facts
        assert len(result["facts"]) >= 1, "Zep 失败后未降级到本地缓存"


# ---------- 本地缓存检索本身的基本正确性 ----------


class TestLocalCacheSearch:
    def test_returns_facts_and_summaries_from_cache(self):
        gen = _make_generator()
        with patch(
            "app.services.zep_entity_reader.ZepEntityReader._read_graph_cache_data",
            return_value=_make_cache_data(),
        ):
            result = gen._search_local_cache_for_entity(_make_entity())

        assert any("DDR5" in f for f in result["facts"])
        assert any("DDR5" in s for s in result["node_summaries"])
        assert "DDR5" in result["context"]

    def test_returns_empty_when_no_cache(self):
        gen = _make_generator()
        with patch(
            "app.services.zep_entity_reader.ZepEntityReader._read_graph_cache_data",
            return_value=None,
        ):
            result = gen._search_local_cache_for_entity(_make_entity())
        assert result == {"facts": [], "node_summaries": [], "context": ""}
