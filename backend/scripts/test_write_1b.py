"""阶段 1B 写路径端到端测试（单 event loop）。"""
import asyncio
import time
import traceback
from datetime import datetime, timezone

from app.services.graphiti_shim import create_graphiti_instance
from graphiti_core.nodes import EpisodeType


async def main():
    g = create_graphiti_instance()

    # 建索引（首次必须）
    await g.build_indices_and_constraints()
    print('=== indices built ===', flush=True)

    TEST_GROUP = 'test_write_1b'
    text = """
    2025年长鑫存储（CXMT）在合肥启动了新一轮DRAM工厂建设，投资额达300亿元。
    公司CEO朱一明表示，新工厂将专注于DDR5内存芯片的量产，预计2026年投产。
    此举将使长鑫存储的全球DRAM市场份额从5%提升至10%。
    """

    t0 = time.time()
    result = await g.add_episode(
        name='test_ep_cxmt',
        episode_body=text,
        source_description='阶段1B写路径测试',
        reference_time=datetime.now(timezone.utc),
        source=EpisodeType.message,
        group_id=TEST_GROUP,
    )
    elapsed = time.time() - t0
    print(f'=== add_episode 成功 (耗时 {elapsed:.1f}s) ===', flush=True)

    ep = getattr(result, 'episode', None)
    nodes = getattr(result, 'nodes', None) or []
    edges = getattr(result, 'edges', None) or []
    print(f'episode uuid: {getattr(ep, "uuid", "N/A")}', flush=True)
    print(f'nodes: {len(nodes)}', flush=True)
    print(f'edges: {len(edges)}', flush=True)
    for n in nodes[:8]:
        print(f'  node: {n.name}', flush=True)
    for e in edges[:5]:
        print(f'  edge: {getattr(e, "fact", "?")}', flush=True)

    await g.close()


try:
    asyncio.run(main())
except Exception:
    print('=== 失败 ===', flush=True)
    traceback.print_exc()
