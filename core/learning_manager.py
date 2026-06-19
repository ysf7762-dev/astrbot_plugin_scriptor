# core/learning_manager.py
"""
学习管理器模块 - 三态认知模型

功能：
1. 三态认知模型（NORMAL/LEARNING/TEACHING）
2. 待确认知识机制（Pending 状态）
3. 双轨写入（知识库 + 知识图谱）
4. 动态 Prompt 注入

设计理念：
- 学习模式：AI 像新员工一样积极提取知识，但需要导师确认
- 授课模式：AI 作为权威专家，知识库锁定为只读
- 日常模式：正常工作状态
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


class CognitiveState(Enum):
    """三态认知模型"""

    NORMAL = "normal"  # 日常模式
    LEARNING = "learning"  # 学习模式（积极提取，需要确认）
    TEACHING = "teaching"  # 授课模式（只读，权威专家）


@dataclass
class KnowledgeExtraction:
    """提取的知识条目"""

    title: str
    content: str
    knowledge_type: str  # fact/skill/preference/rule/experience
    tags: List[str]
    source: str = ""  # 来源（对话/文档）
    is_pending: bool = True  # 待确认状态
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    extracted_by: str = ""  # 提取者（LLM/用户）

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "title": self.title,
            "content": self.content,
            "knowledge_type": self.knowledge_type,
            "tags": self.tags,
            "source": self.source,
            "is_pending": self.is_pending,
            "created_at": self.created_at,
            "extracted_by": self.extracted_by,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeExtraction":
        """从字典创建"""
        return cls(
            title=data.get("title", ""),
            content=data.get("content", ""),
            knowledge_type=data.get("knowledge_type", "fact"),
            tags=data.get("tags", []),
            source=data.get("source", ""),
            is_pending=data.get("is_pending", True),
            created_at=data.get("created_at", datetime.now().isoformat()),
            extracted_by=data.get("extracted_by", ""),
        )


@dataclass
class LearningSession:
    """学习会话"""

    session_id: str
    state: CognitiveState = CognitiveState.NORMAL
    pending_knowledge: List[KnowledgeExtraction] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    total_learned: int = 0  # 本次会话已确认的知识数量

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "pending_count": len(self.pending_knowledge),
            "total_learned": self.total_learned,
            "started_at": self.started_at,
        }


class LearningManager:
    """学习管理器 - 管理 AI 的认知状态（全局模式）"""

    def __init__(self, knowledge_base=None, knowledge_graph=None, config=None, admin_uids=None, identity_manager=None):
        """
        初始化学习管理器

        Args:
            knowledge_base: 知识库实例（用于双轨写入）
            knowledge_graph: 知识图谱实例（用于双轨写入）
            config: 配置对象
            admin_uids: 管理员 UID 列表（用于权限控制）
            identity_manager: 身份管理器（用于超级管理员检查）
        """
        self.kb = knowledge_base
        self.kg = knowledge_graph
        self.config = config or {}
        self.admin_uids = admin_uids or []
        self.identity_manager = identity_manager
        self.sessions: Dict[str, LearningSession] = {}
        self._lock = asyncio.Lock()

        # 全局状态（学习模式/授课模式是全局的）
        self._global_state: CognitiveState = CognitiveState.NORMAL

        # 配置参数
        self.max_pending_items = getattr(config, "learning_max_pending_items", 10) if config else 10

        logger.info("[LearningManager] 初始化完成")

    def get_session(self, session_id: str) -> LearningSession:
        """
        获取或创建会话

        Args:
            session_id: 会话 ID

        Returns:
            LearningSession 对象
        """
        if session_id not in self.sessions:
            self.sessions[session_id] = LearningSession(session_id=session_id)
            logger.debug(f"[LearningManager] 创建新会话：{session_id}")
        return self.sessions[session_id]

    async def set_state(
        self,
        session_id: str = None,
        new_state: CognitiveState = None,
        uid: str = None,
        group_id: str = None,
        requester_uid: str = None,
    ) -> tuple:
        """
        设置全局认知状态（只有管理员才能切换）

        Args:
            session_id: 会话 ID（可选，与 uid+group_id 二选一）
            new_state: 新状态
            uid: 用户 ID（可选）
            group_id: 群组 ID（可选）
            requester_uid: 请求者 UID（用于权限验证）

        Returns:
            (success: bool, message: str, old_state: CognitiveState)
        """
        if new_state is None:
            return False, "未指定新状态", self._global_state

        # 权限检查：只有超级管理员才能切换全局状态
        is_admin = False
        if requester_uid:
            if self.identity_manager:
                is_admin = self.identity_manager.is_super_admin(requester_uid, self.admin_uids)
            else:
                is_admin = requester_uid in self.admin_uids

        if not is_admin:
            logger.warning(f"[LearningManager] 非管理员尝试切换状态：{requester_uid}")
            return False, "⚠️ 只有管理员才能切换学习/授课模式", self._global_state

        async with self._lock:
            old_state = self._global_state

            self._global_state = new_state

            logger.info(f"[LearningManager] 全局状态切换：{old_state.value} -> {new_state.value} (by: {requester_uid})")

            # 清空所有待确认知识
            if new_state == CognitiveState.NORMAL:
                for session in self.sessions.values():
                    session.pending_knowledge.clear()
                logger.info("[LearningManager] 结束特殊模式，清空所有待确认知识")

            return True, f"全局状态已切换：{old_state.value} -> {new_state.value}", old_state

    def get_state(self, session_id: str = None, uid: str = None, group_id: str = None) -> CognitiveState:
        """
        获取当前全局认知状态

        Args:
            session_id: 会话 ID（可选，保留参数兼容性）
            uid: 用户 ID（可选，保留参数兼容性）
            group_id: 群组 ID（可选，保留参数兼容性）

        Returns:
            CognitiveState 枚举值
        """
        return self._global_state

    def get_learning_stats(self) -> Dict[str, Any]:
        """
        获取学习系统全局统计信息

        Returns:
            统计信息字典
        """
        pending_confirmations = 0
        for session in self.sessions.values():
            pending_confirmations += len(session.pending_knowledge)

        return {
            "global_state": self._global_state.value,
            "pending_confirmations": pending_confirmations,
            "admin_count": len(self.admin_uids),
            "total_sessions": len(self.sessions),
        }

    def is_learning(self, uid: str = None, group_id: str = None, session_id: str = None) -> bool:
        """
        检查是否处于学习模式

        Args:
            uid: 用户 ID
            group_id: 群组 ID
            session_id: 会话 ID（可选）

        Returns:
            是否处于学习模式
        """
        state = self.get_state(session_id=session_id, uid=uid, group_id=group_id)
        return state == CognitiveState.LEARNING

    def is_read_only(self, uid: str = None, group_id: str = None, session_id: str = None) -> bool:
        """
        检查是否处于只读模式（授课模式）

        Args:
            uid: 用户 ID
            group_id: 群组 ID
            session_id: 会话 ID（可选）

        Returns:
            是否处于只读模式
        """
        state = self.get_state(session_id=session_id, uid=uid, group_id=group_id)
        return state == CognitiveState.TEACHING

    def get_pending_knowledge(
        self, uid: str = None, group_id: str = None, session_id: str = None
    ) -> Optional[KnowledgeExtraction]:
        """
        获取待确认知识

        Args:
            uid: 用户 ID
            group_id: 群组 ID
            session_id: 会话 ID（可选）

        Returns:
            最新的待确认知识，无则返回 None
        """
        if session_id is None:
            if uid is None or group_id is None:
                return None
            session_id = f"{uid}_{group_id}"

        session = self.get_session(session_id)
        if session.pending_knowledge:
            return session.pending_knowledge[-1]
        return None

    def has_pending_knowledge(self, uid: str = None, group_id: str = None, session_id: str = None) -> bool:
        """
        检查是否有待确认知识

        Args:
            uid: 用户 ID
            group_id: 群组 ID
            session_id: 会话 ID（可选）

        Returns:
            是否有待确认知识
        """
        if session_id is None:
            if uid is None or group_id is None:
                return False
            session_id = f"{uid}_{group_id}"

        session = self.get_session(session_id)
        return len(session.pending_knowledge) > 0

    def check_knowledge_conflict(self, extraction: KnowledgeExtraction) -> Optional[Dict[str, Any]]:
        """
        检查知识冲突（支持模糊匹配）

        Args:
            extraction: 待添加的知识

        Returns:
            冲突信息字典，无冲突返回 None
        """
        if not self.kb:
            return None

        try:
            if hasattr(self.kb, "_items"):
                new_title = extraction.title.lower().strip()
                best_match = None
                best_score = 0.0

                for item in self.kb._items.values():
                    existing_title = item.title.lower().strip()
                    if existing_title == new_title:
                        return {
                            "type": "exact_match",
                            "message": f"已存在相同标题的知识：{item.title}",
                            "existing": item,
                            "score": 1.0,
                        }

                    similarity = self._calculate_title_similarity(new_title, existing_title)
                    if similarity > best_score and similarity >= 0.8:
                        best_score = similarity
                        best_match = item

                if best_match:
                    return {
                        "type": "similar_match",
                        "message": f"已存在相似知识（相似度 {best_score:.0%}）：{best_match.title}",
                        "existing": best_match,
                        "score": best_score,
                    }

        except Exception as e:
            logger.debug(f"[LearningManager] 冲突检查失败：{e}")

        return None

    def _calculate_title_similarity(self, title1: str, title2: str) -> float:
        """
        计算标题相似度（基于关键词重叠）

        Args:
            title1: 标题1
            title2: 标题2

        Returns:
            相似度分数 (0.0 - 1.0)
        """
        import re

        def extract_keywords(text):
            text = re.sub(r"[（\(][^）\)]*[）\)]", "", text)
            text = re.sub(r"[^\w\s]", " ", text)
            return set(text.lower().split())

        keywords1 = extract_keywords(title1)
        keywords2 = extract_keywords(title2)

        if not keywords1 or not keywords2:
            return 0.0

        intersection = keywords1 & keywords2
        union = keywords1 | keywords2

        return len(intersection) / len(union) if union else 0.0

    async def store_pending_knowledge(
        self, extraction: KnowledgeExtraction, uid: str = None, group_id: str = None, session_id: str = None
    ) -> str:
        """
        存储待确认知识并返回提示词

        Args:
            extraction: 提取的知识
            uid: 用户 ID
            group_id: 群组 ID
            session_id: 会话 ID（可选）

        Returns:
            提示词字符串
        """
        if session_id is None:
            if uid is None or group_id is None:
                return "❌ 缺少会话标识"
            session_id = f"{uid}_{group_id}"

        success = await self.add_pending_knowledge(session_id, extraction)

        if success:
            conflict_info = ""
            conflict = self.check_knowledge_conflict(extraction)
            if conflict:
                existing_title = getattr(conflict.get("existing"), "title", "未知")
                score = conflict.get("score", 0)
                if conflict["type"] == "exact_match":
                    conflict_info = f"\n\n⚠️ **注意**：将覆盖已有知识「{existing_title}」"
                elif conflict["type"] == "similar_match":
                    conflict_info = (
                        f"\n\n⚠️ **注意**：与已有知识「{existing_title}」相似度 {score:.0%}，确认后将覆盖更新"
                    )

            return f"""✅ 知识已暂存，等待用户确认：

**标题**: {extraction.title}
**类型**: {extraction.knowledge_type}
**内容摘要**: {extraction.content[:100]}...{conflict_info}

⚠️ 请向用户展示以上内容，等待用户回复"确认"、"是的"、"可以"等确认词后，再调用 confirm_knowledge 工具。
如果用户要求修改，请使用 revise_knowledge 工具。"""
        else:
            return "❌ 知识暂存失败（可能已达上限或处于授课模式）"

    async def confirm_pending_knowledge(self, session_id: str = None, uid: str = None, group_id: str = None) -> tuple:
        """
        确认所有待确认知识（双轨写入：KB + KG）

        Args:
            session_id: 会话 ID（可选）
            uid: 用户 ID（可选）
            group_id: 群组 ID（可选）

        Returns:
            (success: bool, message: str)
        """
        if session_id is None:
            if uid is None or group_id is None:
                return False, "缺少会话标识"
            session_id = f"{uid}_{group_id}"

        async with self._lock:
            session = self.get_session(session_id)

            if not session.pending_knowledge:
                return True, "没有待确认的知识"

            count = 0
            for extraction in session.pending_knowledge:
                try:
                    await self._dual_track_write(session_id, extraction)
                    count += 1
                    session.total_learned += 1
                except Exception as e:
                    logger.error(f"[LearningManager] 写入知识失败：{extraction.title}, 错误：{e}")

            session.pending_knowledge.clear()
            logger.info(f"[LearningManager] 确认了 {count} 条知识 (session: {session_id})")

            return True, f"已确认 {count} 条知识"

    async def add_pending_knowledge(self, session_id: str, extraction: KnowledgeExtraction) -> bool:
        """
        添加待确认知识

        Args:
            session_id: 会话 ID
            extraction: 提取的知识

        Returns:
            是否添加成功
        """
        async with self._lock:
            session = self.get_session(session_id)

            # 检查是否超过上限
            if len(session.pending_knowledge) >= self.max_pending_items:
                logger.warning(f"[LearningManager] 待确认知识已达上限 ({self.max_pending_items})")
                return False

            # 检查状态（授课模式下不允许添加）
            if session.state == CognitiveState.TEACHING:
                logger.warning("[LearningManager] 授课模式下不允许添加知识")
                return False

            session.pending_knowledge.append(extraction)
            logger.info(f"[LearningManager] 添加待确认知识：{extraction.title}")
            return True

    async def _dual_track_write(self, session_id: str, extraction: KnowledgeExtraction):
        """
        双轨写入：同时写入知识库和知识图谱

        Args:
            session_id: 会话 ID
            extraction: 知识条目
        """
        # 写入知识库
        if self.kb:
            try:
                from .knowledge_base import KnowledgeItem, KnowledgeType

                # 映射知识类型
                type_mapping = {
                    "fact": KnowledgeType.FACT,
                    "skill": KnowledgeType.SKILL,
                    "preference": KnowledgeType.PREFERENCE,
                    "rule": KnowledgeType.RULE,
                    "experience": KnowledgeType.EXPERIENCE,
                }
                knowledge_type = type_mapping.get(extraction.knowledge_type, KnowledgeType.FACT)

                # 检查是否有相同或相似的知识（冲突时更新）
                conflict = self.check_knowledge_conflict(extraction)
                if conflict and conflict.get("score", 0) >= 0.8:
                    existing = conflict["existing"]
                    if hasattr(self.kb, "update_item"):
                        self.kb.update_item(
                            existing.id,
                            title=extraction.title,
                            content=extraction.content,
                            knowledge_type=knowledge_type,
                            tags=extraction.tags,
                            source=extraction.source or f"session:{session_id}",
                        )
                        logger.info(
                            f"[LearningManager] 更新知识库（覆盖 {conflict['type']}）：{extraction.title} -> {existing.title}"
                        )
                    return

                # 无冲突，创建新知识条目
                item = KnowledgeItem.create(
                    title=extraction.title,
                    content=extraction.content,
                    knowledge_type=knowledge_type,
                    tags=extraction.tags,
                    category="learned",
                    is_active=True,
                    source=extraction.source or f"session:{session_id}",
                )

                # 添加到知识库
                if hasattr(self.kb, "add_item"):
                    self.kb.add_item(item)
                    logger.info(f"[LearningManager] 写入知识库（新增）：{extraction.title}")
                elif hasattr(self.kb, "knowledge_add"):
                    self.kb.knowledge_add(
                        title=extraction.title,
                        content=extraction.content,
                        knowledge_type=extraction.knowledge_type,
                        tags=extraction.tags,
                        source=extraction.source or f"session:{session_id}",
                    )
                    logger.info(f"[LearningManager] 写入知识库（兼容模式）：{extraction.title}")

            except Exception as e:
                logger.error(f"[LearningManager] 写入知识库失败：{e}")

        # 写入知识图谱
        if self.kg:
            try:
                # 从标题和内容中提取实体
                entities = self._extract_entities_from_knowledge(extraction)
                for entity_name, entity_type in entities:
                    if hasattr(self.kg, "add_entity"):
                        self.kg.add_entity(entity_name, entity_type)
                        logger.debug(f"[LearningManager] 写入知识图谱实体：{entity_name}")

                # 添加关系（如果有）
                if len(entities) >= 2:
                    if hasattr(self.kg, "add_relation"):
                        self.kg.add_relation(
                            entities[0][0],  # source
                            entities[1][0],  # target
                            "related_to",  # relation_type
                            evidence=[extraction.title],
                        )
                        logger.debug(f"[LearningManager] 写入知识图谱关系：{entities[0][0]} -> {entities[1][0]}")

            except Exception as e:
                logger.error(f"[LearningManager] 写入知识图谱失败：{e}")

    def _extract_entities_from_knowledge(self, extraction: KnowledgeExtraction) -> List[tuple]:
        """
        从知识中提取实体（简单版本）

        Args:
            extraction: 知识条目

        Returns:
            实体列表 [(name, type), ...]
        """
        entities = []

        # 简单规则：标题作为主要实体
        if extraction.title:
            entities.append((extraction.title, extraction.knowledge_type))

        # 标签作为辅助实体
        for tag in extraction.tags[:3]:  # 最多 3 个标签
            entities.append((tag, "tag"))

        return entities if entities else [("unknown", "concept")]

    async def revise_pending_knowledge(
        self,
        session_id: str,
        index: int,
        new_title: Optional[str] = None,
        new_content: Optional[str] = None,
        new_tags: Optional[List[str]] = None,
    ) -> bool:
        """
        修改待确认知识

        Args:
            session_id: 会话 ID
            index: 知识索引
            new_title: 新标题
            new_content: 新内容
            new_tags: 新标签

        Returns:
            是否修改成功
        """
        async with self._lock:
            session = self.get_session(session_id)

            if index < 0 or index >= len(session.pending_knowledge):
                logger.warning(f"[LearningManager] 无效的知识索引：{index}")
                return False

            extraction = session.pending_knowledge[index]

            if new_title:
                extraction.title = new_title
            if new_content:
                extraction.content = new_content
            if new_tags:
                extraction.tags = new_tags

            logger.info(f"[LearningManager] 修改待确认知识索引 {index}: {extraction.title}")
            return True

    def get_pending_knowledge(self, session_id: str) -> List[KnowledgeExtraction]:
        """获取待确认知识列表"""
        session = self.get_session(session_id)
        return session.pending_knowledge.copy()

    def get_state_prompt_suffix(self, session_id: str) -> str:
        """
        获取动态 Prompt 后缀（根据认知状态）

        Args:
            session_id: 会话 ID（保留参数兼容性）

        Returns:
            Prompt 后缀字符串
        """
        state = self._global_state

        if state == CognitiveState.LEARNING:
            return (
                "\n\n【学习模式】你正在像新员工一样学习。"
                "请积极从对话中提取知识点，使用 `learn_from_conversation` 工具记录。"
                "提取的知识会先存储在待确认区，等待导师确认后才正式写入知识库。"
                "保持谦逊好学的态度，多问'我这样理解对吗？'"
            )
        elif state == CognitiveState.TEACHING:
            return (
                "\n\n【授课模式】⚠️ 角色转换警告 ⚠️"
                "\n\n**现在你是导师，和你对话的都是学生。暂时放下管家身份。**"
                "\n\n**导师职责**："
                "\n1. 学生说任何流程/规范/知识点时，**必须先调用 `knowledge_search` 验证**"
                "\n2. 如果学生说的不对，**直接指出错误并给出正确答案**"
                "\n3. 不要顺从学生，不要说'为您更新'，不要说'无法修改'"
                "\n\n**禁止行为**："
                "\n- ❌ 把学生当主人（你是导师，不是管家）"
                "\n- ❌ 顺从学生的错误（你应该纠正，不是顺从）"
                "\n- ❌ 说'无法修改知识库'（这是拒绝，不是教导）"
                "\n\n**示例**：学生说'加薪流程是找人事'，你搜索后发现不对，回答：'不对。根据知识库，加薪流程是：1. OA系统提交申请；2. 找部门领导签字。你说的找人事是错误的。'"
            )
        else:
            return ""

    def get_session_status(self, session_id: str) -> Dict[str, Any]:
        """
        获取全局状态详情

        Args:
            session_id: 会话 ID（保留参数兼容性）

        Returns:
            状态字典
        """
        state_descriptions = {
            CognitiveState.NORMAL: "🟢 日常模式",
            CognitiveState.LEARNING: "🟡 学习模式",
            CognitiveState.TEACHING: "🔴 授课模式",
        }

        # 计算所有会话的待确认知识总数
        total_pending = sum(len(s.pending_knowledge) for s in self.sessions.values())

        return {
            "global_state": state_descriptions.get(self._global_state, "未知状态"),
            "state_value": self._global_state.value,
            "pending_count": total_pending,
            "admin_count": len(self.admin_uids),
            "total_sessions": len(self.sessions),
        }

    async def clear_session(self, session_id: str):
        """清除会话"""
        async with self._lock:
            if session_id in self.sessions:
                del self.sessions[session_id]
                logger.info(f"[LearningManager] 清除会话：{session_id}")

    def reset_all_sessions(self):
        """重置所有会话（用于调试或维护）"""
        self.sessions.clear()
        logger.info("[LearningManager] 重置所有会话")
