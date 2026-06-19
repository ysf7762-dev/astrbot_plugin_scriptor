from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from astrbot.api import logger

from .base import BaseMixin

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from ..core.file_monitor import FileChange


class HelpersMixin(BaseMixin):
    """
    内部辅助方法 Mixin

    包含所有私有辅助方法，供其他 Mixin 调用。
    """

    def _handle_file_change(self, change: FileChange):
        """处理文件变更事件"""
        try:
            logger.debug(f"[Scriptor] 文件变更: {change.change_type} - {change.path}")

            if change.change_type in ["created", "modified"]:
                pass
            elif change.change_type == "deleted":
                logger.debug(f"[Scriptor] 文件已删除，下次索引重建时将清理: {change.path}")

        except asyncio.CancelledError:
            logger.warning("[Scriptor] 文件监控任务被取消")
        except OSError as e:
            logger.error(f"[Scriptor] 处理文件变更失败 (OS错误): {e}")

    def _cleanup_invalid_group_directories(self):
        """清理无效的群组目录（历史脏数据修复）

        某些历史版本的 Bug 可能导致 groups/private 等无效目录被创建。
        这些目录应该被清理，因为：
        1. 'private' 是私聊标记，不是真正的群组 ID
        2. 只有通过 get_or_create_group 注册的群组才应该有目录
        """
        import shutil

        groups_dir = self.data_dir / "groups"
        if not groups_dir.exists():
            return

        registered_groups = set(self.group_manager.groups.keys())

        for item in groups_dir.iterdir():
            if item.is_dir() and item.name not in registered_groups:
                if item.name.endswith(".json"):
                    continue

                logger.warning(f"[Scriptor] 清理无效群组目录: {item.name}")
                try:
                    shutil.rmtree(item)
                    logger.info(f"[Scriptor] 已清理无效群组目录: {item.name}")
                except OSError as e:
                    logger.error(f"[Scriptor] 清理无效群组目录失败 {item.name}: {e}")

    async def _wait_for_ready(self):
        """等待所有组件就绪"""
        while not self._is_ready:
            await asyncio.sleep(0.1)

    def _track_background_task(self, task: asyncio.Task) -> None:
        """追踪后台任务，便于 terminate 阶段统一取消并等待收束"""
        self._background_tasks.add(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            self._background_tasks.discard(done_task)
            try:
                if done_task.cancelled():
                    return
                exc = done_task.exception()
                if exc is not None:
                    logger.error(f"后台任务异常退出: {exc}", exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[Scriptor] 任务回调清理异常: {e}")

        task.add_done_callback(_cleanup)

    def _get_identity(self, event: AstrMessageEvent) -> tuple:
        """
        获取用户身份信息

        Returns:
            (uid, group_id, physical_user_id)
        """
        umo = event.unified_msg_origin
        platform = umo.split(":")[0] if umo else "unknown"

        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name() or "User"

        uid = self.identity_manager.get_or_create_uid(sender_id, platform, sender_name, umo)

        is_private = event.is_private_chat() if hasattr(event, "is_private_chat") else False
        if is_private:
            group_id = "private"
        else:
            raw_group = getattr(event.message_obj, "group_id", None)
            if raw_group is not None and raw_group != "" and raw_group != 0 and str(raw_group).strip():
                group_id = f"{platform}_group_{raw_group}"

                group_name = str(raw_group)
                group_obj = getattr(event.message_obj, "group", None)
                if group_obj and hasattr(group_obj, "group_name") and group_obj.group_name:
                    group_name = group_obj.group_name

                self.group_manager.get_or_create_group(group_id, group_name, platform, uid)

                self.group_manager.add_member(group_id, uid, sender_name, "member")
            else:
                group_id = "private"

        return uid, group_id, f"{platform}:{sender_id}"

    async def _update_group_state(self, group_id: str, is_summoned: bool):
        """更新群组交互状态 (简单三状态：absent/observing/active)"""
        now = time.time()
        last_active = self._group_last_active.get(group_id, 0)

        if is_summoned:
            state = "active"
        elif now - last_active < 600:
            state = "observing"
        else:
            state = "absent"

        self._group_states[group_id] = state
        self._group_last_active[group_id] = now
        return state

    def _parse_mentions(self, event: AstrMessageEvent) -> tuple:
        """解析消息中的 @ 提及并替换文本

        核心原则：使用物理ID（如QQ号）作为唯一标识符，放弃不稳定的昵称。

        同时实现"顺手牵羊"式群成员登记：
        当解析到 At 节点时，将被 @ 的人自动注册到群成员列表中，
        即使该用户从未在群里发过言。
        """
        uid, group_id, _ = self._get_identity(event)

        if group_id == "private":
            return event.message_str, []

        message_chain = event.get_messages()
        if not message_chain:
            return event.message_str, []

        processed_parts = []
        mentions_list = []
        umo = event.unified_msg_origin
        platform = umo.split(":")[0] if umo else "unknown"

        from astrbot.api.message_components import At, AtAll, Plain

        self_id = str(event.get_self_id())

        for part in message_chain:
            if isinstance(part, At):
                target_id = str(part.qq)

                if target_id == self_id:
                    continue

                target_uid = self.identity_manager.get_or_create_uid(target_id, platform)

                mention_str = f"[@{target_id}(UID:{target_uid})]"
                processed_parts.append(mention_str)
                if target_uid not in mentions_list:
                    mentions_list.append(target_uid)

                self._register_mentioned_user_to_group(group_id, target_uid, target_id)

            elif isinstance(part, AtAll):
                processed_parts.append("[@全体成员]")
            elif isinstance(part, Plain):
                processed_parts.append(part.text)
            else:
                pass

        if not processed_parts:
            return event.message_str, []

        return "".join(processed_parts), mentions_list

    def _register_mentioned_user_to_group(self, group_id: str, uid: str, physical_id: str):
        """
        将被 @ 的用户注册到群成员列表中（顺手牵羊式登记）

        这样即使该用户从未在群里发过言，只要被 @ 过，
        AI 调用 view_group_members 工具时就能看到他。
        """
        if group_id == "private":
            return

        group = self.group_manager.get_group(group_id)
        if not group:
            return

        existing_member = self.group_manager.get_member(group_id, uid)
        if existing_member:
            return

        self.group_manager.add_member(group_id, uid, physical_id, role="member")
        logger.debug(f"[Scriptor] 顺手牵羊登记群成员：group={group_id}, uid={uid}, physical_id={physical_id}")

    async def _process_media_attachments(self, event: AstrMessageEvent, uid: str, group_id: str):
        """
        处理消息中的媒体附件（图片和文件）
        """
        if not hasattr(self, "media_manager"):
            return

        message_chain = event.get_messages()
        if not message_chain:
            return

        from astrbot.api.message_components import File as FileComponent
        from astrbot.api.message_components import Image

        sender_name = event.get_sender_name() or "User"
        sender_info = {"uid": uid, "name": sender_name}

        for part in message_chain:
            try:
                if isinstance(part, Image):
                    file_path = await part.convert_to_file_path()
                    if file_path and os.path.exists(file_path):
                        with open(file_path, "rb") as f:
                            image_data = f.read()

                        original_name = getattr(part, "name", "") or ""

                        description = ""
                        if self.config.media_save_to_memory:
                            try:
                                from ..core.memory_manager import MemoryManager

                                memory_manager = MemoryManager(
                                    self.data_dir, self.config, self.identity_manager, self.group_manager, self.context
                                )
                                description = await memory_manager.get_image_paraphrase(image_data)
                                logger.info(f"[Scriptor] 图片描述已生成：{description[:50]}...")
                            except Exception as e:
                                logger.warning(f"[Scriptor] 生成图片描述失败：{e}")

                        media_info = await self.media_manager.save_image(
                            image_data=image_data,
                            uid=uid,
                            group_id=group_id,
                            sender_info=sender_info,
                            description=description,
                            original_name=original_name,
                        )

                        if media_info:
                            logger.info(f"[Scriptor] 图片已保存到媒体库：{media_info['filename']}")

                elif isinstance(part, FileComponent):
                    file_path = await part.get_file()
                    if file_path and os.path.exists(file_path):
                        with open(file_path, "rb") as f:
                            file_data = f.read()
                        filename = part.name or "unknown_file"

                        if file_data and filename:
                            media_info = await self.media_manager.save_file(
                                file_data=file_data,
                                filename=filename,
                                uid=uid,
                                group_id=group_id,
                                sender_info=sender_info,
                                description="",
                            )

                            if media_info:
                                logger.info(f"[Scriptor] 文件已保存到媒体库：{media_info['filename']}")

            except Exception as e:
                logger.warning(f"[Scriptor] 处理媒体附件失败：{e}")
                continue

    async def _convert_text_to_chain(self, text: str, event: AstrMessageEvent):
        """
        将包含 [@昵称(UID:xxx)] 的文本转换为 AstrBot 消息链

        多重智能兜底解析机制：
        1. 第一层：查映射表（逻辑ID/昵称 -> 物理ID）
        2. 第二层：智能去前缀提取（user_xxx -> xxx）
        3. 第三层：物理ID兜底（直接使用纯数字）
        """
        import re

        from astrbot.api.all import MessageChain
        from astrbot.api.message_components import At, Plain

        chain = MessageChain()
        pattern = r"\[@.*?\s*\(UID:\s*([a-zA-Z0-9_]+)\s*\)\]"

        last_pos = 0
        for match in re.finditer(pattern, text):
            plain_text = text[last_pos : match.start()]
            if plain_text:
                chain.chain.append(Plain(plain_text))

            target_uid = match.group(1)
            umo = event.unified_msg_origin
            platform = umo.split(":")[0].lower() if umo else "unknown"

            physical_id = self._resolve_at_target(target_uid, platform)

            if physical_id:
                chain.chain.append(At(qq=physical_id))
                logger.debug(f"[Scriptor] At解析成功: target_uid={target_uid} -> physical_id={physical_id}")
            else:
                chain.chain.append(Plain(match.group(0)))
                logger.warning(f"[Scriptor] At解析失败，保留原文: {match.group(0)}")

            last_pos = match.end()

        remaining_text = text[last_pos:]
        if remaining_text:
            chain.chain.append(Plain(remaining_text))

        return chain

    def _resolve_at_target(self, target_uid: str, platform: str) -> Optional[str]:
        """
        多重智能兜底解析 At 目标
        """
        if not target_uid:
            return None

        physical_id = None

        if target_uid.isdigit():
            physical_id = self.identity_manager.get_physical_id_by_digit(target_uid, platform)
            if physical_id:
                logger.debug(f"[Scriptor] At解析-第一层(纯数字查表): {target_uid} -> {physical_id}")
                return physical_id
            logger.debug(f"[Scriptor] At解析-第三层(纯数字兜底): {target_uid}")
            return target_uid

        if target_uid.startswith("user_"):
            digit_part = target_uid[5:]
            if digit_part.isdigit():
                physical_id = self.identity_manager.get_physical_id_by_digit(digit_part, platform)
                if physical_id:
                    logger.debug(f"[Scriptor] At解析-第二层(去前缀查表): {target_uid} -> {physical_id}")
                    return physical_id
                logger.debug(f"[Scriptor] At解析-第二层(去前缀兜底): {target_uid} -> {digit_part}")
                return digit_part

        physical_id = self.identity_manager.get_physical_id(target_uid, platform)
        if physical_id:
            logger.debug(f"[Scriptor] At解析-第一层(逻辑ID查表): {target_uid} -> {physical_id}")
            return physical_id

        logger.debug(f"[Scriptor] At解析失败: 无法识别的目标标识符 {target_uid}")
        return None

    def _combine_prompts_with_token_control(self, base_system_prompt, hot_memory, retrieval_guidance):
        """
        智能组合多个提示词部分，确保总 token 不超限
        """
        from ..tools.common.text_utils import SmartMemoryTrimmer, TokenEstimator

        base_tokens = TokenEstimator.estimate_tokens(base_system_prompt)
        hot_memory_tokens = TokenEstimator.estimate_tokens(hot_memory)
        guidance_tokens = TokenEstimator.estimate_tokens(retrieval_guidance)

        total_current = base_tokens + hot_memory_tokens + guidance_tokens

        logger.debug(
            f"[TokenControl] Token 预估: "
            f"基础={base_tokens}, 热记忆={hot_memory_tokens}, 指导={guidance_tokens}, "
            f"总计={total_current}/{self.config.max_system_prompt_tokens}"
        )

        if total_current <= self.config.max_system_prompt_tokens:
            return base_system_prompt + "\n\n" + hot_memory + retrieval_guidance

        logger.warning(
            f"[TokenControl] Token 超限！当前 {total_current} > 限制 {self.config.max_system_prompt_tokens}，"
            f"将进行智能裁剪，并标记后台复盘任务。"
        )

        trimmer = SmartMemoryTrimmer(self.config.max_system_prompt_tokens - base_tokens)

        if hot_memory:
            trimmer.add_part("hot_memory", hot_memory, 10)

        if retrieval_guidance:
            trimmer.add_part("retrieval_guidance", retrieval_guidance, self.config.retrieval_guidance_priority)

        selected_parts, used_tokens = trimmer.trim()

        combined_parts = [base_system_prompt] if base_system_prompt else []
        for part in selected_parts:
            combined_parts.append(part.content)

        final_prompt = "\n\n".join(combined_parts)
        final_tokens = TokenEstimator.estimate_tokens(final_prompt)

        logger.info(
            f"[TokenControl] 裁剪完成: "
            f"使用 {len(selected_parts)}/{len(trimmer.parts)} 个附加部分, "
            f"最终 Token: {final_tokens}/{self.config.max_system_prompt_tokens}"
        )

        return final_prompt

    def _build_graph_extraction_prompt(self, content: str, uid: str, user_name: str) -> str:
        """构建知识图谱提取提示词（带代词消解）"""
        return f"""你是一个专业的知识图谱构建专家。请从以下对话/日记记录中提取核心实体(Entities)和关系(Relations)。

【当前用户】
姓名: {user_name}
用户ID: {uid}

【提取规则】
1. 实体原子化：实体必须是简短的名词或专有名词（如"苹果"、"北京"、"张三"），绝不能是长句。
2. 代词消解：将文本中的"我"、"自己"统一替换为 "{user_name}({uid})"。将"你"、"管家"统一替换为"AI助手"。
3. 关系标准化：尽量使用以下标准关系类型：
   - 属性/状态: is_a (是), has_property (具有属性), current_status (当前状态)
   - 情感/偏好: likes (喜欢), dislikes (讨厌), wants (想要)
   - 社交/归属: knows (认识), belongs_to (属于), works_at (工作于)
   - 空间/时间: located_in (位于), visited (去过)
   (如果标准关系无法表达，可使用简短的自定义动词)
4. 过滤噪音：忽略日常寒暄、无意义的语气词和临时性的琐事。只提取具有长期记忆价值的事实。

【输出格式】
必须严格输出合法的 JSON，不要包含任何 Markdown 标记（如 ```json）或其他解释性文字：
{{
    "entities": [
        {{"name": "实体名", "type": "Person/Location/Object/Concept/Organization"}}
    ],
    "relations": [
        {{"source": "实体A", "target": "实体B", "type": "标准关系类型"}}
    ]
}}

【输入内容】
{content[:3000]}
"""

    def _get_user_preferred_name(self, uid: str) -> Optional[str]:
        """从 P_PROFILE.md 中提取用户预设的主称呼

        Returns:
            用户预设的称呼，如果未设置则返回 None
        """
        import re

        from ..tools.security.sanitizer import sanitize_id

        uid = sanitize_id(uid)
        profile_dir = self.data_dir / "profiles" / uid

        profile_file = profile_dir / "P_PROFILE.md"
        if not profile_file.exists():
            profile_file = profile_dir / "PROFILE.md"

        if not profile_file.exists():
            return None

        try:
            content = profile_file.read_text(encoding="utf-8")
            match = re.search(r"\*\*主称呼\*\*[：:]\s*(.+?)(?:\n|$)", content)
            if match:
                preferred_name = match.group(1).strip()
                if preferred_name and preferred_name not in ["(主人希望被如何称呼？)", "（主人希望被如何称呼？）", ""]:
                    return preferred_name
        except Exception as e:
            logger.debug(f"[Scriptor] 读取用户预设称呼失败: {e}")

        return None

    async def _get_pending_tasks_for_user(self, uid: str) -> List[str]:
        """使用AI从用户最近的记忆中智能提取待办事项

        Args:
            uid: 用户逻辑 UID

        Returns:
            待办事项列表（最多3条）
        """
        import json
        import re
        from datetime import datetime, timedelta

        from ..tools.security.sanitizer import sanitize_id

        uid = sanitize_id(uid)
        profile_dir = self.data_dir / "profiles" / uid
        memory_dir = profile_dir / "memory"

        if not memory_dir.exists():
            return []

        recent_memories = []
        today = datetime.now().date()

        for i in range(3):
            check_date = today - timedelta(days=i)
            memory_file = memory_dir / f"{check_date.strftime('%Y-%m-%d')}.md"

            if memory_file.exists():
                try:
                    content = memory_file.read_text(encoding="utf-8")
                    if content.strip():
                        recent_memories.append(f"=== {check_date.strftime('%Y-%m-%d')} ===\n{content}")
                except Exception as e:
                    logger.debug(f"[Scriptor] 读取记忆文件失败: {e}")

        if not recent_memories:
            return []

        memory_text = "\n\n".join(recent_memories)

        if len(memory_text) > 3000:
            memory_text = memory_text[:3000] + "\n... (内容已截断)"

        try:
            prompt = f"""请分析以下用户的近期对话记录，提取出用户提到或暗示的待办事项、计划、任务或需要跟进的事项。

要求：
1. 只提取真正的待办事项，不要提取已完成的、纯信息查询、寒暄对话
2. 用简洁的一句话概括每条待办（不超过30字）
3. 按重要性和时效性排序
4. 如果没有明确的待办事项，返回空列表
5. 最多返回3条

用户近期对话记录：
{memory_text}

请直接返回JSON格式的待办列表，例如：
["完成项目报告", "回复客户邮件", "准备周会材料"]

如果没有待办事项，返回：[]
"""

            # 使用 AstrBot v4.x 推荐的 llm_generate 接口
            response = await self.context.llm_generate(
                chat_provider_id=await self.context.get_current_chat_provider_id(None), prompt=prompt
            )

            result_text = response.completion_text.strip() if response.completion_text else ""

            json_match = re.search(r"\[.*\]", result_text, re.DOTALL)
            if json_match:
                try:
                    tasks = json.loads(json_match.group())
                    if isinstance(tasks, list):
                        cleaned_tasks = []
                        for task in tasks:
                            if isinstance(task, str) and task.strip():
                                cleaned = task.strip()[:50]
                                if cleaned and cleaned not in cleaned_tasks:
                                    cleaned_tasks.append(cleaned)
                        return cleaned_tasks[:3]
                except json.JSONDecodeError:
                    pass

            return []

        except Exception as e:
            logger.warning(f"[Scriptor] AI提取待办事项失败: {e}")
            return []

    def _clean_task_text(self, text: str) -> str:
        """清理待办文本，去除冗余内容"""
        import re

        text = re.sub(r"^[-*•]\s*", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 80:
            text = text[:77] + "..."
        return text

    async def _read_document_content(self, file_path: Path, ext: str) -> str:
        """读取文档内容"""
        try:
            if ext in [".txt", ".md"]:
                return file_path.read_text(encoding="utf-8")

            elif ext == ".pdf":
                try:
                    import fitz

                    doc = fitz.open(str(file_path))
                    text_parts = []
                    for page in doc:
                        text_parts.append(page.get_text())
                    doc.close()
                    return "\n\n".join(text_parts)
                except ImportError:
                    logger.warning("[Scriptor] PyMuPDF 未安装，尝试使用 pdfplumber")
                    try:
                        import pdfplumber

                        text_parts = []
                        with pdfplumber.open(str(file_path)) as pdf:
                            for page in pdf.pages:
                                text = page.extract_text()
                                if text:
                                    text_parts.append(text)
                        return "\n\n".join(text_parts)
                    except ImportError:
                        return ""

            elif ext in [".doc", ".docx"]:
                try:
                    from docx import Document

                    doc = Document(str(file_path))
                    text_parts = [para.text for para in doc.paragraphs if para.text.strip()]
                    return "\n\n".join(text_parts)
                except ImportError:
                    logger.warning("[Scriptor] python-docx 未安装")
                    return ""

            return ""

        except Exception as e:
            logger.error(f"[Scriptor] 读取文档失败: {e}")
            return ""

    async def _process_document_chunks(
        self, content: str, source_name: str, category: str, uid: str, group_id: str
    ) -> int:
        """处理文档切片并写入知识库"""
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]

        if len(paragraphs) < 3:
            paragraphs = [p.strip() for p in content.split("\n") if p.strip()]

        chunks_added = 0

        for i, para in enumerate(paragraphs):
            if len(para) < 50:
                continue

            if len(para) > 1000:
                para = para[:1000] + "..."

            title = para[:50].replace("\n", " ")
            if len(para) > 50:
                title += "..."

            from ..core.knowledge_base import KnowledgeItem, KnowledgeType

            item = KnowledgeItem.create(
                title=f"[{source_name}] {title}",
                content=para,
                knowledge_type=KnowledgeType.REFERENCE,
                category=category or source_name,
                source=f"文档学习: {source_name}",
            )

            if not self.learning_manager.is_read_only(uid, group_id):
                self.knowledge_base.add_item(item)
                chunks_added += 1

        return chunks_added
