"""
EntitySelector（LLM 智能实体筛选）回归测试

背景（BUG-R2 预防）：阶段2 引入 LLM 打分筛选替代纯度数截断。
关键失败模式：
1. LLM 失败必须降级为度数截断，绝不能打断 prepare 流程
2. 打分缓存必须命中（重复 prepare 不重复烧 token）
3. 批次解析必须把序号正确映射回 uuid，缺分/越界/脏数据不能崩
"""

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.entity_selector import EntitySelector, load_reality_seed


def make_entity(uuid, name, labels=None, summary=""):
    return SimpleNamespace(uuid=uuid, name=name, labels=labels or ["Entity", "人物"], summary=summary)


def make_pool(n):
    return [make_entity(f"uuid-{i}", f"实体{i}") for i in range(n)]


class FakeLLM:
    """可控的 LLM 替身：model 属性 + chat_json 返回指定分数"""
    model = "fake-model"

    def __init__(self, scores_map=None, error=None):
        self.scores_map = scores_map or {}
        self.error = error
        self.calls = 0

    def chat_json(self, messages, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        # 从 user prompt 解析序号（每个候选实体一行，形如 "1. 实体X（...）"）
        user = messages[-1]["content"]
        import re
        indices = [int(m.group(1)) for m in re.finditer(r'^(\d+)\. ', user, re.M)]
        return {"scores": [{"i": i, "s": self.scores_map.get(i, 7)} for i in indices]}


@pytest.fixture
def no_cache(tmp_path, monkeypatch):
    """隔离缓存目录到 tmp"""
    import app.services.entity_selector as es
    monkeypatch.setattr(es, "_CACHE_DIR", str(tmp_path))
    return tmp_path


class TestSelect:
    def test_pool_not_larger_than_target_returns_all(self, no_cache):
        pool = make_pool(3)
        sel = EntitySelector(llm=FakeLLM())
        selected, meta = sel.select(pool, target_count=5)
        assert selected == pool
        assert meta["source"] == "noop"

    def test_llm_scores_determine_selection(self, no_cache):
        # 5 个候选选 3 个；给序号为 2/4/5 的实体最高分
        pool = make_pool(5)
        fake = FakeLLM(scores_map={1: 2, 2: 9, 3: 1, 4: 8, 5: 10})
        sel = EntitySelector(llm=fake)
        selected, meta = sel.select(pool, target_count=3)
        assert meta["source"] == "llm"
        assert [e.name for e in selected] == ["实体4", "实体1", "实体3"]

    def test_llm_failure_falls_back_to_degree_order(self, no_cache):
        """LLM 异常 -> 按原顺序（度数降序）截断，不抛异常"""
        pool = make_pool(5)
        sel = EntitySelector(llm=FakeLLM(error=RuntimeError("LLM down")))
        selected, meta = sel.select(pool, target_count=3)
        assert meta["source"] == "fallback"
        assert [e.name for e in selected] == ["实体0", "实体1", "实体2"]

    def test_cache_hit_skips_llm(self, no_cache):
        pool = make_pool(5)
        fake = FakeLLM(scores_map={1: 9, 2: 8, 3: 7, 4: 1, 5: 1})
        sel = EntitySelector(llm=fake)
        first, meta1 = sel.select(pool, target_count=3, graph_id="g1")
        assert meta1["source"] == "llm"
        assert fake.calls == 1

        # 第二次：新 selector 实例（模拟重启），应命中缓存
        sel2 = EntitySelector(llm=fake)
        second, meta2 = sel2.select(pool, target_count=3, graph_id="g1")
        assert meta2["source"] == "cache"
        assert fake.calls == 1  # 没有新的 LLM 调用
        assert [e.uuid for e in first] == [e.uuid for e in second]


class TestScoreBatchParsing:
    def test_indices_map_to_correct_uuids(self):
        pool = make_pool(4)
        fake = FakeLLM(scores_map={1: 1, 2: 10, 3: 5, 4: 3})
        sel = EntitySelector(llm=fake)
        scores = sel._score_batch(pool, "seed", "req")
        assert scores == {"uuid-0": 1.0, "uuid-1": 10.0, "uuid-2": 5.0, "uuid-3": 3.0}

    def test_out_of_range_and_dirty_items_ignored(self):
        pool = make_pool(2)

        class DirtyLLM(FakeLLM):
            def chat_json(self, messages, **kwargs):
                return {"scores": [
                    {"i": 1, "s": 8},
                    {"i": 99, "s": 10},          # 越界
                    {"i": "x", "s": 5},           # 非数字序号
                    {"i": 2, "s": "not-a-num"},   # 非数字分数
                    {"i": 2, "s": 99},            # 超界分数 -> clamp 到 10
                ]}

        sel = EntitySelector(llm=DirtyLLM())
        scores = sel._score_batch(pool, "seed", "req")
        assert scores == {"uuid-0": 8.0, "uuid-1": 10.0}

    def test_all_unparseable_raises(self):
        pool = make_pool(2)

        class BadLLM(FakeLLM):
            def chat_json(self, messages, **kwargs):
                return {"scores": [{"i": 99, "s": 1}]}

        sel = EntitySelector(llm=BadLLM())
        with pytest.raises(ValueError):
            sel._score_batch(pool, "seed", "req")


class TestLoadRealitySeed:
    def test_reads_only_md_txt(self, tmp_path, monkeypatch):
        files_dir = tmp_path / "proj_x" / "files"
        files_dir.mkdir(parents=True)
        (files_dir / "a.md").write_text("种子内容A", encoding="utf-8")
        (files_dir / "b.txt").write_text("种子内容B", encoding="utf-8")
        (files_dir / "c.pdf").write_bytes(b"%PDF-fake")

        monkeypatch.setattr(
            "app.models.project.ProjectManager.get_project_files",
            classmethod(lambda cls, pid: [str(files_dir / f) for f in os.listdir(files_dir)])
        )
        seed = load_reality_seed("proj_x")
        assert "种子内容A" in seed
        assert "种子内容B" in seed
        assert "%PDF" not in seed

    def test_no_files_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            "app.models.project.ProjectManager.get_project_files",
            classmethod(lambda cls, pid: [])
        )
        assert load_reality_seed("proj_none") == ""
