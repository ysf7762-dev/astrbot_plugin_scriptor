from __future__ import annotations

# core/file_monitor.py
"""
Scriptor 文件监控模块
监控 Markdown 记忆文件的变更，自动同步到索引
"""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Set

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


@dataclass
class FileChange:
    """文件变更事件"""

    path: Path
    change_type: str  # 'created', 'modified', 'deleted'
    timestamp: float


class FileMonitor:
    """文件监控器 - 监控记忆文件变更并自动同步"""

    def __init__(self, data_dir: Path, on_change: Optional[Callable[[FileChange], None]] = None):
        self.data_dir = data_dir
        self.on_change = on_change

        self._watched_dirs: Set[Path] = set()
        self._file_mtimes: dict[str, float] = {}
        self._is_running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._check_interval = 5.0  # 每 5 秒检查一次

        # 要监控的目录
        self._target_dirs = ["profiles", "groups"]
        # 要监控的文件扩展名
        self._target_extensions = [".md"]

    async def start(self):
        """启动文件监控"""
        if self._is_running:
            logger.warning("[Scriptor] 文件监控已在运行中")
            return

        logger.info("[Scriptor] 启动文件监控...")

        # 初始化文件时间戳
        self._init_file_mtimes()

        self._is_running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())

        logger.info("[Scriptor] 文件监控已启动")

    async def stop(self):
        """停止文件监控"""
        if not self._is_running:
            return

        logger.info("[Scriptor] 停止文件监控...")

        self._is_running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            except (OSError, RuntimeError) as e:
                logger.error(f"[Scriptor] 停止文件监控出错: {e}")

        logger.info("[Scriptor] 文件监控已停止")

    def _init_file_mtimes(self):
        """初始化所有监控文件的修改时间"""
        self._file_mtimes.clear()

        for target_dir in self._target_dirs:
            dir_path = self.data_dir / target_dir
            if not dir_path.exists():
                continue

            for md_file in dir_path.rglob("*.md"):
                try:
                    mtime = md_file.stat().st_mtime
                    self._file_mtimes[str(md_file)] = mtime
                except (OSError, RuntimeError) as e:
                    logger.debug(f"[Scriptor] 无法读取文件时间戳 {md_file}: {e}")

        logger.debug(f"[Scriptor] 已监控 {len(self._file_mtimes)} 个记忆文件")

    async def _monitor_loop(self):
        """监控循环"""
        while self._is_running:
            try:
                await asyncio.sleep(self._check_interval)
                await self._check_for_changes()
            except asyncio.CancelledError:
                break
            except (OSError, RuntimeError) as e:
                logger.error(f"[Scriptor] 文件监控循环出错: {e}")

    async def _check_for_changes(self):
        """检查文件变更"""
        changes: list[FileChange] = []
        current_files: set[str] = set()

        for target_dir in self._target_dirs:
            dir_path = self.data_dir / target_dir
            if not dir_path.exists():
                continue

            for md_file in dir_path.rglob("*.md"):
                file_str = str(md_file)
                current_files.add(file_str)

                try:
                    current_mtime = md_file.stat().st_mtime
                    old_mtime = self._file_mtimes.get(file_str)

                    if old_mtime is None:
                        # 新文件
                        changes.append(FileChange(path=md_file, change_type="created", timestamp=time.time()))
                        self._file_mtimes[file_str] = current_mtime
                    elif abs(current_mtime - old_mtime) > 0.1:
                        # 文件已修改
                        changes.append(FileChange(path=md_file, change_type="modified", timestamp=time.time()))
                        self._file_mtimes[file_str] = current_mtime
                except (OSError, RuntimeError) as e:
                    logger.debug(f"[Scriptor] 检查文件失败 {md_file}: {e}")

        # 检查删除的文件
        for file_str in list(self._file_mtimes.keys()):
            if file_str not in current_files:
                changes.append(FileChange(path=Path(file_str), change_type="deleted", timestamp=time.time()))
                del self._file_mtimes[file_str]

        # 处理变更
        if changes:
            logger.debug(f"[Scriptor] 检测到 {len(changes)} 个文件变更")
            for change in changes:
                if self.on_change:
                    try:
                        if asyncio.iscoroutinefunction(self.on_change):
                            await self.on_change(change)
                        else:
                            self.on_change(change)
                    except (OSError, RuntimeError) as e:
                        logger.error(f"[Scriptor] 处理文件变更失败: {e}")
