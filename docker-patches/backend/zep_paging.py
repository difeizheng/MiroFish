"""
Zep graph node/edge 全量分页拉取（容器 hotfix 版，兼容 zep-cloud 3.13.0）。

背景 bug：旧版用 page_size=100 + `len(batch) < page_size` 判断末页，
但 Zep 服务端单页上限是 50 —— 请求 limit=100 也只返回 50 条，
导致第一页后就被误判为"末页"而停止，全图 2720 节点只读到 50 个。

修复要点：
1. page_size 固定为 50（与服务端上限一致），满页才继续翻；
2. 使用 3.13 SDK 支持的 uuid_cursor 参数翻页；
3. 游标不前进时立即中断，防止死循环；
4. max_items 默认 None = 不截断（旧版默认 2000 上限也会截断大图）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from zep_cloud.client import Zep

from .logger import get_logger

logger = get_logger("mirofish.zep_paging")

# Zep 服务端单页实际上限是 50，请求更大的 limit 也只返回 50
_DEFAULT_PAGE_SIZE = 50
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_DELAY = 2.0


def _fetch_page_with_retry(
    api_call: Callable[..., Any],
    *args: Any,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    retry_delay: float = _DEFAULT_RETRY_DELAY,
    page_description: str = "page",
    **kwargs: Any,
) -> Any:
    """单页请求，带指数退避重试。"""
    last_exception: Exception | None = None
    delay = retry_delay

    for attempt in range(max_retries):
        try:
            return api_call(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - 读操作允许宽捕获重试
            last_exception = e
            if attempt < max_retries - 1:
                logger.warning(
                    f"Zep {page_description} 第 {attempt + 1} 次尝试失败: "
                    f"{str(e)[:100]}, {delay:.1f}秒后重试..."
                )
                time.sleep(delay)
                delay *= 2
            else:
                logger.error(
                    f"Zep {page_description} 在 {max_retries} 次尝试后仍失败: {e}"
                )

    assert last_exception is not None
    raise last_exception


def _item_uuid(item: Any) -> str | None:
    return getattr(item, "uuid_", None) or getattr(item, "uuid", None)


def _fetch_all(
    api_call: Callable[..., Any],
    graph_id: str,
    *,
    item_name: str,
    page_size: int,
    max_items: int | None,
    max_retries: int,
    retry_delay: float,
) -> list[Any]:
    if not 1 <= page_size <= _DEFAULT_PAGE_SIZE:
        raise ValueError(f"page_size 不能超过服务端上限 {_DEFAULT_PAGE_SIZE}")

    all_items: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    page_num = 0

    while True:
        kwargs: dict[str, Any] = {"limit": page_size}
        if cursor is not None:
            kwargs["uuid_cursor"] = cursor

        page_num += 1
        batch = _fetch_page_with_retry(
            api_call,
            graph_id,
            max_retries=max_retries,
            retry_delay=retry_delay,
            page_description=f"fetch {item_name} page {page_num} (graph={graph_id})",
            **kwargs,
        )
        batch = list(batch or [])
        if not batch:
            break

        all_items.extend(batch)

        if max_items is not None and len(all_items) >= max_items:
            all_items = all_items[:max_items]
            logger.warning(
                f"{item_name} 数量达到显式上限 ({max_items})，停止翻页: graph={graph_id}"
            )
            break

        if len(batch) < page_size:
            break  # 真正的末页

        next_cursor = _item_uuid(batch[-1])
        if next_cursor is None:
            logger.warning(
                f"{item_name} 缺少 uuid 字段，在 {len(all_items)} 条处停止翻页"
            )
            break
        if next_cursor in seen_cursors or next_cursor == cursor:
            raise RuntimeError(
                f"Zep {item_name} 分页游标未前进: graph={graph_id}"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    logger.info(f"共获取 {len(all_items)} 个{item_name}（{page_num} 页）")
    return all_items


def fetch_all_nodes(
    client: Zep,
    graph_id: str,
    page_size: int = _DEFAULT_PAGE_SIZE,
    max_items: int | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    retry_delay: float = _DEFAULT_RETRY_DELAY,
) -> list[Any]:
    """分页获取图谱全部节点。默认不截断。"""

    return _fetch_all(
        client.graph.node.get_by_graph_id,
        graph_id,
        item_name="nodes",
        page_size=page_size,
        max_items=max_items,
        max_retries=max_retries,
        retry_delay=retry_delay,
    )


def fetch_all_edges(
    client: Zep,
    graph_id: str,
    page_size: int = _DEFAULT_PAGE_SIZE,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    retry_delay: float = _DEFAULT_RETRY_DELAY,
    max_items: int | None = None,
) -> list[Any]:
    """分页获取图谱全部边。默认不截断。"""

    return _fetch_all(
        client.graph.edge.get_by_graph_id,
        graph_id,
        item_name="edges",
        page_size=page_size,
        max_items=max_items,
        max_retries=max_retries,
        retry_delay=retry_delay,
    )
