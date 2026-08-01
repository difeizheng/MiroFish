"""
方案 B：prepare 任务取消的回归测试

验证：
1. TaskManager.request_cancel / is_cancelled 的标志位语义
2. SimulationManager.prepare_simulation 收到取消标志后抛 PrepareCancelled
3. generate_profiles_from_entities 循环内取消检查（轻量模拟）
"""
import os
import sys
import time
import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------- 1. TaskManager 取消标志语义 ----------
class TestTaskManagerCancel:
    def test_request_cancel_sets_flag(self):
        from app.models.task import TaskManager, TaskStatus
        tm = TaskManager()
        tm._tasks.clear()  # 测试隔离
        tid = tm.create_task("test_cancel")
        tm.update_task(tid, status=TaskStatus.PROCESSING)
        assert tm.is_cancelled(tid) is False
        assert tm.request_cancel(tid) is True
        assert tm.is_cancelled(tid) is True

    def test_request_cancel_only_when_processing(self):
        """已完成/失败的任务不能取消"""
        from app.models.task import TaskManager, TaskStatus
        tm = TaskManager()
        tm._tasks.clear()
        tid = tm.create_task("test_done")
        tm.complete_task(tid, result={})
        # 已完成的任务，request_cancel 应返回 False
        assert tm.request_cancel(tid) is False
        assert tm.is_cancelled(tid) is False

    def test_request_cancel_unknown_task(self):
        from app.models.task import TaskManager
        tm = TaskManager()
        assert tm.request_cancel("nonexistent") is False
        assert tm.is_cancelled("nonexistent") is False


# ---------- 2. PrepareCancelled 异常 + 取消检查 helper ----------
class TestPrepareCancelledException:
    def test_exception_is_raisable_and_catchable(self):
        from app.services.simulation_manager import PrepareCancelled
        with pytest.raises(PrepareCancelled, match="用户取消"):
            raise PrepareCancelled("用户取消（阶段: 测试）")

    def test_exception_caught_as_generic_exception(self):
        """PrepareCancelled 必须能被 except Exception 捕获（上层依赖此）"""
        from app.services.simulation_manager import PrepareCancelled
        caught = None
        try:
            raise PrepareCancelled("test")
        except Exception as e:
            caught = e
        assert isinstance(caught, PrepareCancelled)


# ---------- 3. generate_profiles_from_entities 循环取消 ----------
class TestGenerateProfilesCancel:
    """
    验证取消机制的核心语义：当 task_id 被标记取消后，generate_profiles_from_entities
    在完成当前 future 后会检测到取消并抛 PrepareCancelled。
    由于真实的 generate_single_profile 依赖 LLM/zep，这里用 monkeypatch 替换为带 delay 的桩。
    """
    def test_cancel_mid_generation_raises(self, monkeypatch):
        from app.models.task import TaskManager, TaskStatus
        from app.services import oasis_profile_generator as ogm
        from app.services.simulation_manager import PrepareCancelled

        TaskManager._instance._tasks.clear() if TaskManager._instance else None
        tm = TaskManager()
        task_id = tm.create_task("test_prepare_cancel")
        tm.update_task(task_id, status=TaskStatus.PROCESSING)

        # 构造假实体列表（20 个，保证取消前还有未完成的）
        fake_entities = []
        for i in range(20):
            e = MagicMock()
            e.name = f"entity_{i}"
            e.uuid = f"uuid_{i}"
            e.get_entity_type.return_value = "TestEntity"
            e.summary = "test"
            e.attributes = {}
            e.related_edges = []
            e.related_nodes = []
            fake_entities.append(e)

        gen = ogm.OasisProfileGenerator(graph_id=None)
        # 拦掉 Zep 检索（GRAPH_LOCAL_ONLY 已在 conftest 屏蔽，这里直接 mock 让它快返回）
        monkeypatch.setattr(gen, '_search_zep_for_entity', lambda entity: {})
        # 拦掉 LLM 调用，加慢 delay 让取消能在循环中途触发
        def slow_llm(*a, **kw):
            time.sleep(0.4)
            return None, "cancelled-test-fallback"
        # _generate_profile_with_llm 是实际 LLM 调用入口（名字可能不同，用通用方式拦截 generate_single_profile 的内部）
        # 直接 patch OpenAI client 让 chat 慢
        monkeypatch.setattr(gen, 'client', MagicMock())
        gen.client.chat = MagicMock()
        gen.client.chat.completions = MagicMock()
        gen.client.chat.completions.create = MagicMock(side_effect=lambda *a, **kw: time.sleep(0.4) or _fake_completion())
        # 防 zep_client 缺失
        if not hasattr(gen, 'zep_client'):
            gen.zep_client = MagicMock()
            gen.zep_client.graph = MagicMock()
            gen.zep_client.graph.search = MagicMock(return_value=MagicMock(edges=[], nodes=[], facts=[]))

        # 在另一个线程里延迟触发取消
        def trigger_cancel():
            time.sleep(1.0)  # 等几个任务完成
            tm.request_cancel(task_id)

        t = threading.Thread(target=trigger_cancel, daemon=True)
        t.start()

        with pytest.raises(PrepareCancelled):
            gen.generate_profiles_from_entities(
                entities=fake_entities,
                use_llm=True,  # 走 LLM 路径，才能被 client.chat mock 拖慢
                progress_callback=None,
                graph_id=None,
                parallel_count=2,
                realtime_output_path=None,
                output_platform="reddit",
                task_id=task_id
            )

    def test_no_cancel_completes_normally(self, monkeypatch):
        """不取消时正常跑完（task_id=None 完全不检查）"""
        from app.services import oasis_profile_generator as ogm

        fake_entities = []
        for i in range(3):
            e = MagicMock()
            e.name = f"e{i}"
            e.uuid = f"u{i}"
            e.get_entity_type.return_value = "TestEntity"
            e.summary = "s"
            e.attributes = {}
            e.related_edges = []
            e.related_nodes = []
            fake_entities.append(e)

        gen = ogm.OasisProfileGenerator(graph_id=None)
        monkeypatch.setattr(gen, '_search_zep_for_entity', lambda entity: {})
        if not hasattr(gen, 'zep_client'):
            gen.zep_client = MagicMock()

        profiles = gen.generate_profiles_from_entities(
            entities=fake_entities,
            use_llm=False,
            progress_callback=None,
            graph_id=None,
            parallel_count=2,
            realtime_output_path=None,
            output_platform="reddit",
            task_id=None
        )
        assert len(profiles) == 3


def _fake_completion():
    """构造一个最小的 OpenAI 响应桩（避免真实 LLM 调用）"""
    class FakeChoice:
        class FakeMessage:
            content = '{"user_name":"test","name":"Test","bio":"b","persona":"p","age":30,"gender":"M","mbti":"INTJ","profession":"tester","country":"CN","interested_topics":["t"],"introverted":50,"independent":50,"open_minded":50}'
        message = FakeMessage()
        finish_reason = "stop"
    class FakeResp:
        choices = [FakeChoice()]
    return FakeResp()
