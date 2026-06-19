# core/group_manager.py
"""Scriptor 群体管理模块"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


@dataclass
class GroupMember:
    """群体成员"""

    uid: str
    alias: str
    role: str = "member"  # owner, admin, member
    joined_at: float = field(default_factory=time.time)
    profile_link: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GroupMember":
        return cls(**data)


@dataclass
class Group:
    """群体/群聊"""

    group_id: str
    name: str
    platform: str
    owner_uid: str
    created_at: float = field(default_factory=time.time)
    members: List[GroupMember] = field(default_factory=list)
    cross_group_enabled: bool = True

    def to_dict(self) -> dict:
        data = asdict(self)
        data["members"] = [m.to_dict() for m in self.members]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Group":
        members = [GroupMember.from_dict(m) for m in data.get("members", [])]
        data["members"] = members
        return cls(**data)


class GroupManager:
    """群体/群聊管理"""

    def __init__(self, data_dir: Path, identity_manager):
        self.data_dir = data_dir
        self.identity_manager = identity_manager
        self.group_map_file = data_dir / "groups" / "group_map.json"
        self.groups: Dict[str, Group] = {}
        self._load()

    def _load(self):
        """加载群体数据"""
        if self.group_map_file.exists():
            try:
                with open(self.group_map_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for group_id, group_data in data.get("groups", {}).items():
                        self.groups[group_id] = Group.from_dict(group_data)
            except (OSError, json.JSONDecodeError) as e:
                logger.error(f"[Scriptor] 加载群体数据失败: {e}")

    def _save(self):
        """保存群体数据 (后台线程异步)"""
        import threading

        data = {"groups": {gid: g.to_dict() for gid, g in self.groups.items()}}

        def _save_background():
            import json

            try:
                self.group_map_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.group_map_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except (OSError, json.JSONDecodeError) as e:
                logger.error(f"[Scriptor] 保存群体数据失败: {e}")

        threading.Thread(target=_save_background, daemon=True).start()

    def get_or_create_group(self, group_id: str, group_name: str, platform: str, owner_uid: str) -> Group:
        """获取或创建群体"""
        if group_id not in self.groups:
            group = Group(
                group_id=group_id,
                name=group_name,
                platform=platform,
                owner_uid=owner_uid,
            )
            self.groups[group_id] = group
            self._save()
            self._init_group_directory(group)
            logger.info(f"[Scriptor] 创建新群体: {group_id}")
        else:
            # 如果平台传来的群名有更新，同步更新
            if group_name and self.groups[group_id].name != group_name:
                self.groups[group_id].name = group_name
                self._save()

        return self.groups[group_id]

    def get_group_name(self, group_id: str) -> str:
        """获取群组名称，如果不存在则返回 group_id"""
        if group_id in self.groups:
            return self.groups[group_id].name
        return group_id

    def add_member(self, group_id: str, uid: str, alias: str, role: str = "member"):
        """添加成员到群体"""
        if group_id not in self.groups:
            return

        group = self.groups[group_id]

        # 检查是否已存在
        for member in group.members:
            if member.uid == uid:
                member.alias = alias
                member.role = role
                self._save()
                return

        # 添加新成员
        member = GroupMember(uid=uid, alias=alias, role=role)
        group.members.append(member)
        self._save()

    def remove_member(self, group_id: str, uid: str):
        """从群体移除成员"""
        if group_id not in self.groups:
            return

        group = self.groups[group_id]
        group.members = [m for m in group.members if m.uid != uid]
        self._save()

    def get_member(self, group_id: str, uid: str) -> Optional[GroupMember]:
        """获取群体成员"""
        if group_id not in self.groups:
            return None

        for member in self.groups[group_id].members:
            if member.uid == uid:
                return member
        return None

    def get_group_members(self, group_id: str) -> List[GroupMember]:
        """获取群体所有成员"""
        if group_id not in self.groups:
            return []
        return self.groups[group_id].members

    def get_group(self, group_id: str) -> Optional[Group]:
        """获取群体"""
        return self.groups.get(group_id)

    def get_all_groups(self) -> List[Group]:
        """获取所有群体"""
        return list(self.groups.values())

    def record_group_interaction(
        self, group_id: str, member_uid: str, content: str, role: str = "member", mentions: List[str] = None
    ):
        """记录群体交互（后台异步写入）

        注意：仅处理已注册的群组。私聊等非群组场景应使用 MemoryManager.record_interaction。
        """
        # 边界校验：拒绝未注册的群组 ID（如 'private'、'system' 等）
        if not self._validate_group_exists(group_id):
            logger.warning(f"[Scriptor] record_group_interaction 拒绝非群组 ID: {group_id}")
            return

        import threading

        group_dir = self._get_group_dir(group_id)
        group_dir.mkdir(parents=True, exist_ok=True)

        member = self.get_member(group_id, member_uid)
        alias = member.alias if member else "Unknown"

        today = time.strftime("%Y-%m-%d")
        daily_note_file = group_dir / "memory" / f"{today}.md"

        timestamp = time.strftime("%H:%M:%S")

        # 构建提及信息
        mention_info = ""
        if mentions:
            mention_names = []
            for m_uid in mentions:
                m_member = self.get_member(group_id, m_uid)
                m_name = m_member.alias if m_member else f"Unknown({m_uid})"
                mention_names.append(f"@{m_name}")
            mention_info = f" (提及了: {', '.join(mention_names)})"

        entry = f"### [{timestamp}] {alias}{mention_info}\n{content}\n\n"

        def _write_async():
            daily_note_file.parent.mkdir(parents=True, exist_ok=True)
            with open(daily_note_file, "a", encoding="utf-8") as f:
                f.write(entry)

        threading.Thread(target=_write_async, daemon=True).start()

    def get_group_context(self, group_id: str, current_uid: str) -> Dict:
        """获取群体上下文"""
        group = self.get_group(group_id)
        if not group:
            return {}

        group_dir = self._get_group_dir(group_id)

        context = {
            "group_id": group_id,
            "group_name": group.name,
            "members": [],
            "group_rules": "",
            "recent_memory": "",
        }

        # 获取成员列表
        for member in group.members:
            if member.uid != current_uid:
                context["members"].append({"alias": member.alias, "role": member.role})

        # 获取群体规则
        rules_file = group_dir / "G_GROUP.md"
        if rules_file.exists():
            context["group_rules"] = rules_file.read_text(encoding="utf-8")

        # 获取最近记忆
        memory_dir = group_dir / "memory"
        if memory_dir.exists():
            recent_files = sorted(memory_dir.glob("*.md"), reverse=True)[:3]
            recent_content = []
            for f in recent_files:
                recent_content.append(f.read_text(encoding="utf-8"))
            context["recent_memory"] = "\n\n".join(recent_content)

        return context

    def get_other_groups(self, uid: str, exclude_group: str = "") -> List[str]:
        """获取用户参与的其他群体"""
        user_groups = []
        for group_id, group in self.groups.items():
            if group_id == exclude_group:
                continue
            for member in group.members:
                if member.uid == uid:
                    user_groups.append(group_id)
                    break
        return user_groups

    def get_user_joined_groups(self, uid: str) -> List[str]:
        """获取用户加入的所有群组ID列表（用于 cross 搜索的物理边界）

        Args:
            uid: 用户ID

        Returns:
            该用户加入的所有群组ID列表
        """
        user_groups = []
        for group_id, group in self.groups.items():
            for member in group.members:
                if member.uid == uid:
                    user_groups.append(group_id)
                    break
        return user_groups

    def _get_group_dir(self, group_id: str) -> Path:
        """获取群体目录"""
        return self.data_dir / "groups" / group_id

    def _validate_group_exists(self, group_id: str) -> bool:
        """校验群组是否已注册

        防止未注册的 ID（如 'private'、'system' 等）污染 groups 目录。
        只有通过 get_or_create_group 注册的群组才是合法的。

        Args:
            group_id: 群组 ID

        Returns:
            True 如果群组已注册，False 否则
        """
        return group_id in self.groups

    def _init_group_directory(self, group: Group):
        """初始化群体目录 - 使用新的命名格式

        群组拥有独立的灵魂文件 (G_SOUL.md) 和群组画像 (G_PROFILE.md)
        """
        group_dir = self._get_group_dir(group.group_id)
        group_dir.mkdir(parents=True, exist_ok=True)

        template_dir = Path(__file__).parent.parent / "templates" / "group"
        fallback_template_dir = Path(__file__).parent.parent / "templates"

        import time

        current_time = time.strftime("%Y-%m-%d %H:%M:%S")

        group_templates = {
            "G_SOUL.md": {"vars": []},
            "G_PROFILE.md": {"vars": ["group_id", "admin_uid", "group_vibe"]},
            "G_MEMORY.md": {"vars": ["timestamp"]},
            "G_HEARTBEAT.md": {"vars": []},
            "G_GROUP.md": {"vars": ["timestamp"]},
            "G_BOOTSTRAP.md": {"vars": ["group_id"]},
        }

        for template_name, config in group_templates.items():
            target = group_dir / template_name
            if target.exists():
                continue

            template_path = template_dir / template_name
            if not template_path.exists():
                fallback_path = fallback_template_dir / template_name
                if fallback_path.exists():
                    template_path = fallback_path
                else:
                    continue

            content = template_path.read_text(encoding="utf-8")

            if "group_id" in config["vars"]:
                content = content.replace("{{group_id}}", group.group_id)
            if "admin_uid" in config["vars"]:
                content = content.replace("{{admin_uid}}", group.owner_uid)
            if "group_vibe" in config["vars"]:
                content = content.replace("{{group_vibe}}", "默认")
            if "timestamp" in config["vars"]:
                content = content.replace("{{timestamp}}", current_time)

            target.write_text(content, encoding="utf-8")

        (group_dir / "memory").mkdir(exist_ok=True)
