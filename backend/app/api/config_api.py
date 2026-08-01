"""
Config API 路由
提供 LLM 参数的在线查询与热更新接口（无需重建容器）
"""

import os
import threading
from flask import request, jsonify

from . import config_bp
from ..config import (
    Config,
    ENV_FILE_PATH,
    get_all_llm_config,
    set_llm_override,
    _read_env_file,
)
from ..utils.logger import get_logger
from ..utils.locale import t

logger = get_logger('mirofish.api.config')

# 允许通过 API 热更新的 LLM 字段白名单
_HOT_UPDATABLE_KEYS = {
    "LLM_MODEL_NAME",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_BOOST_MODEL_NAME",
    "LLM_BOOST_BASE_URL",
    "LLM_BOOST_API_KEY",
    "LLM_MAX_CONCURRENCY",
    "LLM_RATE_LIMIT_RETRIES",
}

# 写 .env 文件的锁（防止并发写损坏）
_env_write_lock = threading.Lock()


@config_bp.route('/llm', methods=['GET'])
def get_llm_settings():
    """获取当前生效的 LLM 配置。

    返回值中 LLM_API_KEY 做脱敏处理（只返回是否已配置 + 前4后4位）。
    """
    cfg = get_all_llm_config()
    # 脱敏：API key 只暴露是否配置 + 首尾几位
    def _mask(key_val):
        if not key_val:
            return {"configured": False, "preview": ""}
        return {"configured": True, "preview": key_val[:4] + "****" + key_val[-4:]}
    return jsonify({
        "success": True,
        "data": {
            "LLM_MODEL_NAME": cfg["LLM_MODEL_NAME"],
            "LLM_BASE_URL": cfg["LLM_BASE_URL"],
            "LLM_API_KEY": _mask(cfg["LLM_API_KEY"]),
            "LLM_BOOST_MODEL_NAME": cfg["LLM_BOOST_MODEL_NAME"],
            "LLM_BOOST_BASE_URL": cfg["LLM_BOOST_BASE_URL"],
            "LLM_BOOST_API_KEY": _mask(cfg["LLM_BOOST_API_KEY"]),
            "LLM_MAX_CONCURRENCY": cfg["LLM_MAX_CONCURRENCY"],
            "LLM_RATE_LIMIT_RETRIES": cfg["LLM_RATE_LIMIT_RETRIES"],
        }
    })


@config_bp.route('/llm', methods=['POST'])
def update_llm_settings():
    """热更新 LLM 配置。

    请求体（JSON，只传需要改的字段）：
        {
            "LLM_MODEL_NAME": "MiniMax-M2.7",          // 改主模型
            "LLM_BOOST_MODEL_NAME": "qwen3.7-plus",    // 改加速模型
            "LLM_MAX_CONCURRENCY": "8",                // 改并发数（字符串）
            ...
        }

    行为：
    1. 将改动写入运行时覆盖层（立即生效，无需重启）
    2. 持久化到 .env 文件（保证容器重启后仍生效）

    返回更新后的完整配置。
    """
    data = request.get_json() or {}
    if not data:
        return jsonify({"success": False, "error": "请求体为空"}), 400

    updated = {}
    ignored = {}
    for key, value in data.items():
        if key not in _HOT_UPDATABLE_KEYS:
            ignored[key] = "不在可热更新白名单内"
            continue
        value = str(value).strip() if value is not None else ""
        if not value:
            ignored[key] = "值为空，已忽略（删除请用 DELETE 接口）"
            continue
        set_llm_override(key, value)
        updated[key] = value
        logger.info(f"LLM 参数热更新: {key} = {value if 'API_KEY' not in key else '****'}")

    if not updated:
        return jsonify({
            "success": False,
            "error": "没有有效的可更新字段",
            "ignored": ignored,
        }), 400

    # 持久化到 .env
    persist_error = None
    try:
        _persist_to_env_file(updated)
    except Exception as e:
        persist_error = str(e)
        logger.warning(f"运行时覆盖已生效，但持久化到 .env 失败: {e}")

    return jsonify({
        "success": True,
        "data": get_all_llm_config_masked(),
        "updated": updated,
        "ignored": ignored,
        "persisted": persist_error is None,
        "persist_error": persist_error,
        "message": "配置已热更新" + ("（已持久化到 .env）" if persist_error is None else "（运行时生效，但 .env 持久化失败，重启后会丢失）"),
    })


def get_all_llm_config_masked():
    """带脱敏的完整配置（用于返回体）"""
    cfg = get_all_llm_config()
    def _mask(v):
        if not v:
            return {"configured": False, "preview": ""}
        return {"configured": True, "preview": v[:4] + "****" + v[-4:]}
    return {
        "LLM_MODEL_NAME": cfg["LLM_MODEL_NAME"],
        "LLM_BASE_URL": cfg["LLM_BASE_URL"],
        "LLM_API_KEY": _mask(cfg["LLM_API_KEY"]),
        "LLM_BOOST_MODEL_NAME": cfg["LLM_BOOST_MODEL_NAME"],
        "LLM_BOOST_BASE_URL": cfg["LLM_BOOST_BASE_URL"],
        "LLM_BOOST_API_KEY": _mask(cfg["LLM_BOOST_API_KEY"]),
        "LLM_MAX_CONCURRENCY": cfg["LLM_MAX_CONCURRENCY"],
        "LLM_RATE_LIMIT_RETRIES": cfg["LLM_RATE_LIMIT_RETRIES"],
    }


def _persist_to_env_file(updates: dict):
    """把更新写回 .env 文件。

    策略：读取现有 .env 全文，逐行替换匹配的 key；不存在则追加。
    保留注释和格式。并发安全（_env_write_lock）。
    """
    if ENV_FILE_PATH is None:
        raise RuntimeError("ENV_FILE_PATH 未设置（找不到 .env 文件）")

    with _env_write_lock:
        if not ENV_FILE_PATH.exists():
            raise FileNotFoundError(f".env 文件不存在: {ENV_FILE_PATH}")

        # 读现有内容（保留原编码和换行）
        content = ENV_FILE_PATH.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=False)

        updated_keys = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            # 跳过注释和空行
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            # 解析 KEY=VALUE
            if "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in updates:
                    new_lines.append(f"{k}={updates[k]}")
                    updated_keys.add(k)
                    continue
            new_lines.append(line)

        # 未找到的 key 追加到末尾
        for k, v in updates.items():
            if k not in updated_keys:
                new_lines.append(f"{k}={v}")

        # 写回（CRLF 兼容 Windows 原文件风格）
        new_content = "\n".join(new_lines)
        if not new_content.endswith("\n"):
            new_content += "\n"
        ENV_FILE_PATH.write_text(new_content, encoding="utf-8")
        logger.info(f"已持久化 {len(updates)} 个 LLM 参数到 {ENV_FILE_PATH}")
