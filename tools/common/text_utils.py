# tools/common/text_utils.py
"""文本处理工具模块 - Token估算与智能裁剪"""

import re
from functools import wraps
from typing import Any, Callable, List

try:
    import jieba

    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False

__all__ = [
    "MemoryPart",
    "SmartMemoryTrimmer",
    "TokenEstimator",
    "compact_result",
    "get_tool_max_tokens",
    "set_global_config",
    "tokenize_for_bm25",
]

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

# 全局配置引用（在 main.py 初始化时设置）
_global_config = None


def set_global_config(config):
    """设置全局配置对象（供 compact_result 装饰器使用）"""
    global _global_config
    _global_config = config


def get_tool_max_tokens() -> int:
    """获取工具最大 Token 阈值（从全局配置中读取，默认 8000）"""
    if _global_config and hasattr(_global_config, "tool_max_tokens"):
        return _global_config.tool_max_tokens
    return 8000


def compact_result(max_tokens: int | Callable[[], int] = 8000, strategy: str = "head_tail"):
    """
    微压缩防爆装饰器 (Micro-compact)
    用于拦截工具返回的超长文本，防止撑爆 LLM 上下文窗口。

    Args:
        max_tokens: 允许的最大 Token 数量（可以是固定整数，也可以是一个返回整数的可调用对象）
        strategy: 压缩策略，"truncate" (直接截断尾部) 或 "head_tail" (保留头尾)
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            result = await func(*args, **kwargs)

            # 仅处理字符串类型的结果
            if not isinstance(result, str) or not result:
                return result

            # 动态计算阈值：如果 max_tokens 是可调用对象，则调用它获取实际值
            actual_max_tokens = max_tokens() if callable(max_tokens) else max_tokens

            current_tokens = TokenEstimator.estimate_tokens(result)
            if current_tokens <= actual_max_tokens:
                return result

            logger.warning(
                f"[Micro-compact] 工具 {func.__name__} 返回结果过长 ({current_tokens} tokens)，执行 {strategy} 压缩至 {actual_max_tokens} tokens。"
            )

            if strategy == "truncate":
                # 粗略估算：1 token 约等于 1.5 个字符
                target_chars = int(actual_max_tokens * 1.5)
                truncated = result[:target_chars]
                return truncated + "\n\n[系统提示：工具返回内容过长，已在尾部截断以节省 Token]"

            elif strategy == "head_tail":
                target_chars = int(actual_max_tokens * 1.5)
                head_chars = int(target_chars * 0.4)
                tail_chars = int(target_chars * 0.6)

                head = result[:head_chars]
                tail = result[-tail_chars:] if tail_chars > 0 else ""
                return head + "\n\n...\n[系统提示：工具返回内容过长，中间部分已折叠以节省 Token]\n...\n\n" + tail

            return result

        return wrapper

    return decorator


class TokenEstimator:
    """Token估算器 - 用于估算文本的token数量"""

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        估算文本的token数量（粗略估算）

        使用简单的规则：
        - 中文字符：每个字符约1token
        - 英文单词：每个单词约1.3tokens
        - 其他字符：混合估算

        Args:
            text: 要估算的文本

        Returns:
            估算的token数量
        """
        if not text:
            return 0

        chinese_chars = re.findall(r"[\u4e00-\u9fff]", text)
        chinese_count = len(chinese_chars)

        remaining_text = re.sub(r"[\u4e00-\u9fff]", "", text)

        english_words = re.findall(r"\b\w+\b", remaining_text)
        english_count = len(english_words)

        other_chars = len(re.findall(r"[^\w\s]", remaining_text)) + len(re.findall(r"\d", remaining_text))

        total_tokens = chinese_count + int(english_count * 1.3) + int(other_chars * 0.5)

        return max(1, total_tokens)

    @staticmethod
    def estimate_prompt_tokens(system_prompt: str, user_prompt: str = "") -> int:
        """
        估算完整提示词的token数量

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词（可选）

        Returns:
            估算的总token数量
        """
        system_tokens = TokenEstimator.estimate_tokens(system_prompt)
        user_tokens = TokenEstimator.estimate_tokens(user_prompt) if user_prompt else 0
        return system_tokens + user_tokens


class MemoryPart:
    """记忆片段 - 带有优先级和内容的记忆部分"""

    def __init__(self, name: str, content: str, priority: int):
        self.name = name
        self.content = content
        self.priority = priority
        self.tokens = TokenEstimator.estimate_tokens(content)

    def __repr__(self) -> str:
        return f"MemoryPart(name={self.name}, priority={self.priority}, tokens={self.tokens})"


class SmartMemoryTrimmer:
    """智能记忆裁剪器 - 根据优先级和token预算裁剪记忆"""

    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens
        self.parts: List[MemoryPart] = []

    def add_part(self, name: str, content: str, priority: int):
        """
        添加记忆片段

        Args:
            name: 片段名称
            content: 片段内容
            priority: 优先级（数字越大优先级越高）
        """
        if content:
            self.parts.append(MemoryPart(name, content, priority))

    def trim(self) -> tuple[List[MemoryPart], int]:
        """
        执行智能裁剪

        Returns:
            (保留的记忆片段列表, 总token数)
        """
        if not self.parts:
            return [], 0

        sorted_parts = sorted(self.parts, key=lambda x: x.priority, reverse=True)

        selected_parts: List[MemoryPart] = []
        total_tokens = 0

        for part in sorted_parts:
            if total_tokens + part.tokens <= self.max_tokens:
                selected_parts.append(part)
                total_tokens += part.tokens
            else:
                trimmed_content = self._trim_content_to_tokens(part.content, self.max_tokens - total_tokens)
                if trimmed_content:
                    trimmed_part = MemoryPart(part.name, trimmed_content, part.priority)
                    selected_parts.append(trimmed_part)
                    total_tokens += trimmed_part.tokens

        name_to_index = {}
        for i, part in enumerate(self.parts):
            name_to_index[part.name] = i
        selected_parts.sort(key=lambda x: name_to_index.get(x.name, 999))

        logger.debug(
            f"[TokenControl] 智能裁剪完成: 保留 {len(selected_parts)}/{len(self.parts)} 个部分, "
            f"总tokens: {total_tokens}/{self.max_tokens}"
        )

        return selected_parts, total_tokens

    def _trim_content_to_tokens(self, content: str, target_tokens: int) -> str:
        """
        将内容裁剪到指定token数

        Args:
            content: 原始内容
            target_tokens: 目标token数

        Returns:
            裁剪后的内容
        """
        if target_tokens <= 0:
            return ""

        current_tokens = TokenEstimator.estimate_tokens(content)

        if current_tokens <= target_tokens:
            return content

        target_ratio = target_tokens / current_tokens * 0.85
        target_chars = int(len(content) * target_ratio)

        if target_chars <= 0:
            return ""

        trimmed = content[:target_chars]

        cut_points = [
            trimmed.rfind("\n\n"),
            trimmed.rfind("\n"),
            trimmed.rfind("。"),
            trimmed.rfind("！"),
            trimmed.rfind("？"),
            trimmed.rfind(". "),
            trimmed.rfind("! "),
            trimmed.rfind("? "),
        ]

        best_cut = max(cut_points)
        if best_cut > 0:
            trimmed = trimmed[: best_cut + 1]

        if trimmed and len(trimmed) < len(content):
            trimmed += "\n...（内容已截断以节省Token）"

        return trimmed


def tokenize_for_bm25(text: str) -> List[str]:
    """
    中英文分词（用于BM25）

    使用 jieba 进行中文分词，英文按空格分词。
    如果 jieba 不可用，则回退到简单的正则分词。

    Args:
        text: 待分词文本

    Returns:
        分词后的token列表
    """
    if not text:
        return []

    if JIEBA_AVAILABLE:
        # 使用 jieba 进行中文分词
        # cut_for_search 模式适合搜索引擎，会对长词再切分
        tokens = list(jieba.cut_for_search(text.lower()))
        # 过滤掉纯空白字符
        return [t.strip() for t in tokens if t.strip()]
    else:
        # 回退到简单的正则分词
        return re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fa5]", text.lower())


def jaccard_similarity(text1: str, text2: str) -> float:
    """
    计算 Jaccard 相似度（基于分词后的 token 集合）

    Args:
        text1: 文本1
        text2: 文本2

    Returns:
        相似度分数 (0.0 - 1.0)
    """
    tokens1 = set(tokenize_for_bm25(text1))
    tokens2 = set(tokenize_for_bm25(text2))

    if not tokens1 or not tokens2:
        return 0.0

    intersection = len(tokens1 & tokens2)
    union = len(tokens1 | tokens2)

    return intersection / union if union else 0.0
