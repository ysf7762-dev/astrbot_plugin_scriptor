from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from astrbot.api import logger
from astrbot.api.event import filter

from .base import BaseMixin

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

from ..tools.common.text_utils import compact_result, get_tool_max_tokens


class ToolsMixin(BaseMixin):
    """
    LLM 工具 Mixin

    包含所有 @filter.llm_tool() 装饰的方法。
    """

    @filter.llm_tool()
    @compact_result(max_tokens=get_tool_max_tokens, strategy="head_tail")
    async def tool_search_tool(self, event: AstrMessageEvent, query: str, limit: int = 5):
        """
        搜索可用的工具。

        当你不确定应该使用哪个工具，或者想了解有哪些工具可用时，使用此工具。
        这能帮助你选择最合适的工具来完成任务。

        Args:
            query (str): 你的需求描述（自然语言）
                例如：
                - "我想读取文件"
                - "如何搜索记忆"
                - "有什么工具可以编辑文本"
            limit (int): 返回结果数量（默认 5）

        Returns:
            匹配的工具列表，包含名称、用途说明和关键参数
        """
        from ..tools.tool_search import get_tool_search_engine

        engine = get_tool_search_engine()
        if not engine._built:
            try:
                engine.build_index(self)
            except Exception as e:
                return f"❌ 工具索引构建失败: {e}"

        results = await engine.search(query, limit)

        if not results:
            return '🔍 **未找到匹配的工具**\n\n尝试用其他关键词描述你的需求，例如："文件操作"、"记忆搜索"、"网页获取"'

        output = ["🔍 **找到以下工具：**\n"]
        for i, result in enumerate(results, 1):
            entry = result.entry
            stars = "⭐" * min(int(result.score), 5)
            output.append(f"\n**{i}. {entry.display_name}** (`{entry.name}`)")
            output.append(f"   📊 相关度: {stars} ({result.score:.1f})")
            output.append(f"   📝 用途: {entry.description[:120]}...")
            if entry.parameters:
                params_str = ", ".join(f"{p.name}{'*' if p.required else ''}" for p in entry.parameters[:6])
                output.append(f"   🔧 参数: {params_str}")
            if result.match_reason:
                output.append(f"   💡 匹配原因: {result.match_reason}")

        return "\n".join(output)

    @filter.llm_tool()
    @compact_result(max_tokens=get_tool_max_tokens, strategy="truncate")
    async def memory_search(
        self, event: AstrMessageEvent, query: str, scope: str = "group", limit: int = 5, cross_reason: str = ""
    ):
        """
        检索记忆系统中的相关内容。

        在回答关于过往工作、决策、日期、人物、偏好或待办的问题前使用此工具。

        Args:
            query (str): 搜索查询
            scope (str): 搜索范围。
                - group: 仅搜索当前群聊的群体记忆（群聊默认，私聊不可用）
                - personal: 当前群聊 + 私聊记忆（群聊需提供理由，私聊不可用）
                - cross: 当前群聊 + 私聊 + 所有其他群聊（群聊需提供理由，私聊默认）
            limit (int): 返回结果数量
            cross_reason (str): 【重要】跨场景搜索理由。
                群聊场景下使用 personal 或 cross 时必填。
                私聊场景不需要此参数。

        Returns:
            格式化的搜索结果，或错误提示（如果跨场景搜索理由不充分）
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)

        user_raw_message = event.message_str if hasattr(event, "message_str") else str(event)

        results = await self.search_engine.search(query, uid, group_id, scope, limit, user_raw_message, cross_reason)

        return self.search_engine.format_results(results, group_id)

    @filter.llm_tool()
    async def usage_docs_search(self, event: AstrMessageEvent, query: str, limit: int = 3):
        """
        检索 Scriptor 使用说明文档。

        当用户询问如何使用 Scriptor、命令、工具、配置或其他相关问题时使用此工具。

        Args:
            query (str): 用户的问题
            limit (int): 返回结果数量（默认 3 个）

        Returns:
            相关的使用说明片段
        """
        await self._wait_for_ready()

        results = self.usage_docs_kb.search(query, limit)

        return self.usage_docs_kb.format_results(results)

    @filter.llm_tool()
    async def set_group_admin_tool(self, event: AstrMessageEvent, target_uid: str, is_admin: bool = True):
        """
        授予或撤销用户的群组管理员权限。

        【重要】此工具仅限超级管理员使用。

        Args:
            target_uid (str): 目标用户的逻辑 UID (例如: user_xxx)
            is_admin (bool): 权限开关。True 为授予管理员权限，False 为撤销。默认为 True。

        Returns:
            执行结果消息
        """
        uid, group_id, _ = self._get_identity(event)

        if not self.identity_manager.is_super_admin(uid):
            logger.warning(f"[Scriptor] 非法权限尝试: 用户 {uid} 尝试设置群管")
            return "❌ 权限不足。只有超级管理员可以执行此操作。"

        if group_id == "private":
            return "❌ 此操作只能在群聊中执行。"

        self.identity_manager.set_group_admin(target_uid, group_id, is_admin)

        action = "设置" if is_admin else "取消"
        logger.info(f"[Scriptor] 权限变更: 超级管理员 {uid} {action}了用户 {target_uid} 在群 {group_id} 的管理员权限")

        return f"✅ 已成功{action}用户 {target_uid} 的群组管理员权限。"

    @filter.llm_tool()
    async def update_profile(self, event: AstrMessageEvent, new_facts: str, scope: str = "personal"):
        """
        【核心认知工具】更新画像信息（支持个人画像和群组画像）。

        这是一个常驻工具，你应该在日常对话中主动使用它来沉淀对用户和群组的认知。

        **何时使用**：
        - 个人画像 (scope="personal")：发现用户的新特征、新偏好、新习惯时，立即记录
        - 群组画像 (scope="group")：发现群组的新梗、共同兴趣、群规变化时，记录到群组画像

        **权限规则**：
        - 个人画像：所有用户都可以更新自己的画像
        - 群组画像：仅限群组管理员或超级管理员更新

        **与通用文件编辑工具的区别**：
        - 此工具封装了路径路由、格式化合并、权限校验等复杂逻辑
        - 你只需提供"新发现的事实"，系统会自动将其优雅地合并到画像文件中
        - 不会破坏现有画像结构，安全可靠

        Args:
            new_facts (str): 需要更新的事实信息（简洁描述新发现）
            scope (str): 更新范围。"personal" (个人画像) 或 "group" (群组画像)。默认为 "personal"。

        Returns:
            确认消息
        """
        uid, group_id, _ = self._get_identity(event)

        if scope == "group":
            if group_id == "private":
                return "❌ 无法在私聊中更新群组画像。"

            is_super = self.identity_manager.is_super_admin(uid, self.config.admin_uids)
            is_group_admin = self.identity_manager.is_group_admin(uid, group_id)

            if not (is_super or is_group_admin):
                logger.warning(f"[Scriptor] 权限拒绝: 用户 {uid} 尝试更新群组 {group_id} 的画像")
                return "❌ 权限不足：仅群组管理员或超级管理员可更新群组画像。"

        await self.memory_manager.update_profile(uid, group_id, new_facts, scope)

        log_prefix = "群组" if scope == "group" else "个人"
        logger.info(f"[Scriptor] 更新{log_prefix}画像: uid={uid}, group_id={group_id}, facts={new_facts[:30]}...")

        return f"✅ {log_prefix}画像已更新。"

    @filter.llm_tool()
    async def record_decision(self, event: AstrMessageEvent, decision: str, reason: str = "", context: str = ""):
        """
        【核心认知工具】记录重要决策或事件到长期记忆中。

        此工具用于将用户的重要决定、里程碑事件、会议决议等写入 MEMORY.md 文件，
        确保这些关键信息能够被长期保留和检索。

        适用场景：
        - 个人决策：如"我决定每天早上 7 点起床"、"我选择了方案 A"
        - 群组决策：如"团队决定使用 React 作为前端框架"、"会议决议：每周五下午开例会"
        - 全局决策：仅限管理员模式（Sudo），记录对所有用户可见的重要决策

        路由规则：
        - 私聊场景：自动路由到 P_MEMORY.md（个人记忆）
        - 群聊场景：自动路由到 G_MEMORY.md（群组共享记忆）
        - 管理员模式：自动路由到全局 MEMORY.md

        Args:
            decision (str): 决策内容（必填）
            reason (str): 决策理由（可选）
            context (str): 决策背景/上下文（可选）

        Returns:
            确认消息，包含记录位置信息
        """
        uid, group_id, _ = self._get_identity(event)

        is_sudo = self.identity_manager.is_sudo(uid, self.config.admin_uids)

        content = f"决策: {decision}"
        if reason:
            content += f"\n理由: {reason}"
        if context:
            content += f"\n上下文: {context}"

        from ..core.interfaces import MemoryRecordParams

        await self.memory_manager.record_long_term_memory(
            MemoryRecordParams(uid=uid, group_id=group_id, content=content, memory_type="decision", is_sudo=is_sudo),
            search_engine=self.search_engine,
        )

        if is_sudo:
            return "✅ 决策已记录到全局共享记忆 (GLOBAL_MEMORY.md)。"
        elif group_id != "private":
            return "✅ 决策已记录到群组共享记忆 (G_MEMORY.md)。"
        return "✅ 决策已记录到个人记忆 (P_MEMORY.md)。"

    @filter.llm_tool()
    async def file_read_tool(
        self,
        event: AstrMessageEvent,
        file_path: str,
        start_line: int = None,
        end_line: int = None,
        show_line_numbers: bool = False,
    ):
        """
        读取工作文件内容（支持渐进式披露模式）。

        【核心工具】当你从"上下文目录索引"中看到某个文件需要详细了解时，使用此工具读取。

        支持的路径格式：
        - 用户记忆文件: "P_PROFILE.md", "P_MEMORY.md", "P_SOP.md" 等
        - 群组文件: "G_SOP.md", "G_PROFILE.md" 等
        - 全局文件: "SOP.md", "SOUL.md" 等
        - 技能手册（只读）: "skills/skill-name/SKILL.md"
        - 日记文件: "memory/2026-04-03.md"

        Args:
            file_path (str): 文件路径（如 "PROFILE.md"、"skills/archive-manager/SKILL.md"）
            start_line (int, optional): 起始行号（1-based）
            end_line (int, optional): 结束行号（1-based）
            show_line_numbers (bool, optional): 是否在每行前显示行号

        Returns:
            文件内容（如果文件过长会自动截断并提示）
        """
        from ..tools.common.file_ops import file_read

        return await file_read(event, file_path, start_line, end_line, show_line_numbers, self)

    @filter.llm_tool()
    async def file_write_tool(self, event: AstrMessageEvent, file_path: str, content: str):
        """
        创建或覆盖工作文件（带安全防护）。

        【⚠️ 核心规则 - 先读后写 (Read-Before-Write)】
        **修改已有文件前，必须先使用 file_read_tool 读取该文件！**
        - 如果覆写已有文件但未读取，系统会拒绝操作并提示你先读取
        - 这是为了确保你了解文件的完整上下文，避免误操作导致内容丢失
        - 对于新文件（不存在的文件），可以直接写入，无需先读

        【工具定位】
        - ✅ **推荐用于**：创建全新的文件
        - ⚠️ **谨慎用于**：完全重写已有文件（需要先全量读取）
        - 💡 **替代方案**：如果只是修改部分内容，优先使用 file_edit_tool

        【重要限制】
        - ❌ 禁止写入 skills/ 或 templates/ 目录（官方技能库为只读）
        - ✅ 仅允许在用户目录 (profiles/{uid}/) 或群组目录 (groups/{gid}/) 下创建/修改文件
        - ⚠️ 如果覆写已有文件，系统会检查内容是否异常缩水（防止误删）

        【创建新文件规范】
        当你创建新的 .md 文件（特别是 P_SOP.md、G_SOP.md）时，**必须在内容最顶部包含 YAML 元数据头**：
        ```yaml
        ---
        summary: "一句话描述文件用途"
        keywords: ["关键词1", "关键词2"]
        ---
        ```

        Args:
            file_path (str): 文件路径（如 "P_SOP.md"、"G_SOP.md"、"NOTES.md"）
            content (str): 要写入的完整内容（包含 YAML 头）

        Returns:
            操作结果或错误提示（如果违反安全规则或未先读取）
        """
        from ..tools.common.file_ops import file_write

        return await file_write(event, file_path, content, self)

    @filter.llm_tool()
    async def file_edit_tool(
        self, event: AstrMessageEvent, file_path: str, old_text: str, new_text: str, replace_all: bool = False
    ):
        """
        编辑工作文件（查找替换）- **修改已有文件的首选工具**。

        【⚠️ 核心规则 - 先读后写 (Read-Before-Write)】
        **编辑任何文件前，必须先使用 file_read_tool 读取该文件！**
        - 系统会强制校验你是否已读取该文件
        - 未读取直接编辑会被拒绝

        【工具优势】
        - ✅ 只发送差异部分，Token 消耗更少
        - ✅ 降低误操作风险（不会意外删除其他内容）
        - ✅ 支持模糊匹配（自动处理空格、引号等微小差异）

        【使用技巧】
        - old_text 必须与文件中的内容足够独特（避免匹配到多处）
        - 如果匹配失败，尝试提供更多上下文行
        - 需要全局替换时设置 replace_all=True

        Args:
            file_path (str): 文件路径
            old_text (str): 要查找的文本（支持模糊匹配）
            new_text (str): 替换后的文本
            replace_all (bool, optional): 是否替换所有匹配项（默认 False，仅替换第一个）

        Returns:
            操作结果
        """
        from ..tools.common.file_ops import file_edit

        return await file_edit(event, file_path, old_text, new_text, replace_all, self)

    @filter.llm_tool()
    async def multi_edit_tool(self, event: AstrMessageEvent, file_path: str, edits: list[dict]):
        """
        原子化的多次编辑操作（MultiEdit）。

        在一个文件中执行多处编辑，所有编辑要么全部成功，要么全部不生效。
        适用于同时更新文件中多个不同位置的内容。

        Args:
            file_path (str): 文件路径
            edits (list[dict]): 编辑操作列表，每项为字典，包含：
                - old_string (str): 要查找的文本（精确匹配，包括空白字符）
                - new_string (str): 替换后的文本
                - replace_all (bool, optional): 是否全局替换该项，默认 False

        Returns:
            操作结果（包含成功/失败统计）

        示例:
            edits = [
                {"old_string": "旧标题", "new_string": "新标题"},
                {"old_string": "旧描述", "new_string": "新描述"},
                {"old_string": "port: 8080", "new_string": "port: 9090", "replace_all": True}
        ]

        注意：
            - 所有编辑按顺序执行，任何一项失败则全部回滚
            - old_string 必须精确匹配（包括空白字符），支持模糊容错
            - 自动继承防缩水检测和结构校验
        """
        from ..tools.common.file_ops import multi_edit

        return await multi_edit(event, file_path, edits, self)

    @filter.llm_tool()
    async def file_append_tool(self, event: AstrMessageEvent, file_path: str, content: str):
        """
        追加内容到工作文件末尾。

        【⚠️ 建议先读后写】
        虽然追加操作相对安全，但**强烈建议先读取文件**：
        - 确保你了解文件的当前结尾状态（避免破坏 JSON/Markdown 结构）
        - 避免重复追加相同内容
        - 确保追加位置正确

        Args:
            file_path (str): 文件路径
            content (str): 要追加的内容

        Returns:
            操作结果
        """
        from ..tools.common.file_ops import file_append

        return await file_append(event, file_path, content, self)

    @filter.llm_tool()
    async def file_search_tool(
        self,
        event: AstrMessageEvent,
        pattern: str,
        path: str = None,
        is_regex: bool = False,
        case_sensitive: bool = True,
        context_lines: int = 0,
    ):
        """
        在工作文件中搜索内容。

        Args:
            pattern (str): 搜索模式
            path (str, optional): 文件或目录路径
            is_regex (bool, optional): 是否使用正则表达式
            case_sensitive (bool, optional): 是否区分大小写
            context_lines (int, optional): 上下文行数

        Returns:
            搜索结果
        """
        from ..tools.common.file_ops import file_grep

        return await file_grep(event, pattern, path, is_regex, case_sensitive, context_lines, self)

    @filter.llm_tool()
    async def file_list_tool(self, event: AstrMessageEvent, pattern: str = "*"):
        """
        列出工作目录中的文件。

        Args:
            pattern (str, optional): 文件匹配模式（默认 "*"）

        Returns:
            文件列表
        """
        from ..tools.common.file_ops import file_list

        return await file_list(event, pattern, self)

    @filter.llm_tool()
    async def file_delete_tool(self, event: AstrMessageEvent, file_path: str):
        """
        删除工作文件（⚠️ 高危操作 - 需要用户二次确认）。

        【⚠️ 核心规则 - 高危操作确认】
        **删除文件属于不可逆的高危操作！**
        - 当配置 `require_delete_confirmation` 为 true 时（默认开启），调用此工具不会立即删除文件
        - 系统会挂起操作，并向用户发送确认请求
        - 用户需要回复 `/delete` 命令才能确认执行删除
        - 如果用户回复其他内容，操作将被取消

        【使用场景】
        - 用户明确要求删除某个记忆文件或笔记
        - 清理过时或错误的内容

        【重要限制】
        - ❌ 禁止删除 skills/ 或 templates/ 目录的文件（官方技能库为只读）
        - ❌ 不支持删除目录，仅支持删除单个文件
        - ⚠️ 跨用户/跨群组删除需要管理员权限

        Args:
            file_path (str): 要删除的文件路径（如 "MEMORY.md"、"notes/old.md"）

        Returns:
            操作结果：
            - 成功：返回 "✅ 文件已成功删除: ..."
            - 挂起等待确认：返回 "Error: 操作已挂起 (PENDING_USER_CONFIRMATION)..."
            - 失败：返回具体错误信息
        """
        from ..tools.common.file_ops import file_delete

        result = await file_delete(event, file_path, self)

        if isinstance(result, dict) and result.get("status") == "pending_confirmation":
            return (
                f"Error: 操作已挂起 (PENDING_USER_CONFIRMATION)。\n"
                f"系统已向用户发送确认请求，请等待用户指令。\n"
                f"**不要重复调用此工具**。"
            )

        return result

    @filter.llm_tool()
    async def file_send_tool(self, event: AstrMessageEvent, file_path: str):
        """
        将工作文件作为附件发送给用户。

        当用户要求查看、下载、发送某个文件时使用此工具。
        文件会以真实的文件附件形式发送，用户可以下载到本地。

        Args:
            file_path (str): 文件路径（支持 VFS 路径，如 @personal/P_SOUL.md）

        Returns:
            发送结果
        """
        from ..tools.common.file_ops import file_send

        return await file_send(event, file_path, self)

    @filter.llm_tool(name="web_search_tool")
    @compact_result(max_tokens=get_tool_max_tokens, strategy="head_tail")
    async def web_search_tool_wrapper(
        self, event: AstrMessageEvent, query: str, depth: str = "normal", save_to_memory: bool = False
    ):
        """
        网页搜索工具 - 从互联网获取最新信息

        适用于查询新闻、事实、教程、用户偏好等需要外部信息的场景。

        Args:
            query (str): 搜索关键词
            depth (str): 搜索深度，可选：quick（快速）, normal（标准）, deep（深度）
            save_to_memory (bool): 是否将重要信息保存到记忆（默认 False）

        Returns:
            搜索结果摘要
        """
        if not self.web_search_tool:
            return "⚠️ **网页搜索功能未启用**\n\n请检查配置中是否启用了 web_search_enabled"

        from ..tools.web_search_tool import SearchDepth

        depth_map = {"quick": SearchDepth.QUICK, "normal": SearchDepth.NORMAL, "deep": SearchDepth.DEEP}

        search_depth = depth_map.get(depth.lower(), SearchDepth.NORMAL)

        uid, group_id, _ = self._get_identity(event)

        user_context = {"uid": uid, "group_id": group_id}

        result = await self.web_search_tool.search(
            query=query, depth=search_depth, save_to_memory=save_to_memory, user_context=user_context
        )

        return result

    @filter.llm_tool()
    @compact_result(max_tokens=get_tool_max_tokens, strategy="head_tail")
    async def web_fetch_tool(self, event: AstrMessageEvent, url: str):
        """
        获取指定 URL 的网页内容。

        当用户发送了一个链接，或者需要读取某个网页的具体内容时使用此工具。
        注意：这不同于 web_search_tool（搜索引擎），本工具是直接读取网页原文。

        Args:
            url (str): 要获取的网页 URL（必须以 http:// 或 https:// 开头）

        Returns:
            网页的标题和正文内容（Markdown 格式），或错误信息

        示例:
            - 用户发送 "https://example.com/article/123" → 调用本工具获取文章内容
            - 需要读取文档页面时使用本工具
        """
        from ..tools.web_fetch_tool import WebFetchConfig, WebFetcher

        config = WebFetchConfig()
        fetcher = WebFetcher(config)

        try:
            result = await fetcher.fetch(url)

            if result.error:
                return f"❌ 获取网页失败: {result.error}"

            output = [
                f"📄 **网页标题**: {result.title}",
                f"🔗 **来源**: {result.url}",
                f"📊 **大小**: {result.content_length} 字符",
                "",
                "**内容预览**:",
                "",
                result.content,
            ]

            if result.metadata.get("author"):
                output.insert(3, f"✍️ **作者**: {result.metadata['author']}")

            return "\n".join(output)

        except ValueError as e:
            return f"❌ URL 无效或不安全: {e}"
        except Exception as e:
            return f"❌ 获取网页失败（未知错误）: {e}"

    @filter.llm_tool()
    @compact_result(max_tokens=get_tool_max_tokens, strategy="head_tail")
    async def skill_call_tool(self, event: AstrMessageEvent, skill_name: str, instruction: str, mode: str = "auto"):
        """
        调用一个专业技能来完成特定任务。

        当你需要使用专业领域的知识或工作流时，使用此工具。
        这比你自己摸索更高效，因为技能包含了最佳实践和专用工具链。

        Args:
            skill_name (str): 技能名称（必须是已注册的技能）
                可用技能：
                - "scriptor-knowledge-research": 知识库与研究专家（提取知识、发起研究）
                - "scriptor-todo-schedule": 待办与日程管理（创建待办、设置提醒）
                - "scriptor-archive-manager": 档案馆管理（归档长文、查询历史）
                - "scriptor-media-gallery": 媒体画廊（上传图片、管理相册）
                - "scriptor-group-admin": 群组管理员（权限管理、成员管理）
            instruction (str): 具体指令（告诉技能要做什么）
                例如："把用户说的这个偏好记录下来"
            mode (str): 执行模式
                - "auto": 自动选择（默认，简单任务用inline，复杂任务用forked）
                - "inline": 同步执行，返回完整结果（适合快速任务）
                - "forked": 异步后台执行，立即返回任务ID（适合耗时任务）

        Returns:
            技能执行结果或任务跟踪信息

        示例:
            - skill_name="scriptor-knowledge-research", instruction="记录用户偏好：喜欢Python"
            - mode="inline" → 直接返回执行结果
            - mode="forked" → 返回任务ID，可稍后查询
        """
        from ..tools.skill_tool import get_cooldown_manager, get_skill_executor, get_skill_registry

        registry = get_skill_registry()
        executor = get_skill_executor()
        cooldown = get_cooldown_manager()

        if not registry or not executor:
            return "❌ 技能系统未初始化"

        skill = registry.get_skill(skill_name)
        if not skill:
            available = [s.name for s in registry.list_skills()]
            return f"❌ 未找到技能: {skill_name}\n\n可用技能:\n" + "\n".join(f"- {s}" for s in available)

        uid, group_id, _ = self._get_identity(event)
        session_id = f"{uid}_{group_id}"

        if not cooldown.can_execute(skill.name, session_id):
            remaining = cooldown.get_remaining_cooldown(skill.name, session_id)
            return f"⏳ 技能 **{skill.display_name}** 正在冷却中，请等待 {int(remaining)} 秒后再试"

        if mode == "auto":
            estimated_complexity = len(instruction)
            mode = "forked" if estimated_complexity > 100 else "inline"

        try:
            if mode in ("auto", "inline"):
                result = await executor.execute_inline(event, skill, instruction)
            elif mode == "forked":
                result = await executor.execute_forked(event, skill, instruction)
            else:
                return f"❌ 无效的执行模式: {mode}（可选: auto/inline/forked）"

            return result

        except Exception as e:
            logger.error(f"[SkillTool] 执行失败: {skill_name}, {e}")
            return f"❌ 技能执行失败: {e}"

    @filter.llm_tool()
    @compact_result(max_tokens=get_tool_max_tokens, strategy="head_tail")
    async def skill_status_tool(self, event: AstrMessageEvent, task_id: str = ""):
        """
        查询后台技能任务的执行状态。

        当你使用 forked 模式执行技能后，可以用此工具查询任务进度或结果。

        Args:
            task_id (str): 任务ID（可选，不填则列出所有进行中的任务）

        Returns:
            任务状态详情或任务列表
        """
        from ..tools.skill_tool import get_task_store

        store = get_task_store()
        if not store:
            return "❌ 技能系统未初始化"

        if task_id:
            task = store.get(task_id)
            if not task:
                return f"❌ 任务 {task_id} 不存在"

            elapsed = time.time() - task.created_at
            status_icons = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌", "cancelled": "🚫"}
            icon = status_icons.get(task.status, "❓")

            output = [
                f"📋 **任务详情**: {task_id}",
                "",
                f"- 技能: {task.skill_name}",
                f"- 状态: {icon} {task.status}",
                f"- 创建时间: {time.strftime('%H:%M:%S', time.localtime(task.created_at))}",
                f"- 已运行: {elapsed:.1f}s",
            ]

            if task.result:
                result_preview = task.result[:300] + ("..." if len(task.result) > 300 else "")
                output.append(f"- 结果预览:\n{result_preview}")

            if task.error:
                output.append(f"- 错误: {task.error}")

            return "\n".join(output)

        else:
            active_tasks = [t for t in store.list_all() if t.status in ("pending", "running")]

            if not active_tasks:
                return "📭 当前没有进行中的后台技能任务"

            output = ["📋 **后台任务列表**:", ""]
            for task in active_tasks[:10]:
                elapsed = time.time() - task.created_at
                icon = "⏳" if task.status == "pending" else "🔄"

                output.append(f"{icon} `{task.task_id}` {task.skill_name} " f"({task.status}, {elapsed:.0f}s)")

            if len(active_tasks) > 10:
                output.append(f"\n... 还有 {len(active_tasks) - 10} 个任务未显示")

            return "\n".join(output)

    @filter.llm_tool()
    async def skill_cancel_tool(self, event: AstrMessageEvent, task_id: str):
        """
        取消正在执行的后台技能任务。

        当 forked 模式的任务运行时间过长或不再需要时，使用此工具取消它。

        Args:
            task_id (str): 要取消的任务ID（从 skill_status 或 skill_call_tool 的返回值中获取）

        Returns:
            取消结果
        """
        from ..tools.skill_tool import get_skill_executor

        executor = get_skill_executor()
        if not executor:
            return "❌ 技能系统未初始化"

        success, message = await executor.cancel_skill(task_id)

        if success:
            return f"✅ {message}"
        else:
            return f"❌ {message}"

    @filter.llm_tool()
    async def add_schedule_task(
        self, event: AstrMessageEvent, task_content: str, trigger_time: str, recurrence: str = ""
    ):
        """
        创建定时提醒任务（支持循环）。

        Args:
            task_content (str): 提醒的具体内容
            trigger_time (str): 触发时间，支持格式：
                - "YYYY-MM-DD HH:MM" （精确时间）
                - "HH:MM" （今天或明天）
                - "X minutes/hours/days later" （相对时间）
            recurrence (str): 循环规则（可选），支持自然语言：
                - "every day" / "每天" - 每天重复
                - "every monday" / "每周一" - 每周一重复
                - "every week" / "每周" - 每周重复
                - "weekdays" / "工作日" - 周一至周五
                - "weekends" / "周末" - 周六、周日
                - "every 2 days" / "每2天" - 自定义间隔

        Returns:
            确认消息（包含是否循环的信息）

        示例:
            - 单次提醒: task_content="开会", trigger_time="14:00"
            - 循环提醒: task_content="站会", trigger_time="09:00", recurrence="every weekday"
            - 每周提醒: task_content="周报", trigger_time="17:00", recurrence="every friday"
        """
        import re
        import uuid
        from datetime import datetime, timedelta

        uid, group_id, _ = self._get_identity(event)
        current_time = datetime.now()
        target_time = None

        trigger_time = trigger_time.strip()

        try:
            if "later" in trigger_time.lower():
                match = re.match(r"(\d+)\s*(minutes?|hours?|days?)\s*later", trigger_time.lower())
                if match:
                    amount = int(match.group(1))
                    unit = match.group(2)
                    if "minute" in unit:
                        target_time = current_time + timedelta(minutes=amount)
                    elif "hour" in unit:
                        target_time = current_time + timedelta(hours=amount)
                    elif "day" in unit:
                        target_time = current_time + timedelta(days=amount)

            elif trigger_time.startswith("tomorrow"):
                match = re.match(r"tomorrow\s+(\d{1,2}):(\d{2})", trigger_time.lower())
                if match:
                    hour, minute = int(match.group(1)), int(match.group(2))
                    target_time = current_time + timedelta(days=1)
                    target_time = target_time.replace(hour=hour, minute=minute, second=0, microsecond=0)

            elif ":" in trigger_time and len(trigger_time) <= 5:
                match = re.match(r"(\d{1,2}):(\d{2})", trigger_time)
                if match:
                    hour, minute = int(match.group(1)), int(match.group(2))
                    target_time = current_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if target_time < current_time:
                        target_time += timedelta(days=1)

            else:
                target_time = datetime.strptime(trigger_time, "%Y-%m-%d %H:%M")

            if target_time is None or target_time <= current_time:
                return "❌ 时间解析失败或时间已过。请使用明确的格式。"

            from ..core.scheduler import ScheduledTask

            recurrence_info = None
            task_type = "once"

            if recurrence and recurrence.strip():
                from ..tools.recurrence_parser import parse_recurrence

                parsed = parse_recurrence(recurrence.strip())

                if parsed.valid:
                    recurrence_info = parsed.to_dict()
                    task_type = f"recurring_{parsed.recurrence_type.value}"
                else:
                    return f"❌ 循环表达式解析失败: {parsed.error}"

            task = ScheduledTask(
                task_id=str(uuid.uuid4()),
                trigger_time=target_time.timestamp(),
                content=task_content,
                task_type=task_type,
                uid=uid,
                group_id=group_id,
                interval_seconds=self._calculate_interval_seconds(recurrence_info) if recurrence_info else 0,
            )
            self.scheduler.add_task(task)

            time_str = target_time.strftime("%Y-%m-%d %H:%M")

            if recurrence_info:
                return (
                    f"✅ 已设置**循环提醒**：{task_content}\n\n"
                    f"- ⏰ 首次触发: {time_str}\n"
                    f"- 🔄 循环规则: {recurrence_info.get('description', recurrence)}\n"
                    f"- 📋 任务ID: {task.task_id[:8]}\n\n"
                    f"💡 提示: 循环提醒会在每次触发后自动创建下一次"
                )
            else:
                return f"✅ 已设置提醒：{task_content}，将在 {time_str} 触发。"

        except (OSError, ValueError) as e:
            logger.error(f"[Scheduler] 创建任务失败：{e}")
            return f"❌ 创建提醒失败：{e!s}"

    def _calculate_interval_seconds(self, recurrence_info: dict) -> int:
        """根据循环规则计算间隔秒数"""
        if not recurrence_info:
            return 0

        interval = recurrence_info.get("interval", 1)
        rec_type = recurrence_info.get("recurrence_type", "daily")

        if rec_type == "daily":
            return interval * 24 * 3600
        elif rec_type == "weekly":
            return interval * 7 * 24 * 3600
        elif rec_type == "monthly":
            return interval * 30 * 24 * 3600
        else:
            return 7 * 24 * 3600

    @filter.llm_tool()
    async def add_interval_task(
        self, event: AstrMessageEvent, task_content: str, interval: str, first_trigger_time: str = ""
    ):
        """
        创建周期性定时任务。

        Args:
            task_content (str): 任务内容
            interval (str): 执行间隔，支持格式：
                - "X minutes/hours/days" (例如："30 minutes", "2 hours", "1 day")
                - "daily" / "weekly" / "monthly"
            first_trigger_time (str, optional): 首次触发时间（可选，默认立即触发）
                - 格式同 add_schedule_task

        Returns:
            确认消息
        """
        import uuid
        from datetime import datetime, timedelta

        uid, group_id, _ = self._get_identity(event)
        current_time = datetime.now()

        try:
            interval_seconds = self._parse_interval_string(interval)
            if not interval_seconds:
                return "❌ 时间间隔解析失败。支持格式：'X minutes/hours/days' 或 'daily/weekly/monthly'"

            target_time = None
            if first_trigger_time:
                target_time = self._parse_trigger_time(first_trigger_time, current_time)
                if not target_time:
                    return "❌ 首次触发时间解析失败。请使用明确的时间格式。"
            else:
                target_time = current_time + timedelta(seconds=interval_seconds)

            from ..core.scheduler import ScheduledTask

            task = ScheduledTask(
                task_id=str(uuid.uuid4()),
                trigger_time=target_time.timestamp(),
                content=task_content,
                task_type="interval",
                interval_seconds=interval_seconds,
                uid=uid,
                group_id=group_id,
            )
            self.scheduler.add_task(task)

            interval_desc = self._format_interval_description(interval_seconds)
            return f"✅ 已设置周期性任务：{task_content}\n   执行间隔：{interval_desc}\n   首次触发：{target_time.strftime('%Y-%m-%d %H:%M')}"

        except (OSError, ValueError) as e:
            logger.error(f"[Scheduler] 创建周期性任务失败：{e}")
            return f"❌ 创建周期性任务失败：{e!s}"

    def _parse_interval_string(self, interval_str: str):
        """解析时间间隔字符串为秒数"""
        import re

        interval_str = interval_str.strip().lower()

        if interval_str in ["daily", "每天"]:
            return 86400  # 24 小时
        elif interval_str in ["weekly", "每周"]:
            return 604800  # 7 天
        elif interval_str in ["monthly", "每月"]:
            return 2592000  # 30 天（近似值）

        match = re.match(r"^(\d+)\s*(minutes?|hours?|days?)$", interval_str)
        if match:
            amount = int(match.group(1))
            unit = match.group(2)
            multipliers = {"minute": 60, "minutes": 60, "hour": 3600, "hours": 3600, "day": 86400, "days": 86400}
            return amount * multipliers.get(unit, 0)

        return None

    def _parse_trigger_time(self, trigger_time: str, current_time: datetime):
        """解析触发时间字符串"""
        import re
        from datetime import timedelta

        trigger_time = trigger_time.strip()

        if "later" in trigger_time.lower():
            match = re.match(r"(\d+)\s*(minutes?|hours?|days?)\s*later", trigger_time.lower())
            if match:
                amount = int(match.group(1))
                unit = match.group(2)
                if "minute" in unit:
                    return current_time + timedelta(minutes=amount)
                elif "hour" in unit:
                    return current_time + timedelta(hours=amount)
                elif "day" in unit:
                    return current_time + timedelta(days=amount)

        elif ":" in trigger_time and len(trigger_time) <= 5:
            match = re.match(r"(\d{1,2}):(\d{2})", trigger_time)
            if match:
                hour, minute = int(match.group(1)), int(match.group(2))
                target_time = current_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target_time < current_time:
                    target_time += timedelta(days=1)
                return target_time

        else:
            try:
                return datetime.strptime(trigger_time, "%Y-%m-%d %H:%M")
            except ValueError:
                pass

        return None

    def _format_interval_description(self, interval_seconds: int) -> str:
        """将秒数格式化为可读的时间间隔描述"""
        if interval_seconds < 3600:
            minutes = interval_seconds // 60
            return f"{minutes} 分钟"
        elif interval_seconds < 86400:
            hours = interval_seconds // 3600
            return f"{hours} 小时"
        else:
            days = interval_seconds // 86400
            return f"{days} 天"

    @filter.llm_tool()
    async def view_group_members(self, event: AstrMessageEvent):
        """
        查看当前群体成员列表。

        Returns:
            成员列表
        """
        _, group_id, _ = self._get_identity(event)

        if group_id == "private":
            return "私聊无群体成员。"

        members = self.group_manager.get_group_members(group_id)
        group_name = self.group_manager.get_group_name(group_id)

        if not members:
            return f"群组 [{group_name}] 暂无成员信息。"

        parts = [f"## 群组 [{group_name}] 成员 ({len(members)}人)\n"]
        for member in members:
            role_emoji = {"owner": "👑", "admin": "⭐", "member": "👤"}.get(member.role, "👤")
            parts.append(f"- {role_emoji} {member.alias} ({member.role})")

        return "\n".join(parts)

    @filter.llm_tool()
    async def get_group_info(self, event: AstrMessageEvent, scope: str = "current"):
        """
        获取群组信息。

        当用户询问群组名称、群组ID或用户加入的群组列表时使用此工具。

        Args:
            scope (str): 查询范围。
                - current: 仅返回当前群组信息（群聊默认）
                - all: 返回用户加入的所有群组列表（私聊默认）

        Returns:
            群组信息或群组列表
        """
        uid, group_id, _ = self._get_identity(event)
        is_private_context = group_id == "private"

        if is_private_context:
            scope = "all"

        if scope == "current":
            if is_private_context:
                return "当前处于私聊环境，无群组信息。已为您列出所有加入的群组。"

            group_name = self.group_manager.get_group_name(group_id)
            return f"当前群组名称: {group_name}\n群组ID: {group_id}"

        elif scope == "all":
            joined_groups = self.group_manager.get_user_joined_groups(uid)

            if not joined_groups:
                return "您目前没有加入任何群组。"

            parts = [f"## 您加入的群组 ({len(joined_groups)}个)\n"]

            for gid in joined_groups:
                group_name = self.group_manager.get_group_name(gid)
                current_marker = " (当前)" if gid == group_id else ""
                parts.append(f"- {group_name}{current_marker}\n  群ID: {gid}")

            return "\n".join(parts)

        return "未知的查询范围，请使用 'current' 或 'all'。"

    @filter.llm_tool()
    async def finish_bootstrap(self, event: AstrMessageEvent):
        """完成首次引导

        当且仅当你已经完成了引导文件中要求的所有引导任务后，调用此工具。
        群聊场景完成 G_BOOTSTRAP.md 引导，私聊场景完成 P_BOOTSTRAP.md 引导。
        """
        uid, group_id, _ = self._get_identity(event)

        if group_id != "private":
            group_dir = self.data_dir / "groups" / group_id
            bootstrap_file = group_dir / "G_BOOTSTRAP.md"
            completed_flag = group_dir / ".group_bootstrap_completed"

            if completed_flag.exists() and bootstrap_file.exists():
                logger.info(f"[Scriptor] 检测到群组 {group_id} 手动恢复了 G_BOOTSTRAP.md，允许重新引导")
                completed_flag.unlink()

            if not bootstrap_file.exists():
                return "群组引导已经完成，无需重复操作。"

            bootstrap_file.unlink()
            logger.info(f"[Scriptor] 已删除 G_BOOTSTRAP.md: group={group_id}")

            if not completed_flag.exists():
                completed_flag.touch()
                logger.info(f"[Scriptor] 已创建群组引导完成标记：group={group_id}")

            return "✅ 群组引导程序已成功结束。你现在是一位拥有名字的、真正的群组专属管家了。"
        else:
            profile_dir = self.data_dir / "profiles" / uid
            bootstrap_file = profile_dir / "P_BOOTSTRAP.md"
            completed_flag = profile_dir / ".bootstrap_completed"

            if completed_flag.exists() and bootstrap_file.exists():
                logger.info(f"[Scriptor] 检测到用户 {uid} 手动恢复了 P_BOOTSTRAP.md，允许重新引导")
                completed_flag.unlink()

            if not bootstrap_file.exists():
                return "个人引导已经完成，无需重复操作。"

            bootstrap_file.unlink()
            logger.info(f"[Scriptor] 已删除 P_BOOTSTRAP.md: uid={uid}")

            if not completed_flag.exists():
                completed_flag.touch()
                logger.info(f"[Scriptor] 已创建个人引导完成标记：uid={uid}")

            return "✅ 个人引导程序已成功结束。你现在是一位正式的专属管家了。"

    @filter.llm_tool()
    async def core_memory_remember(
        self,
        event: AstrMessageEvent,
        judgment: str,
        reasoning: str = "",
        tags: str = "",
        strength: int = 80,
        memory_type: str = "knowledge",
    ):
        """
        永久铭记重要信息（主动记忆，永不遗忘）。

        当你发现用户的重要偏好、事实、决策或经验时，使用此工具将其永久记录。

        【重要】如果当前处于管理员模式（Sudo），记忆将被记录到全局共享记忆中，对所有用户可见。

        Args:
            judgment (str): 需要铭记的核心内容（一句话概括）
            reasoning (str): 为什么这很重要的理由（可选）
            tags (str): 标签，逗号分隔（可选）
            strength (int): 记忆强度，0-100，默认 80（可选）
            memory_type (str): 记忆类型，knowledge/preference/decision/experience，默认 knowledge

        Returns:
            确认消息
        """
        await self._wait_for_ready()
        uid, group_id, _ = self._get_identity(event)

        is_sudo = self.identity_manager.is_sudo(uid, self.config.admin_uids)

        content = judgment
        if reasoning:
            content += f"\n理由: {reasoning}"
        if tags:
            content += f"\n标签: {tags}"

        useful_score = 5.0 + (strength / 20)
        useful_score = min(15.0, useful_score)

        from ..core.interfaces import MemoryRecordParams

        await self.memory_manager.record_long_term_memory(
            MemoryRecordParams(
                uid=uid,
                group_id=group_id,
                content=content,
                memory_type=memory_type,
                strength=2.0,
                useful_score=useful_score,
                is_sudo=is_sudo,
            ),
            search_engine=self.search_engine,
        )

        if is_sudo:
            self.identity_manager.record_sudo_operation(uid, "core_memory_remember", judgment[:50])
            logger.info(f"[Scriptor] 全局核心记忆已铭记: {judgment[:50]}...")
            return f"✅ 已铭记到全局共享记忆: {judgment}"

        logger.info(f"[Scriptor] 核心记忆已铭记: {judgment[:50]}...")
        return f"✅ 已铭记: {judgment}"

    @filter.llm_tool()
    async def core_memory_recall(self, event: AstrMessageEvent, limit: int = 5, query: str = ""):
        """
        加权随机回忆核心知识（避免确定性偏见）。

        Args:
            limit (int): 最多返回多少条记忆，默认 5
            query (str): 可选的检索关键词

        Returns:
            随机抽取的核心记忆列表
        """
        await self._wait_for_ready()
        uid, group_id, _ = self._get_identity(event)

        is_private_context = group_id == "private"
        scope = "cross" if is_private_context else "group"

        if query:
            results = await self.search_engine.search(query, uid, group_id, scope, limit * 2)
        else:
            results = await self.search_engine.search("", uid, group_id, scope, limit * 3)

        if not results:
            return "（暂无可回忆的核心记忆）"

        import random

        random.shuffle(results)

        selected = results[:limit]

        parts = []
        for i, res in enumerate(selected, 1):
            source_label = {"personal": "个人画像", "group": "群体记忆", "note": "日记", "cross_group": "跨群记忆"}.get(
                res.source_type, res.source_type
            )
            parts.append(f"### {i}. [{source_label}]\n{res.content}")

        return "\n\n---\n\n".join(parts)

    @filter.llm_tool()
    async def note_recall(
        self, event: AstrMessageEvent, date: str = "", start_line: int = 1, end_line: int = 0, token_budget: int = 2000
    ):
        """
        展开笔记完整上下文（深度阅读能力）。

        Args:
            date (str): 日期，格式 YYYY-MM-DD，留空则读取最近的日记
            start_line (int): 起始行号，默认 1
            end_line (int): 结束行号，0 表示到文件末尾
            token_budget (int): Token 预算，默认 2000

        Returns:
            笔记完整内容
        """
        await self._wait_for_ready()
        uid, group_id, _ = self._get_identity(event)

        if group_id == "private":
            note_dir = self.data_dir / "profiles" / uid / "memory"
        else:
            note_dir = self.data_dir / "groups" / group_id / "memory"

        if not note_dir.exists():
            return "（暂无日记记录）"

        if date:
            target_file = note_dir / f"{date}.md"
            if not target_file.exists():
                return f"（未找到 {date} 的日记）"
        else:
            md_files = sorted(note_dir.glob("*.md"), reverse=True)
            if not md_files:
                return "（暂无日记记录）"
            target_file = md_files[0]
            date = target_file.stem

        content = target_file.read_text(encoding="utf-8")
        lines = content.split("\n")
        total_lines = len(lines)

        if end_line <= 0 or end_line > total_lines:
            end_line = total_lines
        if start_line < 1:
            start_line = 1
        if start_line > total_lines:
            start_line = total_lines

        selected_lines = lines[start_line - 1 : end_line]
        result = "\n".join(selected_lines)

        if len(result) > token_budget * 3:
            result = result[: token_budget * 3] + "\n...（内容过长，已截断）"

        return f"## 日记: {date} (行 {start_line}-{end_line}/{total_lines})\n\n{result}"

    @filter.llm_tool()
    async def learn_from_conversation(
        self,
        event: AstrMessageEvent,
        title: str,
        content: str,
        category: str = "",
    ):
        """
        【学习模式专用】从对话中提取知识并请求确认。

        这是学习模式下添加知识的唯一方式。提取的知识会先暂存，等待用户确认后才写入知识库。

        Args:
            title (str): 知识标题
            content (str): 知识内容（300-800字）
            category (str): 分类标签（可选）

        Returns:
            确认请求消息
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)

        if not self.learning_manager.is_learning(uid, group_id):
            return "⚠️ 此工具仅在【学习模式】下可用。请先使用 /开始学习 进入学习模式。"

        if self.learning_manager.is_read_only(uid, group_id):
            return "⚠️ 当前处于【授课模式】，无法添加知识。"

        from ..core.learning_manager import KnowledgeExtraction

        tag_list = []
        if category:
            tag_list.append(category)

        extraction = KnowledgeExtraction(
            content=content,
            title=title,
            knowledge_type="fact",
            tags=tag_list,
            source=f"对话学习 ({datetime.now().strftime('%Y-%m-%d')})",
        )

        conflict = self.learning_manager.check_knowledge_conflict(extraction)
        conflict_warning = ""
        if conflict:
            conflict_warning = f"\n\n⚠️ **冲突警告**: {conflict['message']}"

        prompt = await self.learning_manager.store_pending_knowledge(uid=uid, group_id=group_id, extraction=extraction)

        logger.info(f"[Scriptor] 学习模式: 暂存知识 '{title}' 等待确认")

        return prompt + conflict_warning

    @filter.llm_tool()
    async def confirm_knowledge(self, event: AstrMessageEvent):
        """
        【学习模式专用-仅用户确认后调用】确认并写入待确认的知识。

        ⚠️ 重要：此工具仅在用户明确回复"确认"、"是的"、"可以"等确认词后才可调用。
        AI 不得自动调用此工具，必须等待用户确认。

        Returns:
            写入结果消息
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)

        if not self.learning_manager.is_learning(uid, group_id):
            return "⚠️ 此工具仅在【学习模式】下可用。"

        success, message = await self.learning_manager.confirm_pending_knowledge(uid=uid, group_id=group_id)

        if success:
            logger.info("[Scriptor] 学习模式: 知识已确认并写入")
        else:
            logger.warning(f"[Scriptor] 学习模式: 确认失败 - {message}")

        return f"{'✅' if success else '❌'} {message}"

    @filter.llm_tool()
    async def revise_knowledge(self, event: AstrMessageEvent, new_content: str = "", new_title: str = ""):
        """
        【学习模式专用-仅用户要求修改时调用】修改待确认的知识内容。

        ⚠️ 重要：此工具仅在用户明确要求修改知识内容后才可调用。
        AI 不得自动调用此工具。

        Args:
            new_content (str): 修改后的知识内容（可选）
            new_title (str): 修改后的标题（可选）

        Returns:
            更新后的确认请求
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)

        if not self.learning_manager.is_learning(uid, group_id):
            return "⚠️ 此工具仅在【学习模式】下可用。"

        if not self.learning_manager.has_pending_knowledge(uid, group_id):
            return "❌ 没有待确认的知识可以修改。"

        extraction = self.learning_manager.get_pending_knowledge(uid, group_id)

        if new_content:
            extraction.content = new_content
        if new_title:
            extraction.title = new_title

        prompt = await self.learning_manager.store_pending_knowledge(uid=uid, group_id=group_id, extraction=extraction)

        return f"📝 已修改，请再次确认：\n\n{prompt}"

    @filter.llm_tool()
    @compact_result(max_tokens=get_tool_max_tokens, strategy="truncate")
    async def query_archives(self, event: AstrMessageEvent, sql: str):
        """
        在档案馆中执行 SQL 查询。

        系统会自动查询当前可访问的所有档案库（个人/群组/全局），并合并结果。

        重要：返回结果包含原始数据表格，你必须在回复中展示这个表格，以便用户可以对照数据验证。

        Args:
            sql (str): 要执行的 SELECT SQL 语句

        Returns:
            包含原始数据表格的查询结果
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)
        session_id = f"{uid}_{group_id}"

        should_send, decorator_msg = self.tool_decorator.should_send("query_archives", session_id)
        if decorator_msg:
            logger.debug(f"[Scriptor] query_archives decorator: {decorator_msg}")

        try:
            from ..core.archives.router import ArchiveIndex, ArchiveRouter

            router = ArchiveRouter(self.data_dir)
            index = ArchiveIndex(router)

            all_results = []
            queried_dbs = []

            is_sudo = self.identity_manager.is_sudo(uid, self.config.admin_uids)
            accessible_dbs = router.resolve_accessible_dbs(uid, group_id, is_sudo)

            for db_info in accessible_dbs:
                db_path = db_info["path"]
                scope_label = db_info["label"]

                if not db_path.exists():
                    continue

                try:
                    from ..core.archives.manager import ArchiveManager

                    manager = ArchiveManager(str(db_path))
                    results = manager.execute_query(sql)

                    for r in results:
                        r["_scope"] = db_info["scope"].value
                        r["_scope_label"] = scope_label

                    all_results.extend(results)
                    queried_dbs.append(scope_label)

                except Exception as e:
                    logger.debug(f"[Scriptor] 在 {scope_label} 中查询失败: {e}")
                    continue

            if not all_results:
                return f"## 📊 查询结果\n\n查询完成，但未找到匹配的数据（已查询：{', '.join(queried_dbs) or '无可用档案库'}）"

            total = len(all_results)
            display_data = all_results[:20]
            has_more = total > 20

            if display_data and isinstance(display_data[0], dict):
                headers = [h for h in display_data[0].keys() if not h.startswith("_")]
                header_row = "| " + " | ".join(str(h) for h in headers) + " | 层级 |"
                separator = "| " + " | ".join("---" for _ in headers) + " | --- |"
                data_rows = []
                for row in display_data:
                    scope_label = row.get("_scope_label", "")
                    data_rows.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + f" | {scope_label} |")
                markdown_table = header_row + "\n" + separator + "\n" + "\n".join(data_rows)
            else:
                markdown_table = "（数据格式不支持表格展示）"

            summary_parts = [f"共找到 {total} 条记录"]
            if has_more:
                summary_parts.append("已显示前 20 条")
            summary_parts.append(f"查询范围：{', '.join(queried_dbs)}")

            result = f"""## 📊 查询结果

**以下为原始数据，请务必在回复中展示此表格：**

{markdown_table}

---
*{'; '.join(summary_parts)}*
"""
            if has_more:
                result += f"\n... (共 {total} 条)"

            return result

        except Exception as e:
            error_msg = str(e)
            logger.error(f"[Scriptor] SQL 查询失败: {error_msg}")

            return f"## ❌ 查询失败\n\n错误信息：{error_msg}"

    @filter.llm_tool()
    async def list_archives(self, event: AstrMessageEvent):
        """
        查看所有可访问的档案表及其字段定义。

        系统会自动聚合个人、群组和全局档案库中的所有档案。

        Returns:
            档案表列表
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)
        session_id = f"{uid}_{group_id}"

        should_send, decorator_msg = self.tool_decorator.should_send("list_archives", session_id)
        if decorator_msg:
            logger.debug(f"[Scriptor] list_archives decorator: {decorator_msg}")

        try:
            from ..core.archives.router import ArchiveIndex, ArchiveRouter

            router = ArchiveRouter(self.data_dir)
            index = ArchiveIndex(router)

            archives = index.get_all_archives_flat(uid, group_id)

            if not archives:
                return {
                    "type": "archive_list",
                    "message": "暂无可访问的档案。请先使用 import_file_to_archive 工具导入数据。",
                    "archives": [],
                    "total": 0,
                }

            scope_icons = {"personal": "👤", "group": "👥", "global": "🌐"}

            archive_summary = []
            current_scope = None

            for arc in archives:
                scope = arc.get("scope", "auto")
                scope_label = arc.get("scope_label", scope)

                if scope != current_scope:
                    icon = scope_icons.get(scope, "📁")
                    archive_summary.append(f"\n### {icon} {scope_label}")
                    current_scope = scope

                archive_summary.append(f"- **{arc['display_name']}** (表名：`{arc['table_name']}`)")
                archive_summary.append(f"  - 描述：{arc['description']}")
                archive_summary.append(f"  - 数据量：{arc['row_count']} 条")
                archive_summary.append(f"  - 字段：{', '.join(arc['columns'])}")

            return {
                "type": "archive_list",
                "message": "## 📚 档案馆目录\n" + "\n".join(archive_summary),
                "archives": archives,
                "total": len(archives),
            }

        except Exception as e:
            logger.error(f"[Scriptor] 获取档案列表失败：{e}")
            return {"type": "archive_list", "message": f"❌ 获取档案列表失败：{e!s}", "archives": [], "total": 0}

    @filter.llm_tool()
    async def search_my_images(self, event: AstrMessageEvent, keyword: str = "", limit: int = 5):
        """
        搜索用户的个人图片库。

        Args:
            keyword (str): 搜索关键词
            limit (int): 返回数量限制

        Returns:
            图片列表
        """
        if not hasattr(self, "media_manager"):
            return "❌ 媒体管理器未初始化"

        uid, group_id, _ = self._get_identity(event)

        try:
            images = await self.media_manager.search_images(uid, "private", keyword, limit)

            if not images:
                return "未找到相关图片。"

            parts = [f"📸 找到 {len(images)} 张图片：\n"]
            for i, img in enumerate(images, 1):
                timestamp = datetime.fromtimestamp(img["timestamp"]).strftime("%Y-%m-%d %H:%M")
                desc = img.get("description", "")[:50] or "无描述"
                parts.append(f"{i}. {img['original_name'] or img['filename']}")
                parts.append(f"   时间：{timestamp} | 发送者：{img['sender_name']}")
                parts.append(f"   描述：{desc}")
                parts.append("")

            return "\n".join(parts)

        except Exception as e:
            logger.error(f"[Scriptor] 搜索个人图片失败：{e}")
            return f"❌ 搜索失败：{e!s}"

    @filter.llm_tool()
    async def search_group_images(self, event: AstrMessageEvent, keyword: str = "", limit: int = 5):
        """
        搜索群组的图片库。

        Args:
            keyword (str): 搜索关键词
            limit (int): 返回数量限制

        Returns:
            图片列表
        """
        if not hasattr(self, "media_manager"):
            return "❌ 媒体管理器未初始化"

        uid, group_id, _ = self._get_identity(event)

        if group_id == "private":
            return "❌ 当前处于私聊环境，无群组图片。"

        try:
            images = await self.media_manager.search_images(uid, group_id, keyword, limit)

            if not images:
                return "群组未找到相关图片。"

            parts = [f"📸 群组找到 {len(images)} 张图片：\n"]
            for i, img in enumerate(images, 1):
                timestamp = datetime.fromtimestamp(img["timestamp"]).strftime("%Y-%m-%d %H:%M")
                desc = img.get("description", "")[:50] or "无描述"
                parts.append(f"{i}. {img['original_name'] or img['filename']}")
                parts.append(f"   时间：{timestamp} | 发送者：{img['sender_name']}")
                parts.append(f"   描述：{desc}")
                parts.append("")

            return "\n".join(parts)

        except Exception as e:
            logger.error(f"[Scriptor] 搜索群组图片失败：{e}")
            return f"❌ 搜索失败：{e!s}"

    @filter.llm_tool()
    async def import_file_to_archive(
        self, event: AstrMessageEvent, filename: str, display_name: str = None, description: str = ""
    ):
        """
        将媒体库中的文件导入到档案馆（支持 Excel、CSV、TXT）。

        【重要】仅在用户明确要求"导入文件到档案馆"或类似指令时才调用此工具。
        用户仅发送文件而未说明要导入时，应先询问用户是否需要导入，不要自动调用此工具。

        导入规则：
        - 日常模式 + 私聊 → 导入到个人档案馆
        - 日常模式 + 群聊 → 导入到群组档案馆
        - 管理员模式 (sudo) → 导入到全局档案馆

        用户上传文件后，系统会自动保存到媒体库。你可以使用原始文件名（如：工资表.xlsx）来导入。

        Args:
            filename (str): 文件名（支持原始文件名或实际保存的文件名）
            display_name (str): 导入后的显示名称（可选）
            description (str): 数据表描述（可选）

        Returns:
            导入结果
        """
        if not hasattr(self, "media_manager"):
            return "❌ 媒体管理器未初始化"

        uid, group_id, _ = self._get_identity(event)

        try:
            from ..core.archives.ingestor import DataIngestor
            from ..core.archives.manager import ArchiveManager
            from ..core.archives.router import ArchiveRouter, ArchiveScope

            is_sudo = self.identity_manager.is_sudo(uid, self.config.admin_uids)

            router = ArchiveRouter(self.data_dir)
            db_path, scope = router.resolve_db_path(uid, group_id, is_sudo)

            file_path = self.media_manager.get_file_path(uid, group_id, filename)

            if not file_path or not file_path.exists():
                return f"❌ 文件不存在：{filename}"

            ext = file_path.suffix.lower()
            supported_exts = [".xlsx", ".xls", ".csv", ".txt"]

            if ext not in supported_exts:
                return f"❌ 不支持的文件类型：{ext}。支持：{', '.join(supported_exts)}"

            index_data = await self.media_manager._load_index(uid, group_id)
            original_name = filename
            for f in index_data.get("files", []):
                if f["filename"] == filename:
                    original_name = f.get("original_name", filename)
                    break

            manager = ArchiveManager(str(db_path))
            ingestor = DataIngestor(archive_manager=manager)

            table_name, row_count = ingestor.ingest_excel(
                file_path=str(file_path),
                display_name=display_name or original_name,
                description=description,
                scope=scope.value,
            )

            import sqlite3

            with sqlite3.connect(str(db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(f"PRAGMA table_info({table_name})")
                columns = cursor.fetchall()
                col_count = len(columns)

            scope_labels = {
                ArchiveScope.PERSONAL: "个人档案馆",
                ArchiveScope.GROUP: "群组档案馆",
                ArchiveScope.GLOBAL: "全局档案馆",
            }
            scope_label = scope_labels.get(scope, "档案馆")

            logger.info(f"[Scriptor] 文件已导入{scope_label}：{filename} -> {table_name}")

            if is_sudo:
                self.identity_manager.record_sudo_operation(
                    uid, "import_archive", f"导入 {original_name} -> {table_name}"
                )

            return (
                f"✅ 文件导入成功！\n"
                f"   原始文件：{original_name}\n"
                f"   目标位置：{scope_label}\n"
                f"   表名：{table_name}\n"
                f"   行数：{row_count}\n"
                f"   列数：{col_count}\n\n"
                f"现在可以使用 `query_archives` 工具查询这个表的数据。"
            )

        except Exception as e:
            logger.error(f"[Scriptor] 导入文件到档案馆失败：{e}")
            return f"❌ 导入失败：{e!s}"

    @filter.llm_tool()
    async def delete_archive_table(self, event: AstrMessageEvent, table_name: str):
        """
        删除档案馆中的表。

        【权限要求】此操作需要管理员权限。
        请先使用 /sudo_state_up 进入管理员模式。

        【重要】删除操作不可恢复，请谨慎操作。

        Args:
            table_name (str): 要删除的表名

        Returns:
            删除结果
        """
        uid, group_id, _ = self._get_identity(event)

        if not self.identity_manager.is_sudo(uid, self.config.admin_uids):
            return "❌ 权限不足：此操作需要管理员权限。\n\n请先使用 /sudo_state_up 进入管理员模式。"

        try:
            from ..core.archives.manager import ArchiveManager
            from ..core.archives.router import ArchiveIndex, ArchiveRouter

            router = ArchiveRouter(self.data_dir)
            index = ArchiveIndex(router)

            result = index.find_table_scope(uid, group_id, table_name)

            if not result:
                return f"❌ 表不存在或无权访问：{table_name}"

            db_path, scope = result

            manager = ArchiveManager(str(db_path))
            success = manager.unregister_table(table_name)

            if success:
                self.identity_manager.record_sudo_operation(
                    uid, "delete_archive", f"删除表 {table_name} (层级: {scope.value})"
                )

                logger.info(f"[Scriptor] 管理员 {uid} 删除了档案表：{table_name}")

                return f"✅ 已删除档案表：{table_name}\n   层级：{scope.value}"
            else:
                return f"❌ 删除失败：表 {table_name} 不存在"

        except Exception as e:
            logger.error(f"[Scriptor] 删除档案表失败：{e}")
            return f"❌ 删除失败：{e!s}"

    @filter.llm_tool()
    async def update_archive_metadata(
        self, event: AstrMessageEvent, table_name: str, display_name: str = None, description: str = None
    ):
        """
        修改档案的元数据（显示名称或描述）。

        【权限要求】此操作需要管理员权限。
        请先使用 /sudo_state_up 进入管理员模式。

        此操作不会修改档案的实际数据，仅修改元数据。

        Args:
            table_name (str): 表名
            display_name (str): 新的显示名称（可选）
            description (str): 新的描述（可选）

        Returns:
            修改结果
        """
        uid, group_id, _ = self._get_identity(event)

        if not self.identity_manager.is_sudo(uid, self.config.admin_uids):
            return "❌ 权限不足：此操作需要管理员权限。\n\n请先使用 /sudo_state_up 进入管理员模式。"

        if display_name is None and description is None:
            return "❌ 请至少提供一个要修改的字段（display_name 或 description）"

        try:
            from ..core.archives.manager import ArchiveManager
            from ..core.archives.router import ArchiveIndex, ArchiveRouter

            router = ArchiveRouter(self.data_dir)
            index = ArchiveIndex(router)

            result = index.find_table_scope(uid, group_id, table_name)

            if not result:
                return f"❌ 表不存在或无权访问：{table_name}"

            db_path, scope = result

            manager = ArchiveManager(str(db_path))
            success = manager.update_metadata(table_name, display_name, description)

            if success:
                changes = []
                if display_name:
                    changes.append(f"显示名称 → {display_name}")
                if description:
                    changes.append(f"描述 → {description}")

                self.identity_manager.record_sudo_operation(
                    uid, "update_archive_metadata", f"更新 {table_name}: {', '.join(changes)}"
                )

                logger.info(f"[Scriptor] 管理员 {uid} 更新了档案元数据：{table_name}")

                return f"✅ 已更新档案元数据：{table_name}\n   修改内容：{', '.join(changes)}"
            else:
                return f"❌ 更新失败：表 {table_name} 不存在"

        except Exception as e:
            logger.error(f"[Scriptor] 更新档案元数据失败：{e}")
            return f"❌ 更新失败：{e!s}"

    @filter.llm_tool()
    @compact_result(max_tokens=get_tool_max_tokens, strategy="head_tail")
    async def learn_document(self, event: AstrMessageEvent, filename: str, category: str = ""):
        """
        从媒体库中的文档学习知识并写入知识库（支持 TXT、MD、DOC、DOCX、PDF）。

        【重要】仅在用户明确要求"学习这个文档"或"把这个文档加入知识库"时才调用此工具。
        用户仅发送文件而未说明要学习时，应先询问用户是否需要学习，不要自动调用此工具。

        用户上传文件后，系统会自动保存到媒体库。你可以使用原始文件名（如：笔记.txt）来学习。

        Args:
            filename (str): 文件名（支持原始文件名或实际保存的文件名）
            category (str): 知识分类（可选）

        Returns:
            学习结果
        """
        if not hasattr(self, "media_manager"):
            return "❌ 媒体管理器未初始化"

        uid, group_id, _ = self._get_identity(event)

        if self.learning_manager.is_read_only(uid, group_id):
            return "⚠️ 当前处于【授课模式】，知识库已锁定为只读状态。无法学习新文档。"

        try:
            file_path = self.media_manager.get_file_path(uid, group_id, filename)

            if not file_path or not file_path.exists():
                return f"❌ 文件不存在：{filename}"

            ext = file_path.suffix.lower()
            supported_exts = [".txt", ".md", ".doc", ".docx", ".pdf"]

            if ext not in supported_exts:
                return f"❌ 不支持的文件类型：{ext}。支持：{', '.join(supported_exts)}"

            index_data = await self.media_manager._load_index(uid, group_id)
            original_name = filename
            for f in index_data.get("files", []):
                if f["filename"] == filename:
                    original_name = f.get("original_name", filename)
                    break

            content = await self._read_document_content(file_path, ext)

            if not content:
                return "❌ 无法读取文档内容或文档为空"

            chunks_added = await self._process_document_chunks(
                content=content, source_name=original_name, category=category, uid=uid, group_id=group_id
            )

            logger.info(f"[Scriptor] 文档学习完成：{original_name}，提取 {chunks_added} 个知识点")

            return (
                f"✅ 文档学习完成！\n"
                f"   文件：{original_name}\n"
                f"   提取知识点：{chunks_added} 条\n"
                f"   已写入知识库和知识图谱\n\n"
                f"现在可以使用 `knowledge_search` 搜索这些知识。"
            )

        except Exception as e:
            logger.error(f"[Scriptor] 文档学习失败：{e}")
            return f"❌ 学习失败：{e!s}"

    @filter.llm_tool()
    async def add_todo(
        self,
        event: AstrMessageEvent,
        content: str,
        scope: str = "personal",
        due_date: str = "",
        advance_minutes: int = 0,
        cron_expression: str = "",
        priority: int = 2,
        need_reminder: bool = False,
    ):
        """
        添加新的待办事项（支持时间属性、周期性任务、优先级和自动提醒）。

        【核心工具】当用户要求记录任务、提醒、计划、待办时使用此工具。
        支持一次性任务、周期性任务、提前提醒等功能。

        【重要】此工具已整合提醒功能。当用户要求定时提醒或周期性提醒时，
        只需设置 due_date 和 need_reminder=True，无需再调用 add_schedule_task。

        Args:
            content (str): 待办事项内容
            scope (str): 作用域，"personal"（个人）或 "group"（群组）
            due_date (str): 截止时间/提醒时间，格式："YYYY-MM-DD HH:MM" 或 "HH:MM"（今天或明天）
            advance_minutes (int): 提前提醒分钟数，0 = 准点提醒（默认）
            cron_expression (str): 周期性任务表达式，如 "daily"（每天）、"weekly"（每周）、
                                   "0 9 * * *"（每天9点，标准Cron格式）
            priority (int): 优先级，1=低、2=中（默认）、3=高
            need_reminder (bool): 是否需要在截止时间到达时自动发送提醒（默认 False）

        Returns:
            添加结果确认

        示例:
            - 简单待办: content="买菜"
            - 定时提醒: content="开会", due_date="14:00", need_reminder=True
            - 周期任务: content="站会", due_date="09:00", cron_expression="daily", need_reminder=True
            - 提前提醒: content="会议", due_date="15:00", advance_minutes=15, need_reminder=True
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)

        if scope == "group":
            target_id = group_id if group_id != "private" else uid
        else:
            target_id = uid

        try:
            from datetime import datetime as dt

            from ..core.todo_manager import TodoManager

            manager = TodoManager(self.data_dir, scope=scope)

            parsed_due_date = None
            if due_date:
                parsed_due_date = self._parse_todo_datetime(due_date)

            linked_reminder_id = ""
            if need_reminder and parsed_due_date:
                reminder_time = parsed_due_date
                if advance_minutes > 0:
                    reminder_time = parsed_due_date - timedelta(minutes=advance_minutes)

                import uuid

                from ..core.scheduler import ScheduledTask

                task = ScheduledTask(
                    task_id=str(uuid.uuid4()),
                    trigger_time=reminder_time.timestamp(),
                    content=f"TODO提醒: {content}",
                    task_type="recurring" if cron_expression else "once",
                    uid=uid,
                    group_id=group_id if group_id != "private" else "",
                )
                self.scheduler.add_task(task)
                linked_reminder_id = task.task_id
                logger.info(f"[Scriptor] 已创建关联提醒: {linked_reminder_id[:8]}")

            item = manager.add_todo(
                target_id,
                content,
                due_date=parsed_due_date,
                advance_minutes=advance_minutes,
                cron_expression=cron_expression,
                priority=priority,
                linked_reminder_id=linked_reminder_id,
            )

            result_lines = [f"✅ 已添加待办事项：", f"   内容：{content}", f"   ID：{item.id}"]

            if parsed_due_date:
                result_lines.append(f"   截止时间：{parsed_due_date.strftime('%Y-%m-%d %H:%M')}")
            if advance_minutes > 0:
                result_lines.append(f"   提前提醒：{advance_minutes} 分钟")
            if cron_expression:
                result_lines.append(f"   周期规则：{cron_expression}")
            if priority != 2:
                priority_labels = {1: "低", 3: "高"}
                result_lines.append(f"   优先级：{priority_labels.get(priority, '中')}")
            if need_reminder and linked_reminder_id:
                result_lines.append(f"   🔔 已设置自动提醒")

            result_lines.append(f"\n使用 `complete_todo` 标记完成，或 `update_todo` 修改内容。")

            return "\n".join(result_lines)

        except Exception as e:
            logger.error(f"[Scriptor] 添加待办失败：{e}")
            return f"❌ 添加失败：{e!s}"

    def _parse_todo_datetime(self, time_str: str) -> Optional[datetime]:
        """解析待办时间字符串"""
        import re
        from datetime import datetime as dt

        time_str = time_str.strip()
        now = dt.now()

        if re.match(r"^\d{1,2}:\d{2}$", time_str):
            hour, minute = map(int, time_str.split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target

        if re.match(r"^\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}$", time_str):
            try:
                return dt.strptime(time_str, "%Y-%m-%d %H:%M")
            except ValueError:
                pass

        if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", time_str):
            try:
                return dt.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        return None

    @filter.llm_tool()
    async def complete_todo(self, event: AstrMessageEvent, task_id: int, scope: str = "personal"):
        """
        将待办事项标记为已完成（支持周期性任务自动推延）。

        当用户表示某项任务已完成、搞定、做完了时使用此工具。
        例如："牛奶买好了"、"那个会开完了"、"第一件事做完了"。

        【周期性任务特殊处理】
        如果待办是周期性任务（如每天站会），标记完成后会自动推延到下一个周期，
        而不是归档。系统会自动更新截止时间和关联的定时提醒。

        Args:
            task_id (int): 待办事项的 ID（从热记忆中获取）
            scope (str): 作用域，"personal" 或 "group"

        Returns:
            完成结果确认
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)

        if scope == "group":
            target_id = group_id if group_id != "private" else uid
        else:
            target_id = uid

        try:
            from ..core.todo_manager import TodoManager

            manager = TodoManager(self.data_dir, scope=scope)
            success, item = manager.complete_todo(target_id, task_id, scheduler=self.scheduler)

            if success and item:
                if item.is_recurring() and item.status == "pending":
                    return (
                        f"✅ 周期任务已完成并推延到下一周期：\n"
                        f"   内容：{item.content}\n"
                        f"   新截止时间：{item.due_date.strftime('%Y-%m-%d %H:%M') if item.due_date else '未设置'}\n"
                        f"   周期规则：{item.cron_expression}"
                    )
                else:
                    return f"✅ 已将待办事项 (ID: {task_id}) 标记为完成"
            else:
                return f"⚠️ 未找到待办事项 (ID: {task_id})，请检查 ID 是否正确"

        except Exception as e:
            logger.error(f"[Scriptor] 完成待办失败：{e}")
            return f"❌ 操作失败：{e!s}"

    @filter.llm_tool()
    async def update_todo(
        self,
        event: AstrMessageEvent,
        task_id: int,
        new_content: str = "",
        scope: str = "personal",
        due_date: str = "",
        advance_minutes: int = -1,
        cron_expression: str = "",
        priority: int = -1,
    ):
        """
        修改现有待办事项（支持修改时间、周期、优先级）。

        当用户要求修改、更新、更改某个待办事项时使用此工具。
        例如："把下午三点的会推迟到四点"、"把买菜改成买水果"、"提高这个任务的优先级"。

        【自动联动】修改截止时间时，系统会自动更新关联的定时提醒。

        Args:
            task_id (int): 待办事项的 ID
            new_content (str): 新的待办内容（可选，不修改则留空）
            scope (str): 作用域，"personal" 或 "group"
            due_date (str): 新的截止时间（可选，格式同 add_todo）
            advance_minutes (int): 新的提前提醒分钟数（可选，-1 表示不修改）
            cron_expression (str): 新的周期规则（可选）
            priority (int): 新的优先级（可选，-1 表示不修改）

        Returns:
            更新结果确认
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)

        if scope == "group":
            target_id = group_id if group_id != "private" else uid
        else:
            target_id = uid

        try:
            from datetime import datetime as dt

            from ..core.todo_manager import TodoManager

            manager = TodoManager(self.data_dir, scope=scope)

            parsed_due_date = None
            if due_date:
                parsed_due_date = self._parse_todo_datetime(due_date)

            update_kwargs = {"scheduler": self.scheduler}
            if new_content:
                update_kwargs["new_content"] = new_content
            if parsed_due_date:
                update_kwargs["due_date"] = parsed_due_date
            if advance_minutes >= 0:
                update_kwargs["advance_minutes"] = advance_minutes
            if cron_expression:
                update_kwargs["cron_expression"] = cron_expression
            if priority >= 0:
                update_kwargs["priority"] = priority

            success, item = manager.update_todo(target_id, task_id, **update_kwargs)

            if success and item:
                result_lines = [f"✅ 已更新待办事项 (ID: {task_id})："]
                result_lines.append(f"   内容：{item.content}")
                if item.due_date:
                    result_lines.append(f"   截止时间：{item.due_date.strftime('%Y-%m-%d %H:%M')}")
                if item.advance_minutes > 0:
                    result_lines.append(f"   提前提醒：{item.advance_minutes} 分钟")
                if item.cron_expression:
                    result_lines.append(f"   周期规则：{item.cron_expression}")
                if item.priority != 2:
                    priority_labels = {1: "低", 3: "高"}
                    result_lines.append(f"   优先级：{priority_labels.get(item.priority, '中')}")
                return "\n".join(result_lines)
            else:
                return f"⚠️ 未找到待办事项 (ID: {task_id})"

        except Exception as e:
            logger.error(f"[Scriptor] 更新待办失败：{e}")
            return f"❌ 操作失败：{e!s}"

    @filter.llm_tool()
    async def delete_todo(self, event: AstrMessageEvent, task_id: int, scope: str = "personal"):
        """
        删除/取消待办事项（自动清理关联的定时提醒）。

        当用户表示某项任务取消、不需要了、删掉时使用此工具。
        例如："那个会取消了"、"不用记了"、"删除第一个待办"。

        【自动联动】删除待办时，系统会自动删除关联的定时提醒，避免"幽灵提醒"。

        Args:
            task_id (int): 待办事项的 ID
            scope (str): 作用域，"personal" 或 "group"

        Returns:
            删除结果确认
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)

        if scope == "group":
            target_id = group_id if group_id != "private" else uid
        else:
            target_id = uid

        try:
            from ..core.todo_manager import TodoManager

            manager = TodoManager(self.data_dir, scope=scope)
            success, item = manager.delete_todo(target_id, task_id, scheduler=self.scheduler)

            if success:
                result = f"✅ 已删除待办事项 (ID: {task_id})"
                if item and item.linked_reminder_id:
                    result += f"\n   🔔 已同步删除关联的定时提醒"
                return result
            else:
                return f"⚠️ 未找到待办事项 (ID: {task_id})"

        except Exception as e:
            logger.error(f"[Scriptor] 删除待办失败：{e}")
            return f"❌ 操作失败：{e!s}"

    @filter.llm_tool()
    async def query_todo_history(
        self,
        event: AstrMessageEvent,
        time_range: str = None,
        specific_date: str = None,
        keyword: str = None,
        status: str = "all",
        limit: int = 20,
    ):
        """
        查询历史待办事项。

        当用户询问过去的待办记录、完成历史等时使用此工具。
        例如："我上个月完成了什么？"、"去年12月有什么待办？"、"有没有关于周报的待办？"

        Args:
            time_range (str): 时间范围，如 "2026-03"（某月）或 "2026"（某年）
            specific_date (str): 具体日期，如 "2026-03-05"
            keyword (str): 关键词搜索
            status (str): 状态过滤，"completed"、"pending" 或 "all"
            limit (int): 返回条数限制（默认 20）

        Returns:
            匹配的历史待办列表
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)

        try:
            from ..core.todo_manager import TodoManager

            manager = TodoManager(self.data_dir, scope="personal")
            items = manager.query_history(
                uid=uid, time_range=time_range, specific_date=specific_date, keyword=keyword, status=status, limit=limit
            )

            if not items:
                return "📋 未找到匹配的待办记录"

            lines = ["📋 **历史待办记录**", ""]

            for item in items:
                status_icon = "✅" if item.status == "completed" else "⏳"
                created_str = item.created_at.strftime("%Y-%m-%d %H:%M")
                completed_str = (
                    f" (完成于: {item.completed_at.strftime('%Y-%m-%d %H:%M')})" if item.completed_at else ""
                )
                lines.append(f"{status_icon} [{created_str}] {item.content}{completed_str}")

            if len(items) >= limit:
                lines.append("")
                lines.append(f"*共找到更多记录，仅显示前 {limit} 条*")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"[Scriptor] 查询待办历史失败：{e}")
            return f"❌ 查询失败：{e!s}"

    @filter.llm_tool()
    async def search_historical_todos(
        self,
        event: AstrMessageEvent,
        scope: str = "personal",
        year: int = None,
        month: int = None,
        keyword: str = None,
    ):
        """
        搜索已归档的历史待办事项（冷数据检索）。

        当用户询问超过3天的历史待办记录时使用此工具。
        例如："我去年做了什么？"、"查一下2024年3月的待办"、"搜索关于服务器的历史任务"。

        Args:
            scope (str): 搜索范围，"personal"（个人）或 "group"（群组）
            year (int): 年份（可选，如 2024）
            month (int): 月份（可选，如 3）
            keyword (str): 关键词搜索（可选）

        Returns:
            匹配的历史待办列表（最多50条，防止爆仓）
        """
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)

        try:
            if scope == "group":
                if group_id == "private":
                    return "❌ 当前处于私聊环境，无法搜索群组历史待办。"
                target_id = group_id
            else:
                target_id = uid

            import re
            from pathlib import Path

            if scope == "personal":
                archive_dir = self.data_dir / "profiles" / target_id / "TODOed"
                file_prefix = "P_TODO_"
            else:
                archive_dir = self.data_dir / "groups" / target_id / "TODOed"
                file_prefix = "G_TODO_"

            if not archive_dir.exists():
                return f"📋 暂无历史归档记录（目录不存在）"

            all_results = []
            MAX_RESULTS = 50

            archive_files = sorted(archive_dir.glob(f"{file_prefix}*.md"), reverse=True)

            for archive_file in archive_files:
                filename = archive_file.name
                match = re.match(rf"{file_prefix}(\d{{4}})-(\d{{2}})\.md", filename)
                if not match:
                    continue

                file_year = int(match.group(1))
                file_month = int(match.group(2))

                if year and file_year != year:
                    continue
                if month and file_month != month:
                    continue

                try:
                    content = archive_file.read_text(encoding="utf-8")
                    lines = content.split("\n")

                    for line in lines:
                        if not line.strip().startswith("- [x]"):
                            continue

                        if keyword:
                            if keyword.lower() not in line.lower():
                                continue

                        all_results.append(
                            {
                                "file": filename,
                                "year": file_year,
                                "month": file_month,
                                "content": line.strip(),
                            }
                        )

                        if len(all_results) >= MAX_RESULTS:
                            break

                except Exception as e:
                    logger.debug(f"[Scriptor] 读取归档文件失败 {archive_file}: {e}")
                    continue

                if len(all_results) >= MAX_RESULTS:
                    break

            if not all_results:
                hint_parts = []
                if year:
                    hint_parts.append(f"{year}年")
                if month:
                    hint_parts.append(f"{month}月")
                if keyword:
                    hint_parts.append(f"关键词'{keyword}'")

                hint = "、".join(hint_parts) if hint_parts else "指定条件"
                return f"📋 未找到匹配的历史待办记录（{hint}）"

            scope_label = "个人" if scope == "personal" else "群组"
            lines = [f"📋 **{scope_label}历史待办归档**（共 {len(all_results)} 条）", ""]

            current_month = None
            for result in all_results:
                month_key = f"{result['year']}-{result['month']:02d}"
                if month_key != current_month:
                    lines.append(f"### 📅 {result['year']}年{result['month']}月")
                    current_month = month_key

                lines.append(result["content"])

            if len(all_results) >= MAX_RESULTS:
                lines.append("")
                lines.append(
                    f"⚠️ 找到大量记录，已截断显示最近的 {MAX_RESULTS} 条。请提供更精确的 year/month 或 keyword 以缩小搜索范围。"
                )

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"[Scriptor] 搜索历史待办失败：{e}")
            return f"❌ 搜索失败：{e!s}"
