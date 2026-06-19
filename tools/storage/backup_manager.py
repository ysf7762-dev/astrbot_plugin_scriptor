# tools/storage/backup_manager.py
"""数据备份管理模块 - 提供数据备份与恢复功能"""

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


class BackupManager:
    """数据备份管理器"""

    def __init__(self, data_dir: Path, max_backups: int = 5):
        self.data_dir = Path(data_dir)
        self.max_backups = max_backups
        self.backup_dir = self.data_dir / ".backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def create_backup(self, name: str = None) -> Path:
        """
        创建数据备份

        Args:
            name: 备份名称（可选，默认使用时间戳）

        Returns:
            备份目录路径
        """
        if name is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = f"backup_{timestamp}"

        backup_path = self.backup_dir / name

        if backup_path.exists():
            logger.warning(f"[BackupManager] 备份已存在: {backup_path}")
            return backup_path

        shutil.copytree(self.data_dir / "profiles", backup_path / "profiles")
        shutil.copytree(self.data_dir / "groups", backup_path / "groups")

        self._cleanup_old_backups()

        metadata = {
            "name": name,
            "created_at": datetime.now().isoformat(),
            "profiles_count": len(list((backup_path / "profiles").iterdir())),
            "groups_count": len(list((backup_path / "groups").iterdir())),
        }
        with open(backup_path / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        logger.info(f"[BackupManager] 备份已创建: {backup_path}")
        return backup_path

    def restore_backup(self, name: str) -> bool:
        """
        恢复数据备份

        Args:
            name: 备份名称

        Returns:
            是否恢复成功
        """
        backup_path = self.backup_dir / name

        if not backup_path.exists():
            logger.error(f"[BackupManager] 备份不存在: {backup_path}")
            return False

        try:
            profiles_backup = backup_path / "profiles"
            groups_backup = backup_path / "groups"

            if profiles_backup.exists():
                profiles_target = self.data_dir / "profiles"
                if profiles_target.exists():
                    shutil.rmtree(profiles_target)
                shutil.copytree(profiles_backup, profiles_target)

            if groups_backup.exists():
                groups_target = self.data_dir / "groups"
                if groups_target.exists():
                    shutil.rmtree(groups_target)
                shutil.copytree(groups_backup, groups_target)

            logger.info(f"[BackupManager] 备份已恢复: {backup_path}")
            return True

        except Exception as e:
            logger.error(f"[BackupManager] 恢复备份失败: {e}")
            return False

    def list_backups(self) -> List[Dict[str, Any]]:
        """
        列出所有备份

        Returns:
            备份信息列表
        """
        backups = []
        for backup_path in sorted(self.backup_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not backup_path.is_dir():
                continue

            metadata_file = backup_path / "metadata.json"
            if metadata_file.exists():
                with open(metadata_file, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                backups.append(metadata)
            else:
                backups.append(
                    {
                        "name": backup_path.name,
                        "created_at": datetime.fromtimestamp(backup_path.stat().st_mtime).isoformat(),
                    }
                )

        return backups

    def delete_backup(self, name: str) -> bool:
        """
        删除备份

        Args:
            name: 备份名称

        Returns:
            是否删除成功
        """
        backup_path = self.backup_dir / name

        if not backup_path.exists():
            logger.error(f"[BackupManager] 备份不存在: {backup_path}")
            return False

        try:
            shutil.rmtree(backup_path)
            logger.info(f"[BackupManager] 备份已删除: {backup_path}")
            return True
        except Exception as e:
            logger.error(f"[BackupManager] 删除备份失败: {e}")
            return False

    def _cleanup_old_backups(self):
        """清理超过数量限制的旧备份"""
        backups = sorted(
            [p for p in self.backup_dir.iterdir() if p.is_dir()], key=lambda x: x.stat().st_mtime, reverse=True
        )

        if len(backups) > self.max_backups:
            for old_backup in backups[self.max_backups :]:
                try:
                    shutil.rmtree(old_backup)
                    logger.info(f"[BackupManager] 已清理旧备份: {old_backup.name}")
                except Exception as e:
                    logger.warning(f"[BackupManager] 清理备份失败: {e}")

    def export_data(self, export_path: Path) -> bool:
        """
        导出数据到指定路径

        Args:
            export_path: 导出路径

        Returns:
            是否导出成功
        """
        try:
            shutil.copytree(self.data_dir / "profiles", export_path / "profiles")
            shutil.copytree(self.data_dir / "groups", export_path / "groups")
            logger.info(f"[BackupManager] 数据已导出: {export_path}")
            return True
        except Exception as e:
            logger.error(f"[BackupManager] 导出数据失败: {e}")
            return False

    def import_data(self, import_path: Path) -> bool:
        """
        从指定路径导入数据

        Args:
            import_path: 导入路径

        Returns:
            是否导入成功
        """
        try:
            profiles_import = import_path / "profiles"
            groups_import = import_path / "groups"

            if profiles_import.exists():
                profiles_target = self.data_dir / "profiles"
                if profiles_target.exists():
                    shutil.rmtree(profiles_target)
                shutil.copytree(profiles_import, profiles_target)

            if groups_import.exists():
                groups_target = self.data_dir / "groups"
                if groups_target.exists():
                    shutil.rmtree(groups_target)
                shutil.copytree(groups_import, groups_target)

            logger.info(f"[BackupManager] 数据已导入: {import_path}")
            return True
        except Exception as e:
            logger.error(f"[BackupManager] 导入数据失败: {e}")
            return False
