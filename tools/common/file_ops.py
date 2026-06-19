# tools/common/file_ops.py
"""文件操作工具模块 - 参考 CoPaw 的 AgentMdManager 设计

提供 AI 主动操作工作文件的能力：
- read_file: 读取工作文件
- write_file: 创建/覆盖工作文件
- edit_file: 编辑文件（查找替换）
- append_file: 追加内容到文件
- grep_search: 在文件中搜索内容
- glob_search: 查找匹配的文件

VFS (Virtual File System) 支持：
- @personal/ -> 当前用户的个人目录
- @group/ -> 当前群组的共享目录
- @root/ -> 系统根目录（需要管理员权限）
"""

import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Tuple

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

try:
    from astrbot.api import logger
    from astrbot.api.star import StarTools
except ImportError:
    import logging

    logger = logging.getLogger(__name__)
    StarTools = None

# ==========================================
# VFS (Virtual File System) 常量定义
# ==========================================
VFS_NAMESPACE_PERSONAL = "@personal/"
VFS_NAMESPACE_GROUP = "@group/"
VFS_NAMESPACE_ROOT = "@root/"
VFS_VIRTUAL_ROOT_MARKER = "."

# ==========================================
# 核心文件结构保护规则 (File Structure Guardian)
# ==========================================
READ_ONLY_DIRECTORIES = [
    "skills/",
    "templates/",
]

MIN_CONTENT_RATIO = 0.5

PROTECTED_FILES_RULES = {
    # 个人层级文件 (Personal Tier)
    "P_PROFILE.md": {
        "require_frontmatter": True,
        "required_headings": [
            "## 1. 基础身份",
            "## 2. 交互协议与偏好",
            "## 3. 隐私与社交边界",
            "## 4. 核心关系图谱",
            "## 5. 持续关注点",
        ],
    },
    "P_MEMORY.md": {
        "require_frontmatter": True,
        "required_headings": [
            "## 个人专属记忆",
            "### 记忆打标机制 (Memory Tagging)",
            "### 重要事件与决策",
            "### 经验教训",
            "## 工具设置",
        ],
    },
    "P_AGENTS.md": {
        "require_frontmatter": True,
        "required_headings": [
            "## 记忆与档案管理",
            "## 安全与边界",
            "## 内部 vs 外部",
        ],
    },
    "P_SOUL.md": {"require_frontmatter": True, "required_headings": ["## 个人核心人格"]},
    # 群组层级文件 (Group Tier)
    "G_PROFILE.md": {
        "require_frontmatter": True,
        "required_headings": [
            "## 1. 群组定义",
            "## 2. 成员角色矩阵与社交关系图谱 (Social Graph)",
            "## 3. 场景集体记忆",
            "## 4. 场景待办",
        ],
    },
    "G_MEMORY.md": {
        "require_frontmatter": True,
        "required_headings": [
            "## 群组共享记忆",
            "### 群组传说与大事件 (Group Lore)",
            "### 群内梗与黑话 (Memes)",
            "### 群内共识与决策",
        ],
    },
    "G_GROUP.md": {
        "require_frontmatter": True,
        "required_headings": [
            "## 群组场景工作流",
            "### 1. 社交感知与边界感",
            "### 2. 群体记忆的提取与记录",
            "### 3. 冲突与异常处理",
            "### 4. 群组专属任务处理",
        ],
    },
    "G_SOUL.md": {"require_frontmatter": True, "required_headings": ["## 群组核心人格"]},
    # 全局层级文件 (Global Tier)
    "SOUL.md": {
        "require_frontmatter": True,
        "required_headings": [
            "## 核心基座心智",
            "## 语言表达深度负向约束 (最高权重)",
            "## 交互与表达准则：极致极简",
            "## 边界与隐私防线",
            "## 连续性与自我进化",
        ],
    },
    "MEMORY.md": {
        "require_frontmatter": True,
        "required_headings": [
            "## 全局共享知识库",
            "### 系统级公共知识",
            "### 系统级重大事件",
            "### 全局经验教训",
        ],
    },
}


def _validate_file_structure(filename: str, content: str) -> tuple[bool, str]:
    """
    校验文件内容是否符合结构要求。
    返回: (是否通过校验, 错误提示信息)
    """
    basename = os.path.basename(filename).upper()
    # 统一转换为大写进行匹配，因为文件系统可能大小写不敏感
    rule_key = next((k for k in PROTECTED_FILES_RULES if k.upper() == basename), None)

    if not rule_key:
        return True, ""  # 非受保护文件，直接放行

    rules = PROTECTED_FILES_RULES[rule_key]
    missing_elements = []

    # 1. 校验 Frontmatter
    if rules.get("require_frontmatter"):
        # 检查开头是否有 --- ... ---
        if not re.match(r"^\s*---[\s\S]*?---\s*", content):
            missing_elements.append("文件开头的 YAML 元数据 (--- 区域)")

    # 2. 校验关键标题
    for heading in rules.get("required_headings", []):
        # 检查标题是否存在（允许标题前后有空格，允许标题后跟其他文字）
        # 使用 re.escape 转义特殊字符，并匹配行首
        if not re.search(rf"^{re.escape(heading)}", content, re.MULTILINE):
            missing_elements.append(f"章节标题 '{heading}'")

    if missing_elements:
        error_msg = (
            f"写入失败：你破坏了 {rule_key} 的核心结构。\n"
            f"缺失了以下必须保留的元素：\n"
            + "\n".join([f"- {elem}" for elem in missing_elements])
            + "\n\n请严格保留这些结构（即使内容为空），重新生成并调用工具。"
        )
        return False, error_msg

    return True, ""


# ==========================================


# ==========================================
# 目录级权限控制 (Directory-Level Access Control)
# ==========================================
def _check_read_only_directory(file_path: str) -> Optional[str]:
    """检查文件路径是否属于只读目录

    Args:
        file_path: 文件路径（可以是相对路径或绝对路径）

    Returns:
        如果是只读目录，返回错误信息；否则返回 None
    """
    path_lower = file_path.lower().replace("\\", "/")

    for readonly_dir in READ_ONLY_DIRECTORIES:
        if path_lower.startswith(readonly_dir.lower()):
            return (
                f"Error: 路径 `{file_path}` 属于只读目录（{readonly_dir}）。\n"
                f"该目录下的文件为官方技能/模板文件，禁止修改、删除或覆写。\n"
                f"如果你需要记录自定义技能或工作流经验，请在用户目录下创建 P_SOP.md 文件。"
            )

    return None


# ==========================================


# ==========================================
# 防缩水机制 (Anti-Shrinkage Protection)
# ==========================================
def _check_content_shrinkage(original_content: str, new_content: str, filename: str) -> tuple[bool, str]:
    """检查内容是否出现异常缩水（防止 AI 删除大部分内容只保留标题）

    Args:
        original_content: 原始文件内容
        new_content: 新的文件内容
        filename: 文件名（用于错误提示）

    Returns:
        (是否通过检查, 错误信息)
    """
    if not original_content.strip():
        return True, ""

    original_len = len(original_content.strip())
    new_len = len(new_content.strip())

    if new_len == 0:
        return False, (
            f"写入失败：你试图将 {filename} 清空。\n"
            f"这属于破坏性操作，被防缩水机制拦截。\n\n"
            f"请保留文件的核心内容，仅修改需要更新的部分。"
        )

    ratio = new_len / original_len

    if ratio < MIN_CONTENT_RATIO:
        return False, (
            f"写入失败：{filename} 的内容出现了异常缩水。\n"
            f"原始长度: {original_len} 字符 → 新长度: {new_len} 字符 (比例: {ratio:.1%})\n\n"
            f"**低于安全阈值 ({MIN_CONTENT_RATIO:.0%})，涉嫌破坏性覆写。**\n\n"
            f"可能的原因：\n"
            f"- 你意外删除了大部分段落内容，只保留了标题\n"
            f"- 模型输出截断导致内容丢失\n\n"
            f"请重新生成完整内容，确保保留原有信息。"
        )

    return True, ""


# ==========================================


# ==========================================
# 空白字符容错匹配辅助函数
# ==========================================
def _normalize_whitespace(text: str) -> str:
    """
    将文本中所有连续的空白字符（空格、换行、Tab）替换为单个空格。
    用于容错匹配，解决大模型生成的文本与原文空白字符不一致的问题。
    """
    return re.sub(r"\s+", " ", text).strip()


def _fuzzy_replace(content: str, old_text: str, new_text: str, replace_all: bool = False) -> tuple[bool, str, str]:
    """
    尝试进行模糊替换（忽略空白字符差异）。
    返回: (是否成功, 替换后的内容, 错误信息)
    """
    replace_count = -1 if replace_all else 1

    # 1. 精确匹配（首选）
    if old_text in content:
        new_content = content.replace(old_text, new_text, replace_count)
        return True, new_content, ""

    # 2. 空白字符容错匹配（降级）
    normalized_content = _normalize_whitespace(content)
    normalized_old = _normalize_whitespace(old_text)

    if normalized_old not in normalized_content:
        return False, content, f"Error: 未找到要替换的文本（即使忽略空白字符差异也无法匹配）: {old_text[:50]}..."

    # 3. 使用正则表达式进行模糊替换
    pattern_parts = []
    last_pos = 0
    for match in re.finditer(r"\s+", old_text):
        pattern_parts.append(re.escape(old_text[last_pos : match.start()]))
        pattern_parts.append(r"\s+")
        last_pos = match.end()
    pattern_parts.append(re.escape(old_text[last_pos:]))

    pattern = "".join(pattern_parts)

    try:
        new_content = re.sub(pattern, new_text, content, count=replace_count, flags=re.MULTILINE)
        return True, new_content, ""
    except Exception as e:
        return False, content, f"Error: 模糊替换失败: {e}"


# ==========================================

BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".svg",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".mkv",
        ".flac",
        ".wav",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".7z",
        ".rar",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".dat",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".pyc",
        ".pyo",
        ".class",
        ".o",
        ".a",
    }
)

MAX_MATCHES = 200
MAX_FILE_SIZE = 2 * 1024 * 1024

# ==========================================
# 文件读取去重缓存 (Read Dedup Cache)
# ==========================================

_READ_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_MAX_SIZE = 50
_CACHE_TTL = 300


def _get_file_mtime(file_path: Path) -> float:
    try:
        return file_path.stat().st_mtime
    except OSError:
        return 0.0


def _make_cache_key(resolved_path: Path, start_line, end_line) -> str:
    s = start_line or 0
    e = end_line or 0
    return f"{resolved_path}:{s}:{e}"


def _invalidate_read_cache(file_path: Path):
    prefix = f"{file_path}:"
    keys_to_remove = [k for k in _READ_CACHE if k.startswith(prefix)]
    for k in keys_to_remove:
        del _READ_CACHE[k]
    if keys_to_remove:
        logger.debug(f"[Scriptor] 已清除 {file_path.name} 的读取缓存（{len(keys_to_remove)} 条）")


def _read_cache_get(cache_key: str, resolved_path: Path) -> str | None:
    if cache_key not in _READ_CACHE:
        return None
    cached_content, cached_mtime = _READ_CACHE[cache_key]
    current_mtime = _get_file_mtime(resolved_path)
    if current_mtime != cached_mtime:
        del _READ_CACHE[cache_key]
        logger.debug(f"[Scriptor] 缓存失效：{resolved_path.name} 文件已变更")
        return None
    logger.info(f"[Scriptor] 命中读取缓存：{resolved_path.name}")
    return cached_content


def _read_cache_put(cache_key: str, content: str, resolved_path: Path):
    if len(_READ_CACHE) >= _CACHE_MAX_SIZE:
        oldest_key = next(iter(_READ_CACHE))
        del _READ_CACHE[oldest_key]
    _READ_CACHE[cache_key] = (content, _get_file_mtime(resolved_path))


# ==========================================
# VFS (Virtual File System) 路径解析
# ==========================================
def _is_vfs_path(file_path: str) -> bool:
    """
    检测路径是否为 VFS 虚拟路径

    VFS 路径特征：
    - 以 @personal/, @group/, @root/ 开头
    - 或者是特殊标记（., 空字符串, /, \\）
    """
    if not file_path:
        return True

    normalized = file_path.lstrip("/").lstrip("\\")

    if normalized.startswith(VFS_NAMESPACE_PERSONAL):
        return True
    if normalized.startswith(VFS_NAMESPACE_GROUP):
        return True
    if normalized.startswith(VFS_NAMESPACE_ROOT):
        return True

    if file_path in (".", "", "/", "\\"):
        return True

    return False


def _get_vfs_namespaces(event: "AstrMessageEvent", plugin: Optional[Any] = None) -> dict:
    """
    获取当前上下文的 VFS 命名空间映射

    Returns:
        dict: {
            "@personal/": Path("/path/to/profiles/{uid}/"),
            "@group/": Path("/path/to/groups/{group_id}/") or None,
            "@root/": Path("/path/to/data/"),
        }
    """
    namespaces = {}

    try:
        base_dir = _get_base_dir(event, plugin)
        data_dir = base_dir.parent.parent if base_dir.parent.name in ("profiles", "groups") else base_dir.parent

        namespaces[VFS_NAMESPACE_ROOT] = data_dir

        uid = _get_logical_uid(event, plugin)
        namespaces[VFS_NAMESPACE_PERSONAL] = data_dir / "profiles" / uid

        group_id = _normalize_group_id(event)
        if group_id != "private":
            namespaces[VFS_NAMESPACE_GROUP] = data_dir / "groups" / group_id
        else:
            namespaces[VFS_NAMESPACE_GROUP] = None

    except Exception as e:
        logger.warning(f"[VFS] 获取命名空间失败: {e}")

    return namespaces


async def _resolve_vfs_path(
    file_path: str,
    event: "AstrMessageEvent",
    plugin: Optional[Any] = None,
    check_permission: bool = True,
) -> Tuple[Path, bool, Optional[str]]:
    """
    解析 VFS 虚拟路径为物理路径

    Args:
        file_path: VFS 路径（如 @personal/P_PROFILE.md）
        event: 消息事件对象
        plugin: 插件实例
        check_permission: 是否检查权限

    Returns:
        (resolved_path, is_virtual, error_message)
        - resolved_path: 解析后的物理路径
        - is_virtual: 是否为虚拟路径
        - error_message: 错误信息（如果有）
    """
    if not _is_vfs_path(file_path):
        return Path(file_path), False, None

    namespaces = _get_vfs_namespaces(event, plugin)

    normalized = file_path.lstrip("/").lstrip("\\")

    if normalized.startswith(VFS_NAMESPACE_PERSONAL):
        relative_path = normalized[len(VFS_NAMESPACE_PERSONAL) :]
        personal_dir = namespaces.get(VFS_NAMESPACE_PERSONAL)
        if not personal_dir:
            return Path(file_path), True, "Error: 无法确定个人目录"
        resolved = personal_dir / relative_path
        return resolved, True, None

    if normalized.startswith(VFS_NAMESPACE_GROUP):
        relative_path = normalized[len(VFS_NAMESPACE_GROUP) :]
        group_dir = namespaces.get(VFS_NAMESPACE_GROUP)
        if not group_dir:
            return Path(file_path), True, "Error: 当前不在群聊环境中，无法访问 @group/ 目录"
        resolved = group_dir / relative_path
        return resolved, True, None

    if normalized.startswith(VFS_NAMESPACE_ROOT):
        relative_path = normalized[len(VFS_NAMESPACE_ROOT) :]
        root_dir = namespaces.get(VFS_NAMESPACE_ROOT)
        if not root_dir:
            return Path(file_path), True, "Error: 无法确定系统根目录"

        if check_permission:
            if not plugin:
                return Path(file_path), True, "Error: 访问 @root/ 目录需要插件实例"
            uid = _get_logical_uid(event, plugin)
            is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)
            if not is_sudo:
                return Path(file_path), True, "Error: 只有处于管理员模式（Sudo）的管理员可以访问 @root/ 目录"

        resolved = root_dir / relative_path
        return resolved, True, None

    if file_path in (".", "", "/", "\\"):
        base_dir = _get_base_dir(event, plugin)
        return base_dir, True, None

    return Path(file_path), False, None


# ==========================================


def _normalize_group_id(event: "AstrMessageEvent") -> str:
    """规范化 group_id

    AstrBot 的 get_group_id() 在私聊时返回空字符串 ""，而不是 "private"。
    为保持一致性，我们将其规范化为 "private"。

    对于群聊，返回格式为 "{platform}_group_{raw_group_id}"，
    与 main.py 中 _get_identity 的逻辑保持一致。
    """
    raw_group_id = event.get_group_id()
    if not raw_group_id:
        return "private"

    platform = _get_platform(event)
    return f"{platform}_group_{raw_group_id}"


def _get_platform(event: "AstrMessageEvent") -> str:
    """获取平台名称"""
    umo = getattr(event, "unified_msg_origin", None)
    if umo:
        return umo.split(":")[0] if ":" in umo else umo
    return "unknown"


def _get_base_dir(event: "AstrMessageEvent", plugin: Optional[Any] = None) -> Path:
    """获取用户或群组的根目录基础路径

    文件操作工具的工作目录：
    - 私聊：profiles/{uid}/
    - 群聊：groups/{group_id}/  (group_id 格式为 "{platform}_group_{raw_group_id}")

    特殊路径支持（只读）：
    - skills/{skill_name}/: 技能手册目录

    重要：
    - 私聊场景以用户为中心，使用逻辑 UID
    - 群聊场景以群组为中心，使用规范化后的 group_id
    """
    from ...tools.security.sanitizer import sanitize_id

    sender_id = str(event.get_sender_id())
    platform = _get_platform(event)
    group_id = _normalize_group_id(event)

    uid = None
    data_dir = None

    if StarTools:
        try:
            data_dir = StarTools.get_data_dir("astrbot_plugin_scriptor")
        except Exception as e:
            logger.warning(f"[Scriptor] StarTools.get_data_dir() 失败: {e}")

    if not plugin:
        plugin = _get_plugin_for_sync(event)
    if not plugin:
        for attr_name in ("_plugin", "plugin"):
            plugin = getattr(event, attr_name, None)
            if plugin is not None:
                break

    if not plugin and hasattr(event, "context"):
        ctx = getattr(event, "context", None)
        if ctx:
            plugin = getattr(ctx, "_plugin", None) or getattr(ctx, "plugin", None)

    if not plugin:
        bot = getattr(event, "bot", None)
        if bot:
            try:
                plugins_attr = getattr(bot, "_plugins", None)
                if isinstance(plugins_attr, dict):
                    plugin = plugins_attr.get("astrbot_plugin_scriptor")
                elif callable(plugins_attr) and not isinstance(plugins_attr, dict):
                    logger.debug("[Scriptor] bot._plugins 是 callable，尝试其他方式")
            except (AttributeError, TypeError) as e:
                logger.debug(f"[Scriptor] 获取 bot._plugins 时出错: {e}")

    if not plugin and hasattr(event, "session"):
        session = getattr(event, "session", None)
        if session:
            plugin = getattr(session, "_plugin", None) or getattr(session, "plugin", None)

    if plugin and hasattr(plugin, "identity_manager"):
        uid = plugin.identity_manager.get_or_create_uid(sender_id, platform)
        if group_id == "private":
            base_dir = Path(plugin.data_dir) / "profiles" / uid
        else:
            base_dir = Path(plugin.data_dir) / "groups" / group_id
    elif data_dir:
        if not plugin:
            plugin = _get_plugin_for_sync(event)
        if plugin and hasattr(plugin, "identity_manager"):
            uid = plugin.identity_manager.get_or_create_uid(sender_id, platform)
        else:
            uid = sanitize_id(sender_id)
            logger.warning("[Scriptor] 无法获取 IdentityManager，使用物理ID作为后备")
        if group_id == "private":
            base_dir = data_dir / "profiles" / uid
        else:
            base_dir = data_dir / "groups" / group_id
    else:
        raise RuntimeError("无法获取插件数据目录，请联系管理员检查 AstrBot 版本兼容性")

    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


def _resolve_file_path(
    base_dir: Path, file_path: str, allow_skills: bool = False, plugin: Optional[Any] = None
) -> Path:
    """解析文件路径（增强版：支持 skills/profiles/groups 目录）

    Args:
        base_dir: 基础目录 (profiles/uid 或 groups/gid)
        file_path: 文件路径（支持以下前缀）
            - skills/{name}/: 技能手册目录（只读）
            - profiles/{uid}/: 访问其他用户的个人目录
            - groups/{group_id}/: 访问其他群组目录
        allow_skills: 是否允许访问 skills 目录（默认 False）
        plugin: 插件实例（用于获取 data_dir）

    Returns:
        解析后的绝对路径
    """
    path = Path(file_path).expanduser()

    if path.is_absolute():
        return path

    if allow_skills and file_path.startswith("skills/"):
        skills_base = base_dir.parent / "skills"
        return skills_base / file_path.replace("skills/", "", 1)

    if file_path.startswith("profiles/"):
        profiles_base = base_dir.parent / "profiles"
        return profiles_base / file_path.replace("profiles/", "", 1)

    if file_path.startswith("groups/"):
        groups_base = base_dir.parent / "groups"
        return groups_base / file_path.replace("groups/", "", 1)

    return base_dir / file_path


def _is_text_file(path: Path) -> bool:
    """检查是否为文本文件"""
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return False
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return False
    except OSError:
        return False
    return True


def _read_file_safe(file_path: Path) -> str:
    """安全读取文件"""
    try:
        return file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return file_path.read_text(encoding="gbk", errors="ignore")


def _truncate_output(content: str, max_chars: int = 3000) -> str:
    """截断输出"""
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"\n\n[... 内容过长，已截断，剩余 {len(content) - max_chars} 字符 ...]"


def _get_plugin_for_sync(event: "AstrMessageEvent"):
    """获取插件实例用于向量数据库同步"""
    plugin = None
    for attr_name in ("_plugin", "plugin"):
        plugin = getattr(event, attr_name, None)
        if plugin is not None:
            break

    if not plugin and hasattr(event, "context"):
        ctx = getattr(event, "context", None)
        if ctx:
            plugin = getattr(ctx, "_plugin", None) or getattr(ctx, "plugin", None)

    if not plugin:
        bot = getattr(event, "bot", None)
        if bot:
            try:
                plugins_attr = getattr(bot, "_plugins", None)
                if isinstance(plugins_attr, dict):
                    plugin = plugins_attr.get("astrbot_plugin_scriptor")
                elif callable(plugins_attr) and not isinstance(plugins_attr, dict):
                    logger.debug("[Scriptor] bot._plugins 是 callable，尝试其他方式")
            except (AttributeError, TypeError) as e:
                logger.debug(f"[Scriptor] 获取 bot._plugins 时出错: {e}")

    if not plugin and hasattr(event, "session"):
        session = getattr(event, "session", None)
        if session:
            plugin = getattr(session, "_plugin", None) or getattr(session, "plugin", None)

    return plugin


def _get_logical_uid(event: "AstrMessageEvent", plugin: Optional[Any] = None) -> str:
    """获取事件的逻辑 UID（始终使用 IdentityManager）

    这是核心函数，确保所有文件操作都使用逻辑 UID 而不是物理 ID。
    """
    from ...tools.security.sanitizer import sanitize_id

    sender_id = str(event.get_sender_id())
    platform = _get_platform(event)

    if not plugin:
        plugin = _get_plugin_for_sync(event)

    if plugin and hasattr(plugin, "identity_manager"):
        return plugin.identity_manager.get_or_create_uid(sender_id, platform)

    logger.warning("[Scriptor] 无法获取 IdentityManager，降级使用物理ID")
    return sanitize_id(sender_id)


async def file_read(
    event: "AstrMessageEvent",
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    show_line_numbers: bool = False,
    plugin: Optional[Any] = None,
) -> str:
    """读取工作文件（增强版：支持 VFS 虚拟路径和 skills 目录只读访问）

    Args:
        event: 消息事件对象
        file_path: 文件路径，支持：
            - VFS 路径：@personal/P_PROFILE.md, @group/G_MEMORY.md, @root/config.yaml
            - 物理路径：profiles/{uid}/, groups/{group_id}/, skills/{name}/
        start_line: 起始行号（1-based，可选）
        end_line: 结束行号（1-based，可选）
        show_line_numbers: 是否在每行前显示行号（默认 False）
        plugin: 可选的插件实例（用于直接访问 IdentityManager）

    Returns:
        文件内容或错误信息
    """
    # VFS 路径解析
    is_skills_path = False
    if _is_vfs_path(file_path):
        resolved_path, is_virtual, vfs_error = await _resolve_vfs_path(file_path, event, plugin, check_permission=True)
        if vfs_error:
            return vfs_error
        if is_virtual:
            logger.debug(f"[VFS] 解析虚拟路径: {file_path} -> {resolved_path}")
    else:
        try:
            is_skills_path = file_path.startswith("skills/")
            base_dir = _get_base_dir(event, plugin)
        except RuntimeError as e:
            return f"Error: {e}"

        resolved_path = _resolve_file_path(base_dir, file_path, allow_skills=True)

        if not str(resolved_path).startswith(str(base_dir)):
            data_dir = base_dir.parent.parent if base_dir.parent.name in ("profiles", "groups") else base_dir.parent
            if not str(resolved_path).startswith(str(data_dir)):
                return "Error: 路径不允许访问数据目录外的内容（安全限制）"

            if file_path.startswith("profiles/"):
                if not plugin:
                    return "Error: 跨目录访问需要插件实例"
                uid = _get_logical_uid(event, plugin)
                target_uid = file_path.split("/")[1] if "/" in file_path else ""
                if target_uid and target_uid != uid:
                    is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)
                    if not is_sudo:
                        return f"Error: 只有管理员可以访问其他用户的个人目录（当前用户: {uid}，目标用户: {target_uid}）"

            if file_path.startswith("groups/"):
                if not plugin:
                    return "Error: 跨目录访问需要插件实例"
                uid = _get_logical_uid(event, plugin)
                target_group = file_path.split("/")[1] if "/" in file_path else ""
                current_group_id = _normalize_group_id(event)
                if target_group and target_group != current_group_id:
                    is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)
                    if is_sudo:
                        pass
                    elif hasattr(plugin, "group_manager"):
                        group = plugin.group_manager.get_group(target_group)
                        if group:
                            member_uids = [m.uid for m in group.members]
                            if uid not in member_uids:
                                return f"Error: 你不是群组 {target_group} 的成员，无法访问该群组目录"
                        else:
                            return f"Error: 群组 {target_group} 不存在或未注册"
                    else:
                        return f"Error: 只有管理员或群组成员可以访问其他群组的目录"

    if not resolved_path.exists():
        return f"Error: 文件不存在: {resolved_path}"

    if not resolved_path.is_file():
        return f"Error: 路径不是文件: {resolved_path}"

    # Skills 目录安全校验：确保是只读访问
    if is_skills_path and not str(resolved_path).endswith(".md"):
        return "Error: 技能目录仅允许读取 Markdown (.md) 文件"

    cache_key = _make_cache_key(resolved_path, start_line, end_line)
    cached_result = _read_cache_get(cache_key, resolved_path)
    if cached_result is not None:
        return cached_result

    try:
        content = _read_file_safe(resolved_path)
        lines = content.split("\n")
        total = len(lines)

        if start_line is not None and end_line is not None:
            s = max(1, start_line)
            e = min(total, end_line)
            if s > total:
                return f"Error: 起始行 {s} 超出文件总行数 {total}"
            if s > e:
                return f"Error: 起始行 {s} 大于结束行 {e}"
            selected_lines = lines[s - 1 : e]
            if show_line_numbers:
                max_line_num_width = len(str(e))
                selected = "\n".join(
                    f"{i:>{max_line_num_width}}→{line}" for i, line in zip(range(s, e + 1), selected_lines)
                )
            else:
                selected = "\n".join(selected_lines)
            remaining = total - e
            result = f"文件: {resolved_path.name} (行 {s}-{e}，共 {total} 行)\n{selected}"
            if remaining > 0:
                result += f"\n\n[还有 {remaining} 行未显示，使用 start_line={e + 1} 继续读取]"
            _read_cache_put(cache_key, result, resolved_path)
            return result
        else:
            if show_line_numbers:
                max_line_num_width = len(str(total))
                numbered_content = "\n".join(f"{i:>{max_line_num_width}}→{line}" for i, line in enumerate(lines, 1))
                truncated = _truncate_output(numbered_content, max_chars=4000)
            else:
                truncated = _truncate_output(content)
            if (show_line_numbers and len(numbered_content) > 4000) or (not show_line_numbers and truncated != content):
                truncated += f"\n\n[文件共 {total} 行，已截断显示]"
            result = f"文件: {resolved_path.name} (共 {total} 行)\n\n{truncated}"
            _read_cache_put(cache_key, result, resolved_path)
            return result

    except Exception as e:
        return f"Error: 读取文件失败: {e}"


def _check_file_permission(event: "AstrMessageEvent", file_path: Path, plugin: Optional[Any] = None) -> Optional[str]:
    """检查文件操作权限

    规则：
    1. 全局目录 (global/):
       - Global_SOUL.md, Global_MEMORY.md, Global_HEARTBEAT.md: 仅 Sudo 模式下的管理员可修改
    2. Personal_SOUL.md:
       - 私聊: 用户可以修改自己的 Personal_SOUL.md
    3. Group_SOUL.md:
       - 群聊: 仅超级管理员可修改群组 Group_SOUL.md
    4. Group_PROFILE.md:
       - 私聊: 不存在（私聊无 Group_PROFILE）
       - 群聊: 仅超级管理员或群组管理员可修改
    5. 其他文件: 所有人可操作（受 base_dir 限制）
    """
    if not plugin:
        plugin = _get_plugin_for_sync(event)
    if not plugin:
        return None

    uid = _get_logical_uid(event, plugin)
    group_id = _normalize_group_id(event)

    is_super = plugin.identity_manager.is_super_admin(uid, plugin.config.admin_uids)
    is_group_admin = plugin.identity_manager.is_group_admin(uid, group_id)
    is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)

    filename = file_path.name.upper()
    file_path_str = str(file_path)

    if "global" in file_path_str:
        global_protected_files = ["SOUL.MD", "MEMORY.MD", "HEARTBEAT.MD"]
        if filename in global_protected_files:
            if not is_sudo:
                return "Error: 只有处于管理员模式（Sudo）的管理员可以修改全局文件。"
            return None
        if not is_sudo:
            return "Error: 只有处于管理员模式（Sudo）的管理员可以修改全局目录中的文件。"
        return None

    personal_soul_files = ["P_SOUL.MD", "SOUL.MD"]
    if filename in personal_soul_files:
        if group_id == "private":
            if f"profiles/{uid}" in str(file_path):
                return None
        if not is_super:
            return "Error: 只有超级管理员可以修改群组核心准则。"
        return None

    group_soul_files = ["G_SOUL.MD"]
    if filename in group_soul_files:
        if group_id == "private":
            return "Error: 私聊环境下不存在群组核心人格文件。"
        if not is_super:
            return "Error: 只有超级管理员可以修改群组核心准则 (G_SOUL.md)。"
        return None

    group_profile_files = ["G_PROFILE.MD", "GROUP_PROFILE.MD"]
    if filename in group_profile_files:
        if group_id == "private":
            return "Error: 私聊环境下不存在群组画像文件。"
        if not (is_super or is_group_admin):
            return "Error: 只有管理员可以修改群组公共身份 (G_PROFILE.md)。"
        return None

    return None


async def file_write(
    event: "AstrMessageEvent",
    file_path: str,
    content: str,
    plugin: Optional[Any] = None,
) -> str:
    """创建或覆盖工作文件（支持 VFS 虚拟路径）

    Args:
        event: 消息事件对象
        file_path: 文件路径，支持：
            - VFS 路径：@personal/P_PROFILE.md, @group/G_MEMORY.md, @root/config.yaml
            - 物理路径：profiles/{uid}/, groups/{group_id}/, global/
        content: 文件内容
        plugin: 可选的插件实例

    Returns:
        操作结果
    """
    if not plugin:
        plugin = _get_plugin_for_sync(event)

    # ================= 新增：只读目录拦截器 =================
    readonly_error = _check_read_only_directory(file_path)
    if readonly_error:
        logger.warning(f"[Scriptor] file_write 拦截了对 {file_path} 的只读目录访问。")
        return readonly_error
    # =======================================================

    # VFS 路径解析
    is_global_path = False
    if _is_vfs_path(file_path):
        resolved_path, is_virtual, vfs_error = await _resolve_vfs_path(file_path, event, plugin, check_permission=True)
        if vfs_error:
            return vfs_error
        if is_virtual:
            logger.debug(f"[VFS] 写入虚拟路径: {file_path} -> {resolved_path}")
    else:
        # 检查是否访问全局目录
        is_global_path = file_path.startswith("global/") or file_path.startswith("/global/")

        if is_global_path:
            # 全局目录访问
            if not plugin:
                return "Error: 无法访问全局目录，插件实例不可用"

            uid = _get_logical_uid(event, plugin)
            is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)

            if not is_sudo:
                return "Error: 只有处于管理员模式（Sudo）的管理员可以访问全局目录。"

            data_dir = Path(plugin.data_dir)
            resolved_path = data_dir / file_path.lstrip("/")
        else:
            try:
                base_dir = _get_base_dir(event, plugin)
            except RuntimeError as e:
                return f"Error: {e}"

            resolved_path = _resolve_file_path(base_dir, file_path)

            if not str(resolved_path).startswith(str(base_dir)):
                data_dir = base_dir.parent.parent if base_dir.parent.name in ("profiles", "groups") else base_dir.parent
                if not str(resolved_path).startswith(str(data_dir)):
                    return "Error: 路径不允许访问数据目录外的内容（安全限制）"

                if file_path.startswith("profiles/"):
                    if not plugin:
                        return "Error: 跨目录访问需要插件实例"
                    uid = _get_logical_uid(event, plugin)
                    target_uid = file_path.split("/")[1] if "/" in file_path else ""
                    if target_uid and target_uid != uid:
                        is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)
                        if not is_sudo:
                            return f"Error: 只有管理员可以访问其他用户的个人目录（当前用户: {uid}，目标用户: {target_uid}）"

                if file_path.startswith("groups/"):
                    if not plugin:
                        return "Error: 跨目录访问需要插件实例"
                    uid = _get_logical_uid(event, plugin)
                    target_group = file_path.split("/")[1] if "/" in file_path else ""
                    current_group_id = _normalize_group_id(event)
                    if target_group and target_group != current_group_id:
                        is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)
                        if is_sudo:
                            pass
                        elif hasattr(plugin, "group_manager"):
                            group = plugin.group_manager.get_group(target_group)
                            if group:
                                member_uids = [m.uid for m in group.members]
                                if uid not in member_uids:
                                    return f"Error: 你不是群组 {target_group} 的成员，无法访问该群组目录"
                            else:
                                return f"Error: 群组 {target_group} 不存在或未注册"
                        else:
                            return f"Error: 只有管理员或群组成员可以访问其他群组的目录"

    # 权限检查
    perm_error = _check_file_permission(event, resolved_path, plugin)
    if perm_error:
        return perm_error

    if not resolved_path.parent.exists():
        return f"❌ 错误：父目录 `{resolved_path.parent.name}` 不存在\n\n请先用 file_list_tool 查看可用目录和文件列表。"

    # ================= 新增：防缩水拦截器 =================
    if resolved_path.exists():
        try:
            original_content = _read_file_safe(resolved_path)
            is_valid, shrinkage_error = _check_content_shrinkage(original_content, content, resolved_path.name)
            if not is_valid:
                logger.warning(f"[Scriptor] file_write 拦截了对 {resolved_path.name} 的缩水覆写。")
                return shrinkage_error
        except Exception as e:
            logger.debug(f"[Scriptor] 读取原始内容进行缩水检测失败: {e}")
    # =======================================================

    # 结构校验拦截器
    is_valid, error_msg = _validate_file_structure(str(resolved_path), content)
    if not is_valid:
        logger.warning(f"[Scriptor-Debug] file_write 拦截了对 {resolved_path.name} 的破坏性写入。")
        return error_msg

    try:
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path.write_text(content, encoding="utf-8")
        logger.info(f"[Scriptor] AI 写入工作文件: {resolved_path}")
        _invalidate_read_cache(resolved_path)

        if is_global_path and plugin:
            uid = _get_logical_uid(event, plugin)
            plugin.identity_manager.record_sudo_operation(uid, "file_write", f"写入 {resolved_path.name}")

        if not plugin:
            plugin = _get_plugin_for_sync(event)
        if plugin and hasattr(plugin, "search_engine") and plugin.search_engine:
            uid = _get_logical_uid(event, plugin)
            group_id = _normalize_group_id(event)
            resolved_name = resolved_path.name
            resolved_parent = (
                resolved_path.parent.name
                if resolved_path.parent != (plugin.data_dir if is_global_path else _get_base_dir(event, plugin))
                else ""
            )
            source_path = (
                f"md_files/{resolved_parent}/{resolved_name}" if resolved_parent else f"md_files/{resolved_name}"
            )

            scope = "global" if is_global_path else ("personal" if group_id == "private" else "group")
            if is_global_path:
                doc_id = f"mdfile_global_{resolved_name}"
            elif group_id == "private":
                doc_id = f"mdfile_private_{uid}_{resolved_name}"
            else:
                doc_id = f"mdfile_group_{group_id}_{resolved_name}"
            metadata = {
                "uid": uid,
                "group_id": group_id,
                "scope": scope,
                "source": source_path,
                "source_type": "md_files",
                "date": time.strftime("%Y-%m-%d"),
            }
            import asyncio

            asyncio.create_task(plugin.search_engine.add_to_vector_db(doc_id, content, metadata))

        return f"✓ 已写入 {len(content)} 字符到 {resolved_path.name}"
    except Exception as e:
        return f"Error: 写入文件失败: {e}"


async def file_edit(
    event: "AstrMessageEvent",
    file_path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    plugin: Optional[Any] = None,
) -> str:
    """在文件中查找并替换文本

    Args:
        event: 消息事件对象
        file_path: 文件路径
        old_text: 要查找的文本（精确匹配）
        new_text: 替换后的文本
        replace_all: 是否全局替换所有匹配项（默认 False，仅替换第一个）
        plugin: 可选的插件实例（用于直接访问 IdentityManager）

    Returns:
        操作结果
    """
    if not plugin:
        plugin = _get_plugin_for_sync(event)

    # ================= 新增：只读目录拦截器 =================
    readonly_error = _check_read_only_directory(file_path)
    if readonly_error:
        logger.warning(f"[Scriptor] file_edit 拦截了对 {file_path} 的只读目录访问。")
        return readonly_error
    # =======================================================

    # VFS 路径解析
    is_global_path = False
    if _is_vfs_path(file_path):
        resolved_path, is_virtual, vfs_error = await _resolve_vfs_path(file_path, event, plugin, check_permission=True)
        if vfs_error:
            return vfs_error
        if is_virtual:
            logger.debug(f"[VFS] 编辑虚拟路径: {file_path} -> {resolved_path}")
    elif file_path.startswith("global/") or file_path.startswith("/global/"):
        is_global_path = True
        if not plugin:
            return "Error: 无法访问全局目录，插件实例不可用"

        uid = _get_logical_uid(event, plugin)
        is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)

        if not is_sudo:
            return "Error: 只有处于管理员模式（Sudo）的管理员可以访问全局目录。"

        data_dir = Path(plugin.data_dir)
        resolved_path = data_dir / file_path.lstrip("/")
    else:
        try:
            base_dir = _get_base_dir(event, plugin)
        except RuntimeError as e:
            return f"Error: {e}"

        resolved_path = _resolve_file_path(base_dir, file_path)

        if not str(resolved_path).startswith(str(base_dir)):
            return "Error: 路径不允许访问目录外的内容（安全限制）"

    if not resolved_path.exists():
        return f"Error: 文件不存在: {resolved_path}"

    if not resolved_path.is_file():
        return f"Error: 路径不是文件: {resolved_path}"

    # 权限检查
    perm_error = _check_file_permission(event, resolved_path, plugin)
    if perm_error:
        return perm_error

    if not resolved_path.parent.exists():
        return f"❌ 错误：父目录 `{resolved_path.parent.name}` 不存在\n\n请先用 file_list_tool 查看可用目录和文件列表。"

    try:
        content = _read_file_safe(resolved_path)

        # 使用模糊替换函数（支持空白字符容错）
        success, new_content, fuzzy_error = _fuzzy_replace(content, old_text, new_text, replace_all)
        if not success:
            return fuzzy_error

        # ================= 新增：防缩水拦截器 =================
        is_valid, shrinkage_error = _check_content_shrinkage(content, new_content, resolved_path.name)
        if not is_valid:
            logger.warning(f"[Scriptor] file_edit 拦截了对 {resolved_path.name} 的缩水编辑。")
            return shrinkage_error
        # =======================================================

        # 结构校验拦截器
        is_valid, error_msg = _validate_file_structure(str(resolved_path), new_content)
        if not is_valid:
            logger.warning(f"[Scriptor-Debug] file_edit 拦截了对 {resolved_path.name} 的破坏性替换。")
            return error_msg

        resolved_path.write_text(new_content, encoding="utf-8")
        logger.info(f"[Scriptor] AI 编辑工作文件: {resolved_path}")
        _invalidate_read_cache(resolved_path)

        # 记录 Sudo 操作
        if is_global_path and plugin:
            uid = _get_logical_uid(event, plugin)
            plugin.identity_manager.record_sudo_operation(uid, "file_edit", f"编辑 {resolved_path.name}")

        if not plugin:
            plugin = _get_plugin_for_sync(event)
        if plugin and hasattr(plugin, "search_engine") and plugin.search_engine:
            uid = _get_logical_uid(event, plugin)
            group_id = _normalize_group_id(event)
            resolved_name = resolved_path.name
            resolved_parent = (
                resolved_path.parent.name
                if resolved_path.parent != (plugin.data_dir if is_global_path else _get_base_dir(event, plugin))
                else ""
            )
            source_path = (
                f"md_files/{resolved_parent}/{resolved_name}" if resolved_parent else f"md_files/{resolved_name}"
            )

            scope = "global" if is_global_path else ("personal" if group_id == "private" else "group")
            if is_global_path:
                doc_id = f"mdfile_global_{resolved_name}"
            elif group_id == "private":
                doc_id = f"mdfile_private_{uid}_{resolved_name}"
            else:
                doc_id = f"mdfile_group_{group_id}_{resolved_name}"
            metadata = {
                "uid": uid,
                "group_id": group_id,
                "scope": scope,
                "source": source_path,
                "source_type": "md_files",
                "date": time.strftime("%Y-%m-%d"),
            }
            import asyncio

            asyncio.create_task(plugin.search_engine.add_to_vector_db(doc_id, new_content, metadata))

        count = new_content.count(new_text) - content.count(new_text)
        if count <= 0:
            count = 1
        return f"✓ 已在 {resolved_path.name} 中替换 {count} 处"
    except Exception as e:
        return f"Error: 编辑文件失败: {e}"


async def multi_edit(
    event: "AstrMessageEvent",
    file_path: str,
    edits: list[dict],
    plugin: Optional[Any] = None,
) -> str:
    """原子化的多次编辑操作（MultiEdit）

    所有编辑按顺序执行，任何一项失败则全部不生效。
    自动继承：只读目录拦截、防缩水检测、结构校验、模糊匹配容错。

    Args:
        event: 消息事件对象
        file_path: 文件路径
        edits: 编辑操作列表，每项包含：
            - old_string (str): 要查找的文本
            - new_string (str): 替换后的文本
            - replace_all (bool, optional): 是否全局替换，默认 False
        plugin: 可选的插件实例

    Returns:
        操作结果汇总
    """
    if not plugin:
        plugin = _get_plugin_for_sync(event)

    if not edits:
        return "Error: 编辑列表为空，至少需要一项编辑操作"

    for i, edit in enumerate(edits):
        if "old_string" not in edit or "new_string" not in edit:
            return f"Error: 第 {i+1} 项编辑缺少 old_string 或 new_string 字段"

    readonly_error = _check_read_only_directory(file_path)
    if readonly_error:
        logger.warning(f"[Scriptor] multi_edit 拦截了对 {file_path} 的只读目录访问。")
        return readonly_error

    # VFS 路径解析
    is_global_path = False
    if _is_vfs_path(file_path):
        resolved_path, is_virtual, vfs_error = await _resolve_vfs_path(file_path, event, plugin, check_permission=True)
        if vfs_error:
            return vfs_error
        if is_virtual:
            logger.debug(f"[VFS] 多重编辑虚拟路径: {file_path} -> {resolved_path}")
    elif file_path.startswith("global/") or file_path.startswith("/global/"):
        is_global_path = True
        if not plugin:
            return "Error: 无法访问全局目录，插件实例不可用"
        uid = _get_logical_uid(event, plugin)
        is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)
        if not is_sudo:
            return "Error: 只有处于管理员模式（Sudo）的管理员可以访问全局目录。"
        data_dir = Path(plugin.data_dir)
        resolved_path = data_dir / file_path.lstrip("/")
    else:
        try:
            base_dir = _get_base_dir(event, plugin)
        except RuntimeError as e:
            return f"Error: {e}"
        resolved_path = _resolve_file_path(base_dir, file_path)
        if not str(resolved_path).startswith(str(base_dir)):
            return "Error: 路径不允许访问目录外的内容（安全限制）"

    if not resolved_path.exists():
        return f"Error: 文件不存在: {resolved_path}"
    if not resolved_path.is_file():
        return f"Error: 路径不是文件: {resolved_path}"

    perm_error = _check_file_permission(event, resolved_path, plugin)
    if perm_error:
        return perm_error

    try:
        content = _read_file_safe(resolved_path)
        working_content = content
        success_count = 0
        errors = []

        for i, edit in enumerate(edits):
            old_str = edit["old_string"]
            new_str = edit["new_string"]
            replace_all = edit.get("replace_all", False)

            success, working_content, fuzzy_error = _fuzzy_replace(working_content, old_str, new_str, replace_all)
            if not success:
                errors.append(f"第 {i+1} 项失败：{fuzzy_error}")
            else:
                success_count += 1

        if errors:
            error_summary = "\n".join(errors)
            return (
                f"Error: MultiEdit 部分失败（{success_count}/{len(edits)} 成功），文件保持原样不变。\n\n"
                f"失败详情:\n{error_summary}\n\n"
                f"请修正失败的编辑项后重试。"
            )

        is_valid, shrinkage_error = _check_content_shrinkage(content, working_content, resolved_path.name)
        if not is_valid:
            logger.warning(f"[Scriptor] multi_edit 拦截了对 {resolved_path.name} 的缩水编辑。")
            return shrinkage_error

        is_valid, struct_error = _validate_file_structure(str(resolved_path), working_content)
        if not is_valid:
            logger.warning(f"[Scriptor-Debug] multi_edit 拦截了对 {resolved_path.name} 的破坏性替换。")
            return struct_error

        resolved_path.write_text(working_content, encoding="utf-8")
        logger.info(f"[Scriptor] AI MultiEdit 工作文件: {resolved_path} ({success_count}/{len(edits)} 项)")
        _invalidate_read_cache(resolved_path)

        if is_global_path and plugin:
            uid = _get_logical_uid(event, plugin)
            plugin.identity_manager.record_sudo_operation(uid, "multi_edit", f"多编辑 {resolved_path.name}")

        if not plugin:
            plugin = _get_plugin_for_sync(event)
        if plugin and hasattr(plugin, "search_engine") and plugin.search_engine:
            uid = _get_logical_uid(event, plugin)
            group_id = _normalize_group_id(event)
            resolved_name = resolved_path.name
            resolved_parent = (
                resolved_path.parent.name
                if resolved_path.parent != (plugin.data_dir if is_global_path else _get_base_dir(event, plugin))
                else ""
            )
            source_path = (
                f"md_files/{resolved_parent}/{resolved_name}" if resolved_parent else f"md_files/{resolved_name}"
            )
            scope = "global" if is_global_path else ("personal" if group_id == "private" else "group")
            if is_global_path:
                doc_id = f"mdfile_global_{resolved_name}"
            elif group_id == "private":
                doc_id = f"mdfile_private_{uid}_{resolved_name}"
            else:
                doc_id = f"mdfile_group_{group_id}_{resolved_name}"
            metadata = {
                "uid": uid,
                "group_id": group_id,
                "scope": scope,
                "source": source_path,
                "source_type": "md_files",
                "date": time.strftime("%Y-%m-%d"),
            }
            import asyncio

            asyncio.create_task(plugin.search_engine.add_to_vector_db(doc_id, working_content, metadata))

        return f"✓ MultiEdit 完成：{resolved_path.name} 中成功编辑 {success_count}/{len(edits)} 处"
    except Exception as e:
        return f"Error: MultiEdit 操作失败: {e}"


async def file_append(
    event: "AstrMessageEvent",
    file_path: str,
    content: str,
    plugin: Optional[Any] = None,
) -> str:
    """追加内容到文件末尾

    Args:
        event: 消息事件对象
        file_path: 文件路径
        content: 要追加的内容
        plugin: 可选的插件实例（用于直接访问 IdentityManager）

    Returns:
        操作结果
    """
    if not plugin:
        plugin = _get_plugin_for_sync(event)

    # VFS 路径解析
    is_global_path = False
    if _is_vfs_path(file_path):
        resolved_path, is_virtual, vfs_error = await _resolve_vfs_path(file_path, event, plugin, check_permission=True)
        if vfs_error:
            return vfs_error
        if is_virtual:
            logger.debug(f"[VFS] 追加虚拟路径: {file_path} -> {resolved_path}")
    elif file_path.startswith("global/") or file_path.startswith("/global/"):
        is_global_path = True
        if not plugin:
            return "Error: 无法访问全局目录，插件实例不可用"

        uid = _get_logical_uid(event, plugin)
        is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)

        if not is_sudo:
            return "Error: 只有处于管理员模式（Sudo）的管理员可以访问全局目录。"

        data_dir = Path(plugin.data_dir)
        resolved_path = data_dir / file_path.lstrip("/")
    else:
        try:
            base_dir = _get_base_dir(event, plugin)
        except RuntimeError as e:
            return f"Error: {e}"

        resolved_path = _resolve_file_path(base_dir, file_path)

        if not str(resolved_path).startswith(str(base_dir)):
            return "Error: 路径不允许访问目录外的内容（安全限制）"

    # 权限检查
    perm_error = _check_file_permission(event, resolved_path, plugin)
    if perm_error:
        return perm_error

    if not resolved_path.parent.exists():
        return f"❌ 错误：父目录 `{resolved_path.parent.name}` 不存在\n\n请先用 file_list_tool 查看可用目录和文件列表。"

    try:
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved_path, "a", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"[Scriptor] AI 追加内容到工作文件: {resolved_path}")
        _invalidate_read_cache(resolved_path)

        # 记录 Sudo 操作
        if is_global_path and plugin:
            uid = _get_logical_uid(event, plugin)
            plugin.identity_manager.record_sudo_operation(uid, "file_append", f"追加到 {resolved_path.name}")

        if not plugin:
            plugin = _get_plugin_for_sync(event)
        if plugin and hasattr(plugin, "search_engine") and plugin.search_engine:
            uid = _get_logical_uid(event, plugin)
            group_id = _normalize_group_id(event)
            resolved_name = resolved_path.name
            resolved_parent = (
                resolved_path.parent.name
                if resolved_path.parent != (plugin.data_dir if is_global_path else _get_base_dir(event, plugin))
                else ""
            )
            source_path = (
                f"md_files/{resolved_parent}/{resolved_name}" if resolved_parent else f"md_files/{resolved_name}"
            )

            scope = "global" if is_global_path else ("personal" if group_id == "private" else "group")
            if is_global_path:
                doc_id = f"mdfile_global_{resolved_name}"
            elif group_id == "private":
                doc_id = f"mdfile_private_{uid}_{resolved_name}"
            else:
                doc_id = f"mdfile_group_{group_id}_{resolved_name}"
            new_full_content = resolved_path.read_text(encoding="utf-8")
            metadata = {
                "uid": uid,
                "group_id": group_id,
                "scope": scope,
                "source": source_path,
                "source_type": "md_files",
                "date": time.strftime("%Y-%m-%d"),
            }
            import asyncio

            asyncio.create_task(plugin.search_engine.add_to_vector_db(doc_id, new_full_content, metadata))

        return f"✓ 已追加 {len(content)} 字符到 {resolved_path.name}"
    except Exception as e:
        return f"Error: 追加内容失败: {e}"


async def file_edit_by_line(
    event: "AstrMessageEvent",
    file_path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    plugin: Optional[Any] = None,
) -> str:
    """按行号编辑文件（精准替换指定行范围的内容）

    Args:
        event: 消息事件对象
        file_path: 文件路径
        start_line: 起始行号（1-based，包含）
        end_line: 结束行号（1-based，包含）
        new_content: 新的内容（将替换 start_line 到 end_line 之间的所有行）
        plugin: 可选的插件实例（用于直接访问 IdentityManager）

    Returns:
        操作结果
    """
    # ================= 新增：只读目录拦截器 =================
    readonly_error = _check_read_only_directory(file_path)
    if readonly_error:
        logger.warning(f"[Scriptor] file_edit_by_line 拦截了对 {file_path} 的只读目录访问。")
        return readonly_error
    # =======================================================

    # VFS 路径解析
    if _is_vfs_path(file_path):
        resolved_path, is_virtual, vfs_error = await _resolve_vfs_path(file_path, event, plugin, check_permission=True)
        if vfs_error:
            return vfs_error
        if is_virtual:
            logger.debug(f"[VFS] 按行编辑虚拟路径: {file_path} -> {resolved_path}")
    else:
        try:
            base_dir = _get_base_dir(event, plugin)
        except RuntimeError as e:
            return f"Error: {e}"

        resolved_path = _resolve_file_path(base_dir, file_path)

        if not str(resolved_path).startswith(str(base_dir)):
            return "Error: 路径不允许访问目录外的内容（安全限制）"

    # 权限检查
    perm_error = _check_file_permission(event, resolved_path, plugin)
    if perm_error:
        return perm_error

    if start_line < 1:
        return "Error: 起始行号必须 >= 1"
    if end_line < start_line:
        return f"Error: 结束行号 ({end_line}) 不能小于起始行号 ({start_line})"

    try:
        content = _read_file_safe(resolved_path)
        lines = content.split("\n")
        total_lines = len(lines)

        if start_line > total_lines:
            return f"Error: 起始行号 {start_line} 超出文件总行数 {total_lines}"

        actual_end = min(end_line, total_lines)

        # 构建新内容
        new_lines = lines[: start_line - 1] + [new_content] + lines[actual_end:]
        new_full_content = "\n".join(new_lines)

        # ================= 新增：防缩水拦截器 =================
        is_valid, shrinkage_error = _check_content_shrinkage(content, new_full_content, resolved_path.name)
        if not is_valid:
            logger.warning(f"[Scriptor] file_edit_by_line 拦截了对 {resolved_path.name} 的缩水编辑。")
            return shrinkage_error
        # =======================================================

        # 结构校验拦截器
        is_valid, error_msg = _validate_file_structure(str(resolved_path), new_full_content)
        if not is_valid:
            logger.warning(f"[Scriptor-Debug] file_edit_by_line 拦截了对 {resolved_path.name} 的破坏性替换。")
            return error_msg
        # ===================================================

        resolved_path.write_text(new_full_content, encoding="utf-8")
        logger.info(f"[Scriptor] AI 按行编辑文件: {resolved_path} (替换行 {start_line}-{actual_end})")
        _invalidate_read_cache(resolved_path)

        if not plugin:
            plugin = _get_plugin_for_sync(event)
        if plugin and hasattr(plugin, "search_engine") and plugin.search_engine:
            uid = _get_logical_uid(event, plugin)
            group_id = _normalize_group_id(event)
            resolved_name = resolved_path.name
            resolved_parent = resolved_path.parent.name if resolved_path.parent != base_dir else ""
            source_path = (
                f"md_files/{resolved_parent}/{resolved_name}" if resolved_parent else f"md_files/{resolved_name}"
            )
            if group_id == "private":
                doc_id = f"mdfile_private_{uid}_{resolved_name}"
            else:
                doc_id = f"mdfile_group_{group_id}_{resolved_name}"
            metadata = {
                "uid": uid,
                "group_id": group_id,
                "scope": "personal" if group_id == "private" else "group",
                "source": source_path,
                "source_type": "md_files",
                "date": time.strftime("%Y-%m-%d"),
            }
            import asyncio

            asyncio.create_task(plugin.search_engine.add_to_vector_db(doc_id, new_full_content, metadata))

        replaced_count = actual_end - start_line + 1
        return f"✓ 已替换 {resolved_path.name} 的第 {start_line}-{actual_end} 行（共 {replaced_count} 行）为 {len(new_content)} 字符的新内容"
    except Exception as e:
        return f"Error: 按行编辑文件失败: {e}"


async def file_grep(
    event: "AstrMessageEvent",
    pattern: str,
    path: Optional[str] = None,
    is_regex: bool = False,
    case_sensitive: bool = True,
    context_lines: int = 0,
    plugin: Optional[Any] = None,
) -> str:
    """在文件中搜索内容

    Args:
        event: 消息事件对象
        pattern: 搜索模式
        path: 文件或目录路径（默认搜索根目录）
        is_regex: 是否使用正则表达式
        case_sensitive: 是否区分大小写
        context_lines: 上下文行数
        plugin: 可选的插件实例（用于直接访问 IdentityManager）

    Returns:
        搜索结果
    """
    # VFS 路径解析
    if path and _is_vfs_path(path):
        search_root, is_virtual, vfs_error = await _resolve_vfs_path(path, event, plugin, check_permission=True)
        if vfs_error:
            return vfs_error
        if is_virtual:
            logger.debug(f"[VFS] 搜索虚拟路径: {path} -> {search_root}")
    else:
        try:
            base_dir = _get_base_dir(event, plugin)
        except RuntimeError as e:
            return f"Error: {e}"

        search_root = _resolve_file_path(base_dir, path) if path else base_dir

        if not str(search_root).startswith(str(base_dir)):
            return "Error: 路径不允许访问目录外的内容（安全限制）"

    if not search_root.exists():
        return f"Error: 路径不存在: {search_root}"

    try:
        flags = 0 if case_sensitive else re.IGNORECASE
        if is_regex:
            regex = re.compile(pattern, flags)
        else:
            regex = re.compile(re.escape(pattern), flags)
    except re.error as e:
        return f"Error: 无效的正则表达式: {e}"

    matches = []
    truncated = False

    single_file = search_root.is_file()
    if single_file:
        files_to_search = [search_root]
    else:
        files_to_search = [f for f in search_root.rglob("*") if f.is_file() and _is_text_file(f)]

    for file_path in files_to_search:
        if truncated:
            break
        try:
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue

        for line_no, line in enumerate(lines, start=1):
            if regex.search(line):
                if len(matches) >= MAX_MATCHES:
                    truncated = True
                    break

                start = max(0, line_no - 1 - context_lines)
                end = min(len(lines), line_no + context_lines)

                rel_path = file_path.relative_to(search_root) if search_root != file_path else file_path.name
                for ctx_idx in range(start, end):
                    prefix = ">" if ctx_idx == line_no - 1 else " "
                    matches.append(f"{rel_path}:{ctx_idx + 1}:{prefix} {lines[ctx_idx]}")
                if context_lines > 0:
                    matches.append("---")

    if not matches:
        return f"未找到匹配 '{pattern}' 的内容"

    result = "\n".join(matches)
    if truncated:
        result += f"\n\n[结果已截断，最多显示 {MAX_MATCHES} 条匹配]"
    return result


async def file_list(
    event: "AstrMessageEvent",
    pattern: str = "*",
    plugin: Optional[Any] = None,
) -> str:
    """列出工作目录中的文件

    Args:
        event: 消息事件对象
        pattern: 文件匹配模式（默认 "*"）
            支持 VFS 虚拟路径：
            - @personal/* - 列出个人目录的文件
            - @group/* - 列出当前群组目录的文件
            - @root/* - 列出全局目录的文件（需要管理员权限）
            支持跨目录访问：
            - profiles/{uid}/* - 列出指定用户的文件
            - groups/{group_id}/* - 列出指定群组的文件
        plugin: 可选的插件实例（用于直接访问 IdentityManager）

    Returns:
        文件列表
    """
    # VFS 路径解析
    if _is_vfs_path(pattern):
        resolved_path, is_virtual, vfs_error = await _resolve_vfs_path(pattern, event, plugin, check_permission=True)
        if vfs_error:
            return vfs_error
        if is_virtual:
            logger.debug(f"[VFS] 列出虚拟路径: {pattern} -> {resolved_path}")
            target_dir = resolved_path.parent if resolved_path.suffix else resolved_path
            list_pattern = resolved_path.name if resolved_path.suffix else "*"

            try:
                results = []
                for entry in sorted(target_dir.glob(list_pattern)):
                    suffix = "/" if entry.is_dir() else ""
                    rel_path = entry.relative_to(target_dir)
                    size = entry.stat().st_size if entry.is_file() else 0
                    results.append(f"{rel_path}{suffix} ({size} bytes)")

                if not results:
                    return f"目录为空（模式: {pattern}）"

                return f"文件列表:\n" + "\n".join(results)
            except Exception as e:
                return f"Error: 列出文件失败: {e}"

    try:
        base_dir = _get_base_dir(event, plugin)
    except RuntimeError as e:
        return f"Error: {e}"

    target_dir = base_dir
    list_pattern = pattern

    if pattern.startswith("profiles/") or pattern.startswith("groups/"):
        if not plugin:
            return "Error: 跨目录访问需要插件实例"

        data_dir = base_dir.parent.parent if base_dir.parent.name in ("profiles", "groups") else base_dir.parent
        parts = pattern.split("/", 2)
        target_type = parts[0]
        target_id = parts[1] if len(parts) > 1 else ""

        if target_type == "profiles":
            uid = _get_logical_uid(event, plugin)
            if target_id and target_id != uid:
                is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)
                if not is_sudo:
                    return f"Error: 只有管理员可以列出其他用户的个人目录（当前用户: {uid}，目标用户: {target_id}）"
            target_dir = data_dir / "profiles" / target_id
            list_pattern = "/".join(parts[2:]) if len(parts) > 2 else "*"

        elif target_type == "groups":
            uid = _get_logical_uid(event, plugin)
            current_group_id = _normalize_group_id(event)
            if target_id and target_id != current_group_id:
                is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)
                if is_sudo:
                    pass
                elif hasattr(plugin, "group_manager"):
                    group = plugin.group_manager.get_group(target_id)
                    if group:
                        member_uids = [m.uid for m in group.members]
                        if uid not in member_uids:
                            return f"Error: 你不是群组 {target_id} 的成员，无法列出该群组目录"
                    else:
                        return f"Error: 群组 {target_id} 不存在或未注册"
                else:
                    return f"Error: 只有管理员或群组成员可以列出其他群组的目录"
            target_dir = data_dir / "groups" / target_id
            list_pattern = "/".join(parts[2:]) if len(parts) > 2 else "*"

    try:
        results = []
        for entry in sorted(target_dir.glob(list_pattern)):
            suffix = "/" if entry.is_dir() else ""
            rel_path = entry.relative_to(target_dir)
            size = entry.stat().st_size if entry.is_file() else 0
            results.append(f"{rel_path}{suffix} ({size} bytes)")

        if not results:
            return f"目录为空（模式: {pattern}）"

        return f"文件列表:\n" + "\n".join(results)
    except Exception as e:
        return f"Error: 列出文件失败: {e}"


async def file_delete(
    event: "AstrMessageEvent",
    file_path: str,
    plugin: Optional[Any] = None,
    force: bool = False,
) -> str | dict:
    """
    删除工作文件（带安全防护和二次确认机制）。

    【⚠️ 高危操作】此工具用于删除文件，属于不可逆操作。

    Args:
        event: 消息事件对象
        file_path: 文件路径（如 "MEMORY.md"、"notes/old.md"）
            支持 VFS 虚拟路径：
            - @personal/file.md - 删除个人目录的文件
            - @group/file.md - 删除当前群组目录的文件
            - @root/file.md - 删除全局目录的文件（需要管理员权限）
        plugin: 可选的插件实例（用于访问配置和权限管理器）
        force: 是否强制执行（跳过确认检查，仅内部使用）

    Returns:
        操作结果：
        - 正常删除：返回成功消息字符串
        - 需要确认：返回字典 {"status": "pending_confirmation", ...} 供上层拦截处理
        - 错误：返回错误信息字符串
    """
    # 检查是否为只读目录
    readonly_check = _check_read_only_directory(file_path)
    if readonly_check:
        return readonly_check

    # VFS 路径解析
    if _is_vfs_path(file_path):
        resolved_path, is_virtual, vfs_error = await _resolve_vfs_path(file_path, event, plugin, check_permission=True)
        if vfs_error:
            return vfs_error
        if is_virtual:
            logger.debug(f"[VFS] 删除虚拟路径: {file_path} -> {resolved_path}")
    else:
        try:
            base_dir = _get_base_dir(event, plugin)
        except RuntimeError as e:
            return f"Error: {e}"

        resolved_path = _resolve_file_path(base_dir, file_path)

        if not str(resolved_path).startswith(str(base_dir)):
            return "Error: 不允许删除数据目录外的文件（安全限制）"

        # 跨目录权限检查
        if file_path.startswith("profiles/") or file_path.startswith("groups/"):
            if not plugin:
                return "Error: 跨目录删除需要插件实例"

            data_dir = base_dir.parent.parent if base_dir.parent.name in ("profiles", "groups") else base_dir.parent
            parts = file_path.split("/", 2)
            target_type = parts[0]
            target_id = parts[1] if len(parts) > 1 else ""

            uid = _get_logical_uid(event, plugin)

            if target_type == "profiles":
                if target_id and target_id != uid:
                    is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)
                    if not is_sudo:
                        return f"Error: 只有管理员可以删除其他用户的文件（当前用户: {uid}，目标用户: {target_id}）"

            elif target_type == "groups":
                current_group_id = _normalize_group_id(event)
                if target_id and target_id != current_group_id:
                    is_sudo = plugin.identity_manager.is_sudo(uid, plugin.config.admin_uids)
                    if not is_sudo and hasattr(plugin, "group_manager"):
                        group = plugin.group_manager.get_group(target_id)
                        if group:
                            member_uids = [m.uid for m in group.members]
                            if uid not in member_uids:
                                return f"Error: 你不是群组 {target_id} 的成员，无法删除该群组的文件"

    # 文件存在性检查
    if not resolved_path.exists():
        return f"Error: 文件不存在: {resolved_path}"

    if not resolved_path.is_file():
        return f"Error: 路径不是文件（不支持删除目录）: {resolved_path}"

    # 检查配置是否需要二次确认
    require_confirmation = getattr(plugin, "config", None) and getattr(
        plugin.config, "require_delete_confirmation", True
    )

    if require_confirmation and not force:
        from ...core.pending_tasks import PendingTaskType, get_pending_task_store

        store = get_pending_task_store()
        session_id = event.session_id if hasattr(event, "session_id") else str(id(event))

        store.add_task(session_id, PendingTaskType.FILE_DELETE, str(resolved_path))

        return {
            "status": "pending_confirmation",
            "message": "操作已挂起，等待用户通过 /delete 命令确认",
            "file_path": str(resolved_path),
            "session_id": session_id,
        }

    # 执行实际删除
    try:
        import os

        os.remove(resolved_path)

        logger.info(f"[file_delete] 文件已删除: {resolved_path} (会话: {event.session_id})")

        return f"✅ 文件已成功删除: {file_path}"

    except PermissionError:
        return f"Error: 权限不足，无法删除文件: {resolved_path}"
    except OSError as e:
        return f"Error: 删除文件失败: {e}"


async def file_send(
    event: "AstrMessageEvent",
    file_path: str,
    plugin: Optional[Any] = None,
) -> Any:
    """将 VFS 文件系统中的文件作为附件发送给用户。

    Args:
        event: AstrBot 消息事件
        file_path: VFS 路径（如 @personal/P_SOUL.md）或物理路径
        plugin: 可选的插件实例（用于 VFS 解析）

    Returns:
        发送结果或错误消息
    """
    from astrbot.api.message_components import File

    # VFS 路径解析
    if _is_vfs_path(file_path):
        resolved_path, is_virtual, vfs_error = await _resolve_vfs_path(file_path, event, plugin, check_permission=True)
        if vfs_error:
            return vfs_error
        if is_virtual:
            logger.debug(f"[VFS] 发送虚拟路径文件: {file_path} -> {resolved_path}")
    else:
        try:
            base_dir = _get_base_dir(event, plugin)
        except RuntimeError as e:
            return f"Error: {e}"

        resolved_path = _resolve_file_path(base_dir, file_path)

        if not str(resolved_path).startswith(str(base_dir)):
            return "Error: 路径不允许访问目录外的内容（安全限制）"

    if not resolved_path.exists():
        return f"Error: 文件不存在: {file_path}"

    if not resolved_path.is_file():
        return f"Error: 路径不是文件: {file_path}"

    try:
        file_component = File(file=str(resolved_path), name=resolved_path.name)
        await event.send_msg(message_chain=[file_component])

        logger.info(f"[file_send] 文件已发送: {resolved_path} (会话: {event.session_id})")

        return f"✅ 文件已发送: {resolved_path.name}"

    except Exception as e:
        logger.error(f"[file_send] 发送文件失败: {e}")
        return f"Error: 发送文件失败: {e}"
