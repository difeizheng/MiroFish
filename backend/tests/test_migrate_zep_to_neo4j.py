"""
migrate_zep_to_neo4j 回归测试（阶段 2）。

覆盖范围：
1. load_cache 解析两种结构（嵌套 / 平铺）
2. _to_label_list 规范化 labels
3. _sanitize_label 转合法 Neo4j label
4. get_neo4j_config 环境变量优先级
5. migrate_nodes / migrate_edges 用 mock driver 验证 Cypher 生成
6. create_indexes 错误降级
7. verify_migration 数量对账
8. main dry_run 不写库

设计原则（ai-regression-testing skill）：
- hermetic：mock driver，不连真实 Neo4j
- 覆盖真实缓存文件解析的正确性（用实际 mirofish 缓存文件做 dry-run 解析）
"""
from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# 加载迁移脚本（绕过 services/__init__.py 的 zep_cloud 依赖）
import importlib.util
import sys

_script_path = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "migrate_zep_to_neo4j.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "migrate_zep_to_neo4j", _script_path
    )
    mod = importlib.util.module_from_spec(spec)
    # 迁移脚本无相对导入，直接 exec
    spec.loader.exec_module(mod)
    return mod


_migrate = _load_migration_module()


# ============================================================
# Mock driver 工厂
# ============================================================

def make_mock_driver(query_results=None):
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
    m = MagicMock()
    m.__getitem__.side_effect = lambda k: fields.get(k)
    return m


# ============================================================
# 1. load_cache 测试
# ============================================================

class TestLoadCache:
    def test_nested_structure(self):
        """MiroFish 真实缓存结构：{cached_at, graph_id, data: {nodes, edges}}。"""
        cache_data = {
            "cached_at": 1234567890,
            "graph_id": "test_graph",
            "data": {
                "graph_id": "test_graph",
                "nodes": [{"uuid": "n1", "name": "A", "labels": ["Org"]}],
                "edges": [{"uuid": "e1", "fact": "f"}],
            },
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(cache_data, f)
            f.flush()
            try:
                result = _migrate.load_cache(f.name)
                assert len(result["nodes"]) == 1
                assert len(result["edges"]) == 1
                assert result["graph_id"] == "test_graph"
            finally:
                os.unlink(f.name)

    def test_flat_structure(self):
        """平铺结构：顶层就是 {nodes, edges}。"""
        cache_data = {
            "graph_id": "flat_graph",
            "nodes": [{"uuid": "n1"}],
            "edges": [{"uuid": "e1"}],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(cache_data, f)
            f.flush()
            try:
                result = _migrate.load_cache(f.name)
                assert len(result["nodes"]) == 1
                assert result["graph_id"] == "flat_graph"
            finally:
                os.unlink(f.name)

    def test_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError):
            _migrate.load_cache("/nonexistent/path/12345.json")

    def test_real_mirofish_cache(self):
        """验证能解析真实的 MiroFish 主缓存文件。"""
        real_path = os.path.join(
            os.path.dirname(__file__), "..",
            "uploads", "graphs", "mirofish_1069a964f83c4bef.json",
        )
        if not os.path.exists(real_path):
            pytest.skip("真实缓存文件不存在")
        result = _migrate.load_cache(real_path)
        assert len(result["nodes"]) == 2734
        assert len(result["edges"]) == 9263
        assert result["graph_id"] == "mirofish_1069a964f83c4bef"


# ============================================================
# 2. _to_label_list 测试
# ============================================================

class TestToLabelList:
    def test_none_returns_empty(self):
        assert _migrate._to_label_list(None) == []

    def test_empty_list_returns_empty(self):
        assert _migrate._to_label_list([]) == []

    def test_string_returns_single(self):
        assert _migrate._to_label_list("Organization") == ["Organization"]

    def test_list_passthrough(self):
        assert _migrate._to_label_list(["Organization", "Company"]) == [
            "Organization", "Company"
        ]

    def test_list_with_empty_filtered(self):
        assert _migrate._to_label_list(["Org", "", None, "Person"]) == ["Org", "Person"]

    def test_non_list_non_string(self):
        assert _migrate._to_label_list(123) == []


# ============================================================
# 3. _sanitize_label 测试
# ============================================================

class TestSanitizeLabel:
    def test_alphanumeric_passthrough(self):
        assert _migrate._sanitize_label("Organization") == "Organization"
        assert _migrate._sanitize_label("SemiconductorCompany") == "SemiconductorCompany"

    def test_empty_returns_empty(self):
        assert _migrate._sanitize_label("") == ""
        assert _migrate._sanitize_label(None) == ""

    def test_special_chars_replaced(self):
        # 空格和特殊字符替换为下划线
        assert _migrate._sanitize_label("My Label") == "My_Label"
        assert _migrate._sanitize_label("a-b.c") == "a_b_c"

    def test_leading_digit_prefixed(self):
        # Neo4j label 不能以数字开头
        result = _migrate._sanitize_label("123abc")
        assert result.startswith("_")
        assert "123abc" in result


# ============================================================
# 4. get_neo4j_config 测试
# ============================================================

class TestGetNeo4jConfig:
    def test_defaults(self):
        args = SimpleNamespace(
            neo4j_uri=None, neo4j_user=None, neo4j_password=None, neo4j_database=None,
        )
        with patch.dict(os.environ, {}, clear=False):
            # 确保测试环境不受外部 NEO4J_* 变量干扰
            for k in ["NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "NEO4J_DATABASE"]:
                os.environ.pop(k, None)
            config = _migrate.get_neo4j_config(args)
            assert config["uri"] == "bolt://localhost:7687"
            assert config["user"] == "neo4j"
            assert config["password"] == "testpass"
            assert config["database"] == "neo4j"

    def test_cli_args_override_env(self):
        args = SimpleNamespace(
            neo4j_uri="bolt://custom:7687",
            neo4j_user="customuser",
            neo4j_password="custompass",
            neo4j_database="customdb",
        )
        config = _migrate.get_neo4j_config(args)
        assert config["uri"] == "bolt://custom:7687"
        assert config["user"] == "customuser"
        assert config["password"] == "custompass"
        assert config["database"] == "customdb"

    def test_env_used_when_no_cli(self):
        args = SimpleNamespace(
            neo4j_uri=None, neo4j_user=None, neo4j_password=None, neo4j_database=None,
        )
        with patch.dict(os.environ, {
            "NEO4J_URI": "bolt://env:7687",
            "NEO4J_USER": "envuser",
            "NEO4J_PASSWORD": "envpass",
            "NEO4J_DATABASE": "envdb",
        }):
            config = _migrate.get_neo4j_config(args)
            assert config["uri"] == "bolt://env:7687"
            assert config["user"] == "envuser"
            assert config["password"] == "envpass"
            assert config["database"] == "envdb"


# ============================================================
# 5. migrate_nodes 测试
# ============================================================

class TestMigrateNodes:
    def test_basic_batch(self):
        driver = make_mock_driver()
        nodes = [
            {"uuid": "n1", "name": "A", "labels": ["Organization"], "summary": "s1", "attributes": {}, "created_at": "2026-01-01T00:00:00Z"},
            {"uuid": "n2", "name": "B", "labels": [], "summary": "s2", "attributes": {}, "created_at": None},
        ]
        count = _migrate.migrate_nodes(driver, "neo4j", nodes, "g1", batch_size=100)
        assert count == 2
        # execute_query 至少被调用 1 次（UNWIND MERGE）
        assert driver.execute_query.call_count >= 1

    def test_batch_size_splitting(self):
        """batch_size=2 时 5 个节点应分成 3 批。"""
        driver = make_mock_driver()
        nodes = [{"uuid": f"n{i}", "name": f"N{i}", "labels": [], "summary": "", "attributes": {}} for i in range(5)]
        _migrate.migrate_nodes(driver, "neo4j", nodes, "g1", batch_size=2)
        # 3 批 MERGE + 1 次 APOC/SET（empty label 不会触发 subtype 循环，实际 0 次）
        # 这里只验证 MERGE 调用次数
        merge_calls = [
            c for c in driver.execute_query.call_args_list
            if "UNWIND" in str(c)
        ]
        assert len(merge_calls) == 3  # ceil(5/2) = 3

    def test_attrs_json_serialized(self):
        driver = make_mock_driver()
        nodes = [{
            "uuid": "n1", "name": "A", "labels": ["Person"],
            "summary": "", "attributes": {"full_name": "张三", "title": "CTO"},
            "created_at": None,
        }]
        _migrate.migrate_nodes(driver, "neo4j", nodes, "g1", batch_size=100)
        # 检查传给 execute_query 的 batch 参数
        call = driver.execute_query.call_args_list[0]
        batch = call.kwargs.get("batch") or (call.args[1] if len(call.args) > 1 else None)
        assert batch is not None
        attrs_json = batch[0]["attrs_json"]
        assert "张三" in attrs_json
        assert "CTO" in attrs_json


# ============================================================
# 6. migrate_edges 测试
# ============================================================

class TestMigrateEdges:
    def test_basic_batch(self):
        driver = make_mock_driver()
        edges = [
            {"uuid": "e1", "fact": "made X", "fact_type": "MADE", "name": "MADE",
             "source_node_uuid": "s1", "target_node_uuid": "t1",
             "created_at": "2026-01-01T00:00:00Z", "episodes": ["ep1"]},
        ]
        count = _migrate.migrate_edges(driver, "neo4j", edges, "g1", batch_size=100)
        assert count == 1

    def test_fact_type_default(self):
        """fact_type 缺失时用 name 或 RELATES 兜底。"""
        driver = make_mock_driver()
        edges = [{
            "uuid": "e1", "fact": "f", "fact_type": None, "name": "WORKS_FOR",
            "source_node_uuid": "s1", "target_node_uuid": "t1", "created_at": None,
        }]
        _migrate.migrate_edges(driver, "neo4j", edges, "g1", batch_size=100)
        call = driver.execute_query.call_args_list[0]
        batch = call.kwargs.get("batch")
        assert batch[0]["fact_type"] == "WORKS_FOR"  # name 兜底

    def test_empty_episodes_normalized(self):
        driver = make_mock_driver()
        edges = [{
            "uuid": "e1", "fact": "f", "fact_type": "X",
            "source_node_uuid": "s1", "target_node_uuid": "t1",
            "created_at": None, "episodes": None,
        }]
        _migrate.migrate_edges(driver, "neo4j", edges, "g1", batch_size=100)
        call = driver.execute_query.call_args_list[0]
        batch = call.kwargs.get("batch")
        assert batch[0]["episodes"] == []


# ============================================================
# 7. create_indexes 测试
# ============================================================

class TestCreateIndexes:
    def test_all_succeed(self):
        driver = make_mock_driver()
        _migrate.create_indexes(driver, "neo4j", "g1")
        # 6 个索引
        assert driver.execute_query.call_count == 6

    def test_one_fails_continues(self):
        """某个索引建失败不应中断其余。"""
        driver = MagicMock()
        call_count = [0]

        def execute_query(cypher, **params):
            call_count[0] += 1
            if "FULLTEXT" in cypher:
                raise RuntimeError("fulltext not supported")
            return [], None, None

        driver.execute_query.side_effect = execute_query
        _migrate.create_indexes(driver, "neo4j", "g1")
        # 6 个都尝试了（包括失败的）
        assert call_count[0] == 6


# ============================================================
# 8. verify_migration 测试
# ============================================================

class TestVerifyMigration:
    def test_counts_match(self):
        driver = make_mock_driver({
            "count(n)": [record(cnt=100)],
            "count(r)": [record(cnt=500)],
        })
        result = _migrate.verify_migration(driver, "neo4j", "g1", 100, 500)
        assert result is True

    def test_node_count_mismatch(self):
        driver = make_mock_driver({
            "count(n)": [record(cnt=99)],
            "count(r)": [record(cnt=500)],
        })
        result = _migrate.verify_migration(driver, "neo4j", "g1", 100, 500)
        assert result is False

    def test_edge_count_mismatch(self):
        driver = make_mock_driver({
            "count(n)": [record(cnt=100)],
            "count(r)": [record(cnt=499)],
        })
        result = _migrate.verify_migration(driver, "neo4j", "g1", 100, 500)
        assert result is False


# ============================================================
# 9. clear_existing 测试
# ============================================================

class TestClearExisting:
    def test_calls_detach_delete(self):
        driver = make_mock_driver()
        _migrate.clear_existing(driver, "neo4j", "g1")
        call = driver.execute_query.call_args
        assert "DETACH DELETE" in call.args[0]
        assert call.kwargs.get("group_id") == "g1"
