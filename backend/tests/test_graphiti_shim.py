"""
graphiti_shim 回归测试（阶段 1A）。

覆盖范围：
1. 对象工厂函数（_make_node/_make_edge/_make_episode）字段对齐
2. _RawResponse.content/.json() 契约
3. _BatchAPI 全流程（create→add→process→get→list→list_items）
4. _GraphAPI 读路径（get/create/delete/search）用 mock driver
5. _NodeAPI.get / get_edges / get_by_graph_id 用 mock driver
6. _EdgeAPI.get_by_graph_id 用 mock driver
7. _EpisodeAPI.get 用 mock driver
8. NotFoundError 语义（对齐 zep_cloud.NotFoundError）

设计原则（来自 ai-regression-testing skill）：
- hermetic：用 mock driver，不连真实 Neo4j
- 覆盖 sandbox/production 路径一致性（shim 是生产路径，mock 是测试路径）
- 为每个公开方法至少 1 个 happy path + 1 个 error path
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

# 直接从模块导入（避免触发 services/__init__.py 的 zep_cloud 依赖）
# 用 importlib 直加载模块文件，绕过包初始化
from unittest.mock import MagicMock
import pytest

# 直接从包导入（容器内 zep_cloud 已安装，本地需先 pip install）
from app.services.graphiti_shim import (
    GraphitiShimClient,
    NotFoundError,
    _make_node,
    _make_edge,
    _make_episode,
    _RawResponse,
    _BatchAPI,
    _GraphAPI,
    _NodeAPI,
    _EdgeAPI,
    _EpisodeAPI,
    GraphDatabase,
)

_shim = SimpleNamespace(
    GraphitiShimClient=GraphitiShimClient,
    NotFoundError=NotFoundError,
    _make_node=_make_node,
    _make_edge=_make_edge,
    _make_episode=_make_episode,
    _RawResponse=_RawResponse,
    _BatchAPI=_BatchAPI,
    _GraphAPI=_GraphAPI,
    _NodeAPI=_NodeAPI,
    _EdgeAPI=_EdgeAPI,
    _EpisodeAPI=_EpisodeAPI,
    GraphDatabase=GraphDatabase,
)


# ============================================================
# Mock Neo4j driver 工厂
# ============================================================

def make_mock_driver(query_results: Dict[str, List[SimpleNamespace]] | None = None):
    """构造一个 mock neo4j driver。

    query_results: {cypher_substring: [record1, record2, ...]}
    record 是 SimpleNamespace（通过 r["field"] 访问）。

    mock driver 的 execute_query 会匹配 cypher 里的 substring 返回对应结果。
    """
    driver = MagicMock()
    results = query_results or {}

    def execute_query(cypher, **params):
        for substr, records in results.items():
            if substr in cypher:
                return records, None, None
        return [], None, None

    driver.execute_query.side_effect = execute_query
    return driver


def record(**fields):
    """构造一个可通过 r['key'] 访问的 mock record。"""
    m = MagicMock()
    m.__getitem__.side_effect = lambda k: fields.get(k)
    return m


# ============================================================
# 1. 对象工厂测试
# ============================================================

class TestMakeNode:
    def test_basic_fields(self):
        n = _shim._make_node("uuid-1", "张三", ["Person"], "a summary", {"x": 1})
        assert n.uuid_ == "uuid-1"
        assert n.uuid == "uuid-1"  # 兼容 alias
        assert n.name == "张三"
        assert n.labels == ["Person"]
        assert n.summary == "a summary"
        assert n.attributes == {"x": 1}

    def test_defaults_empty(self):
        n = _shim._make_node("u", "n", None, None, None)
        assert n.labels == []
        assert n.summary == ""
        assert n.attributes == {}

    def test_created_at_passthrough(self):
        n = _shim._make_node("u", "n", [], "", {}, created_at="2026-01-01")
        assert n.created_at == "2026-01-01"


class TestMakeEdge:
    def test_basic_fields(self):
        e = _shim._make_edge(
            "e1", "DEVELOPED", "made X", "DEVELOPED", "s1", "t1",
            episodes=["ep1"], attributes={"k": "v"},
        )
        assert e.uuid_ == "e1"
        assert e.fact == "made X"
        assert e.fact_type == "DEVELOPED"
        assert e.source_node_uuid == "s1"
        assert e.target_node_uuid == "t1"
        assert e.episodes == ["ep1"]
        assert e.attributes == {"k": "v"}

    def test_defaults_empty(self):
        e = _shim._make_edge("e", None, None, None, "s", "t")
        assert e.name == ""
        assert e.fact == ""
        # fact_type 默认: fact_type or name or "" → ""
        assert e.fact_type == ""


class TestMakeEpisode:
    def test_basic(self):
        ep = _shim._make_episode("ep1", True)
        assert ep.uuid_ == "ep1"
        assert ep.processed is True

    def test_extra_fields(self):
        ep = _shim._make_episode("ep1", False, content="hello", name="test")
        assert ep.processed is False
        assert ep.content == "hello"
        assert ep.name == "test"


# ============================================================
# 2. _RawResponse 测试
# ============================================================

class TestRawResponse:
    def test_data_is_list(self):
        raw = _shim._RawResponse([{"a": 1}])
        assert isinstance(raw.data, list)
        assert raw.data == [{"a": 1}]

    def test_content_is_bytes(self):
        raw = _shim._RawResponse([{"a": 1}])
        assert isinstance(raw.content, bytes)

    def test_content_is_utf8_json(self):
        raw = _shim._RawResponse([{"name": "中文"}])
        decoded = json.loads(raw.content.decode("utf-8"))
        assert decoded == [{"name": "中文"}]

    def test_json_roundtrip(self):
        raw = _shim._RawResponse([1, 2, 3])
        assert raw.json() == [1, 2, 3]

    def test_status_code(self):
        assert _shim._RawResponse([]).status_code == 200


# ============================================================
# 3. _BatchAPI 测试（纯内存，无需 mock driver）
# ============================================================

class TestBatchAPI:
    def test_create_returns_batch_id(self):
        b = _shim._BatchAPI("neo4j")
        result = b.create(metadata={"k": "v"})
        assert hasattr(result, "batch_id")
        assert result.batch_id.startswith("batch_")
        assert result.metadata == {"k": "v"}

    def test_add_returns_item_details(self):
        b = _shim._BatchAPI("neo4j")
        created = b.create()
        bid = created.batch_id
        items = [
            SimpleNamespace(type="graph_episode", data="chunk1", graph_id="g1"),
            SimpleNamespace(type="graph_episode", data="chunk2", graph_id="g1"),
        ]
        details = b.add(bid, items)
        assert len(details) == 2
        assert details[0].sequence_index == 0
        assert details[1].sequence_index == 1
        assert details[0].episode_uuid != details[1].episode_uuid
        assert details[0].status == "succeeded"

    def test_add_nonexistent_batch_raises(self):
        b = _shim._BatchAPI("neo4j")
        with pytest.raises(_shim.NotFoundError):
            b.add("nonexistent", [])

    def test_process_sets_succeeded(self):
        b = _shim._BatchAPI("neo4j")
        bid = b.create().batch_id
        b.add(bid, [SimpleNamespace(data="x")])
        b.process(bid)
        summary = b.get(bid)
        assert summary.status == "succeeded"

    def test_get_returns_progress(self):
        b = _shim._BatchAPI("neo4j")
        bid = b.create().batch_id
        b.add(bid, [SimpleNamespace(data="x"), SimpleNamespace(data="y")])
        b.process(bid)
        summary = b.get(bid)
        assert summary.progress.percent_complete == 100
        assert summary.progress.succeeded_items == 2

    def test_list_pagination(self):
        b = _shim._BatchAPI("neo4j")
        for i in range(3):
            b.create(metadata={"i": i})
        page1 = b.list(limit=2)
        assert len(page1.batches) == 2
        assert page1.next_cursor == 2
        page2 = b.list(limit=2, cursor=2)
        assert len(page2.batches) == 1
        assert page2.next_cursor is None

    def test_list_items_pagination(self):
        b = _shim._BatchAPI("neo4j")
        bid = b.create().batch_id
        b.add(bid, [SimpleNamespace(data=f"c{i}") for i in range(5)])
        page1 = b.list_items(bid, limit=2)
        assert len(page1.items) == 2
        assert page1.next_cursor == 2
        page2 = b.list_items(bid, limit=2, cursor=2)
        assert len(page2.items) == 2
        page3 = b.list_items(bid, limit=2, cursor=4)
        assert len(page3.items) == 1
        assert page3.next_cursor is None

    def test_add_items_source_uuid_matches_episode_uuid(self):
        """回归测试：add() 后每个 item 的 source_uuid 必须 == episode_uuid。

        graph_builder._wait_for_batch 会校验 source_uuid == episode_uuid，
        不一致时报 'mismatched episode UUIDs'。
        """
        b = _shim._BatchAPI("neo4j")
        bid = b.create().batch_id
        b.add(bid, [SimpleNamespace(data="chunk1"), SimpleNamespace(data="chunk2")])
        items = b.list_items(bid).items
        for item in items:
            assert item.episode_uuid == item.source_uuid, (
                f"episode_uuid={item.episode_uuid} != source_uuid={item.source_uuid}"
            )

    def test_process_noop_preserves_uuid_consistency(self):
        """回归测试：process() noop 模式后 source_uuid 仍 == episode_uuid。"""
        b = _shim._BatchAPI("neo4j")  # 无 graphiti_factory → noop
        bid = b.create().batch_id
        b.add(bid, [SimpleNamespace(data="x")])
        b.process(batch_id=bid)
        items = b.list_items(bid).items
        assert items[0].episode_uuid == items[0].source_uuid
        assert items[0].status == "succeeded"


# ============================================================
# 4. _GraphAPI 读路径测试（mock driver）
# ============================================================

class TestGraphAPI:
    def test_create_merges_graphmeta(self):
        driver = make_mock_driver()
        g = _shim._GraphAPI(driver, "neo4j")
        result = g.create(graph_id="g1", name="Test", description="desc")
        assert result.graph_id == "g1"
        driver.execute_query.assert_called()

    def test_get_existing_via_graphmeta(self):
        driver = make_mock_driver({
            "__GraphMeta": [record(g={})],
        })
        g = _shim._GraphAPI(driver, "neo4j")
        result = g.get(graph_id="g1")
        assert result.graph_id == "g1"

    def test_get_existing_via_entity_count(self):
        # 没有 GraphMeta 但有 Entity 节点
        driver = make_mock_driver({
            "__GraphMeta": [],  # 第一条 cypher 返回空
            "count(n)": [record(cnt=5)],  # 第二条返回有节点
        })
        g = _shim._GraphAPI(driver, "neo4j")
        result = g.get(graph_id="g1")
        assert result.graph_id == "g1"

    def test_get_nonexistent_raises_not_found(self):
        driver = make_mock_driver({
            "__GraphMeta": [],
            "count(n)": [record(cnt=0)],
        })
        g = _shim._GraphAPI(driver, "neo4j")
        with pytest.raises(_shim.NotFoundError):
            g.get(graph_id="nope")

    def test_delete_removes_all_nodes(self):
        driver = make_mock_driver()
        g = _shim._GraphAPI(driver, "neo4j")
        g.delete(graph_id="g1")
        driver.execute_query.assert_called()
        # 确认 DETACH DELETE cypher 被调用
        call_args = driver.execute_query.call_args
        assert "DETACH DELETE" in call_args.args[0]

    def test_set_ontology_caches(self):
        driver = make_mock_driver()
        g = _shim._GraphAPI(driver, "neo4j")
        g.set_ontology(
            graph_ids=["g1"],
            entities={"Person": dict()},
            edges={"WORKS_AT": dict()},
        )
        assert "g1" in g._ontology_cache
        assert "Person" in g._ontology_cache["g1"]["entities"]


class TestGraphAPI_Search:
    def test_cypher_fallback_no_graphiti(self):
        """没有 graphiti_factory 时走 Cypher LIKE 搜索。"""
        driver = make_mock_driver({
            "toLower(r.fact)": [
                record(
                    uuid_="e1", fact="made X", fact_type="MADE",
                    source_node_uuid="s1", target_node_uuid="t1",
                    source_node_name="A", target_node_name="B",
                    created_at=None,
                ),
            ],
        })
        g = _shim._GraphAPI(driver, "neo4j")
        results = g.search("X", graph_id="g1", top_k=5)
        assert len(results) == 1
        assert results[0].fact == "made X"
        assert results[0].source_node_name == "A"

    def test_cypher_fallback_no_graph_id_returns_empty(self):
        driver = make_mock_driver()
        g = _shim._GraphAPI(driver, "neo4j")
        results = g.search("x")
        assert results == []


# ============================================================
# 5. _NodeAPI 测试
# ============================================================

class TestNodeAPI:
    def test_get_existing(self):
        driver = make_mock_driver({
            "n.uuid AS uuid_": [record(
                uuid_="u1", name="张三", labels=["Person"],
                summary="a", created_at=None,
            )],
        })
        node_api = _shim._NodeAPI(driver, "neo4j")
        n = node_api.get(uuid_="u1")
        assert n.uuid_ == "u1"
        assert n.name == "张三"

    def test_get_nonexistent_raises(self):
        driver = make_mock_driver({"n.uuid AS uuid_": []})
        node_api = _shim._NodeAPI(driver, "neo4j")
        with pytest.raises(_shim.NotFoundError):
            node_api.get(uuid_="nope")

    def test_get_edges(self):
        driver = make_mock_driver({
            "CASE WHEN startNode": [
                record(
                    uuid_="e1", fact="made X", fact_type="MADE",
                    other_uuid="t1", other_name="B",
                    created_at=None, direction="out",
                ),
            ],
        })
        node_api = _shim._NodeAPI(driver, "neo4j")
        result = node_api.get_edges(node_uuid="s1")
        assert hasattr(result, "edges")
        assert len(result.edges) == 1
        assert result.edges[0].source_node_uuid == "s1"
        assert result.edges[0].target_node_uuid == "t1"

    def test_get_by_graph_id(self):
        driver = make_mock_driver({
            "n.group_id AS group_id": [
                record(uuid_="u1", name="A", labels=["Org"], summary="s", created_at=None),
                record(uuid_="u2", name="B", labels=["Person"], summary="s", created_at=None),
            ],
        })
        node_api = _shim._NodeAPI(driver, "neo4j")
        result = node_api.get_by_graph_id(graph_id="g1")
        assert len(result.nodes) == 2
        assert result.nodes[0].name == "A"

    def test_with_raw_response_get_by_graph_id(self):
        driver = make_mock_driver({
            "n.group_id AS group_id": [
                record(uuid_="u1", name="A", labels=["Org"], summary="s", created_at=None),
            ],
        })
        node_api = _shim._NodeAPI(driver, "neo4j")
        raw = node_api.with_raw_response.get_by_graph_id(graph_id="g1")
        assert isinstance(raw, _shim._RawResponse)
        # data 是节点列表（每个元素是 SimpleNamespace 模拟 Zep Node）
        assert isinstance(raw.data, list)
        assert len(raw.data) == 1
        assert raw.data[0].name == "A"


# ============================================================
# 6. _EdgeAPI 测试
# ============================================================

class TestEdgeAPI:
    def test_get_by_graph_id(self):
        driver = make_mock_driver({
            "r.uuid AS uuid_": [
                record(
                    uuid_="e1", fact="made X", fact_type="MADE",
                    source_node_uuid="s1", target_node_uuid="t1",
                    source_node_name="A", target_node_name="B",
                    created_at=None, valid_at=None, invalid_at=None,
                    expired_at=None, episodes=["ep1"],
                ),
            ],
        })
        edge_api = _shim._EdgeAPI(driver, "neo4j")
        result = edge_api.get_by_graph_id(graph_id="g1")
        assert len(result.edges) == 1
        assert result.edges[0].fact == "made X"

    def test_with_raw_response(self):
        driver = make_mock_driver({
            "r.uuid AS uuid_": [
                record(
                    uuid_="e1", fact="f", fact_type="F",
                    source_node_uuid="s1", target_node_uuid="t1",
                    source_node_name="A", target_node_name="B",
                    created_at=None, valid_at=None, invalid_at=None,
                    expired_at=None, episodes=None,
                ),
            ],
        })
        edge_api = _shim._EdgeAPI(driver, "neo4j")
        raw = edge_api.with_raw_response.get_by_graph_id(graph_id="g1")
        assert isinstance(raw.data, list)
        assert len(raw.data) == 1
        assert raw.data[0].fact == "f"


# ============================================================
# 7. _EpisodeAPI 测试
# ============================================================

class TestEpisodeAPI:
    def test_get_existing(self):
        driver = make_mock_driver({
            "e.uuid AS uuid_": [record(
                uuid_="ep1", created_at=None, valid_at=None,
                group_id="g1", name="ep", content="text",
                episode_type="message",
            )],
        })
        ep_api = _shim._EpisodeAPI(driver, "neo4j")
        ep = ep_api.get(uuid_="ep1")
        assert ep.processed is True
        assert ep.content == "text"

    def test_get_nonexistent_assumes_processed(self):
        """迁移来的图可能没有 Episodic 节点，shim 返回 processed=True。"""
        driver = make_mock_driver({"e.uuid AS uuid_": []})
        ep_api = _shim._EpisodeAPI(driver, "neo4j")
        ep = ep_api.get(uuid_="missing-ep")
        assert ep.processed is True  # 安全降级


# ============================================================
# 8. GraphitiShimClient 顶层测试
# ============================================================

class TestGraphitiShimClient:
    def test_init_creates_graph_and_batch_attrs(self):
        """验证顶层 client 有 .graph 和 .batch 属性（对齐 Zep client 结构）。"""
        # 用 mock driver 绕过真实连接
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_shim.GraphDatabase, "driver", lambda *a, **kw: MagicMock())
            client = _shim.GraphitiShimClient(
                "bolt://localhost:7687", "neo4j", "pass"
            )
            assert hasattr(client, "graph")
            assert hasattr(client, "batch")
            assert isinstance(client.graph, _shim._GraphAPI)
            assert isinstance(client.batch, _shim._BatchAPI)

    def test_close_calls_driver_close(self):
        mock_driver = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_shim.GraphDatabase, "driver", lambda *a, **kw: mock_driver)
            client = _shim.GraphitiShimClient("bolt://x", "u", "p")
            client.close()
            mock_driver.close.assert_called_once()
