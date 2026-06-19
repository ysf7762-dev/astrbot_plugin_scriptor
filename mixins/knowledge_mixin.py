from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import filter

from .base import BaseMixin

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


class KnowledgeMixin(BaseMixin):
    """
    知识库管理 Mixin

    包含：
    - 知识库状态查询
    - 知识库搜索工具
    - 知识添加工具
    - 研究工具

    注意：所有命令装饰器已移至 main.py 中注册，避免指令冲突
    """

    async def cmd_kb_status(self, event: AstrMessageEvent):
        """查看知识库状态"""
        await self._wait_for_ready()

        stats = self.knowledge_base.get_stats()
        tasks = self.research_tool.get_all_tasks()

        msg = f"""## 📚 知识库状态

- **总条目**: {stats['total']}
- **主动知识**: {stats['active']}
- **总使用**: {stats['total_uses']} 次
- **分类数**: {stats['categories']}

### 知识类型分布
"""
        for type_name, count in stats["types"].items():
            if count > 0:
                msg += f"- {type_name}: {count}\n"

        if tasks:
            msg += f"\n### 研究任务 ({len(tasks)} 个)\n"
            for task in tasks[:5]:
                status_emoji = {
                    "pending": "⏳",
                    "in_progress": "🔍",
                    "paused": "⏸️",
                    "completed": "✅",
                    "failed": "❌",
                }.get(task.status.value, "❓")
                msg += f"- {status_emoji} {task.topic} ({task.status.value})\n"
            if len(tasks) > 5:
                msg += f"... 还有 {len(tasks) - 5} 个任务\n"

        yield event.plain_result(msg)

    @filter.llm_tool()
    async def knowledge_search(self, event: AstrMessageEvent, query: str, limit: int = 5):
        """
        搜索知识库中的知识条目。

        【重要】在以下场景必须先调用此工具搜索知识库：
        - 回答工作经验、工作流程、工具使用等问题前
        - **当用户陈述任何流程/规范/知识点时，必须先搜索验证是否正确**
        - 授课模式下，学生陈述任何内容时必须验证

        知识库包含事实、技能、偏好、规则、经验等知识条目。

        Args:
            query (str): 搜索关键词
            limit (int): 返回结果数量，默认 5

        Returns:
            格式化的知识库搜索结果
        """
        await self._wait_for_ready()

        results = self.knowledge_base.search(query, limit)

        if not results:
            return "（知识库中未找到相关内容）"

        parts = ["## 📚 知识库搜索结果\n"]
        for i, item in enumerate(results, 1):
            parts.append(f"### {i}. {item.title}")
            if item.tags:
                parts.append(f"**标签**: {', '.join(item.tags)}")
            parts.append(f"\n{item.content}")
            if item.useful_count > 0:
                parts.append(f"\n*使用 {item.useful_count} 次*")

        return "\n".join(parts)

    @filter.llm_tool()
    async def knowledge_add(
        self,
        event: AstrMessageEvent,
        title: str,
        content: str,
        knowledge_type: str = "fact",
        tags: str = "",
        category: str = "",
    ):
        """
        添加知识到知识库（条目，1000字以内）。

        【注意】学习模式下此工具不可用，请使用 `learn_from_conversation` 工具。

        当你发现重要的知识、经验或规则时，使用此工具将其添加到知识库中。

        Args:
            title (str): 知识标题（一句话概括）
            content (str): 知识内容（1000字以内）
            knowledge_type (str): 知识类型：fact/skill/preference/rule/experience/reference，默认 fact
            tags (str): 标签，逗号分隔（可选）
            category (str): 分类（可选）

        Returns:
            确认消息
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)

        if self.learning_manager.is_learning(uid, group_id):
            return (
                "⚠️ 当前处于【学习模式】，请使用 `learn_from_conversation` 工具提取知识，等待导师确认后再写入知识库。"
            )

        if self.learning_manager.is_read_only(uid, group_id):
            return (
                "⚠️ 当前处于【授课模式】，知识库已锁定为只读状态。无法添加新知识。如需修改，请联系管理员退出授课模式。"
            )

        from ..core.knowledge_base import KnowledgeItem, KnowledgeType

        type_map = {
            "fact": KnowledgeType.FACT,
            "skill": KnowledgeType.SKILL,
            "preference": KnowledgeType.PREFERENCE,
            "rule": KnowledgeType.RULE,
            "experience": KnowledgeType.EXPERIENCE,
            "reference": KnowledgeType.REFERENCE,
        }

        kb_type = type_map.get(knowledge_type.lower(), KnowledgeType.FACT)

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

        is_valid, msg = self.knowledge_base.validate_content(content)
        if not is_valid:
            return f"❌ {msg}"

        item = KnowledgeItem.create(
            title=title,
            content=content,
            knowledge_type=kb_type,
            tags=tag_list,
            category=category,
            is_active=True,
            source="LLM主动添加",
        )

        self.knowledge_base.add_item(item)

        logger.info(f"[Scriptor] 添加知识库: {title}")
        return f"✅ 知识已添加到知识库: {title}"

    @filter.llm_tool()
    async def research_topic(self, event: AstrMessageEvent, topic: str, depth: str = "normal"):
        """
        主动研究一个话题，将研究结果转化为知识库条目。

        这不是被动问答，而是主动探索和学习，研究结果会自动保存到知识库。

        Args:
            topic (str): 要研究的话题
            depth (str): 研究深度：quick/normal/deep/comprehensive，默认 normal

        Returns:
            研究任务开始的确认消息
        """
        await self._wait_for_ready()

        from ..core.research_tool import ResearchDepth

        depth_map = {
            "quick": ResearchDepth.QUICK,
            "normal": ResearchDepth.NORMAL,
            "deep": ResearchDepth.DEEP,
            "comprehensive": ResearchDepth.COMPREHENSIVE,
        }

        research_depth = depth_map.get(depth.lower(), ResearchDepth.NORMAL)

        task = self.research_tool.create_research_task(topic, research_depth)
        self.research_tool.start_research(task.id)

        logger.info(f"[Scriptor] 开始研究: {topic}")

        next_question = self.research_tool.generate_next_question(task.id)

        return f"""🔍 开始主动研究: {topic}

**研究深度**: {depth}
**任务 ID**: {task.id}

{next_question if next_question else '请告诉我关于这个话题你想了解什么？'}

研究过程中发现的关键点会自动保存到知识库。"""

    @filter.llm_tool()
    async def research_note(self, event: AstrMessageEvent, task_id: str, content: str, is_key: bool = True):
        """
        添加研究笔记到研究任务中。

        Args:
            task_id (str): 研究任务 ID
            content (str): 笔记内容
            is_key (bool): 是否是关键点，默认 true

        Returns:
            确认消息
        """
        await self._wait_for_ready()

        task = self.research_tool.get_task(task_id)
        if not task:
            return f"❌ 未找到研究任务: {task_id}"

        note = self.research_tool.add_research_note(task_id, content, is_key)

        if note:
            next_question = self.research_tool.generate_next_question(task_id)
            return f"""✅ 笔记已添加 (第 {note.round_num} 轮)

{next_question if next_question else '研究完成！知识已自动提取到知识库。'}"""

        return "❌ 添加笔记失败"

    @filter.llm_tool()
    async def research_complete(self, event: AstrMessageEvent, task_id: str):
        """
        完成研究任务，自动提取知识到知识库。

        Args:
            task_id (str): 研究任务 ID

        Returns:
            研究总结
        """
        await self._wait_for_ready()

        success = self.research_tool.complete_research(task_id)
        if not success:
            return f"❌ 未找到研究任务: {task_id}"

        summary = self.research_tool.get_research_summary(task_id)

        if summary:
            return f"""✅ 研究完成！

**话题**: {summary['topic']}
**轮次**: {summary['rounds']}/{summary['max_rounds']}
**笔记数**: {summary['notes_count']}
**关键点**: {summary['key_notes_count']}
**提取知识**: {summary['knowledge_count']} 条

知识已自动保存到知识库！"""

        return "✅ 研究完成！"
