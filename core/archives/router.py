# core/archives/router.py
"""
档案路由器与索引器

功能：
1. ArchiveRouter: 根据上下文决定查询/导入哪个数据库
2. ArchiveIndex: 聚合所有可访问数据库的目录信息

三级架构：
- Personal: profiles/{uid}/archives.db
- Group: groups/{group_id}/archives.db
- Global: global/archives.db

路由规则：
- Sudo 模式 → Global
- 私聊 + Normal → Personal
- 群聊 + Normal → Group
"""

import json
import sqlite3
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


class ArchiveScope(Enum):
    """档案层级"""

    PERSONAL = "personal"
    GROUP = "group"
    GLOBAL = "global"


class ArchiveRouter:
    """档案路由器 - 根据上下文决定操作哪个数据库"""

    def __init__(self, data_dir: Path):
        """
        初始化路由器

        Args:
            data_dir: 插件数据目录
        """
        self.data_dir = Path(data_dir)

        self._ensure_directories()

    def _ensure_directories(self):
        """确保必要的目录存在"""
        (self.data_dir / "global").mkdir(parents=True, exist_ok=True)

    def resolve_db_path(self, uid: str, group_id: str, is_sudo: bool = False) -> Tuple[Path, ArchiveScope]:
        """
        解析数据库路径

        Args:
            uid: 用户 ID (格式: user_xxx)
            group_id: 群组 ID ("private" 表示私聊)
            is_sudo: 是否处于 Sudo 模式

        Returns:
            (数据库路径, 层级)
        """
        if is_sudo:
            return self.data_dir / "global" / "archives.db", ArchiveScope.GLOBAL

        if group_id == "private":
            # uid 已经是 user_xxx 格式，直接使用
            profile_dir = self.data_dir / "profiles" / uid
            profile_dir.mkdir(parents=True, exist_ok=True)
            return profile_dir / "archives.db", ArchiveScope.PERSONAL
        else:
            group_dir = self.data_dir / "groups" / group_id
            group_dir.mkdir(parents=True, exist_ok=True)
            return group_dir / "archives.db", ArchiveScope.GROUP

    def resolve_accessible_dbs(self, uid: str, group_id: str, is_sudo: bool = False) -> List[Dict[str, Any]]:
        """
        获取当前上下文可访问的所有数据库

        按优先级返回：[personal, group, global]

        Args:
            uid: 用户 ID (格式: user_xxx)
            group_id: 群组 ID
            is_sudo: 是否处于 Sudo 模式（Sudo 模式下可访问所有群组档案）

        Returns:
            可访问数据库列表
        """
        dbs = []

        if is_sudo:
            all_groups_db = self._get_all_groups_dbs()
            for group_db_info in all_groups_db:
                dbs.append(group_db_info)
        else:
            if group_id != "private":
                group_dir = self.data_dir / "groups" / group_id
                group_db = group_dir / "archives.db"
                if group_db.exists():
                    dbs.append(
                        {"scope": ArchiveScope.GROUP, "path": group_db, "priority": 2, "label": f"群组档案({group_id})"}
                    )

        global_db = self.data_dir / "global" / "archives.db"
        if global_db.exists():
            dbs.append({"scope": ArchiveScope.GLOBAL, "path": global_db, "priority": 3, "label": "全局档案"})

        return sorted(dbs, key=lambda x: x["priority"])

    def _get_all_groups_dbs(self) -> List[Dict[str, Any]]:
        """获取所有群组的档案数据库"""
        dbs = []
        groups_dir = self.data_dir / "groups"
        if not groups_dir.exists():
            return dbs

        for group_path in groups_dir.iterdir():
            if group_path.is_dir() and not group_path.name.startswith("."):
                group_db = group_path / "archives.db"
                if group_db.exists():
                    dbs.append(
                        {
                            "scope": ArchiveScope.GROUP,
                            "path": group_db,
                            "priority": 2,
                            "label": f"群组档案({group_path.name})",
                        }
                    )
        return dbs

    def get_db_path_for_scope(self, uid: str, group_id: str, scope: ArchiveScope) -> Path:
        """
        获取指定层级的数据库路径

        Args:
            uid: 用户 ID (格式: user_xxx)
            group_id: 群组 ID
            scope: 目标层级

        Returns:
            数据库路径
        """
        if scope == ArchiveScope.GLOBAL:
            return self.data_dir / "global" / "archives.db"
        elif scope == ArchiveScope.PERSONAL:
            # uid 已经是 user_xxx 格式，直接使用
            profile_dir = self.data_dir / "profiles" / uid
            profile_dir.mkdir(parents=True, exist_ok=True)
            return profile_dir / "archives.db"
        elif scope == ArchiveScope.GROUP:
            if group_id == "private":
                raise ValueError("私聊场景无法使用群组档案馆")
            group_dir = self.data_dir / "groups" / group_id
            group_dir.mkdir(parents=True, exist_ok=True)
            return group_dir / "archives.db"
        else:
            raise ValueError(f"未知的层级: {scope}")

    def get_all_archives_flat(self) -> List[Dict[str, Any]]:
        """
        获取所有档案的扁平列表（用于 Web UI 管理员视图）

        扫描所有层级的数据库，返回包含层级信息的档案列表

        Returns:
            档案列表，每个档案包含 scope 字段
        """
        result = []

        # 1. 扫描全局档案
        global_db = self.data_dir / "global" / "archives.db"
        if global_db.exists():
            archives = self._get_archives_from_db(global_db)
            for arc in archives:
                arc["scope"] = ArchiveScope.GLOBAL.value
                arc["scope_label"] = "全局"
                arc["db_path"] = str(global_db)
                result.append(arc)

        # 2. 扫描所有群组档案
        groups_dir = self.data_dir / "groups"
        if groups_dir.exists():
            for group_dir in groups_dir.iterdir():
                if group_dir.is_dir():
                    group_db = group_dir / "archives.db"
                    if group_db.exists():
                        group_id = group_dir.name
                        archives = self._get_archives_from_db(group_db)
                        for arc in archives:
                            arc["scope"] = ArchiveScope.GROUP.value
                            arc["scope_label"] = f"群组 {group_id}"
                            arc["target_id"] = group_id
                            arc["db_path"] = str(group_db)
                            result.append(arc)

        # 3. 扫描所有个人档案
        profiles_dir = self.data_dir / "profiles"
        if profiles_dir.exists():
            for profile_dir in profiles_dir.iterdir():
                if profile_dir.is_dir() and profile_dir.name.startswith("user_"):
                    personal_db = profile_dir / "archives.db"
                    if personal_db.exists():
                        uid = profile_dir.name.replace("user_", "")
                        archives = self._get_archives_from_db(personal_db)
                        for arc in archives:
                            arc["scope"] = ArchiveScope.PERSONAL.value
                            arc["scope_label"] = f"个人 {uid[:8]}..."
                            arc["target_id"] = uid
                            arc["db_path"] = str(personal_db)
                            result.append(arc)

        # 按导入时间排序
        result.sort(key=lambda x: x.get("import_time", ""), reverse=True)

        return result

    def _get_archives_from_db(self, db_path: Path) -> List[Dict[str, Any]]:
        """从数据库获取档案列表"""
        if not db_path.exists():
            return []

        try:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute("""
                    SELECT table_name, display_name, description, row_count, columns_json
                    FROM archive_registry
                    ORDER BY import_time DESC
                """)

                result: List[Dict[str, Any]] = []
                rows = cursor.fetchall()
                for row in rows:
                    arc = dict(row)
                    columns_json = row.get("columns_json")
                    if columns_json:
                        try:
                            arc["columns"] = json.loads(columns_json)
                        except json.JSONDecodeError:
                            arc["columns"] = []
                    result.append(arc)
        except Exception as e:
            logger.debug(f"[ArchiveRouter] 读取数据库 {db_path} 失败: {e}")
            return []


class ArchiveIndex:
    """档案索引器 - 聚合所有可访问数据库的目录信息"""

    def __init__(self, router: ArchiveRouter):
        """
        初始化索引器

        Args:
            router: 档案路由器实例
        """
        self.router = router

    def build_unified_catalog(self, uid: str, group_id: str, is_sudo: bool = False) -> str:
        """
        构建统一的档案目录提示词

        Args:
            uid: 用户 ID
            group_id: 群组 ID
            is_sudo: 是否处于 Sudo 模式

        Returns:
            档案目录提示词
        """
        accessible_dbs = self.router.resolve_accessible_dbs(uid, group_id, is_sudo)

        if not accessible_dbs:
            return ""

        catalog_parts = ["【档案馆目录】"]

        for db_info in accessible_dbs:
            scope = db_info["scope"]
            path = db_info["path"]
            label = db_info["label"]

            archives = self._get_archives_from_db(path)

            if not archives:
                continue

            scope_icons = {ArchiveScope.PERSONAL: "👤", ArchiveScope.GROUP: "👥", ArchiveScope.GLOBAL: "🌐"}

            icon = scope_icons.get(scope, "📁")
            catalog_parts.append(f"\n## {icon} {label}")

            for arc in archives:
                table_name = arc.get("table_name", "unknown")
                display_name = arc.get("display_name", table_name)
                row_count = arc.get("row_count", 0)
                description = arc.get("description", "")

                desc_str = f" - {description}" if description else ""
                catalog_parts.append(f"- {display_name} ({table_name}) [{row_count}条]{desc_str}")

        if len(catalog_parts) == 1:
            return ""

        return "\n".join(catalog_parts)

    def get_all_archives_flat(self, uid: str, group_id: str, is_sudo: bool = False) -> List[Dict[str, Any]]:
        """
        获取所有可访问档案的扁平列表

        Args:
            uid: 用户 ID
            group_id: 群组 ID
            is_sudo: 是否处于 Sudo 模式

        Returns:
            档案列表
        """
        accessible_dbs = self.router.resolve_accessible_dbs(uid, group_id, is_sudo)
        result = []

        for db_info in accessible_dbs:
            scope = db_info["scope"]
            path = db_info["path"]
            label = db_info["label"]

            archives = self._get_archives_from_db(path)

            for arc in archives:
                arc["scope"] = scope.value
                arc["scope_label"] = label
                arc["db_path"] = str(path)
                result.append(arc)

        return result

    def find_table_scope(
        self, uid: str, group_id: str, table_name: str, is_sudo: bool = False
    ) -> Optional[Tuple[Path, ArchiveScope]]:
        """
        查找表所在的数据库

        Args:
            uid: 用户 ID
            group_id: 群组 ID
            table_name: 表名
            is_sudo: 是否处于 Sudo 模式

        Returns:
            (数据库路径, 层级) 或 None
        """
        accessible_dbs = self.router.resolve_accessible_dbs(uid, group_id, is_sudo)

        for db_info in accessible_dbs:
            path = db_info["path"]
            scope = db_info["scope"]

            if self._table_exists_in_db(path, table_name):
                return path, scope

        return None

    def _get_archives_from_db(self, db_path: Path) -> List[Dict[str, Any]]:
        """从数据库获取档案列表"""
        if not db_path.exists():
            return []

        try:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute("""
                    SELECT table_name, display_name, description, row_count, columns_json
                    FROM archive_registry
                    ORDER BY import_time DESC
                """)

                results = []
                for row in cursor.fetchall():
                    arc = dict(row)
                    columns_json = arc.pop("columns_json", "{}")
                    try:
                        arc["columns"] = list(json.loads(columns_json).keys()) if columns_json else []
                    except json.JSONDecodeError:
                        arc["columns"] = []
                    results.append(arc)
                return results
        except Exception as e:
            logger.debug(f"[ArchiveIndex] 读取数据库 {db_path} 失败: {e}")
            return []

    def _table_exists_in_db(self, db_path: Path, table_name: str) -> bool:
        """检查表是否存在于数据库中"""
        if not db_path.exists():
            return False

        try:
            with sqlite3.connect(str(db_path)) as conn:
                cursor = conn.cursor()

                cursor.execute("SELECT 1 FROM archive_registry WHERE table_name = ?", (table_name,))

                return cursor.fetchone() is not None
        except Exception as e:
            logger.debug(f"[ArchiveRouter] 检查表是否存在失败: {e}")
            return False

    def get_archive_stats(self, uid: str, group_id: str) -> Dict[str, Any]:
        """
        获取档案统计信息

        Args:
            uid: 用户 ID
            group_id: 群组 ID

        Returns:
            统计信息字典
        """
        accessible_dbs = self.router.resolve_accessible_dbs(uid, group_id)

        stats = {"total_archives": 0, "total_rows": 0, "by_scope": {}}

        for db_info in accessible_dbs:
            scope = db_info["scope"]
            path = db_info["path"]

            archives = self._get_archives_from_db(path)

            scope_stats = {"count": len(archives), "total_rows": sum(a.get("row_count", 0) for a in archives)}

            stats["by_scope"][scope.value] = scope_stats
            stats["total_archives"] += scope_stats["count"]
            stats["total_rows"] += scope_stats["total_rows"]

        return stats


def migrate_legacy_archive(data_dir: Path) -> bool:
    """
    迁移旧版 archives.db 到 global 目录

    Args:
        data_dir: 插件数据目录

    Returns:
        是否执行了迁移
    """
    legacy_db = data_dir / "archives.db"
    global_dir = data_dir / "global"
    global_db = global_dir / "archives.db"

    if not legacy_db.exists():
        return False

    if global_db.exists():
        logger.info("[ArchiveMigration] 全局档案馆已存在，跳过迁移")
        return False

    global_dir.mkdir(parents=True, exist_ok=True)

    import shutil

    shutil.move(str(legacy_db), str(global_db))

    logger.info(f"[ArchiveMigration] 已迁移 {legacy_db} -> {global_db}")
    return True
