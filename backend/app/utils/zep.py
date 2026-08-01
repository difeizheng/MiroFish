"""Shared Zep Cloud client, request limits, and retry policy."""

from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Any, Callable, TypeVar

import httpx
from zep_cloud.client import Zep
from zep_cloud.core.api_error import ApiError as ZepApiError

from ..config import Config
from .logger import get_logger

logger = get_logger("mirofish.zep")

T = TypeVar("T")

ZEP_CLOUD_BASE_URL = "https://api.getzep.com/api/v2"
# Keep request behavior aligned with the zep-cloud 3.25.0 SDK default that
# MiroFish used before introducing the shared client. This is an internal
# integration policy, not a deployment setting users need to tune.
ZEP_HTTP_REQUEST_TIMEOUT_SECONDS = 60.0
# Zep ingestion is asynchronous and may take several minutes. Preserve the
# original GraphBuilder deadline while keeping it separate from HTTP timeout.
ZEP_INGESTION_WAIT_TIMEOUT_SECONDS = 600
MAX_ZEP_SEARCH_QUERY_CHARS = 400
MAX_ZEP_SEARCH_RESULTS = 50


def is_graph_local_only() -> bool:
    """GRAPH_LOCAL_ONLY 离线模式检查（统一入口）。

    GRAPH_BACKEND=graphiti 时自动返回 False（读路径走 shim 直连 Neo4j，不走本地 JSON 降级）。
    直接读 os.environ 而非 Config.GRAPH_BACKEND，避免模块加载快照导致测试需要 reload。
    """
    if os.environ.get('GRAPH_BACKEND', 'zep') == 'graphiti':
        return False
    return os.environ.get('GRAPH_LOCAL_ONLY', '').lower() in ('1', 'true', 'yes')


def should_use_local_write() -> bool:
    """写路径是否走本地 JSONL 降级。

    - GRAPH_LOCAL_ONLY=1 时走本地（Zep 额度耗尽离线模式）
    - GRAPH_BACKEND=graphiti 且未配置 EXTRACTION_*/EMBED_* 时走本地（写路径降级）
    - GRAPH_BACKEND=graphiti 且已配置 EXTRACTION_*/EMBED_* 时走 graphiti.add_episode（阶段 1B）
    """
    if os.environ.get('GRAPH_BACKEND', 'zep') == 'graphiti':
        # 阶段 1B：配置了抽取 LLM 和 embedder 时走 graphiti 写路径
        ext_key = os.environ.get('EXTRACTION_API_KEY', '').strip()
        emb_key = os.environ.get('EMBED_API_KEY', '').strip()
        if ext_key and emb_key:
            return False  # 走 graphiti.add_episode
        return True  # 未配置写路径 LLM，降级到本地
    return os.environ.get('GRAPH_LOCAL_ONLY', '').lower() in ('1', 'true', 'yes')


def normalize_zep_search_query(query: Any) -> str:
    """Return a non-empty query within Zep Cloud's endpoint limit."""

    if not isinstance(query, str):
        raise ValueError("Zep search query must be a string")
    normalized = query.strip()
    if not normalized:
        raise ValueError("Zep search query must not be empty")
    return normalized[:MAX_ZEP_SEARCH_QUERY_CHARS]


def normalize_zep_search_limit(limit: Any) -> int:
    """Clamp a search result limit to the current Zep Cloud contract."""

    try:
        normalized = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("Zep search limit must be an integer") from exc
    if normalized < 1:
        raise ValueError("Zep search limit must be at least 1")
    return min(normalized, MAX_ZEP_SEARCH_RESULTS)


@lru_cache(maxsize=4)
def _cached_zep_client(api_key: str, timeout: float) -> Zep:
    return Zep(
        api_key=api_key,
        base_url=ZEP_CLOUD_BASE_URL,
        timeout=timeout,
    )


@lru_cache(maxsize=4)
def _cached_graphiti_shim(uri: str, user: str, password: str, database: str, graphiti_factory=None):
    """缓存 GraphitiShimClient 实例（方案 D）。"""
    # 延迟导入：仅在启用 graphiti 后端时才加载 neo4j driver
    from ..services.graphiti_shim import GraphitiShimClient
    return GraphitiShimClient(uri, user, password, database=database, graphiti_factory=graphiti_factory)


def get_zep_client(api_key: str | None = None, timeout: float | None = None):
    """Return a process-shared graph client.

    根据 Config.GRAPH_BACKEND 返回：
    - 'zep'（默认）: Zep Cloud 客户端
    - 'graphiti': GraphitiShimClient（直连 Neo4j，鸭子类型兼容 Zep SDK）
    """
    backend = os.environ.get('GRAPH_BACKEND', 'zep')

    if backend == 'graphiti':
        return _get_graphiti_client()

    # --- Zep Cloud 路径 ---
    # zep-cloud gives ZEP_API_URL precedence even when base_url is explicit.
    # Reject it so this Cloud-only integration cannot silently target a
    # self-hosted or compatibility endpoint.
    if os.environ.get("ZEP_API_URL"):
        raise ValueError("ZEP_API_URL is unsupported; unset it to use Zep Cloud")

    normalized_key = (api_key or os.environ.get('ZEP_API_KEY', '') or "").strip()
    if not normalized_key:
        raise ValueError("ZEP_API_KEY 未配置")

    request_timeout = float(
        timeout if timeout is not None else ZEP_HTTP_REQUEST_TIMEOUT_SECONDS
    )
    if request_timeout <= 0:
        raise ValueError("Zep request timeout must be greater than 0")
    return _cached_zep_client(normalized_key, request_timeout)


def _get_graphiti_client():
    """构造 GraphitiShimClient（方案 D）。

    阶段 1B：如果配置了 EXTRACTION_* 和 EMBED_*，传 graphiti_factory 启用写路径；
    未配置时 graphiti_factory=None（写路径降级到本地 JSONL）。
    """
    uri = os.environ.get('NEO4J_URI', 'bolt://localhost:7687')
    user = os.environ.get('NEO4J_USER', 'neo4j')
    password = os.environ.get('NEO4J_PASSWORD', '')
    database = os.environ.get('NEO4J_DATABASE', 'neo4j')
    if not password:
        raise ValueError("NEO4J_PASSWORD 未配置（GRAPH_BACKEND=graphiti 需要）")

    # 阶段 1B：配置了抽取 LLM 和 embedder 时启用写路径
    graphiti_factory = None
    ext_key = os.environ.get('EXTRACTION_API_KEY', '').strip()
    emb_key = os.environ.get('EMBED_API_KEY', '').strip()
    if ext_key and emb_key:
        try:
            from ..services.graphiti_shim import create_graphiti_instance
            graphiti_factory = create_graphiti_instance  # 工厂函数，shim 调用时才创建实例
        except ImportError as e:
            logger.warning(f"graphiti-core 未安装，写路径降级到本地 JSONL: {e}")

    return _cached_graphiti_shim(uri, user, password, database, graphiti_factory)


def clear_zep_client_cache() -> None:
    """Clear cached clients. Intended for tests and controlled reconfiguration."""

    _cached_zep_client.cache_clear()
    _cached_graphiti_shim.cache_clear()


def is_retryable_zep_error(error: BaseException) -> bool:
    """Return whether a failed *read* is safe and useful to retry."""

    if isinstance(error, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return True
    if isinstance(error, ZepApiError):
        status_code = error.status_code
        return status_code in {408, 429} or (
            status_code is not None and 500 <= status_code <= 599
        )
    return False


def _retry_after_seconds(error: BaseException) -> float | None:
    if not isinstance(error, ZepApiError) or not error.headers:
        return None
    value = next(
        (
            header_value
            for header_name, header_value in error.headers.items()
            if header_name.lower() == "retry-after"
        ),
        None,
    )
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def call_zep_read_with_retry(
    operation: Callable[[], T],
    *,
    operation_name: str,
    max_attempts: int = 3,
    initial_delay: float = 2.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry a safe Zep read only for transport, 408, 429, or 5xx errors."""

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as error:
            if attempt == max_attempts or not is_retryable_zep_error(error):
                raise

            retry_after = _retry_after_seconds(error)
            delay = min(
                retry_after if retry_after is not None else initial_delay * (2 ** (attempt - 1)),
                max_delay,
            )
            logger.warning(
                "Zep %s attempt %s/%s failed (%s); retrying in %.1fs",
                operation_name,
                attempt,
                max_attempts,
                type(error).__name__,
                delay,
            )
            sleep(delay)

    raise AssertionError("unreachable")
