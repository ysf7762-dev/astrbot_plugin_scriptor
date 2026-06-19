# core/active_reply_manager.py
"""
Scriptor 群聊主动回复管理器

实现高情商的群聊主动回复机制：
- 状态机驱动的注意力窗口
- 防抖与打包队列
- 双模型决策链（小模型意图判定 + 大模型内容生成）
- 智能引用机制
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.api.provider import Provider
    from astrbot.api.star import Context

    from .config_pydantic import ScriptorConfigPydantic
    from .group_manager import GroupManager


class GroupStatus(Enum):
    IDLE = "idle"
    AWAKE = "awake"


@dataclass
class QueuedMessage:
    message_id: str
    sender_id: str
    sender_name: str
    content: str
    timestamp: datetime
    is_at_bot: bool = False
    raw_event: Any = None


@dataclass
class GroupState:
    status: GroupStatus = GroupStatus.IDLE
    expires_at: Optional[datetime] = None
    message_count: int = 0
    last_ai_message_time: Optional[datetime] = None
    message_queue: List[QueuedMessage] = field(default_factory=list)
    debounce_task: Optional[asyncio.Task] = None
    processing: bool = False
    conversation_start_time: Optional[datetime] = None
    all_messages: List[QueuedMessage] = field(default_factory=list)


@dataclass
class ReplyDecision:
    should_reply: bool
    target_msg_id: Optional[str] = None
    reply_text: Optional[str] = None
    reasoning: str = ""


class ActiveReplyManager:
    """
    群聊主动回复管理器

    核心功能：
    1. 状态机管理：IDLE <-> AWAKE
    2. 防抖与打包队列
    3. 触发条件检测（名字唤醒、活跃任务、连续对话）
    4. 双模型决策链
    5. 智能引用决策
    """

    INTENT_PROMPT_TEMPLATE = """你现在在一个群聊中。以下是最近的聊天记录：
{chat_history}

请分析最后一条消息。
结合上下文，这条消息是否是在对你说话，或者是在追问你？
如果是，请只输出 YES。如果不是，或者是在和其他人聊天，请只输出 NO。"""

    CONTENT_PROMPT_TEMPLATE = """你现在在一个群聊中，需要回复以下消息：

{message_list}

请根据你的判断，生成回复。你必须以 JSON 格式输出：
{{
  "reply_text": "你的回复内容...",
  "target_msg_id": "消息ID或null"
}}

规则：
1. 如果你是针对某一条特定消息回复，填入它的消息ID
2. 如果你是综合回复大家，或者回复多条消息，填入 null
3. 回复要自然、简洁，符合你在群聊中的定位"""

    def __init__(
        self,
        config: "ScriptorConfigPydantic",
        group_manager: "GroupManager",
        context: "Context",
        data_dir: "Path",
    ):
        self.config = config
        self.group_manager = group_manager
        self.context = context
        self.data_dir = data_dir

        self.group_states: Dict[str, GroupState] = {}
        self._lock = asyncio.Lock()

        self.hard_stop_words = set(word.strip() for word in config.ar_hard_stop_words.split(",") if word.strip())

        self._bot_names_cache: Dict[str, List[str]] = {}
        self._bot_names_cache_time: Dict[str, float] = {}
        self._cache_ttl = 60

    def _get_group_state(self, group_id: str) -> GroupState:
        if group_id not in self.group_states:
            self.group_states[group_id] = GroupState()
        return self.group_states[group_id]

    def _get_bot_names(self, group_id: str) -> List[str]:
        now = time.time()
        if group_id in self._bot_names_cache:
            if now - self._bot_names_cache_time.get(group_id, 0) < self._cache_ttl:
                return self._bot_names_cache[group_id]

        names = ["灵笔司书", "司书", "管家"]

        try:
            group_profile_path = self.data_dir / "groups" / group_id / "G_PROFILE.md"
            if group_profile_path.exists():
                content = group_profile_path.read_text(encoding="utf-8")
                match = re.search(r"群内称呼[：:]\s*(.+?)(?:\n|$)", content)
                if match:
                    custom_name = match.group(1).strip()
                    if custom_name and custom_name not in ["(等待记录)", "等待记录", "{{group_bot_name}}"]:
                        names.insert(0, custom_name)
        except Exception as e:
            logger.debug(f"[ActiveReply] 获取群内称呼失败: {e}")

        self._bot_names_cache[group_id] = names
        self._bot_names_cache_time[group_id] = now
        return names

    def _get_active_tasks(self, group_id: str) -> List[str]:
        tasks = []
        try:
            group_profile_path = self.data_dir / "groups" / group_id / "G_PROFILE.md"
            if group_profile_path.exists():
                content = group_profile_path.read_text(encoding="utf-8")
                match = re.search(r"活跃任务[：:]\s*(.+?)(?:\n\n|\n#|$)", content, re.DOTALL)
                if match:
                    task_text = match.group(1).strip()
                    for line in task_text.split("\n"):
                        line = line.strip().lstrip("- ").strip()
                        if line and line not in ["(记录仅限本群可见、需要全员或多人协作的待办事项)"]:
                            tasks.append(line)
        except Exception as e:
            logger.debug(f"[ActiveReply] 获取活跃任务失败: {e}")
        return tasks

    def _check_name_wakeup(self, content: str, group_id: str) -> bool:
        if not self.config.ar_name_wakeup:
            return False

        names = self._get_bot_names(group_id)
        content_lower = content.lower()

        for name in names:
            if name.lower() in content_lower:
                return True
        return False

    def _check_task_sniffing(self, content: str, group_id: str) -> bool:
        if not self.config.ar_task_sniffing:
            return False

        tasks = self._get_active_tasks(group_id)
        if not tasks:
            return False

        content_lower = content.lower()
        for task in tasks:
            keywords = [w for w in task.lower().split() if len(w) > 1]
            for kw in keywords:
                if kw in content_lower:
                    logger.debug(f"[ActiveReply] 活跃任务嗅探命中: {kw} -> {task}")
                    return True
        return False

    def _check_hard_stop(self, content: str) -> bool:
        content_clean = content.strip().lower()
        for word in self.hard_stop_words:
            if word.lower() in content_clean:
                return True
        return False

    def _is_attention_window_valid(self, state: GroupState) -> bool:
        if state.status != GroupStatus.AWAKE:
            return False

        if state.expires_at and datetime.now() > state.expires_at:
            return False

        if state.message_count >= self.config.ar_attention_window_messages:
            return False

        return True

    def _activate_attention(self, state: GroupState):
        state.status = GroupStatus.AWAKE
        state.expires_at = datetime.now() + timedelta(minutes=self.config.ar_attention_window_minutes)
        state.message_count = 0
        if state.conversation_start_time is None:
            state.conversation_start_time = datetime.now()
        logger.debug(f"[ActiveReply] 注意力窗口激活，过期时间: {state.expires_at}")

    def _refresh_attention(self, state: GroupState):
        state.expires_at = datetime.now() + timedelta(minutes=self.config.ar_attention_window_minutes)
        state.message_count = 0
        logger.debug(f"[ActiveReply] 注意力窗口刷新，过期时间: {state.expires_at}")

    def _deactivate_attention(self, state: GroupState, reason: str = ""):
        state.status = GroupStatus.IDLE
        state.expires_at = None
        state.message_count = 0
        state.message_queue.clear()
        state.all_messages.clear()
        state.conversation_start_time = None
        if state.debounce_task and not state.debounce_task.done():
            state.debounce_task.cancel()
        state.debounce_task = None
        logger.debug(f"[ActiveReply] 注意力窗口关闭: {reason}")

    async def on_ai_message_sent(self, group_id: str):
        if not self.config.active_reply_enabled:
            return

        async with self._lock:
            state = self._get_group_state(group_id)
            if state.status == GroupStatus.AWAKE:
                self._refresh_attention(state)
                state.last_ai_message_time = datetime.now()

    async def process_message(
        self,
        group_id: str,
        message_id: str,
        sender_id: str,
        sender_name: str,
        content: str,
        is_at_bot: bool,
        raw_event: Any = None,
    ) -> Optional[ReplyDecision]:
        if not self.config.active_reply_enabled:
            return None

        if self._check_hard_stop(content):
            async with self._lock:
                state = self._get_group_state(group_id)
                self._deactivate_attention(state, "硬打断词触发")
            return None

        async with self._lock:
            state = self._get_group_state(group_id)

            if state.processing:
                state.message_count += 1
                return None

            should_activate = False
            trigger_reason = ""

            if is_at_bot:
                should_activate = True
                trigger_reason = "被@提及"
            elif self._check_name_wakeup(content, group_id):
                should_activate = True
                trigger_reason = "名字唤醒"
            elif self.config.ar_task_sniffing and self._check_task_sniffing(content, group_id):
                should_activate = True
                trigger_reason = "活跃任务嗅探"
            elif self.config.ar_continuous_dialogue and self._is_attention_window_valid(state):
                should_activate = True
                trigger_reason = "连续对话"

            if should_activate:
                if state.status == GroupStatus.IDLE:
                    self._activate_attention(state)
                    logger.info(f"[ActiveReply] 触发唤醒: {trigger_reason}")

                queued_msg = QueuedMessage(
                    message_id=message_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    content=content,
                    timestamp=datetime.now(),
                    is_at_bot=is_at_bot,
                    raw_event=raw_event,
                )
                state.message_queue.append(queued_msg)
                state.all_messages.append(queued_msg)
                state.message_count += 1

                if len(state.message_queue) >= self.config.ar_max_queue_size:
                    return await self._process_queue(group_id, state)
                else:
                    if state.debounce_task and not state.debounce_task.done():
                        state.debounce_task.cancel()

                    state.debounce_task = asyncio.create_task(self._debounce_wait(group_id))

        return None

    async def _debounce_wait(self, group_id: str):
        await asyncio.sleep(self.config.ar_debounce_seconds)

        async with self._lock:
            state = self._get_group_state(group_id)
            if state.message_queue and not state.processing:
                await self._process_queue(group_id, state)

    async def _process_queue(self, group_id: str, state: GroupState) -> Optional[ReplyDecision]:
        if not state.message_queue:
            return None

        state.processing = True
        messages_to_process = state.message_queue.copy()
        state.message_queue.clear()

        try:
            decision = await self._make_decision(group_id, state, messages_to_process)

            if decision and decision.should_reply:
                self._refresh_attention(state)
            else:
                if not self._is_attention_window_valid(state):
                    self._deactivate_attention(state, "注意力窗口过期")

            return decision

        except Exception as e:
            logger.error(f"[ActiveReply] 处理队列失败: {e}")
            return None
        finally:
            state.processing = False

    async def _make_decision(
        self,
        group_id: str,
        state: GroupState,
        messages: List[QueuedMessage],
    ) -> Optional[ReplyDecision]:
        context_messages = state.all_messages[-self.config.ar_context_messages :]

        chat_history = self._format_chat_history(context_messages)

        intent_result = await self._check_intent(chat_history)

        if not intent_result:
            return ReplyDecision(should_reply=False, reasoning="意图判定: 不需要回复")

        content_decision = await self._generate_content(group_id, messages, context_messages)

        return content_decision

    def _format_chat_history(self, messages: List[QueuedMessage]) -> str:
        lines = []
        for msg in messages:
            lines.append(f"[{msg.sender_name}]: {msg.content}")
        return "\n".join(lines)

    async def _check_intent(self, chat_history: str) -> bool:
        prompt = self.INTENT_PROMPT_TEMPLATE.format(chat_history=chat_history)

        try:
            # 优先使用意图判定小模型
            if self.config.ar_intent_model_provider:
                try:
                    intent_provider_id = self.config.ar_intent_model_provider
                    response = await self.context.llm_generate(chat_provider_id=intent_provider_id, prompt=prompt)
                    result = response.completion_text.strip().upper()
                    return "YES" in result
                except Exception as e:
                    logger.warning(f"[ActiveReply] 小模型意图判定失败，Fallback到大模型: {e}")

            # Fallback 到默认模型
            provider_id = await self.context.get_current_chat_provider_id(None)
            response = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            result = response.completion_text.strip().upper()
            return "YES" in result

        except Exception as e:
            logger.error(f"[ActiveReply] 意图判定失败: {e}")
            return False

    async def _generate_content(
        self,
        group_id: str,
        messages: List[QueuedMessage],
        context_messages: List[QueuedMessage],
    ) -> ReplyDecision:
        message_list = self._format_message_list(messages)
        prompt = self.CONTENT_PROMPT_TEMPLATE.format(message_list=message_list)

        try:
            provider_id = await self.context.get_current_chat_provider_id(None)
            response = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)

            result_text = response.completion_text.strip()

            return self._parse_content_response(result_text, messages)

        except Exception as e:
            logger.error(f"[ActiveReply] 内容生成失败: {e}")
            return ReplyDecision(should_reply=False, reasoning=f"内容生成失败: {e}")

    def _format_message_list(self, messages: List[QueuedMessage]) -> str:
        lines = []
        for msg in messages:
            lines.append(f"[msg_id:{msg.message_id}] {msg.sender_name}: {msg.content}")
        return "\n".join(lines)

    def _parse_content_response(
        self,
        response_text: str,
        messages: List[QueuedMessage],
    ) -> ReplyDecision:
        try:
            json_match = re.search(r"\{[\s\S]*\}", response_text)
            if json_match:
                data = json.loads(json_match.group())

                reply_text = data.get("reply_text", "")
                target_msg_id = data.get("target_msg_id")

                if target_msg_id and target_msg_id.lower() in ["null", "none", ""]:
                    target_msg_id = None

                if target_msg_id:
                    valid_ids = [msg.message_id for msg in messages]
                    if target_msg_id not in valid_ids:
                        logger.warning(f"[ActiveReply] 无效的 target_msg_id: {target_msg_id}")
                        target_msg_id = None

                return ReplyDecision(
                    should_reply=bool(reply_text),
                    target_msg_id=target_msg_id,
                    reply_text=reply_text,
                    reasoning="成功解析JSON响应",
                )
        except json.JSONDecodeError as e:
            logger.debug(f"[ActiveReply] JSON解析失败: {e}")

        return ReplyDecision(
            should_reply=True, target_msg_id=None, reply_text=response_text, reasoning="使用原始响应文本"
        )

    def get_state_info(self, group_id: str) -> Dict[str, Any]:
        state = self._get_group_state(group_id)
        return {
            "status": state.status.value,
            "expires_at": state.expires_at.isoformat() if state.expires_at else None,
            "message_count": state.message_count,
            "queue_size": len(state.message_queue),
            "processing": state.processing,
            "conversation_start_time": (
                state.conversation_start_time.isoformat() if state.conversation_start_time else None
            ),
        }

    def force_deactivate(self, group_id: str, reason: str = "手动关闭"):
        state = self._get_group_state(group_id)
        self._deactivate_attention(state, reason)
