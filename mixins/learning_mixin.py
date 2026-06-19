from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import filter

from .base import BaseMixin

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


class LearningMixin(BaseMixin):
    """
    学习/授课模式 Mixin

    包含：
    - 学习模式命令
    - 授课模式命令
    - 学习状态查询

    注意：所有命令装饰器已移至 main.py 中注册，避免指令冲突
    """

    async def cmd_start_learning(self, event: AstrMessageEvent):
        """进入学习模式（仅管理员）"""
        uid, group_id, _ = self._get_identity(event)

        if not self.config.learning_mode_enabled:
            yield event.plain_result("❌ 学习模式功能未启用。")
            return

        from ..core.learning_manager import CognitiveState

        success, message, _ = await self.learning_manager.set_state(
            uid=uid, group_id=group_id, new_state=CognitiveState.LEARNING, requester_uid=uid
        )

        if success:
            logger.info(f"[Scriptor] 用户 {uid} 在 {group_id} 进入学习模式")

        yield event.plain_result(f"{'✅' if success else '❌'} {message}")

    async def cmd_end_learning(self, event: AstrMessageEvent):
        """退出学习模式（仅管理员）"""
        uid, group_id, _ = self._get_identity(event)

        from ..core.learning_manager import CognitiveState

        success, message, _ = await self.learning_manager.set_state(
            uid=uid, group_id=group_id, new_state=CognitiveState.NORMAL, requester_uid=uid
        )

        yield event.plain_result(f"{'✅' if success else '❌'} {message}")

    async def cmd_start_teaching(self, event: AstrMessageEvent):
        """进入授课模式（仅管理员）"""
        uid, group_id, _ = self._get_identity(event)

        if not self.config.teaching_mode_enabled:
            yield event.plain_result("❌ 授课模式功能未启用。")
            return

        from ..core.learning_manager import CognitiveState

        success, message, _ = await self.learning_manager.set_state(
            uid=uid, group_id=group_id, new_state=CognitiveState.TEACHING, requester_uid=uid
        )

        if success:
            logger.info(f"[Scriptor] 用户 {uid} 在 {group_id} 进入授课模式")

        yield event.plain_result(f"{'✅' if success else '❌'} {message}")

    async def cmd_end_teaching(self, event: AstrMessageEvent):
        """退出授课模式（仅管理员）"""
        uid, group_id, _ = self._get_identity(event)

        from ..core.learning_manager import CognitiveState

        success, message, _ = await self.learning_manager.set_state(
            uid=uid, group_id=group_id, new_state=CognitiveState.NORMAL, requester_uid=uid
        )

        yield event.plain_result(f"{'✅' if success else '❌'} {message}")

    async def cmd_learning_status(self, event: AstrMessageEvent):
        """查看当前学习状态"""
        uid, group_id, _ = self._get_identity(event)

        from ..core.learning_manager import CognitiveState

        state = self.learning_manager.get_state(uid, group_id)
        stats = self.learning_manager.get_learning_stats()

        state_names = {
            CognitiveState.NORMAL: "🟢 日常模式",
            CognitiveState.LEARNING: "🟡 学习模式",
            CognitiveState.TEACHING: "🔴 授课模式",
        }

        msg = f"""## 🎓 学习系统状态

**当前全局模式**: {state_names.get(state, str(state))}

**统计信息**:
- 待确认知识: {stats['pending_confirmations']}
- 管理员数量: {stats['admin_count']}
- 活跃会话数: {stats['total_sessions']}
"""
        yield event.plain_result(msg)
