from __future__ import annotations

# tools/common/json_parser.py
"""JSON解析工具模块"""

import json
import re
from typing import Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


def extract_json_from_llm_output(text: str) -> Optional[str]:
    """
    从 LLM 输出中提取纯 JSON 字符串，清洗 Markdown 标记

    处理情况：
    - ```json ... ```
    - ```JSON ... ```
    - ``` ... ```
    - 直接输出 JSON

    Args:
        text: LLM 原始输出

    Returns:
        提取出的 JSON 字符串，失败返回 None
    """
    if not text:
        return None

    text = text.strip()

    json_match = re.search(r"```(?:json|JSON)?\s*([\s\S]*?)\s*```", text)
    if json_match:
        return json_match.group(1).strip()

    first_brace = text.find("{")
    first_bracket = text.find("[")

    if first_brace == -1 and first_bracket == -1:
        return None

    # 找到第一个出现的 JSON 结构起始位置
    if first_brace != -1 and first_bracket != -1:
        start_idx = min(first_brace, first_bracket)
    else:
        start_idx = max(first_brace, first_bracket)

    # 提取第一个完整的 JSON 结构（处理连体 JSON 情况，如 {"a":1}{"b":2}）
    if text[start_idx] == "{":
        balance = 0
        end_idx = -1
        for i in range(start_idx, len(text)):
            if text[i] == "{":
                balance += 1
            elif text[i] == "}":
                balance -= 1
                if balance == 0:
                    end_idx = i + 1
                    break
    else:
        balance = 0
        end_idx = -1
        for i in range(start_idx, len(text)):
            if text[i] == "[":
                balance += 1
            elif text[i] == "]":
                balance -= 1
                if balance == 0:
                    end_idx = i + 1
                    break

    if end_idx != -1:
        return text[start_idx:end_idx].strip()

    return None


def safe_json_loads(json_str: str, default=None):
    """
    安全的 JSON 解析，带有容错处理

    Args:
        json_str: JSON 字符串
        default: 解析失败时的默认值

    Returns:
        解析后的对象或 default
    """
    try:
        extracted = extract_json_from_llm_output(json_str)
        if extracted:
            return json.loads(extracted)
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error(f"[Scriptor] JSON 解析失败: {e}")
        logger.debug(f"[Scriptor] 原始内容: {json_str[:200]}")
        return default
    except Exception as e:
        logger.error(f"[Scriptor] 解析异常: {e}")
        return default
