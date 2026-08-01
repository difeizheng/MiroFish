"""
LLM 智能实体筛选器

背景：原 filter_defined_entities 纯规则筛选（类型 + 黑名单 + 度数排序截断），
完全不考虑模拟场景——度数高的实体（如"营业收入""存货"等财务概念）不一定是
好的舆论 Agent，而度数中等的关键人物/机构可能更会在社交平台发声。

本服务在规则过滤后的候选池上，用 LLM 结合「现实种子 + 模拟需求」给每个
候选实体打分（0-10，作为舆论模拟 Agent 的适合度），按分数选出目标数量。

设计要点：
- 打分结果按内容哈希缓存（uploads/entity_selection/），同一图谱+种子+模型
  重复 prepare 不重复消耗 token
- 任何 LLM 失败都降级为按度数截断（保证 prepare 流程不被打断）
- 默认走 BOOST 模型（打分是廉价批量任务），未配置则回退主模型
"""

import hashlib
import json
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from ..config import Config, _get_llm_config
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger

logger = get_logger('mirofish.entity_selector')

# 打分缓存目录
_CACHE_DIR = os.path.join(os.path.dirname(__file__), '../../uploads/entity_selection')
_cache_lock = threading.Lock()


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def llm_select_enabled() -> bool:
    """是否启用 LLM 智能筛选（env: AGENT_LLM_SELECT，默认开）"""
    return os.environ.get('AGENT_LLM_SELECT', '1').strip().lower() not in ('0', 'false', 'no')


def load_reality_seed(project_id: str, max_chars: int = 6000) -> str:
    """
    加载项目的现实种子文本。

    来源：Step1 上传到项目的 .md/.txt 文件（招股书等 PDF 是图谱语料，不是种子；
    种子是用户写的 markdown 事实汇编）。多个文件按顺序拼接，截断到 max_chars。

    Args:
        project_id: 项目ID
        max_chars: 总字符上限（控制 prompt 长度）

    Returns:
        种子文本，找不到返回空字符串
    """
    try:
        from ..models.project import ProjectManager
        files = ProjectManager.get_project_files(project_id)
    except Exception as e:
        logger.warning(f"读取项目文件列表失败 project={project_id}: {e}")
        return ""

    parts = []
    total = 0
    for path in sorted(files):
        ext = os.path.splitext(path)[1].lower()
        if ext not in ('.md', '.txt', '.markdown'):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read().strip()
        except Exception as e:
            logger.warning(f"读取种子文件失败 {path}: {e}")
            continue
        if not text:
            continue
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break

    seed = "\n\n".join(parts)[:max_chars]
    if seed:
        logger.info(f"加载现实种子: project={project_id}, {len(parts)} 个文件, {len(seed)} 字符")
    else:
        logger.info(f"项目 {project_id} 无 .md/.txt 种子文件")
    return seed


def _make_llm() -> LLMClient:
    """构造打分用 LLM 客户端：优先 BOOST 模型（廉价批量任务），未配置回退主模型。"""
    boost_model = _get_llm_config('LLM_BOOST_MODEL_NAME')
    if boost_model:
        return LLMClient(
            api_key=_get_llm_config('LLM_BOOST_API_KEY') or Config.LLM_API_KEY,
            base_url=_get_llm_config('LLM_BOOST_BASE_URL') or Config.LLM_BASE_URL,
            model=boost_model,
        )
    return LLMClient()


def _cache_key(graph_id: str, uuids: List[str], seed_text: str, requirement: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(requirement.encode())
    h.update(hashlib.sha256(seed_text.encode()).hexdigest().encode())
    for u in sorted(uuids):
        h.update(u.encode())
    return h.hexdigest()[:24]


def _cache_path(graph_id: str, key: str) -> str:
    safe_gid = "".join(c if c.isalnum() or c in '-_' else '_' for c in graph_id)
    return os.path.join(_CACHE_DIR, f"{safe_gid}_{key}.json")


def _load_cache(graph_id: str, key: str) -> Optional[Dict[str, float]]:
    path = _cache_path(graph_id, key)
    with _cache_lock:
        try:
            if not os.path.exists(path):
                return None
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            scores = data.get('scores')
            if isinstance(scores, dict):
                logger.info(f"实体打分缓存命中: {path} ({len(scores)} 条)")
                return {k: float(v) for k, v in scores.items()}
        except Exception as e:
            logger.warning(f"读取打分缓存失败 {path}: {e}")
    return None


def _save_cache(graph_id: str, key: str, scores: Dict[str, float], model: str) -> None:
    path = _cache_path(graph_id, key)
    with _cache_lock:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({"model": model, "scores": scores}, f, ensure_ascii=False)
            logger.info(f"实体打分已缓存: {path} ({len(scores)} 条)")
        except Exception as e:
            logger.warning(f"写入打分缓存失败 {path}: {e}")


def evaluate_agents(
    profiles: List[Dict[str, Any]],
    seed_text: str,
    requirement: str,
    llm: Optional[LLMClient] = None,
) -> Dict[str, Any]:
    """
    LLM 评估当前 Agent 阵容质量（供「评估」按钮）。

    Args:
        profiles: Agent 人设列表（reddit/twitter 格式的 dict）
        seed_text: 现实种子文本
        requirement: 模拟需求文本
        llm: 可选注入的 LLM 客户端（测试用）

    Returns:
        {
          "overall_score": 0-100,
          "summary": "一句话总评",
          "dimensions": [{"name": str, "score": 0-10, "comment": str}],
          "missing_roles": [str],
          "redundant": [str],
          "suggestions": [str]
        }
    """
    client = llm or _make_llm()

    lines = []
    for i, p in enumerate(profiles[:200], start=1):
        name = p.get('realname') or p.get('username') or p.get('name') or f'agent_{i}'
        profession = p.get('profession') or ''
        bio = (p.get('bio') or '').replace('\n', ' ')[:60]
        lines.append(f"{i}. {name}（{profession}）{('— ' + bio) if bio else ''}")

    seed_excerpt = seed_text[:2500] if seed_text else "（无现实种子）"
    req_excerpt = requirement[:800] if requirement else "（无模拟需求描述）"

    messages = [
        {
            "role": "system",
            "content": (
                "你是舆论模拟的选角总监。给定模拟场景和已选出的 Agent 阵容，"
                "评估阵容质量。只输出 JSON：\n"
                "{\n"
                "  \"overall_score\": 0-100 的整数,\n"
                "  \"summary\": \"一句话总评\",\n"
                "  \"dimensions\": [\n"
                "    {\"name\": \"利益相关方覆盖\", \"score\": 0-10, \"comment\": \"...\"},\n"
                "    {\"name\": \"立场多样性\", \"score\": 0-10, \"comment\": \"...\"},\n"
                "    {\"name\": \"发声活跃度梯度\", \"score\": 0-10, \"comment\": \"...\"},\n"
                "    {\"name\": \"普通网民代表性\", \"score\": 0-10, \"comment\": \"...\"}\n"
                "  ],\n"
                "  \"missing_roles\": [\"场景中重要但缺失的角色类型\"],\n"
                "  \"redundant\": [\"明显重复/冗余的角色\"],\n"
                "  \"suggestions\": [\"具体可操作的调整建议\"]\n"
                "}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"【模拟需求】\n{req_excerpt}\n\n"
                f"【现实种子】\n{seed_excerpt}\n\n"
                f"【Agent 阵容（{len(lines)} 个）】\n" + "\n".join(lines)
            ),
        },
    ]

    result = client.chat_json(messages, temperature=0.3, max_tokens=4096, max_attempts=2)
    if not isinstance(result.get('overall_score'), (int, float)):
        raise ValueError(f"评估结果缺少 overall_score: {str(result)[:200]}")
    result['overall_score'] = max(0, min(100, int(result['overall_score'])))
    for key, default in [('summary', ''), ('dimensions', []), ('missing_roles', []),
                         ('redundant', []), ('suggestions', [])]:
        result.setdefault(key, default)
    return result


class EntitySelector:
    """LLM 实体打分筛选器"""

    def __init__(self, llm: Optional[LLMClient] = None):
        self._llm = llm

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = _make_llm()
        return self._llm

    def select(
        self,
        entities: List[Any],
        target_count: int,
        seed_text: str = "",
        requirement: str = "",
        graph_id: Optional[str] = None,
    ) -> Tuple[List[Any], Dict[str, Any]]:
        """
        从候选池中 LLM 打分选出 target_count 个实体。

        Args:
            entities: 候选实体池（EntityNode 列表，已按度数降序）
            target_count: 目标数量
            seed_text: 现实种子文本
            requirement: 模拟需求文本
            graph_id: 图谱ID（用于缓存；None 则不缓存）

        Returns:
            (选中的实体列表, 元信息 {source: llm|cache|fallback|noop, pool_size, ...})
        """
        pool_size = len(entities)
        if pool_size <= target_count:
            return entities, {"source": "noop", "pool_size": pool_size,
                              "reason": "候选池不大于目标数量，无需筛选"}

        uuids = [e.uuid for e in entities]
        model = self.llm.model

        # 1. 缓存
        key = None
        if graph_id:
            key = _cache_key(graph_id, uuids, seed_text, requirement, model)
            cached = _load_cache(graph_id, key)
            if cached:
                return self._pick(entities, cached, target_count), {
                    "source": "cache", "pool_size": pool_size,
                }

        # 2. LLM 打分
        try:
            scores = self._score_all(entities, seed_text, requirement)
        except Exception as e:
            logger.error(f"LLM 实体打分失败，降级为按度数截断: {e}")
            return entities[:target_count], {
                "source": "fallback", "pool_size": pool_size, "error": str(e),
            }

        if graph_id and key:
            _save_cache(graph_id, key, scores, model)

        return self._pick(entities, scores, target_count), {
            "source": "llm", "pool_size": pool_size,
        }

    @staticmethod
    def _pick(entities: List[Any], scores: Dict[str, float], target_count: int) -> List[Any]:
        """按分数降序选出 target_count 个；同分保持原顺序（度数降序）。缺分按 5 分计。"""
        indexed = list(enumerate(entities))
        indexed.sort(key=lambda iv: (-scores.get(iv[1].uuid, 5.0), iv[0]))
        return [e for _, e in indexed[:target_count]]

    def _score_all(
        self,
        entities: List[Any],
        seed_text: str,
        requirement: str,
    ) -> Dict[str, float]:
        """分批打分，返回 {uuid: score}"""
        batch_size = max(10, _env_int('AGENT_LLM_BATCH', 40))
        scores: Dict[str, float] = {}
        total_batches = (len(entities) + batch_size - 1) // batch_size

        for bi in range(total_batches):
            batch = entities[bi * batch_size:(bi + 1) * batch_size]
            logger.info(f"LLM 实体打分: 批次 {bi + 1}/{total_batches} ({len(batch)} 个)")
            batch_scores = self._score_batch(batch, seed_text, requirement, index_offset=bi * batch_size)
            scores.update(batch_scores)

        return scores

    def _score_batch(
        self,
        batch: List[Any],
        seed_text: str,
        requirement: str,
        index_offset: int = 0,
    ) -> Dict[str, float]:
        """单批打分。实体用序号引用（省 token 且避免 uuid 转写错误）。"""
        lines = []
        for i, e in enumerate(batch, start=1):
            entity_type = next((l for l in (e.labels or []) if l not in ("Entity", "Node")), "实体")
            summary = (e.summary or "").replace("\n", " ")[:80]
            lines.append(f"{i}. {e.name}（{entity_type}）{('— ' + summary) if summary else ''}")

        seed_excerpt = seed_text[:3000] if seed_text else "（无现实种子）"
        req_excerpt = requirement[:1000] if requirement else "（无模拟需求描述）"

        messages = [
            {
                "role": "system",
                "content": (
                    "你是舆论模拟的选角导演。给定一个模拟场景和一批候选实体，"
                    "评估每个实体作为模拟 Agent 的适合度（0-10 分）。\n"
                    "评分标准：\n"
                    "- 与模拟场景的相关性（会不会关心/参与这个话题）\n"
                    "- 发声可能性（这类主体在社交媒体上表达观点的可能性，"
                    "普通网民/投资者/媒体/分析师通常高，抽象概念/财务科目/地理名词通常低）\n"
                    "- 代表性（是否代表某类重要利益相关方视角）\n"
                    "注意：抽象概念（如「营业收入」「存货」）即使信息量大也应打低分；"
                    "具体的人、公司、机构、媒体、群体打高分。\n"
                    "只输出 JSON：{\"scores\": [{\"i\": 序号, \"s\": 分数}, ...]}，覆盖全部序号。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"【模拟需求】\n{req_excerpt}\n\n"
                    f"【现实种子（背景事实）】\n{seed_excerpt}\n\n"
                    f"【候选实体】\n" + "\n".join(lines)
                ),
            },
        ]

        result = self.llm.chat_json(messages, temperature=0.2, max_tokens=4096, max_attempts=2)
        raw_scores = result.get("scores")
        if not isinstance(raw_scores, list):
            raise ValueError(f"LLM 返回缺少 scores 数组: {str(result)[:200]}")

        scores: Dict[str, float] = {}
        for item in raw_scores:
            try:
                idx = int(item.get("i"))
                s = float(item.get("s"))
            except (TypeError, ValueError, AttributeError):
                continue
            if 1 <= idx <= len(batch):
                scores[batch[idx - 1].uuid] = max(0.0, min(10.0, s))

        if not scores:
            raise ValueError("LLM 打分结果全部无法解析")

        logger.info(
            f"批次打分完成: {len(scores)}/{len(batch)} 有效, "
            f"最高 {max(scores.values()):.1f}, 最低 {min(scores.values()):.1f}"
        )
        return scores
