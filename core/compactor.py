from __future__ import annotations

# core/compactor.py
"""Scriptor 记忆压缩模块

Prompt模板已迁移至 tools/config/compactor_prompts.py
"""

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from tools.config.compactor_prompts import (
    COMPACT_PROMPT,
    EXPERIENCE_EXTRACTION_PROMPT,
    PROFILE_REFINEMENT_PROMPT,
    SLEEP_CONSOLIDATION_PROMPT,
)


class Compactor:
    """记忆压缩器 - 主动式记忆管理而非被动衰减"""

    COMPACT_PROMPT = COMPACT_PROMPT

    PROFILE_REFINEMENT_PROMPT = PROFILE_REFINEMENT_PROMPT

    EXPERIENCE_EXTRACTION_PROMPT = EXPERIENCE_EXTRACTION_PROMPT

    SLEEP_CONSOLIDATION_PROMPT = SLEEP_CONSOLIDATION_PROMPT

    def __init__(self, config, context):
        self.config = config
        self.context = context

    async def _llm_generate(self, prompt: str, timeout: float = 30.0) -> str | None:
        """统一 LLM 调用入口，使用 AstrBot v4.x 推荐的 llm_generate 接口"""
        try:
            provider_id = await self.context.get_current_chat_provider_id(None)
            response = await asyncio.wait_for(
                self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt),
                timeout=timeout,
            )
            if response and response.completion_text:
                return response.completion_text.strip()
            return None
        except asyncio.TimeoutError:
            logger.warning("[Scriptor] LLM 调用超时")
            return None
        except Exception as e:
            logger.error(f"[Scriptor] LLM 调用失败: {e}")
            return None

    async def compact(self, messages: list, previous_summary: str = "") -> str:
        """
        压缩对话历史为摘要

        Args:
            messages: 对话消息列表
            previous_summary: 之前的摘要

        Returns:
            压缩后的摘要
        """
        if not messages:
            return previous_summary

        history_text = self._format_messages(messages)

        prompt = self.COMPACT_PROMPT.format(previous_summary=previous_summary or "(无)", history=history_text)

        result = await self._llm_generate(prompt)
        return result if result else previous_summary

    async def refine_profile(self, messages: list, current_profile: str) -> str:
        """
        基于对话历史精炼用户画像
        """
        if not messages:
            return ""

        history_text = self._format_messages(messages)
        prompt = self.PROFILE_REFINEMENT_PROMPT.format(current_profile=current_profile or "(无)", history=history_text)

        result = await self._llm_generate(prompt)
        if result and "无更新" not in result:
            return result
        return ""

    async def extract_experience(self, messages: list) -> str:
        """
        基于对话历史提取经验法则
        """
        if not messages:
            return ""

        history_text = self._format_messages(messages)
        prompt = self.EXPERIENCE_EXTRACTION_PROMPT.format(history=history_text)

        result = await self._llm_generate(prompt)
        if result and "无经验" not in result:
            return result
        return ""

    async def consolidate_sleep(self, recent_memories_text: str) -> str:
        """
        执行睡眠巩固，提取长期模式
        """
        if not recent_memories_text:
            return ""

        prompt = self.SLEEP_CONSOLIDATION_PROMPT.format(recent_memories=recent_memories_text)

        result = await self._llm_generate(prompt)
        if result and "无需要巩固的内容" not in result:
            return result
        return ""

    async def merge_memories(self, memories: List[str]) -> Tuple[str, float]:
        """
        使用 LLM 合并相似记忆，并累加强度

        Args:
            memories: 相似记忆内容列表

        Returns:
            (合并后的记忆内容, 累加的有用性分数)
        """
        if not memories or len(memories) < 2:
            return memories[0] if memories else "", 0.0

        prompt = f"""你是一个记忆整理专家。请将以下几条相似或相关的记忆合并为一条精炼的记忆。

要求：
1. 提取所有核心事实，去除重复信息。
2. 如果存在冲突，保留最新的状态。
3. 保持客观的第三人称陈述。
4. 只输出合并后的内容，不要输出其他解释。

待合并记忆：
{chr(10).join(f"- {m}" for m in memories)}

合并结果："""

        result = await self._llm_generate(prompt)
        if result:
            total_score = 5.0 + (len(memories) - 1) * 2.0
            return result, min(15.0, total_score)

        return memories[0], 0.0

    def _format_messages(self, messages: list) -> str:
        """格式化消息为文本"""
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if role == "system":
                continue

            lines.append(f"{role}: {content[:200]}")

        return "\n".join(lines)

    async def generate_summary(self, content: str, summary_type: str = "general") -> str:
        """
        生成内容摘要

        Args:
            content: 原始内容
            summary_type: 摘要类型

        Returns:
            摘要内容
        """
        prompt = f"请将以下内容提炼为关键信息点：\n\n{content[:1000]}"

        result = await self._llm_generate(prompt)
        return result if result else content

    async def analyze_maintenance_needs(self, data_dir: Path, identity_manager, group_manager) -> str:
        """
        分析记忆维护需求，生成半自动维护建议报告

        Returns:
            维护建议报告 (Markdown 格式)
        """
        suggestions = []
        issues = []

        profiles_dir = data_dir / "profiles"
        if profiles_dir.exists():
            for profile_dir in profiles_dir.iterdir():
                if not profile_dir.is_dir():
                    continue

                uid = profile_dir.name
                memory_dir = profile_dir / "memory"

                if memory_dir.exists():
                    memories = sorted(memory_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)

                    old_memories = []
                    for md in memories:
                        try:
                            date_str = md.stem
                            file_date = datetime.strptime(date_str, "%Y-%m-%d")
                            days_ago = (datetime.now() - file_date).days

                            if days_ago > 30:
                                content = md.read_text(encoding="utf-8")
                                score_match = re.search(r"\[Score:\s*([\d\.]+)\]", content)
                                score = float(score_match.group(1)) if score_match else 0

                                if score < 3:
                                    old_memories.append((md, days_ago, score))
                        except:
                            pass

                    if len(old_memories) > 5:
                        issues.append(f"⚠️ 用户 {uid}: 发现 {len(old_memories)} 条可能过期的低价值记忆，建议清理")

        all_contents = {}
        if profiles_dir.exists():
            for profile_dir in profiles_dir.iterdir():
                if not profile_dir.is_dir():
                    continue
                memory_dir = profile_dir / "memory"
                if memory_dir.exists():
                    for md in memory_dir.glob("*.md"):
                        content = md.read_text(encoding="utf-8")
                        key = content[:50]
                        if key in all_contents:
                            issues.append(f"📝 可能重复的记忆: {md.name} 与 {all_contents[key]}")
                        else:
                            all_contents[key] = md.name

        report_lines = ["# 🔧 记忆维护建议报告", "", f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]

        if not issues and not suggestions:
            report_lines.append("✅ 记忆状态良好，无需特殊维护。")
        else:
            if issues:
                report_lines.append("## 🚨 需要关注的问题")
                for issue in issues:
                    report_lines.append(f"- {issue}")
                report_lines.append("")

            if suggestions:
                report_lines.append("## 💡 优化建议")
                for suggestion in suggestions:
                    report_lines.append(f"- {suggestion}")
                report_lines.append("")

            report_lines.append("---")
            report_lines.append("💡 以上为 AI 生成的维护建议，请确认后手动执行相关操作。")
            report_lines.append("   执行命令如 `/memory cleanup` 可一键清理标记的记忆。")

        return "\n".join(report_lines)
