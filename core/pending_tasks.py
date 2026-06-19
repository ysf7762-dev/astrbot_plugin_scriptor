# core/pending_tasks.py
"""
待确认任务池 (Pending Task Store)

用于存储需要用户二次确认的高危操作（如文件删除）。
当 AI 调用高危操作工具时，任务会被挂起并存储在此处，
等待用户通过斜杠命令（如 /delete）确认或拒绝。

架构设计：
- 基于 session_id 的内存字典，线程安全
- 支持自动过期（超时清理）
- 提供挂起、确认、拒绝、查询、清理等完整操作
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


class PendingTaskType(Enum):
    """待确认任务类型"""

    FILE_DELETE = "file_delete"


class PendingTaskStatus(Enum):
    """待确认任务状态"""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class PendingTask:
    """待确认任务数据"""

    task_type: PendingTaskType
    session_id: str
    file_path: str
    created_at: float = field(default_factory=lambda: time.time())
    status: PendingTaskStatus = PendingTaskStatus.PENDING
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    def is_expired(self, timeout_seconds: float = 120.0) -> bool:
        return self.age_seconds > timeout_seconds


class PendingTaskStore:
    """
    待确认任务池（全局单例）

    管理所有会话的待确认高危操作任务。
    线程安全：使用简单的字典操作，在 asyncio 单线程环境下天然安全。
    """

    DEFAULT_TIMEOUT_SECONDS = 120.0

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self._tasks: Dict[str, PendingTask] = {}
        self._timeout_seconds = timeout_seconds

    def add_task(
        self,
        session_id: str,
        task_type: PendingTaskType,
        file_path: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> PendingTask:
        """
        添加待确认任务。

        如果同一 session 已有待处理任务，会被覆盖（防止重复挂起）。

        Args:
            session_id: 会话 ID
            task_type: 任务类型
            file_path: 目标文件路径
            extra: 额外信息

        Returns:
            创建的任务对象
        """
        task = PendingTask(
            task_type=task_type,
            session_id=session_id,
            file_path=file_path,
            extra=extra or {},
        )
        self._tasks[session_id] = task
        logger.info(f"[PendingTaskStore] 任务已挂起: {session_id} -> {file_path}")
        return task

    def get_task(self, session_id: str) -> Optional[PendingTask]:
        """
        获取指定会话的待确认任务。

        Args:
            session_id: 会话 ID

        Returns:
            任务对象，如果不存在或已过期则返回 None
        """
        task = self._tasks.get(session_id)
        if task is None:
            return None

        if task.is_expired(self._timeout_seconds):
            task.status = PendingTaskStatus.EXPIRED
            del self._tasks[session_id]
            logger.info(f"[PendingTaskStore] 任务已超时过期: {session_id}")
            return None

        return task

    def confirm_task(self, session_id: str) -> tuple[bool, Optional[PendingTask]]:
        """
        确认执行待确认任务。

        Args:
            session_id: 会话 ID

        Returns:
            (是否成功, 任务对象)
        """
        task = self._tasks.get(session_id)
        if task is None:
            return False, None

        if task.is_expired(self._timeout_seconds):
            task.status = PendingTaskStatus.EXPIRED
            del self._tasks[session_id]
            logger.warning(f"[PendingTaskStore] 确认失败: 任务已超时 {session_id}")
            return False, None

        task.status = PendingTaskStatus.CONFIRMED
        del self._tasks[session_id]
        logger.info(f"[PendingTaskStore] 任务已确认: {session_id} -> {task.file_path}")
        return True, task

    def reject_task(self, session_id: str) -> tuple[bool, Optional[PendingTask]]:
        """
        拒绝/取消待确认任务。

        Args:
            session_id: 会话 ID

        Returns:
            (是否成功, 任务对象)
        """
        task = self._tasks.pop(session_id, None)
        if task is None:
            return False, None

        task.status = PendingTaskStatus.REJECTED
        logger.info(f"[PendingTaskStore] 任务已被用户取消: {session_id} -> {task.file_path}")
        return True, task

    def has_pending_task(self, session_id: str) -> bool:
        """检查指定会话是否有待确认任务"""
        task = self.get_task(session_id)
        return task is not None and task.status == PendingTaskStatus.PENDING

    def clear_task(self, session_id: str) -> bool:
        """强制清除指定会话的待确认任务"""
        if session_id in self._tasks:
            del self._tasks[session_id]
            logger.debug(f"[PendingTaskStore] 任务已清除: {session_id}")
            return True
        return False

    def cleanup_expired(self) -> int:
        """
        清理所有过期的任务。

        Returns:
            清理的任务数量
        """
        expired_keys = [sid for sid, task in self._tasks.items() if task.is_expired(self._timeout_seconds)]
        for key in expired_keys:
            self._tasks[key].status = PendingTaskStatus.EXPIRED
            del self._tasks[key]

        if expired_keys:
            logger.info(f"[PendingTaskStore] 清理了 {len(expired_keys)} 个过期任务")

        return len(expired_keys)

    @property
    def size(self) -> int:
        """当前待确认任务数量"""
        return len(self._tasks)

    def get_all_pending(self) -> Dict[str, PendingTask]:
        """获取所有待确认任务的副本"""
        return dict(self._tasks)


# 全局单例实例
_global_store: Optional[PendingTaskStore] = None


def get_pending_task_store() -> PendingTaskStore:
    """获取全局待确认任务池实例"""
    global _global_store
    if _global_store is None:
        _global_store = PendingTaskStore()
    return _global_store


def init_pending_task_store(timeout_seconds: float = PendingTaskStore.DEFAULT_TIMEOUT_SECONDS) -> PendingTaskStore:
    """初始化全局待确认任务池（可配置超时时间）"""
    global _global_store
    _global_store = PendingTaskStore(timeout_seconds=timeout_seconds)
    return _global_store
