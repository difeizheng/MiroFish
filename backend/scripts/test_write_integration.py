"""通过 MiroFish 的 get_zep_client / graph.add 接口测试写路径（阶段 1B 集成验证）。"""
import time
import traceback
from datetime import datetime, timezone

# 模拟 memory_updater 的调用方式
from app.utils.zep import get_zep_client

client = get_zep_client()
print(f"client type: {type(client).__name__}", flush=True)
print(f"graphiti_factory set: {client.graph._graphiti_factory is not None}", flush=True)

GRAPH_ID = "test_write_integration"

text = """
2025年长鑫存储宣布完成B轮融资，估值达500亿元。
投资方包括国家大基金二期、合肥产投集团等机构。
公司计划用这笔资金扩建合肥DRAM研发中心。
"""

t0 = time.time()
try:
    # 这正是 zep_graph_memory_updater.py 调用的方式
    result = client.graph.add(
        graph_id=GRAPH_ID,
        type="text",
        data=text,
        created_at=datetime.now(timezone.utc),
        source_description="集成测试：通过 MiroFish graph.add 接口",
    )
    elapsed = time.time() - t0
    print(f"=== graph.add 成功 (耗时 {elapsed:.1f}s) ===", flush=True)
    print(f"episode uuid: {getattr(result, 'uuid', 'N/A')}", flush=True)
except Exception:
    elapsed = time.time() - t0
    print(f"=== graph.add 失败 (耗时 {elapsed:.1f}s) ===", flush=True)
    traceback.print_exc()
