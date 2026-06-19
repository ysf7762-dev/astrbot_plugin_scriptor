# core/context_indexer.py
"""统一上下文索引器 - 渐进式披露引擎核心组件

将 Skills 和 Memory 统一为"上下文节点"，只向 AI 暴露目录树（Index），
AI 需要时通过 file_read_tool 主动读取详细内容。

核心理念：
1. 一切皆节点：无论是 PROFILE.md、MEMORY.md 还是 skills/xxx/SKILL.md
2. 目录即 Prompt：System Prompt 只包含精简的文件目录树
3. 按需读取：AI 通过工具主动获取需要的节点内容
4. 节约 Token：基础 Prompt 从几千 Token 降至几百 Token
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


@dataclass
class ContextNode:
    """上下文节点 - 表示一个可被 AI 读取的 Markdown 文件"""

    path: str
    display_name: str
    description: str
    node_type: str
    size: int = 0
    modified_time: str = ""
    priority: int = 5


@dataclass
class ContextSection:
    """上下文分区 - 将节点按类别分组"""

    title: str
    icon: str
    nodes: List[ContextNode] = field(default_factory=list)


class ContextIndexer:
    """统一上下文索引器

    扫描 profiles/、groups/、skills/ 等目录，
    生成结构化的 Markdown 目录树供 AI 参考。
    """

    def __init__(self, data_dir: Path, config=None):
        self.data_dir = data_dir
        self.config = config

        self._node_descriptions = {
            "P_PROFILE.md": "用户画像与核心偏好",
            "P_SOUL.md": "个人人格定义",
            "P_SOP.md": "个人标准操作流程",
            "G_SOUL.md": "群组人格定义",
            "G_SOP.md": "群组标准操作流程",
            "SOUL.md": "全局核心人格基座",
            "SOP.md": "全局标准操作流程",
            "P_AGENTS.md": "个人代理设定",
            "P_MEMORY.md": "个人长期记忆",
            "MEMORY.md": "长期记忆与经验积累",
            "HEARTBEAT.md": "心跳复盘指令",
            "P_HEARTBEAT.md": "个人心跳指令",
            "G_HEARTBEAT.md": "群组心跳指令",
            "P_TODO.md": "个人待办事项",
            "TODO.md": "待办事项清单",
            "G_TODO.md": "群组待办事项",
            "NOTES.md": "随手笔记与灵感",
            "CONTEXT.md": "当前任务上下文",
            "P_BOOTSTRAP.md": "首次引导脚本",
            "G_BOOTSTRAP.md": "群组首次引导脚本",
            "G_GROUP.md": "群组工作流定义",
            "G_PROFILE.md": "群组公共身份画像",
            "SKILL.md": "技能操作手册",
        }

    def _extract_frontmatter_summary(self, file_path: Path) -> Optional[str]:
        """从文件的 YAML Front Matter 中提取 summary 字段"""
        try:
            content = file_path.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter_text = parts[1]
                    summary_match = re.search(r'summary:\s*["\']?(.+?)["\']?\s*$', frontmatter_text, re.MULTILINE)
                    if summary_match:
                        return summary_match.group(1).strip()
        except Exception as e:
            logger.debug(f"[ContextIndexer] 提取 Front Matter 失败 {file_path}: {e}")
        return None

    def _extract_first_heading(self, file_path: Path, max_length: int = 80) -> str:
        """提取文件的第一个一级标题作为描述"""
        try:
            content = file_path.read_text(encoding="utf-8")
            heading_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
            if heading_match:
                heading = heading_match.group(1).strip()
                if len(heading) > max_length:
                    heading = heading[:max_length] + "..."
                return heading
        except Exception as e:
            logger.debug(f"[ContextIndexer] 提取标题失败 {file_path}: {e}")
        return ""

    def _get_file_description(self, file_path: Path, filename: str) -> str:
        """获取文件的简短描述（优先级调整：Front Matter > 首个标题 > 内置映射）

        根据渐进式披露架构，用户自定义的 YAML summary 应该优先于内置映射，
        因为它反映了文件的实际内容和最新状态。
        """
        # 1. 最高优先级：YAML Front Matter 中的 summary 字段（用户自定义）
        summary = self._extract_frontmatter_summary(file_path)
        if summary:
            return summary

        # 2. 次高优先级：文件的第一个一级标题
        heading = self._extract_first_heading(file_path)
        if heading:
            return heading

        # 3. 兜底：使用内置映射（仅当上述两种都失败时）
        if filename in self._node_descriptions:
            return self._node_descriptions[filename]

        return "Markdown 文档"

    def _scan_personal_nodes(self, uid: str, group_id: str = "private") -> List[ContextNode]:
        """扫描个人记忆目录

        只索引需要渐进式披露的文件，已全量加载的文件不在此处索引：
        - 已全量加载：P_SOUL.md, P_PROFILE.md, P_AGENTS.md, P_TODO.md, P_BOOTSTRAP.md, P_HEARTBEAT.md
        - 渐进式披露：P_SOP.md, P_MEMORY.md, NOTES.md, CONTEXT.md
        - 冷数据（工具检索）：TODOed/ 目录下的归档文件（不在此处索引）
        """
        nodes = []
        profile_dir = self.data_dir / "profiles" / uid

        if not profile_dir.exists():
            return nodes

        priority_map = {
            "P_SOP.md": 7,
            "P_MEMORY.md": 6,
            "NOTES.md": 2,
            "CONTEXT.md": 1,
        }

        for md_file in profile_dir.glob("*.md"):
            if not md_file.is_file():
                continue

            filename = md_file.name

            if filename not in priority_map:
                continue

            if group_id != "private" and filename == "P_SOP.md":
                continue

            stat = md_file.stat()
            rel_path = f"profiles/{uid}/{filename}"

            nodes.append(
                ContextNode(
                    path=rel_path,
                    display_name=filename,
                    description=self._get_file_description(md_file, filename),
                    node_type="personal_memory",
                    size=stat.st_size,
                    modified_time="",
                    priority=priority_map.get(filename, 1),
                )
            )

        memory_dir = profile_dir / "memory"
        if memory_dir.exists():
            md_files = sorted(memory_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:7]
            for md_file in md_files:
                if not md_file.is_file():
                    continue

                stat = md_file.stat()
                filename = md_file.name
                date_str = filename.replace(".md", "")
                rel_path = f"profiles/{uid}/memory/{filename}"

                nodes.append(
                    ContextNode(
                        path=rel_path,
                        display_name=f"日记: {date_str}",
                        description=f"{date_str} 的对话记录",
                        node_type="personal_diary",
                        size=stat.st_size,
                        modified_time="",
                        priority=0,
                    )
                )

        return nodes

    def _scan_group_nodes(self, group_id: str) -> List[ContextNode]:
        """扫描群组记忆目录

        只索引需要渐进式披露的文件，已全量加载的文件不在此处索引：
        - 已全量加载：G_SOUL.md, G_PROFILE.md, G_GROUP.md, G_TODO.md, G_BOOTSTRAP.md, G_HEARTBEAT.md
        - 渐进式披露：G_SOP.md, G_MEMORY.md, NOTES.md
        - 冷数据（工具检索）：TODOed/ 目录下的归档文件（不在此处索引）
        """
        nodes = []
        group_dir = self.data_dir / "groups" / group_id

        if not group_dir.exists():
            return nodes

        priority_map = {
            "G_SOP.md": 7,
            "G_MEMORY.md": 6,
            "NOTES.md": 2,
        }

        for md_file in group_dir.glob("*.md"):
            if not md_file.is_file():
                continue

            filename = md_file.name

            if filename not in priority_map:
                continue

            stat = md_file.stat()
            rel_path = f"groups/{group_id}/{filename}"

            nodes.append(
                ContextNode(
                    path=rel_path,
                    display_name=filename,
                    description=self._get_file_description(md_file, filename),
                    node_type="group_memory",
                    size=stat.st_size,
                    modified_time="",
                    priority=priority_map.get(filename, 1),
                )
            )

        memory_dir = group_dir / "memory"
        if memory_dir.exists():
            md_files = sorted(memory_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:5]
            for md_file in md_files:
                if not md_file.is_file():
                    continue

                stat = md_file.stat()
                filename = md_file.name
                date_str = filename.replace(".md", "")
                rel_path = f"groups/{group_id}/memory/{filename}"

                nodes.append(
                    ContextNode(
                        path=rel_path,
                        display_name=f"日记: {date_str}",
                        description=f"{date_str} 的群组记录",
                        node_type="group_diary",
                        size=stat.st_size,
                        modified_time="",
                        priority=0,
                    )
                )

        return nodes

    def _scan_skill_nodes(self) -> List[ContextNode]:
        """扫描技能目录"""
        nodes = []
        skills_dir = self.data_dir.parent / "skills"

        if not skills_dir.exists():
            skills_dir = self.data_dir / "skills"

        if not skills_dir.exists():
            return nodes

        for skill_folder in sorted(skills_dir.iterdir()):
            if not skill_folder.is_dir():
                continue

            skill_md = skill_folder / "SKILL.md"
            if not skill_md.exists():
                continue

            stat = skill_md.stat()
            skill_name = skill_folder.name
            rel_path = f"skills/{skill_name}/SKILL.md"

            description = self._get_file_description(skill_md, "SKILL.md")
            if description == "技能操作手册":
                heading = self._extract_first_heading(skill_md, 60)
                if heading:
                    description = heading

            nodes.append(
                ContextNode(
                    path=rel_path,
                    display_name=skill_name.replace("scriptor-", "").replace("-", " ").title(),
                    description=description,
                    node_type="skill",
                    size=stat.st_size,
                    modified_time="",
                    priority=5,
                )
            )

        return nodes

    def _scan_global_nodes(self) -> List[ContextNode]:
        """扫描全局共享目录

        只索引需要渐进式披露的文件，已全量加载的文件不在此处索引：
        - 已全量加载：SOUL.md, HEARTBEAT.md
        - 渐进式披露：SOP.md, MEMORY.md
        """
        nodes = []
        global_dir = self.data_dir / "global"

        if not global_dir.exists():
            return nodes

        priority_map = {
            "SOP.md": 7,
            "MEMORY.md": 6,
        }

        for md_file in global_dir.glob("*.md"):
            if not md_file.is_file():
                continue

            filename = md_file.name

            if filename not in priority_map:
                continue

            stat = md_file.stat()
            rel_path = f"global/{filename}"

            nodes.append(
                ContextNode(
                    path=rel_path,
                    display_name=filename,
                    description=self._get_file_description(md_file, filename),
                    node_type="global",
                    size=stat.st_size,
                    modified_time="",
                    priority=priority_map.get(filename, 1),
                )
            )

        return nodes

    def build_context_map(self, uid: str, group_id: str, include_skills: bool = True) -> str:
        """构建完整的上下文目录地图（Markdown 格式）

        Args:
            uid: 用户 ID
            group_id: 群组 ID
            include_skills: 是否包含技能目录

        Returns:
            格式化的 Markdown 目录文本
        """
        sections = []

        personal_nodes = self._scan_personal_nodes(uid, group_id)
        if personal_nodes:
            personal_nodes.sort(key=lambda x: (-x.priority, x.display_name))
            section = self._format_section("📚 个人记忆 (Personal Memory)", personal_nodes)
            sections.append(section)

        if group_id != "private":
            group_nodes = self._scan_group_nodes(group_id)
            if group_nodes:
                group_nodes.sort(key=lambda x: (-x.priority, x.display_name))
                section = self._format_section("👥 群组记忆 (Group Memory)", group_nodes)
                sections.append(section)

        global_nodes = self._scan_global_nodes()
        if global_nodes:
            global_nodes.sort(key=lambda x: (-x.priority, x.display_name))
            section = self._format_section("🌐 全局共享 (Global)", global_nodes)
            sections.append(section)

        if include_skills:
            skill_nodes = self._scan_skill_nodes()
            if skill_nodes:
                skill_nodes.sort(key=lambda x: x.display_name)
                section = self._format_section("🛠️ 可用技能 (Skills)", skill_nodes)
                sections.append(section)

        if not sections:
            return ""

        header = (
            "## 📋 可用上下文节点目录 (Context Index)\n\n"
            "**你的大脑容量有限，以下是你的知识库目录。**\n"
            "当你需要详细信息时，请使用 `file_read_tool(file_path)` 读取对应文件。\n\n"
        )

        usage_hint = (
            "\n---\n\n"
            "**使用示例**：\n"
            '- 查看用户画像 → `file_read_tool("profiles/user123/PROFILE.md")`\n'
            '- 学习归档技能 → `file_read_tool("skills/archive-manager/SKILL.md")`\n'
            '- 读取今日日记 → `file_read_tool("profiles/user123/memory/2026-03-10.md")`\n'
        )

        return header + "\n".join(sections) + usage_hint

    def _format_section(self, title: str, nodes: List[ContextNode]) -> str:
        """格式化一个分区"""
        lines = [f"### {title}", ""]
        for node in nodes:
            size_hint = ""
            if node.size > 5000:
                size_hint = f" ({node.size // 1000}KB)"
            elif node.size > 1000:
                size_hint = f" ({node.size // 1024}KB)"

            lines.append(f"- **{node.display_name}**{size_hint}: `{node.path}`")
            lines.append(f"  - {node.description}")
            lines.append("")
        return "\n".join(lines)

    def get_node_content(self, node_path: str, max_chars: int = 8000) -> Tuple[bool, str]:
        """读取指定节点的内容（带安全校验）

        Args:
            node_path: 节点路径（如 profiles/user123/PROFILE.md）
            max_chars: 最大返回字符数

        Returns:
            (是否成功, 内容或错误信息)
        """
        allowed_prefixes = [
            "profiles/",
            "groups/",
            "skills/",
            "global/",
        ]

        is_allowed = any(node_path.startswith(prefix) for prefix in allowed_prefixes)
        if not is_allowed:
            return False, f"Error: 路径不在允许的访问范围内: {node_path}"

        full_path = self.data_dir.parent / node_path if node_path.startswith("skills/") else self.data_dir / node_path

        full_path = full_path.resolve()

        data_dir_resolved = self.data_dir.resolve()
        parent_dir = data_dir_resolved.parent.resolve()

        if not (str(full_path).startswith(str(data_dir_resolved)) or str(full_path).startswith(str(parent_dir))):
            return False, f"Error: 路径穿越检测失败: {node_path}"

        if not full_path.exists():
            return False, f"Error: 文件不存在: {node_path}"

        if not full_path.is_file():
            return False, f"Error: 路径不是文件: {node_path}"

        try:
            content = full_path.read_text(encoding="utf-8")

            if len(content) > max_chars:
                content = content[:max_chars] + f"\n\n... [内容过长，已截断，共 {len(content)} 字符] ..."

            return True, content
        except Exception as e:
            return False, f"Error: 读取文件失败: {e}"

    def list_available_nodes(self, uid: str, group_id: str, node_type: Optional[str] = None) -> List[Dict]:
        """列出所有可用节点（用于调试和管理界面）

        Args:
            uid: 用户 ID
            group_id: 群组 ID
            node_type: 可选的类型过滤

        Returns:
            节点信息列表
        """
        all_nodes = []

        all_nodes.extend(self._scan_personal_nodes(uid))

        if group_id != "private":
            all_nodes.extend(self._scan_group_nodes(group_id))

        all_nodes.extend(self._scan_global_nodes())
        all_nodes.extend(self._scan_skill_nodes())

        if node_type:
            all_nodes = [n for n in all_nodes if n.node_type == node_type]

        return [
            {
                "path": n.path,
                "display_name": n.display_name,
                "description": n.description,
                "type": n.node_type,
                "size": n.size,
                "priority": n.priority,
            }
            for n in all_nodes
        ]
