# tools/skill_tool.py
"""
SkillTool 技能宏调用系统

功能：
1. 技能注册表（从 skills/ 目录自动加载）
2. Inline 同步执行模式（注入技能Prompt，限制工具集）
3. Forked 异步后台执行模式
4. 冷却机制防止重复调用
5. 智能推荐（根据上下文自动推荐技能）
"""

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


class ExecutionMode(Enum):
    """执行模式"""

    INLINE = "inline"
    FORKED = "forked"


@dataclass
class SkillDefinition:
    """技能定义 (v2.1 极简版)"""

    name: str
    display_name: str
    description: str
    full_prompt: str

    when_to_use: str = ""  # 核心新增：触发场景描述（用于智能推荐）
    allowed_tools: List[str] = field(default_factory=list)  # 核心新增：工具白名单

    required_tools: List[str] = field(default_factory=list)
    optional_tools: List[str] = field(default_factory=list)
    execution_mode: ExecutionMode = ExecutionMode.INLINE
    estimated_tokens: int = 2000
    triggers: List[str] = field(default_factory=list)
    cooldown_seconds: int = 30
    category: str = "general"
    version: str = "1.0"
    author: str = ""


@dataclass
class SkillTask:
    """技能任务（Forked 模式）"""

    task_id: str
    skill_name: str
    instruction: str
    created_at: float
    status: str = "pending"
    result: Optional[str] = None
    error: Optional[str] = None
    completed_at: Optional[float] = None


@dataclass
class CooldownEntry:
    """冷却条目"""

    session_id: str
    skill_name: str
    last_executed: float
    count: int = 1


class CooldownManager:
    """冷却管理器"""

    def __init__(self, default_cooldown: int = 30):
        self.default_cooldown = default_cooldown
        self._cooldowns: Dict[str, CooldownEntry] = {}
        self._max_entries = 1000

    def can_execute(self, skill_name: str, session_id: str) -> bool:
        key = f"{session_id}:{skill_name}"

        if key not in self._cooldowns:
            return True

        entry = self._cooldowns[key]
        elapsed = time.time() - entry.last_executed

        return elapsed >= self.default_cooldown

    def record_execution(self, skill_name: str, session_id: str):
        key = f"{session_id}:{skill_name}"
        now = time.time()

        if len(self._cooldowns) >= self._max_entries:
            oldest_key = min(self._cooldowns.keys(), key=lambda k: self._cooldowns[k].last_executed)
            del self._cooldowns[oldest_key]

        if key in self._cooldowns:
            self._cooldowns[key].last_executed = now
            self._cooldowns[key].count += 1
        else:
            self._cooldowns[key] = CooldownEntry(session_id=session_id, skill_name=skill_name, last_executed=now)

    def get_remaining_cooldown(self, skill_name: str, session_id: str) -> float:
        key = f"{session_id}:{skill_name}"

        if key not in self._cooldowns:
            return 0.0

        entry = self._cooldowns[key]
        elapsed = time.time() - entry.last_executed
        remaining = self.default_cooldown - elapsed

        return max(0, remaining)

    def clear(self, skill_name: Optional[str] = None, session_id: Optional[str] = None):
        if skill_name and session_id:
            key = f"{session_id}:{skill_name}"
            self._cooldowns.pop(key, None)
        elif skill_name:
            keys_to_remove = [k for k in self._cooldowns if k.endswith(f":{skill_name}")]
            for key in keys_to_remove:
                del self._cooldowns[key]
        elif session_id:
            keys_to_remove = [k for k in self._cooldowns if k.startswith(f"{session_id}:")]
            for key in keys_to_remove:
                del self._cooldowns[key]
        else:
            self._cooldowns.clear()


class SkillTaskStore:
    """任务存储"""

    def __init__(self):
        self._tasks: Dict[str, SkillTask] = {}
        self._max_tasks = 100

    def add(self, task: SkillTask):
        if len(self._tasks) >= self._max_tasks:
            oldest_task = min(self._tasks.values(), key=lambda t: t.created_at)
            del self._tasks[oldest_task.task_id]

        self._tasks[task.task_id] = task

    def get(self, task_id: str) -> Optional[SkillTask]:
        return self._tasks.get(task_id)

    def complete(self, task_id: str, result: str):
        if task_id in self._tasks:
            self._tasks[task_id].status = "completed"
            self._tasks[task_id].result = result
            self._tasks[task_id].completed_at = time.time()

    def fail(self, task_id: str, error: str):
        if task_id in self._tasks:
            self._tasks[task_id].status = "failed"
            self._tasks[task_id].error = error
            self._tasks[task_id].completed_at = time.time()

    def list_by_session(self, session_id: str) -> List[SkillTask]:
        return [t for t in self._tasks.values() if getattr(t, "session_id", "") == session_id]

    def list_all(self) -> List[SkillTask]:
        """v2.1: 列出所有任务"""
        return list(self._tasks.values())

    def cleanup_old(self, max_age_seconds: int = 3600):
        now = time.time()
        old_keys = [tid for tid, task in self._tasks.items() if now - task.created_at > max_age_seconds]
        for key in old_keys:
            del self._tasks[key]


class SkillRegistry:
    """技能注册表 (v2.1 双级加载版)"""

    def __init__(self, skills_dir: Optional[Path] = None, custom_skills_dir: Optional[Path] = None):
        self._skills: Dict[str, SkillDefinition] = {}
        self._triggers_index: Dict[str, Set[str]] = {}
        self.skills_dir = skills_dir
        self.custom_skills_dir = custom_skills_dir
        self._skill_sources: Dict[str, str] = {}  # name → source ('system' / 'custom')

        if skills_dir and skills_dir.exists():
            self._load_skills(skills_dir, source="system")

        if custom_skills_dir and custom_skills_dir.exists():
            self._load_skills(custom_skills_dir, source="custom")

    def _load_skills(self, skills_dir: Path, source: str = "system"):
        """
        从目录加载所有技能（支持双级覆盖）

        Args:
            skills_dir: 技能目录路径
            source: 来源标识 ('system' 或 'custom')
        """
        if not skills_dir.exists():
            logger.warning(f"[SkillTool] 技能目录不存在: {skills_dir}")
            return

        for skill_folder in sorted(skills_dir.iterdir()):
            if not skill_folder.is_dir():
                continue

            skill_md = skill_folder / "SKILL.md"
            if not skill_md.exists():
                continue

            try:
                content = skill_md.read_text(encoding="utf-8")
                meta, body = self._parse_frontmatter(content)

                skill = SkillDefinition(
                    name=skill_folder.name,
                    display_name=meta.get("name", skill_folder.name),
                    description=meta.get("description", ""),
                    full_prompt=body.strip(),
                    when_to_use=self._extract_when_to_use(meta),  # v2.1 新增
                    allowed_tools=self._parse_allowed_tools(meta),  # v2.1 新增
                    required_tools=self._extract_required_tools(body),
                    optional_tools=self._extract_optional_tools(body),
                    execution_mode=ExecutionMode(meta.get("execution_mode", "inline").lower()),
                    estimated_tokens=meta.get("estimated_tokens", 2000),
                    triggers=self._extract_triggers(body),
                    cooldown_seconds=meta.get("cooldown_seconds", 30),
                    category=meta.get("category", "general"),
                    version=meta.get("version", "1.0"),
                    author=meta.get("author", ""),
                )

                self._skills[skill.name] = skill
                self._update_triggers_index(skill)
                self._skill_sources[skill.name] = source

                if source == "custom":
                    logger.info(
                        f"[SkillTool] 加载 [{source}] {skill.name} ({skill.display_name}) {'(覆盖内置)' if source == 'custom' else ''}"
                    )
                else:
                    logger.info(f"[SkillTool] 加载技能: {skill.name} ({skill.display_name})")

            except Exception as e:
                logger.error(f"[SkillTool] 加载技能失败 {skill_folder.name}: {e}")

    def _parse_frontmatter(self, content: str) -> tuple:
        """解析 YAML frontmatter"""
        meta = {}
        body = content

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    import yaml

                    meta = yaml.safe_load(parts[1]) or {}
                    body = parts[2].strip()
                except Exception as e:
                    logger.debug(f"[SkillTool] YAML 解析失败: {e}")

        return meta, body

    def _extract_when_to_use(self, meta: dict) -> str:
        """v2.1: 提取 when-to-use 字段（支持连字符和下划线格式）"""
        return meta.get("when-to-use", "") or meta.get("when_to_use", "") or ""

    def _parse_allowed_tools(self, meta: dict) -> List[str]:
        """v2.1: 解析 allowed-tools 白名单（支持列表或逗号分隔字符串）"""
        tools = meta.get("allowed-tools") or meta.get("allowed_tools") or []
        if isinstance(tools, str):
            return [t.strip() for t in tools.split(",") if t.strip()]
        elif isinstance(tools, list):
            return [str(t).strip() for t in tools if str(t).strip()]
        return []

    def _extract_required_tools(self, body: str) -> List[str]:
        """提取必需工具列表"""
        tools = []
        pattern = r"```python\s*\n(.*?)```"
        matches = re.findall(pattern, body, re.DOTALL)

        for match in matches:
            func_calls = re.findall(r"(\w+)\s*\(", match)
            for func in func_calls:
                if func.endswith("_tool") or func in ("knowledge_search", "knowledge_add", "research_topic"):
                    if func not in tools:
                        tools.append(func)

        return tools[:10]

    def _extract_optional_tools(self, body: str) -> List[str]:
        """提取可选工具列表"""
        return []

    def _extract_triggers(self, body: str) -> List[str]:
        """提取触发关键词"""
        triggers = []

        trigger_section = re.search(
            r"(?:何时触发|触发条件|Triggers?)[：:\s]*\n(.*?)(?:\n\n|\Z)", body, re.DOTALL | re.IGNORECASE
        )
        if trigger_section:
            text = trigger_section.group(1)
            items = re.findall(r"[-*]\s*(.+)", text)
            triggers.extend([item.strip()[:50] for item in items[:10]])

        return triggers

    def _update_triggers_index(self, skill: SkillDefinition):
        """更新触发词索引"""
        for trigger in skill.triggers:
            words = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]+", trigger.lower())
            for word in words:
                if len(word) >= 2:
                    if word not in self._triggers_index:
                        self._triggers_index[word] = set()
                    self._triggers_index[word].add(skill.name)

    def get_skill(self, name: str) -> Optional[SkillDefinition]:
        return self._skills.get(name)

    def list_skills(self) -> List[SkillDefinition]:
        return list(self._skills.values())

    def get_skill_source(self, name: str) -> str:
        """获取技能来源 ('system' / 'custom' / None)"""
        return self._skill_sources.get(name)

    def reload_custom_skills(self, custom_dir: Optional[Path] = None):
        """
        v2.1: 动态重新加载自定义技能（支持热更新）

        Args:
            custom_dir: 自定义目录路径（可选，不传则使用初始化时的路径）
        """
        target_dir = custom_dir or self.custom_skills_dir

        if not target_dir or not target_dir.exists():
            logger.warning(f"[SkillTool] 自定义技能目录不存在: {target_dir}")
            return

        old_custom_skills = [name for name, src in self._skill_sources.items() if src == "custom"]

        for skill_name in old_custom_skills:
            if skill_name in self._skills:
                del self._skills[skill_name]
                if skill_name in self._skill_sources:
                    del self._skill_sources[skill_name]

        self._triggers_index.clear()
        for skill in self._skills.values():
            self._update_triggers_index(skill)

        self._load_skills(target_dir, source="custom")

        logger.info(
            f"[SkillTool] 自定义技能重新加载完成: "
            f"移除 {len(old_custom_skills)} 个, "
            f"当前共 {len(self._skills)} 个技能"
        )

    def get_stats(self) -> dict:
        """获取技能统计信息"""
        system_count = sum(1 for s in self._skill_sources.values() if s == "system")
        custom_count = sum(1 for s in self._skill_sources.values() if s == "custom")

        return {
            "total": len(self._skills),
            "system": system_count,
            "custom": custom_count,
            "custom_dir": str(self.custom_skills_dir) if self.custom_skills_dir else None,
        }

    def recommend_skills(
        self, context: str, limit: int = 2, session_id: Optional[str] = None, cooldown_manager=None
    ) -> List[SkillDefinition]:
        """
        v2.1 智能推荐算法

        评分权重：
        - Level 1: when_to_use 语义匹配 (x3.0) ← 最高优先级
        - Level 2: triggers 精确匹配 (x2.0)
        - Level 3: description 关键词 (x1.0)
        - Level 4: 冷却惩罚 (x0.3) ← 冷却中的降权
        """
        scores: Dict[str, float] = {}
        context_lower = context.lower()
        context_words = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}", context_lower))

        for skill_name, skill in self._skills.items():
            score = 0.0

            if skill.when_to_use:
                when_words = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}", skill.when_to_use.lower()))
                overlap = len(context_words & when_words)
                if overlap > 0:
                    score += overlap * 3.0

            for word in context_words:
                if word in self._triggers_index and skill_name in self._triggers_index[word]:
                    score += 2.0

            desc_matches = len(
                context_words & set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}", skill.description.lower()))
            )
            score += desc_matches * 1.0

            if cooldown_manager and session_id:
                if not cooldown_manager.can_execute(skill_name, session_id):
                    score *= 0.3

            if score > 0.5:
                scores[skill_name] = score

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]

        results = []
        for skill_name, _ in ranked:
            skill = self._skills.get(skill_name)
            if skill:
                results.append(skill)

        return results

    def format_skill_recommendation(self, skills: List[SkillDefinition]) -> str:
        """格式化技能推荐提示（用于注入 System Prompt）"""
        if not skills:
            return ""

        lines = ["", "💡 **相关技能推荐**:", ""]
        for i, skill in enumerate(skills, 1):
            lines.append(f"{i}. **{skill.display_name}** (`{skill.name}`)")
            if skill.when_to_use:
                when_preview = skill.when_to_use[:80] + ("..." if len(skill.when_to_use) > 80 else "")
                lines.append(f"   📌 适用场景: {when_preview}")
            if skill.allowed_tools:
                tools_preview = ", ".join(skill.allowed_tools[:3])
                if len(skill.allowed_tools) > 3:
                    tools_preview += f" (+{len(skill.allowed_tools)-3})"
                lines.append(f"   🔧 可用工具: {tools_preview}")
            lines.append("")

        lines.append("💡 如需使用，可通过 `skill_call_tool` 调用。")
        lines.append("")
        return "\n".join(lines)

    def search_skills(self, query: str, limit: int = 5) -> List[tuple[SkillDefinition, float]]:
        """搜索技能"""
        query_lower = query.lower()
        query_words = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}", query_lower))

        scored = []
        for skill in self._skills.values():
            score = 0.0

            if query_lower in skill.name.lower():
                score += 3.0
            if query_lower in skill.description.lower():
                score += 2.0
            if query_lower in skill.full_prompt.lower():
                score += 1.0

            skill_words = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}", skill.full_prompt.lower()))
            matched = query_words & skill_words
            score += len(matched) * 0.5

            if score > 0:
                scored.append((skill, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]


class SkillExecutor:
    """技能执行器"""

    def __init__(
        self,
        registry: SkillRegistry,
        cooldown_manager: CooldownManager,
        task_store: SkillTaskStore,
        plugin_instance=None,
    ):
        self.registry = registry
        self.cooldown = cooldown_manager
        self.tasks = task_store
        self.plugin = plugin_instance
        self._background_tasks: Set[asyncio.Task] = set()

    async def execute_inline(self, event, skill: SkillDefinition, instruction: str, max_rounds: int = 5) -> str:
        """
        Inline 同步执行模式 (v2.1 增强版)

        注入技能 Prompt，限制工具集（支持白名单），同步执行并返回结果
        """
        effective_tools = skill.allowed_tools if skill.allowed_tools else (skill.required_tools + skill.optional_tools)

        system_prompt = f"""你现在是 {skill.display_name}。

{skill.full_prompt}

【当前任务】
{instruction}

【可用工具】
你只能使用以下工具：{', '.join(effective_tools) if effective_tools else '所有可用工具'}
如果需要其他功能，请告知用户无法完成。

【重要】
- 你必须通过调用工具来完成任务
- 不要偏离你的专业领域
- 保持简洁、专业的回答"""

        tool_map = self._build_tool_map(event, effective_tools)

        logger.info(
            f"[SkillTool] 技能 {skill.name} 工具集: "
            f"白名单={skill.allowed_tools or '未设置'}, "
            f"实际可用={list(tool_map.keys())}"
        )

        llm_timeout = getattr(self.plugin.config, "llm_call_timeout", 60)

        try:
            # 使用 AstrBot v4.x 推荐的 tool_loop_agent 接口
            # 自动处理工具调用循环
            response = await asyncio.wait_for(
                self.plugin.context.tool_loop_agent(
                    event=event,
                    chat_provider_id=await self.plugin.context.get_current_chat_provider_id(
                        event.unified_msg_origin if event else None
                    ),
                    prompt=instruction,
                    system_prompt=system_prompt,
                    tool_call_timeout=llm_timeout,
                ),
                timeout=llm_timeout * 2,  # 留出工具执行的时间
            )
            final_response = response.completion_text
        except Exception as e:
            logger.error(f"[SkillTool] 执行失败: {e}")
            return f"❌ 技能执行失败: {e}"

        uid = getattr(event, "get_sender_id", lambda: None)() or "unknown"
        gid = getattr(event, "get_group_id", lambda: None)() or "private"
        session_id = f"{uid}_{gid}"

        self.cooldown.record_execution(skill.name, session_id)

        if not final_response:
            final_response = "（技能执行完成，无额外输出）"

        return f"🎯 **[{skill.display_name}] 执行结果**\n\n{final_response}"

    async def execute_forked(self, event, skill: SkillDefinition, instruction: str) -> str:
        """
        Forked 异步执行模式

        创建后台任务，立即返回 TaskID
        """
        task_id = f"skill_{uuid.uuid4().hex[:8]}"

        uid = getattr(event, "get_sender_id", lambda: None)() or "unknown"
        gid = getattr(event, "get_group_id", lambda: None)() or "private"
        session_id = f"{uid}_{gid}"

        task = SkillTask(
            task_id=task_id,
            skill_name=skill.name,
            instruction=instruction,
            created_at=time.time(),
            status="pending",
            session_id=session_id,
        )
        self.tasks.add(task)

        async def background_execute():
            try:
                task.status = "running"
                result = await self.execute_inline(event, skill, instruction)
                self.tasks.complete(task_id, result)

                try:
                    await self._notify_user(event, f"✅ 后台任务 {task_id} 完成:\n{result[:500]}")
                except Exception as e:
                    logger.warning(f"[SkillTool] 通知用户失败: {e}")

            except Exception as e:
                error_msg = str(e)
                self.tasks.fail(task_id, error_msg)
                logger.error(f"[SkillTool] 后台任务失败 {task_id}: {e}")

        bg_task = asyncio.create_task(background_execute())
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(lambda t: self._background_tasks.discard(t))

        return (
            f"✅ 技能已在后台启动，任务ID: **{task_id}**\n\n"
            f"- 技能: {skill.display_name}\n"
            f"- 状态: 排队中\n"
            f"- 可用 `skill_status` 命令查询进度"
        )

    def _build_tool_map(self, event, allowed_tools: List[str]) -> Dict[str, Any]:
        """
        v2.1: 构建工具映射（仅包含允许的工具，支持通配符）

        支持的通配符格式：
        - "file_*" → 匹配所有 file_ 开头的工具
        - "*_tool" → 匹配所有 _tool 结尾的工具
        """
        import fnmatch

        tool_map = {}

        from ..tools.common.file_ops import (
            file_append,
            file_edit,
            file_grep,
            file_list,
            file_read,
            file_write,
            multi_edit,
        )

        base_tools = {
            "file_read_tool": file_read,
            "file_write_tool": file_write,
            "file_edit_tool": file_edit,
            "file_append_tool": file_append,
            "file_search_tool": file_grep,
            "file_list_tool": file_list,
            "multi_edit_tool": multi_edit,
        }

        if hasattr(self.plugin, "web_search_tool") and self.plugin.web_search_tool:
            base_tools["web_search_tool"] = self.plugin._handle_web_search

        if hasattr(self.plugin, "archive_manager") and self.plugin.archive_manager:
            base_tools["archives_query_tool"] = lambda args: self.plugin.archive_manager.execute_query(
                args.get("sql", "")
            )

        if not allowed_tools:
            return base_tools

        for tool_pattern in allowed_tools:
            is_wildcard = "*" in tool_pattern or "?" in tool_pattern

            if is_wildcard:
                for available_tool in base_tools:
                    if fnmatch.fnmatch(available_tool, tool_pattern):
                        tool_map[available_tool] = base_tools[available_tool]
            else:
                if tool_pattern in base_tools:
                    tool_map[tool_pattern] = base_tools[tool_pattern]

        return tool_map

    async def _execute_single_tool(self, tool_func: Any, tool_name: str, tool_args: dict, event) -> str:
        """执行单个工具调用"""
        import inspect

        try:
            if inspect.isasyncgenfunction(tool_func):
                parts = []
                async for part in tool_func(event, **tool_args, plugin=self.plugin):
                    parts.append(str(part))
                return "\n".join(parts)
            else:
                result = await tool_func(event, **tool_args, plugin=self.plugin)
                return str(result)

        except TypeError:
            filtered_args = {k: v for k, v in tool_args.items() if k != "event" and k != "plugin"}

            if inspect.isasyncgenfunction(tool_func):
                parts = []
                async for part in tool_func(event, **filtered_args):
                    parts.append(str(part))
                return "\n".join(parts)
            else:
                result = await tool_func(event, **filtered_args)
                return str(result)

    async def _notify_user(self, event, message: str):
        """向用户发送通知"""
        try:
            from astrbot.api.all import MessageChain
            from astrbot.api.message_components import Plain

            message_chain = MessageChain([Plain(message)])
            umo = event.unified_msg_origin
            if umo:
                await self.plugin.context.send_message(umo, message_chain)
        except Exception as e:
            logger.error(f"[SkillTool] 发送通知失败: {e}")

    async def cancel_skill(self, task_id: str) -> tuple:
        """
        v2.1: 取消正在执行的后台技能任务

        Args:
            task_id: 任务ID

        Returns:
            (success, message) 元组
        """
        task = self.tasks.get(task_id)

        if not task:
            return False, f"任务 {task_id} 不存在"

        if task.status != "running":
            return False, f"任务状态为 {task.status.value}，无法取消（只能取消运行中的任务）"

        bg_task_to_cancel = None
        for bg_task in self._background_tasks:
            if hasattr(bg_task, "task_id") or True:
                if task_id == task.task_id and not bg_task.done():
                    bg_task_to_cancel = bg_task
                    break

        if bg_task_to_cancel is None:
            for bg_task in self._background_tasks:
                if not bg_task.done() and task.created_at > time.time() - 3600:
                    try:
                        result = bg_task.result()
                        if isinstance(result, str) and task_id in result:
                            bg_task_to_cancel = bg_task
                            break
                    except (asyncio.InvalidStateError, asyncio.CancelledError):
                        continue

        if bg_task_to_cancel is None:
            return False, "无法找到对应的后台任务（可能已完成或已取消）"

        try:
            bg_task_to_cancel.cancel()

            try:
                await bg_task_to_cancel
            except asyncio.CancelledError:
                pass

            task.status = "cancelled"
            task.completed_at = time.time()

            logger.info(f"[SkillTool] 任务 {task_id} 已被用户取消")

            return True, f"✅ 任务 {task_id} 已成功取消（技能: {task.skill_name}）"

        except Exception as e:
            logger.error(f"[SkillTool] 取消任务失败 {task_id}: {e}")
            return False, f"❌ 取消任务失败: {e}"

    async def cleanup(self):
        """清理资源"""
        for task in self._background_tasks:
            if not task.done():
                task.cancel()

        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)


_registry_instance: Optional[SkillRegistry] = None
_executor_instance: Optional[SkillExecutor] = None
_cooldown_instance: Optional[CooldownManager] = None
_task_store_instance: Optional[SkillTaskStore] = None


def get_skill_registry() -> SkillRegistry:
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = SkillRegistry()
    return _registry_instance


def get_skill_executor() -> Optional[SkillExecutor]:
    return _executor_instance


def get_cooldown_manager() -> CooldownManager:
    global _cooldown_instance
    if _cooldown_instance is None:
        _cooldown_instance = CooldownManager()
    return _cooldown_instance


def get_task_store() -> SkillTaskStore:
    global _task_store_instance
    if _task_store_instance is None:
        _task_store_instance = SkillTaskStore()
    return _task_store_instance


def initialize_skill_system(skills_dir: Path, plugin_instance=None, custom_skills_dir: Optional[Path] = None):
    """
    初始化技能系统 (v2.1 双级加载版)

    Args:
        skills_dir: 内置技能目录
        plugin_instance: 插件实例
        custom_skills_dir: 自定义技能目录（可选）
    """
    global _registry_instance, _executor_instance, _cooldown_instance, _task_store_instance

    _registry_instance = SkillRegistry(skills_dir, custom_skills_dir=custom_skills_dir)
    _cooldown_instance = CooldownManager()
    _task_store_instance = SkillTaskStore()
    _executor_instance = SkillExecutor(_registry_instance, _cooldown_instance, _task_store_instance, plugin_instance)

    stats = _registry_instance.get_stats()
    logger.info(
        f"[SkillSystem] 初始化完成，"
        f"内置 {stats['system']} 个技能, "
        f"自定义 {stats['custom']} 个技能, "
        f"总计 {stats['total']} 个"
    )

    return _registry_instance, _executor_instance
