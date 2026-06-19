from __future__ import annotations

import time
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import LLMResponse, ProviderRequest

from ..core.concurrency_guard import compute_priority_from_event
from ..core.constants import RETRIEVAL_GUIDANCE
from .base import BaseMixin

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


class EventsMixin(BaseMixin):
    """
    事件拦截 Mixin

    包含：
    - 全局消息记录器
    - LLM 请求前钩子
    - LLM 响应后钩子
    - 工具调用钩子
    - 消息装饰器
    """

    async def global_recorder(self, event: AstrMessageEvent):
        """全局消息记录器（带并发控制和消息缓冲）"""
        uid, group_id, physical_user_id = self._get_identity(event)

        concurrency_session_id = self._compute_concurrency_session_id(uid, group_id)
        priority = compute_priority_from_event(event, set(self.config.admin_uids))

        session_lock_acquired = False
        global_lock_acquired = False

        try:
            if hasattr(self, "session_lock_manager") and self.session_lock_manager:
                session_lock_acquired = await self.session_lock_manager.acquire_session(
                    concurrency_session_id, wait=True
                )

            if hasattr(self, "concurrency_guard") and self.concurrency_guard:
                global_lock_acquired = await self.concurrency_guard.acquire(concurrency_session_id, priority=priority)
                if not global_lock_acquired:
                    if session_lock_acquired:
                        self.session_lock_manager.release_session(concurrency_session_id)
                    yield "⚠️ 系统繁忙，请稍后再试"
                    return

            session_id = f"{uid}_{group_id}"

            self._last_interaction_time[session_id] = time.time()
            self._has_new_content[session_id] = True

            processed_message, mentions = self._parse_mentions(event)
            event.set_extra("processed_message", processed_message)
            event.set_extra("mentions", mentions)

            await self._process_media_attachments(event, uid, group_id)

            is_at_bot = False
            if group_id != "private":
                is_at_bot = event.is_at_or_wake_command if hasattr(event, "is_at_or_wake_command") else False
                if is_at_bot:
                    event.set_extra("is_at_triggered", True)

            if group_id != "private" and self.config.active_reply_enabled:
                message_id = None
                try:
                    if hasattr(event, "message_obj") and event.message_obj:
                        message_id = getattr(event.message_obj, "message_id", None)
                except Exception as e:
                    logger.debug(f"[Scriptor] 获取消息ID失败: {e}")

                if message_id and processed_message:
                    sender_name = event.get_sender_name() or "User"

                    decision = await self.active_reply_manager.process_message(
                        group_id=group_id,
                        message_id=message_id,
                        sender_id=uid,
                        sender_name=sender_name,
                        content=processed_message,
                        is_at_bot=is_at_bot,
                        raw_event=event,
                    )

                    if decision and decision.should_reply:
                        await self._handle_active_reply_decision(event, decision, group_id, uid)

            if processed_message:
                user_name = event.get_sender_name() or "User"

                from ..core.pending_tasks import get_pending_task_store

                pending_store = get_pending_task_store()
                concurrency_session_id_for_pending = self._compute_concurrency_session_id(uid, group_id)

                if pending_store.has_pending_task(concurrency_session_id_for_pending):
                    message_stripped = processed_message.strip().lower()
                    if not (message_stripped == "/delete" or message_stripped == "delete"):
                        _, rejected_task = pending_store.reject_task(concurrency_session_id_for_pending)
                        if rejected_task:
                            logger.info(
                                f"[Scriptor] 用户取消了删除操作: {rejected_task.file_path} (会话: {concurrency_session_id_for_pending})"
                            )

                async def process_buffered_messages(sid: str, messages: list):
                    for msg in messages:
                        await self._process_single_message(uid, group_id, sid, user_name, msg, mentions)

                await self.message_buffer.add_message(
                    session_id=session_id,
                    content=processed_message,
                    sender_id=physical_user_id,
                    callback=process_buffered_messages,
                )

        finally:
            if global_lock_acquired and hasattr(self, "concurrency_guard") and self.concurrency_guard:
                self.concurrency_guard.release(concurrency_session_id)

            if session_lock_acquired and hasattr(self, "session_lock_manager") and self.session_lock_manager:
                self.session_lock_manager.release_session(concurrency_session_id)

    async def _process_single_message(
        self, uid: str, group_id: str, session_id: str, user_name: str, message: str, mentions: list = None
    ):
        """处理单条消息（从缓冲器调用）"""
        await self.conversation_ledger.add_message(
            session_id=session_id, role="user", content=message, source="user_input"
        )

        if group_id != "private":
            now = time.time()
            last_active = self._group_last_active.get(group_id, 0)
            current_state = self._group_states.get(group_id, "absent")

            if now - last_active > 600:
                self._group_states[group_id] = "absent"
            elif current_state == "absent":
                self._group_states[group_id] = "observing"

            self._group_last_active[group_id] = now

        await self.memory_manager.record_interaction(uid, group_id, user_name, message)

        if group_id != "private":
            self.group_manager.record_group_interaction(group_id, uid, message, "member", mentions)

    async def _handle_active_reply_decision(
        self,
        event: AstrMessageEvent,
        decision,
        group_id: str,
        uid: str,
    ):
        """处理主动回复决策

        引用逻辑：
        - 完全尊重 AI 的 target_msg_id 决策
        - 如果 AI 返回 null，表示综合回复，不引用任何消息
        - 如果 AI 返回具体的 message_id，则引用该消息
        """
        from astrbot.api.message_components import Plain, Reply

        if not decision.reply_text:
            return

        try:
            reply_to_id = decision.target_msg_id

            result = event.get_result()
            if result:
                result.chain = [Plain(decision.reply_text)]

                if reply_to_id and self.config.smart_split_group_reply:
                    result.chain.insert(0, Reply(id=reply_to_id))
                    logger.info(f"[ActiveReply] 主动回复已注入引用: target_msg_id={reply_to_id}")
                else:
                    logger.info(f"[ActiveReply] 主动回复（无引用）: AI决策={reply_to_id}")

                await self.active_reply_manager.on_ai_message_sent(group_id)

        except Exception as e:
            logger.error(f"[ActiveReply] 处理主动回复决策失败: {e}")

    async def before_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 请求前：注入提示词（带 Token 控制）"""
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)

        user_message = event.message_str or ""

        if group_id != "private":
            self._group_states[group_id] = "active"
            self._group_last_active[group_id] = time.time()

        hot_memory = self.prompt_builder.build_system_prompt(uid, group_id, user_message)

        archive_catalog = self.archive_manager.get_archive_catalog_prompt()
        if archive_catalog:
            hot_memory += (
                "\n\n"
                + archive_catalog
                + "\n\n【指令】若用户问题涉及上述档案数据，请使用 `query_archives` 工具编写 SQL 查询。查询时请务必参考字段定义，确保 SQL 语法正确。"
            )

        session_id = f"{uid}_{group_id}"
        if hot_memory:
            from ..tools.common.text_utils import TokenEstimator

            hot_tokens = TokenEstimator.estimate_tokens(hot_memory)
            if hot_tokens > self.config.max_system_prompt_tokens * 0.8:
                if not self._has_new_content.get(session_id, False):
                    logger.info(
                        f"[Scriptor] 检测到 Token 压力 ({hot_tokens}/{self.config.max_system_prompt_tokens})，标记用户 {uid} 需要复盘"
                    )
                    self._has_new_content[session_id] = True

        if hot_memory:
            if self.config.enable_token_control:
                combined_prompt = self._combine_prompts_with_token_control(
                    req.system_prompt or "", hot_memory, RETRIEVAL_GUIDANCE
                )
                req.system_prompt = combined_prompt
            else:
                req.system_prompt = (req.system_prompt or "") + "\n\n" + hot_memory + RETRIEVAL_GUIDANCE

        skill_recommendation = self._inject_skill_recommendation(event, req)
        if skill_recommendation:
            req.system_prompt += skill_recommendation

        logger.debug(f"[Scriptor] 注入记忆上下文: uid={uid}, group={group_id}")

    def _inject_skill_recommendation(self, event: AstrMessageEvent, req: ProviderRequest) -> str:
        """
        v2.1: 智能推荐注入（灵魂功能）

        每次请求前，根据用户输入动态推荐最相关的技能，并注入到 System Prompt 中。
        这解决了"工具太多，LLM 记不住"的核心痛点。
        """
        try:
            from ..tools.skill_tool import get_cooldown_manager, get_skill_registry

            registry = get_skill_registry()
            if not registry or not registry._skills:
                return ""

            user_message = event.message_str or ""
            if not user_message.strip():
                return ""

            uid, group_id, _ = self._get_identity(event)
            session_id = f"{uid}_{group_id}"

            cooldown_mgr = get_cooldown_manager()

            recommended = registry.recommend_skills(
                context=user_message, limit=2, session_id=session_id, cooldown_manager=cooldown_mgr
            )

            if not recommended:
                return ""

            recommendation_text = registry.format_skill_recommendation(recommended)

            logger.debug(f"[Scriptor-Skill] 推荐了 {len(recommended)} 个技能: " f"{[s.name for s in recommended]}")

            return recommendation_text

        except Exception as e:
            logger.warning(f"[Scriptor-Skill] 技能推荐注入失败: {e}")
            return ""

    async def after_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """记录 AI 回复并触发记忆提取"""
        await self._wait_for_ready()

        uid, group_id, _ = self._get_identity(event)
        session_id = f"{uid}_{group_id}"

        if self.config.debug_mode:
            if resp:
                logger.debug(f"[Scriptor-Debug] 💬 AI 原始响应: {resp.completion_text[:1000]}...")
                if hasattr(resp, "tool_calls") and resp.tool_calls:
                    for tc in resp.tool_calls:
                        logger.debug(f"[Scriptor-Debug] 🔗 工具调用: {tc}")

        if resp and resp.completion_text:
            await self.conversation_ledger.add_message(
                session_id=session_id, role="assistant", content=resp.completion_text, source="ai_response"
            )

            is_new_session = await self.memory_manager.record_interaction(
                uid, group_id, "Assistant", resp.completion_text
            )

            if group_id != "private":
                self.group_manager.record_group_interaction(group_id, uid, resp.completion_text, "assistant")

            if self.memory_manager.should_extract_memory(resp.completion_text):
                memory_type = self.memory_manager.extract_memory_type(resp.completion_text)
                from ..core.interfaces import MemoryRecordParams

                await self.memory_manager.record_long_term_memory(
                    MemoryRecordParams(
                        uid=uid, group_id=group_id, content=resp.completion_text, memory_type=memory_type or "fact"
                    ),
                    search_engine=self.search_engine,
                )

            if self.memory_manager.should_trigger_llm_extraction(uid, group_id):
                await self._run_llm_extraction(uid, group_id)

            if is_new_session:
                logger.info(
                    f"[Scriptor] 检测到新会话 (跨天且超过60分钟未活跃)，触发睡眠巩固: uid={uid}, group={group_id}"
                )
                await self._try_sleep_consolidation(uid, group_id)

    async def on_tool_respond(self, event: AstrMessageEvent, tool, tool_args, tool_result):
        """工具执行后：记录调试日志 + 处理高危操作确认"""
        tool_name = getattr(tool, "name", str(tool))

        if self.config.debug_mode:
            logger.debug(f"[Scriptor-Debug] 🔧 工具执行完成: {tool_name}")
            logger.debug(f"[Scriptor-Debug] 📥 工具参数: {tool_args}")

            if hasattr(tool_result, "content"):
                for content_item in tool_result.content:
                    if hasattr(content_item, "text"):
                        text = content_item.text[:500] if len(content_item.text) > 500 else content_item.text
                        logger.debug(f"[Scriptor-Debug] 📤 工具返回: {text}")
                    elif hasattr(content_item, "data"):
                        logger.debug(f"[Scriptor-Debug] 📤 工具返回(非文本): {type(content_item)}")

        if tool_name == "file_delete_tool" or tool_name == "file_delete":
            result_text = ""
            if hasattr(tool_result, "content"):
                for content_item in tool_result.content:
                    if hasattr(content_item, "text"):
                        result_text += content_item.text
            elif isinstance(tool_result, str):
                result_text = tool_result

            if "PENDING_USER_CONFIRMATION" in result_text or "pending_confirmation" in result_text:
                await self._send_delete_confirmation_message(event, tool_args)

    async def _send_delete_confirmation_message(self, event: AstrMessageEvent, tool_args: dict):
        """
        发送删除确认请求消息（系统级直接发送，绕过 AI）

        当 file_delete_tool 返回 pending_confirmation 状态时调用，
        直接向用户发送确认请求，不经过 AI 处理。
        """
        try:
            file_path = tool_args.get("file_path", "未知文件") if tool_args else "未知文件"

            msg = (
                f"⚠️ **系统警告：高危操作拦截**\n\n"
                f"AI 尝试删除文件：`{file_path}`\n\n"
                f"这是一个**不可逆的高危操作**，需要您明确确认。\n\n"
                f"👉 **如果您确认要删除此文件**，请回复：`/delete`\n"
                f"👉 **如果您不想删除或改变了主意**，回复其他任意内容即可取消。\n\n"
                f"⏰ 此确认请求将在 2 分钟后自动过期。"
            )

            from astrbot.api.all import MessageChain
            from astrbot.api.message_components import Plain

            message_chain = MessageChain([Plain(msg)])
            success = await self.context.send_message(event.session, message_chain)

            if success:
                logger.info(f"[Scriptor] 已向用户发送删除确认请求 (文件: {file_path})")
            else:
                logger.warning(f"[Scriptor] 发送删除确认请求失败 (文件: {file_path})")

        except Exception as e:
            logger.error(f"[Scriptor] 发送删除确认消息时出错: {e}")

    async def on_tool_call(self, event: AstrMessageEvent, tool, tool_args):
        """工具调用前：记录详细调试日志"""
        if not self.config.debug_mode:
            return

        tool_name = getattr(tool, "name", str(tool))
        logger.debug(f"[Scriptor-Debug] ⚡ 即将调用工具: {tool_name}")
        logger.debug(f"[Scriptor-Debug] 📋 调用参数: {tool_args}")

    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        发送前的消息装饰器

        平台差异化处理策略（方案 C）：
        - QQ/微信：分段发送 + Markdown 清洗 + @ 转换 + 消息引用
        - 其他平台：不分段 + 不清洗 + @ 转换 + 消息引用（保留原始 Markdown）

        所有平台共同处理：
        - 错误消息拦截
        - 群聊 @ 提及转换
        - 群聊消息引用
        """
        try:
            result = event.get_result()
            if not result or not result.chain:
                return

            from astrbot.api.message_components import Plain

            result_text = ""
            for comp in result.chain:
                if hasattr(comp, "text"):
                    result_text += comp.text

            if not result_text:
                return

            umo = event.unified_msg_origin
            from ..core.message_sanitizer import Platform

            platform = Platform.DEFAULT

            if umo:
                parts = umo.split(":")
                platform_str = parts[0].lower() if parts else ""

                if platform_str.startswith("qq") or any(
                    x in platform_str for x in ["onebot", "aiocqhttp", "napcat", "cqhttp"]
                ):
                    platform = Platform.QQ
                elif platform_str.startswith("weixin") or "wx" in platform_str:
                    platform = Platform.WECHAT
                elif "telegram" in platform_str or "tg" in platform_str:
                    platform = Platform.TELEGRAM
                elif "discord" in platform_str:
                    platform = Platform.DISCORD

                logger.info(f"[Scriptor] 平台检测: umo={umo}, platform={platform.name}")

            is_error, error_text = self.message_sanitizer.sanitize(result_text, platform)
            if is_error:
                logger.warning("[Scriptor] 拦截到错误消息，已替换为友好提示")
                result.chain = [Plain(error_text)]
                return

            uid, group_id, _ = self._get_identity(event)

            session_id = group_id if group_id != "private" else uid

            reply_to_id = None
            is_at_triggered = event.get_extra("is_at_triggered", False)
            if group_id != "private" and self.config.smart_split_group_reply and is_at_triggered:
                try:
                    if hasattr(event, "message_obj") and event.message_obj:
                        reply_to_id = getattr(event.message_obj, "message_id", None)
                        if reply_to_id:
                            logger.debug(f"[Scriptor] 被@触发，提取消息 ID 用于引用: {reply_to_id}")
                except Exception as e:
                    logger.warning(f"[Scriptor] 提取消息 ID 失败: {e}")

            if platform in [Platform.QQ, Platform.WECHAT]:
                await self._handle_qq_wechat_platform(
                    event, result, result_text, platform, uid, group_id, session_id, reply_to_id
                )
            else:
                await self._handle_other_platform(event, result, result_text, platform, uid, group_id, reply_to_id)

        except (OSError, RuntimeError) as e:
            logger.error(f"[Scriptor] 消息装饰器处理失败: {e}")
            return

    async def _handle_qq_wechat_platform(
        self, event, result, result_text, platform, uid, group_id, session_id, reply_to_id
    ):
        """
        处理 QQ 和微信平台的消息发送

        特性：
        - 智能分段发送（根据配置）
        - Markdown 清洗
        - @ 提及转换
        - 消息引用
        """
        from astrbot.api.all import MessageChain
        from astrbot.api.message_components import Plain

        is_llm_result = result.is_llm_result() if hasattr(result, "is_llm_result") else False
        is_model_result = result.is_model_result() if hasattr(result, "is_model_result") else False
        should_smart_split = self.config.smart_split_enabled and (
            not self.config.smart_split_only_llm or is_llm_result or is_model_result
        )

        def _sanitize_text(text: str) -> str:
            """清洗文本（QQ 和微信进行 Markdown 清洗）"""
            _, cleaned = self.message_sanitizer.sanitize(text, platform)
            return cleaned

        if should_smart_split:

            async def convert_at_callback(text: str):
                """将文本中的 @ 标签转换为消息链"""
                if group_id != "private":
                    return await self._convert_text_to_chain(text, event)
                return MessageChain([Plain(text)])

            async def send_callback(message_chain):
                """发送消息链到平台"""
                try:
                    await event.send(message_chain)
                    return True
                except Exception as e:
                    logger.error(f"[Scriptor] 智能分段发送失败: {e}")
                    return False

            success = await self.smart_sender.send_with_split(
                text=result_text,
                session_id=session_id,
                send_callback=send_callback,
                convert_at_callback=convert_at_callback,
                sanitizer=self.message_sanitizer,
                platform=platform,
                reply_to_id=reply_to_id,
                target_uid=uid if group_id != "private" else None,
                debug_mode=self.config.debug_mode,
            )

            if success:
                result.chain = []
                logger.debug("[Scriptor] 智能分段发送完成")
            else:
                logger.warning("[Scriptor] 智能分段发送失败，回退到默认发送")
                fallback_text = _sanitize_text(result_text)
                result.chain = [Plain(fallback_text)]
            return

        final_text = _sanitize_text(result_text)

        if group_id != "private" and uid:
            final_text = self.smart_sender.remove_leading_at(final_text, uid)

        if group_id != "private":
            new_chain = await self._convert_text_to_chain(final_text, event)

            if reply_to_id and len(new_chain.chain) > 0:
                try:
                    from astrbot.api.message_components import Reply

                    new_chain.chain.insert(0, Reply(id=reply_to_id))
                    logger.debug(f"[Scriptor] 非分段模式引用注入: reply_to_id={reply_to_id}")
                except Exception as e:
                    logger.warning(f"[Scriptor] 引用注入失败: {e}")

            if len(new_chain.chain) > 0:
                result.chain = new_chain.chain
                return

        result.chain = [Plain(final_text)]

    async def _handle_other_platform(self, event, result, result_text, platform, uid, group_id, reply_to_id):
        """
        处理其他平台的消息发送（Telegram, Discord, WebChat 等）

        特性：
        - 不分段发送
        - 不清洗 Markdown（保留原始格式）
        - @ 提及转换
        - 消息引用
        """
        from astrbot.api.message_components import Plain

        final_text = result_text

        if group_id != "private" and uid:
            final_text = self.smart_sender.remove_leading_at(final_text, uid)

        if group_id != "private":
            new_chain = await self._convert_text_to_chain(final_text, event)

            if reply_to_id and len(new_chain.chain) > 0:
                try:
                    from astrbot.api.message_components import Reply

                    new_chain.chain.insert(0, Reply(id=reply_to_id))
                    logger.debug(f"[Scriptor] 其他平台引用注入: reply_to_id={reply_to_id}")
                except Exception as e:
                    logger.warning(f"[Scriptor] 引用注入失败: {e}")

            if len(new_chain.chain) > 0:
                result.chain = new_chain.chain
                logger.debug(f"[Scriptor] 其他平台发送完成: platform={platform.name}, 长度={len(final_text)}")
                return

        result.chain = [Plain(final_text)]
        logger.debug(f"[Scriptor] 其他平台发送完成: platform={platform.name}, 长度={len(final_text)}")

    def _compute_concurrency_session_id(self, uid: str, group_id: str) -> str:
        """
        计算并发控制用的会话 ID

        规则：
        - 私聊: "{uid}_private"
        - 群聊: "*_{group_id}"
        - WebUI: "webchat_{user_id}"
        """
        if group_id and group_id != "private":
            return f"*_{group_id}"
        elif uid:
            return f"{uid}_private"
        else:
            return "unknown_session"
