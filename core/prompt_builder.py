# core/prompt_builder.py
"""Scriptor 提示词构建模块 - 渐进式披露模式

重构说明：
- 保留必须全量加载的核心文件（SOUL、权限、TODO、BOOTSTRAP）
- 将大体积文件（PROFILE、MEMORY、SOP、AGENTS、日记）改为目录索引模式
- 通过 ContextIndexer 生成精简的上下文目录树，AI 按需读取详细内容
- 大幅降低基础 Token 消耗，提升响应速度和性价比
"""

from pathlib import Path

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from tools.common.text_utils import SmartMemoryTrimmer

from .context_indexer import ContextIndexer


class PromptBuilder:
    """系统提示词构建器 - 构建完整的人格+记忆上下文（渐进式披露模式）"""

    def __init__(
        self,
        data_dir,
        config,
        identity_manager,
        group_manager,
        memory_manager,
        file_manager=None,
        archive_manager=None,
        knowledge_graph=None,
        learning_manager=None,
        archive_router=None,
    ):
        self.data_dir = data_dir
        self.config = config
        self.identity_manager = identity_manager
        self.group_manager = group_manager
        self.memory_manager = memory_manager
        self.file_manager = file_manager
        self.archive_manager = archive_manager
        self.knowledge_graph = knowledge_graph
        self.learning_manager = learning_manager
        self.archive_router = archive_router

        self.context_indexer = ContextIndexer(data_dir, config)

    def _get_global_dir(self) -> Path:
        """获取全局目录"""
        return self.data_dir / "global"

    def _load_global_template(self, template_name: str) -> str:
        """加载全局模板内容（使用新的短命名格式）"""
        global_file = self._get_global_dir() / template_name
        if global_file.exists():
            return global_file.read_text(encoding="utf-8")
        return ""

    def _extract_keywords(self, text: str) -> list:
        """轻量级关键词提取（基于正则，避免调用 LLM）"""
        import re

        stop_words = {
            "的",
            "了",
            "是",
            "在",
            "我",
            "你",
            "他",
            "她",
            "它",
            "我们",
            "你们",
            "他们",
            "和",
            "或",
            "但",
            "而",
            "也",
            "就",
            "都",
            "很",
            "太",
            "真",
            "最",
            "更",
            "这个",
            "那个",
            "什么",
            "怎么",
            "为什么",
            "如何",
            "哪里",
            "什么时候",
            "可以",
            "能",
            "会",
            "要",
            "想",
            "做",
            "去",
            "来",
            "有",
            "没有",
            "啊",
            "吧",
            "呢",
            "吗",
            "哦",
            "嗯",
            "呀",
            "啦",
        }

        words = re.findall(r"[\u4e00-\u9fa5]+|[a-zA-Z0-9]+", text)

        keywords = []
        for word in words:
            if len(word) >= 2 and word not in stop_words:
                keywords.append(word)

        return list(dict.fromkeys(keywords))[:10]

    def _build_graph_context(self, uid: str, user_message: str = "") -> str:
        """构建知识图谱上下文（带权重召回与严格去重）

        去重策略（阶段四优化）：
        - P_PROFILE.md 的核心关系（权重>=0.8）已作为 System Prompt 全量注入
        - 从 JSON 检索时，自动过滤掉已在 MD 中存在的关系
        - 最终效果：[MD核心设定] + [JSON边缘补充]，绝不重复
        """
        if not self.knowledge_graph:
            return ""

        try:
            # 步骤 1：提取 MD 中已有的核心关系指纹（用于去重）
            md_relation_fingerprints = self._get_md_graph_fingerprints(uid)

            graph_context_parts = []

            user_name = self.identity_manager.uid_metadata.get(uid, {}).get("primary_name", f"User_{uid[-4:]}")
            user_entity_key = f"{user_name}({uid})"

            keywords = [user_entity_key]
            if user_message:
                extracted = self._extract_keywords(user_message)
                keywords.extend(extracted)

            all_relations = []
            seen_relation_keys = set()

            for keyword in keywords:
                results = self.knowledge_graph.search(keyword, limit=self.config.graph_keyword_search_limit)
                for res in results:
                    for rel in res.get("relations", []):
                        rel_key = (rel["source"], rel["target"], rel["type"])

                        # ====== 核心去重逻辑 ======
                        # 如果该关系已在 MD 中存在，跳过（避免重复占用 Token）
                        if rel_key in md_relation_fingerprints:
                            continue

                        if rel_key not in seen_relation_keys:
                            seen_relation_keys.add(rel_key)
                            all_relations.append(rel)

            all_relations.sort(key=lambda x: x.get("weight", 1), reverse=True)

            deduped_count = len(all_relations)
            filtered_count = len(seen_relation_keys) - deduped_count if seen_relation_keys else 0

            for rel in all_relations[: self.config.graph_recall_limit]:
                rel_str = f"{rel['source']} --({rel['type']})--> {rel['target']}"
                if rel.get("weight", 1) > 1:
                    rel_str += f" [强度:{rel['weight']}]"
                graph_context_parts.append(f"- {rel_str}")

            if graph_context_parts:
                context = (
                    "\n\n---\n\n## 🧠 知识图谱关联 (Knowledge Graph)\n"
                    + "以下是与当前对话相关的背景知识（已自动过滤重复项）：\n"
                    + "\n".join(graph_context_parts)
                )

                if filtered_count > 0:
                    logger.info(
                        f"[PromptBuilder] 图谱上下文去重完成: "
                        f"保留 {deduped_count} 条, 过滤 {filtered_count} 条重复项"
                    )

                return context

        except Exception as e:
            logger.debug(f"[PromptBuilder] 构建图谱上下文失败: {e}")

        return ""

    def _get_md_graph_fingerprints(self, uid: str) -> set:
        """
        提取 Markdown 文件中已有的核心关系指纹集合

        用于在 JSON 检索时进行去重，避免同一关系被重复注入上下文

        Args:
            uid: 用户 ID

        Returns:
            集合 of (source, target, relation_type) 元组
        """
        fingerprints = set()

        try:
            profile_dir = self.data_dir / "profiles" / uid
            profile_file = profile_dir / "P_PROFILE.md"

            if not profile_file.exists() or not self.knowledge_graph:
                return fingerprints

            # 使用知识图谱模块的解析器提取 MD 中的关系
            markdown_content = profile_file.read_text(encoding="utf-8")
            _, existing_relations = self.knowledge_graph.parse_graph_from_markdown(markdown_content)

            for rel in existing_relations:
                fingerprints.add((rel.get("source", ""), rel.get("target", ""), rel.get("type", "")))

        except Exception as e:
            logger.debug(f"[PromptBuilder] 提取 MD 关系指纹失败: {e}")

        return fingerprints

    def _build_emotion_instruction(self):
        """构建情绪感知指令（不计入 Token 控制）"""
        return """
## 行为准则：管家的自我修养

1. **时间与环境感知**：
   - 关注当前时间。清晨应有朝气，深夜应温和。
   - 识别对话氛围。严肃讨论时保持专业，闲聊时可以幽默。

2. **记忆的连贯性与去重**：
   - **引用记忆**：当引用之前的记忆时，使用"我记得你之前说过..."或"关于上次提到的..."来增强真实感。
   - **高质量记录**：记录新记忆时，确保信息具有"增量价值"。如果该信息已存在且无变化，无需重复记录。

3. **主动性**：
   - 如果主人表现出困惑，主动提供帮助或查阅文档。
   - 如果发现主人的计划存在冲突（如两个预约时间重叠），主动委婉提醒。
"""

    def _build_working_context(self, uid, group_id):
        """构建工作文件上下文（VFS 增强版：虚拟文件系统 + 渐进式披露 + 自主进化）"""
        if not self.file_manager:
            return ""

        try:
            working_context = self.file_manager.get_working_context(uid, group_id)
            if working_context:
                # ========== VFS 虚拟文件系统说明 ==========
                instruction = f"""
## 📁 记忆与工作区 (Virtual Workspace - VFS)

### 🆔 虚拟工作区架构

**重要：你现在运行在虚拟文件系统（VFS）中，所有文件操作都使用语义化的命名空间路径。**

#### 可用命名空间（Namespaces）：
- **`@personal/`** : 当前用户的个人目录
  - 私聊环境：这是你的主工作区
  - 群聊环境：用于管理当前用户的个人档案和记忆

- **`@group/`** : 当前群组的共享目录（仅群聊环境）
  - 用于访问和管理群组级别的文件、记忆、配置

- **`@root/`** : 系统根目录（需要管理员 Sudo 权限）
  - 用于跨用户/跨群组的管理操作
  - 访问系统级配置和全局文件

---

**🎯 核心原则：所见即所得**
- 你在上下文中看到的文件标签格式就是你应该使用的操作路径
- 例如：看到 `[@personal/P_PROFILE.md]` → 使用 `@personal/P_PROFILE.md` 进行读写
- **永远不要尝试使用物理绝对路径或猜测 UID**

---

你的工作区下存放着你的长期记忆、笔记和任务上下文。
**你的大脑容量（Context Window）是有限的，因此你必须学会"外挂大脑"：**

### 🧠 核心工作原则 (Progressive Disclosure)

1. **命名空间即索引**：我只为你提供了文件的**精简目录树**（包含摘要），而非全文内容。
2. **按需读取**：当你需要某个文件的详细信息时，**必须**使用 `file_read_tool(file_path)` 主动读取。
3. **节约 Token**：不要一次性读取所有文件。根据当前对话需求，选择最相关的 2-3 个文件读取。

### 🛡️ 知识库架构与权限体系

#### 官方技能库（Read-Only）
- **路径**：`skills/` 目录下的所有文件
- **权限**：**只读** - 你可以阅读和学习，但**禁止修改或删除**
- **用途**：提供标准化的操作手册和技能指南

#### 用户级记忆（Read-Write）
- **路径**：`@personal/` 或 `@group/` 下的文件
- **权限**：可读可写 - 你可以自由编辑和创建新文件
- **用途**：存储用户画像、长期记忆、个人笔记等

### 🚀 SOP 流程管理规范

#### 文件层级与命名
- **个人级**：`@personal/P_SOP.md` - 个人专属的操作流程
- **群组级**：`@group/G_SOP.md` - 群组共享的操作流程
- **全局级**：`@root/SOP.md` - 全局通用的操作流程

#### 优先级规则
- **私聊场景**：个人设定 > 全局设定
- **群聊场景**：群组设定 > 全局设定

#### 新增流程（必须严格遵守）
1. **唯一文件原则**：
   - 个人流程**必须且只能**保存在 `@personal/P_SOP.md`
   - 群组流程**必须且只能**保存在 `@group/G_SOP.md`
   - 全局流程**必须且只能**保存在 `@root/SOP.md`
   - **严禁**创建任何其他名称的 SOP 文件（如 `SOP_xxx.md`）

2. **追加格式**：
   - 使用 `file_append` 工具将新流程追加到文件末尾
   - 必须使用 `## 流程：[流程名称]` 作为二级标题
   - 示例：
     ```markdown
     ## 流程：请假报备
     - **触发条件**：当用户提到要请假时
     - **执行步骤**：
       1. 询问请假时间
       2. 询问请假事由
       3. 提醒用户在考勤系统提交
     ```

3. **修改流程**：
   - 必须先使用 `file_read` 读取文件
   - 使用 `file_edit` 或 `multi_edit` 工具进行精准修改
   - **严禁**使用 `file_write` 覆盖整个文件

#### YAML 元数据头（创建新文件时必须包含）
```yaml
---
summary: "用一句话描述该文件的核心内容"
keywords: ["关键词1", "关键词2", "关键词3"]
created: "2026-04-10"
---
```

### 📝 日常操作指南

1. **主动记录**：发现重要信息、用户偏好或待办事项时，立即使用 `file_append_tool` 记录到 `NOTES.md` 或 `TODO.md`
2. **按需读取**：不要试图记住所有文件内容。我只为你提供了简短的摘要。当你需要细节时，**必须**使用 `file_read_tool`
3. **先读后写**：在修改任何已有文件（使用 `file_write_tool` 或 `file_edit_tool`）之前，**必须先使用 `file_read_tool` 读取该文件**，否则操作将被系统拦截。
4. **高危操作确认**：`file_delete_tool` 是一个**不可逆的高危操作**。当你调用此工具时：
   - 系统会**自动挂起操作**并向用户发送确认请求（你不需要手动发送）
   - 你会收到 `PENDING_USER_CONFIRMATION` 错误提示
   - **收到此错误后，请立即停止并等待用户指令**
   - 不要重复调用 `file_delete_tool`，不要尝试其他删除方式
   - 用户回复 `/delete` 后系统会自动执行删除，用户回复其他内容则取消
5. **自我整理**：在后台闲时整理时，你会将 `NOTES.md` 中的零散信息提炼到 `MEMORY.md`
6. **维护元数据**：当你使用工具更新 `PROFILE.md` 或其他带有 `last_refined` 字段的文件时，请务必同时将该字段更新为当前时间

### 🔧 工具使用策略（v2.0 智能指引）

#### ⚡ 第一优先级：直觉调用（无需思考）

以下高频场景**直接使用对应工具**，不要犹豫或搜索：

| 用户意图 | 立即使用的工具 | 示例 |
|---------|--------------|------|
| 发送 URL/链接 | `web_fetch_tool` | "看看这个 https://..." |
| 问"你能做什么" | 直接列举核心功能 | 不需要调任何工具 |
| 设置提醒/待办 | `create_reminder` / `todo_*` | "明天9点提醒我..." |
| 读取/编辑文件 | `file_read/edit/write_tool` | "帮我读一下 @personal/P_PROFILE.md" |
| 搜索记忆/知识 | `memory_search` / `knowledge_search` | "我记得之前说过..." |
| 身份绑定/查询 | `/whoami` 命令 / `bind_identity` | "我是谁？" |

**【VFS 路径示例】**
```
✅ 正确用法：
   file_read_tool("@personal/P_PROFILE.md")
   file_write_tool("@group/G_SOUL.md", content="...")
   file_list_tool("@personal/")  # 列出个人目录

❌ 错误用法：
   file_read_tool("profiles/user_123/P_PROFILE.md")  # 不要使用物理路径
   file_read_tool("D:/data/...")                       # 不要使用绝对路径
```

#### 🎯 第二优先级：专业技能任务（优先用 Skill）

对于需要**多步骤、领域知识、复杂工作流**的任务，优先使用 `skill_call_tool`：

```python
# 知识提取与学习
skill_call_tool(
    skill_name="scriptor-knowledge-research",
    instruction="记录用户偏好：...",
    mode="inline"
)

# 数据归档与历史查询
skill_call_tool(
    skill_name="scriptor-archive-manager",
    instruction="归档这篇长文到档案馆",
    mode="auto"
)

# 批量媒体处理
skill_call_tool(
    skill_name="scriptor-media-gallery",
    instruction="上传并管理这些图片",
    mode="forked"  # 耗时任务用异步
)
```

**触发条件**（满足任一即考虑用 Skill）：
- ✅ 任务涉及 3+ 个步骤
- ✅ 需要遵循特定的工作流/SOP
- ✅ 属于明确的专业领域（研究/归档/管理）
- ✅ 用户说"学习"、"研究"、"整理"、"归档"、"分析"

#### 🔍 第三优先级：不确定时再搜索

**只有当你完全不知道该用什么工具时**，才使用 `tool_search_tool`：

```python
# ✅ 正确用法：LLM 确实困惑
tool_search_tool(query="如何批量处理 Excel 并生成报告")

# ❌ 错误用法：你应该知道答案
tool_search_tool(query="读取文件")  # 应该直接用 file_read_tool！
```

**判断标准**：
- ❌ 不要搜索基础操作（读写文件、搜索、提醒等）
- ✅ 可以搜索组合操作或冷门功能
- ✅ 当用户需求模糊且你无法推断最佳工具时

#### 🌐 Web 工具选择指南

| 场景 | 使用工具 | 说明 |
|------|---------|------|
| 用户发送 URL | `web_fetch_tool` | 直接读取网页内容 |
| 需要搜索信息 | `web_search_tool` | 用搜索引擎查找 |
| URL + 搜索结合 | 先 fetch 再 search | 先看链接内容，再补充背景 |

**示例流程**：
```
用户："帮我看一下这篇文章 https://example.com/article 然后总结一下"

你的思考：
1. 检测到 URL → web_fetch_tool(url="https://...")
2. 获取文章内容后 → 总结要点
3. 如果需要背景知识 → web_search_tool(query="相关主题")
```

#### 🛠️ 核心工具速查表（Top 15 高频工具）

**文件操作**（最常用）：
- `file_read_tool` - 读取文件（支持行号范围）
- `file_edit_tool` - 编辑文件（精确替换）
- `file_write_tool` - 写入/创建新文件
- `file_list_tool` - 列出目录文件

**记忆与知识**：
- `memory_search` - 检索长期记忆
- `knowledge_add` - 添加知识条目
- `knowledge_search` - 搜索知识库 ⭐ **回答经验类问题前请先调用此工具**

**Web 与信息获取**：
- `web_search_tool` - 搜索引擎查询
- `web_fetch_tool` - 读取网页原文 ⭐ v2.0 新增

**系统与调度**：
- `create_reminder` - 创建提醒（支持循环）⭐ v2.0 增强
- `todo_show/todo_add` - 待办事项管理

**元工具**（特殊用途）：
- `tool_search_tool` - 发现可用工具 ⭐ v2.0 新增
- `skill_call_tool` - 调用专业技能包 ⭐ v2.0 新增

#### ⚠️ 反模式（避免的错误用法）

1. **不要过度使用 tool_search**
   - ❌ 每次都先搜索再行动
   - ✅ 对常见操作直接调用对应工具

2. **不要滥用 skill_call**
   - ❌ 简单任务也调 skill（增加延迟）
   - ✅ 单步操作直接用原生工具

3. **不要重复调用 web_fetch**
   - ❌ 同一个 URL 抓取多次
   - ✅ 利用内置缓存（5分钟内自动复用）

4. **不要忽略循环提醒功能**
   - ❌ 每次只设单次提醒
   - ✅ 定期任务用 recurrence 参数（如 "every weekday"）

5. **不要使用物理路径**
   - ❌ `profiles/user_xxx/file.md`
   - ✅ `@personal/file.md` 或 `@group/file.md`

6. **不要盲目覆写文件**
   - ❌ 未读取文件内容就直接调用 `file_write_tool` 或 `file_edit_tool`
   - ✅ **必须先调用 `file_read_tool`** 获取完整上下文，然后再进行修改

7. **回答经验类问题时先搜索知识库**
   - ❌ 直接凭记忆回答工作经验、流程规范、工具使用等问题
   - ✅ **先调用 `knowledge_search`** 搜索知识库中的相关经验

### 🔧 可用工具完整列表

> 💡 提示：你共有 **50+ 个工具**可用，这里列出核心分类。
> 完整列表可通过 `tool_search_tool(query="所有工具")` 查看。

**文件操作类** (7个):
- `file_list_tool`: 查看目录结构（支持 VFS 虚拟根目录）
- `file_read_tool`: 读取文件（支持 @personal/, @group/, @root/）
- `file_write_tool`: 创建/覆盖文件（⚠️ 覆写已有文件前必须先调用 file_read_tool）
- `file_edit_tool`: 编辑文件（精确替换，⚠️ 编辑前必须先调用 file_read_tool）
- `file_append_tool`: 追加内容到文件末尾
- `file_search_tool`: 文件内关键词搜索
- `multi_edit_tool`: 批量原子化编辑（⚠️ 编辑前必须先调用 file_read_tool）

**记忆与知识类** (5个):
- `memory_search`: 检索长期记忆（支持跨会话）
- `knowledge_add`: 提取并存储知识点
- `knowledge_search`: 搜索知识库
- `research_topic`: 发起深度研究
- `usage_docs_search`: 搜索使用文档

**Web 信息获取类** (2个):
- `web_search_tool`: 搜索引擎查询（SearXNG）
- `web_fetch_tool`: 直接读取网页内容 ⭐

**身份与管理类** (5个):
- `bind_identity`: 绑定身份
- `set_group_admin_tool`: 群组权限管理
- `sudo_*`: 超级管理员命令

**调度与提醒类** (4个):
- `create_reminder`: 创建定时提醒（支持自然语言循环）
- `add_schedule_task`: 添加调度任务
- `todo_show/add/update/delete`: 待办事项 CRUD

**专业技能入口** (1个):
- `skill_call_tool`: 调用技能宏（5个专业领域）⭐

**工具自省** (1个):
- `tool_search_tool`: 搜索可用工具 ⭐

**其他** (媒体、归档、图谱等): 通过 `tool_search_tool` 发现
"""
                return "\n\n---\n\n" + instruction + "\n" + working_context
        except Exception as e:
            logger.debug(f"[PromptBuilder] 获取工作文件上下文失败: {e}")

        return ""

    def _build_archives_context(self, uid: str, group_id: str):
        """构建档案馆上下文（使用三级架构）"""
        if not self.archive_router:
            if self.archive_manager:
                catalog = self.archive_manager.get_archive_catalog_prompt()
                if catalog:
                    return f"\n\n---\n\n{catalog}\n\n**注意：如果你需要查询这些档案，请使用 `query_archives` 工具。查询结果包含原始数据表格，你必须在回复中展示该表格，以便用户对照验证。**"
            return ""

        try:
            from .archives.router import ArchiveIndex

            index = ArchiveIndex(self.archive_router)
            is_sudo = self.identity_manager.is_sudo(uid, self.config.admin_uids)
            catalog = index.build_unified_catalog(uid, group_id, is_sudo)

            if catalog:
                return f"\n\n---\n\n{catalog}\n\n**注意：如果你需要查询这些档案，请使用 `query_archives` 工具。查询结果包含原始数据表格，你必须在回复中展示该表格，以便用户对照验证。**"
        except Exception as e:
            logger.debug(f"[PromptBuilder] 构建档案目录失败: {e}")

        return ""

    def _build_todo_context(self, uid: str, group_id: str) -> str:
        """构建 TODO 热记忆上下文

        注入未完成待办 + 最近3天已完成的待办
        同时静默触发归档，确保热数据绝对干净
        """
        try:
            from .todo_manager import TodoManager

            todo_manager = TodoManager(self.data_dir, scope="personal")
            todo_manager.archive_old_completed(uid)

            personal_todo = todo_manager.get_hot_memory(uid)

            result = ""

            if personal_todo and personal_todo != "【当前待办状态】\n\n**无未完成待办**\n\n**无最近完成记录**":
                archive_hint = '> [系统提示] 超过3天的历史已完成任务已归档至 TODOed 目录。如需查询历史记录，请调用 search_historical_todos 工具（参数 scope="personal"）。\n\n'
                result += f"# 个人待办清单\n{archive_hint}{personal_todo}\n"

            if group_id != "private":
                group_manager = TodoManager(self.data_dir, scope="group")
                group_manager.archive_old_completed(group_id)

                group_todo = group_manager.get_hot_memory(group_id)

                if group_todo and group_todo != "【当前待办状态】\n\n**无未完成待办**\n\n**无最近完成记录**":
                    archive_hint = '> [系统提示] 超过3天的历史已完成任务已归档至 TODOed 目录。如需查询历史记录，请调用 search_historical_todos 工具（参数 scope="group"）。\n\n'
                    result += f"# 群组待办清单\n{archive_hint}{group_todo}\n"

            return result

        except Exception as e:
            logger.debug(f"[PromptBuilder] 构建 TODO 上下文失败: {e}")
            return ""

    def _build_sudo_context(self, uid: str) -> str:
        """构建管理员模式上下文"""
        return self.identity_manager.get_sudo_prompt_suffix(uid, self.config.admin_uids)

    def build_system_prompt(self, uid, group_id, user_message: str = ""):
        """
        构建完整的系统提示词（带智能 Token 控制）- 渐进式披露模式

        策略：
        1. 必须全量加载：SOUL、权限、TODO、BOOTSTRAP（短小/实时性要求高）
        2. 改为目录索引：PROFILE、MEMORY、SOP、AGENTS、日记（大体积/按需读取）
        3. 新增：Context Indexer 生成的上下文目录树
        """
        profile_dir = self.data_dir / "profiles" / uid
        if not profile_dir.exists():
            return ""

        if not self.config.enable_token_control:
            return self._build_system_prompt_without_control(uid, group_id, profile_dir, user_message)

        return self._build_system_prompt_with_control(uid, group_id, profile_dir, user_message)

    def _build_system_prompt_without_control(self, uid, group_id, profile_dir, user_message: str = ""):
        """不使用 Token 控制的旧版本构建方法（同步更新：Global -> Group -> Personal 顺序）"""
        prompt_parts = []

        # [Global] 全局核心准则
        global_soul_content = self._load_global_template("SOUL.md")
        if global_soul_content:
            prompt_parts.append("# 全局核心人格基座 (Global SOUL)\n" + global_soul_content)

        # [Group] 群组文件（仅群聊加载，放在个人文件之前）
        if group_id != "private":
            group_soul_file = self.data_dir / "groups" / group_id / "G_SOUL.md"
            if group_soul_file.exists():
                content = group_soul_file.read_text(encoding="utf-8")
                content = content.replace("{group_id}", group_id)
                content = content.replace("{uid}", uid)
                prompt_parts.append("# 群组核心准则 (Group SOUL)\n" + content)

            group_profile_file = self.data_dir / "groups" / group_id / "G_PROFILE.md"
            if group_profile_file.exists():
                content = group_profile_file.read_text(encoding="utf-8")
                content = content.replace("{group_id}", group_id)
                content = content.replace("{uid}", uid)
                prompt_parts.append("# 群组公共身份与关系网 (Group PROFILE)\n" + content)

            group_workflow_file = self.data_dir / "groups" / group_id / "G_GROUP.md"
            if group_workflow_file.exists():
                content = group_workflow_file.read_text(encoding="utf-8")
                content = content.replace("{group_id}", group_id)
                content = content.replace("{uid}", uid)
                prompt_parts.append("# 群组工作流与行为守则 (GROUP)\n" + content)

            group_context = self.group_manager.get_group_context(group_id, uid)

            if group_context.get("group_rules"):
                prompt_parts.append("# 群体规则\n" + group_context["group_rules"])

            if group_context.get("members"):
                member_list = "\n".join(["- %s (%s)" % (m["alias"], m["role"]) for m in group_context["members"]])
                prompt_parts.append("# 群体成员\n" + member_list)

        # [Personal] 个人文件（放在群组文件之后）
        soul_file = profile_dir / "P_SOUL.md"
        if soul_file.exists():
            content = soul_file.read_text(encoding="utf-8")
            content = content.replace("{uid}", uid)
            content = content.replace("{自己的uid}", uid)
            prompt_parts.append("# 个人核心准则 (Personal SOUL)\n" + content)

        agents_file = profile_dir / "P_AGENTS.md"
        if agents_file.exists():
            content = agents_file.read_text(encoding="utf-8")
            content = content.replace("{uid}", uid)
            prompt_parts.append("# 个人行为准则与操作手册 (Personal AGENTS)\n" + content)

        profile_file = profile_dir / "P_PROFILE.md"
        if profile_file.exists():
            content = profile_file.read_text(encoding="utf-8")
            content = content.replace("{uid}", uid)
            prompt_parts.append("# 个人画像与人设 (Personal PROFILE)\n" + content)

        recent_notes = self.memory_manager.get_recent_notes_text(uid, group_id, limit=2)
        if recent_notes:
            prompt_parts.append("# 最近日记\n" + recent_notes)

        todo_context = self._build_todo_context(uid, group_id)
        if todo_context:
            prompt_parts.append(todo_context)

        # 添加知识图谱关联（新增）
        graph_context = self._build_graph_context(uid, user_message)
        if graph_context:
            prompt_parts.append(graph_context)

        # SOP 文件已迁移到渐进式披露模式，通过 ContextIndexer 按需加载
        # 不再全量注入 SOP.md / P_SOP.md / G_SOP.md

        bootstrap_file = profile_dir / "P_BOOTSTRAP.md"
        if bootstrap_file.exists():
            prompt_parts.append("# 首次引导\n" + bootstrap_file.read_text(encoding="utf-8"))

        if not prompt_parts:
            return ""

        final_prompt = "\n\n---\n\n".join(prompt_parts)
        final_prompt += self._build_working_context(uid, group_id)
        final_prompt += self._build_archives_context(uid, group_id)
        final_prompt += self._build_sudo_context(uid)
        final_prompt += self._build_emotion_instruction()

        if self.learning_manager:
            session_id = f"{uid}_{group_id}"
            learning_prompt = self.learning_manager.get_state_prompt_suffix(session_id)
            if learning_prompt:
                logger.debug(f"[PromptBuilder] 添加认知状态提示词 (session={session_id}): {learning_prompt[:100]}...")
                final_prompt += learning_prompt

        return final_prompt

    def _build_system_prompt_with_control(self, uid, group_id, profile_dir, user_message: str = ""):
        """使用智能 Token 控制的新版本构建方法（渐进式披露模式 + Prompt Caching 优化）
        核心改动：
        - 移除 PROFILE.md、MEMORY.md、SOP.md、AGENTS.md 的全量注入
        - 移除 Global MEMORY.md 的全量注入
        - 移除 最近日记的全量注入
        - 新增 ContextIndexer 生成的上下文目录树
        - 保留 SOUL、权限、TODO、BOOTSTRAP 的全量加载
        - 【Prompt Caching 优化】严格的静动分离 + 群组优先策略：
          * 第一层（常驻静态区）：Global(100) -> Group(99-97) -> Personal(96-94)
          * 第二层（动态索引区）：ContextIndexer 目录索引 (89) + 知识图谱 (80)
          * 第三层（绝对动态区）：规则/成员(29-27)、TODO(26)、Bootstrap(25)
        """
        trimmer = SmartMemoryTrimmer(self.config.max_system_prompt_tokens)

        # ========== 第一层：绝对静态区 (Static Zone) - 命中缓存的核心 ==========
        # 优先级 100~90，严格前缀匹配，最大化 Prompt Caching 命中率
        # 策略：Global -> Group -> Personal，确保同一群内不同用户共享群组缓存

        # [100] 全局核心准则 (Global SOUL.md) — 所有用户/群聊共享
        global_soul_content = self._load_global_template("SOUL.md")
        if global_soul_content:
            trimmer.add_part("global_soul", "# 全局核心人格基座 (Global SOUL)\n" + global_soul_content, 100)

        # [99] 群组核心准则 (G_SOUL.md) — 仅群聊加载，同一群内所有用户共享
        if group_id != "private":
            group_soul_file = self.data_dir / "groups" / group_id / "G_SOUL.md"
            if group_soul_file.exists():
                content = group_soul_file.read_text(encoding="utf-8")
                content = content.replace("{group_id}", group_id)
                content = content.replace("{uid}", uid)
                trimmer.add_part("group_soul", "# 群组核心准则 (Group SOUL)\n" + content, 99)

        # [98] 群组公共身份与关系网 (G_PROFILE.md) — 仅群聊加载，同一群内所有用户共享
        if group_id != "private":
            group_profile_file = self.data_dir / "groups" / group_id / "G_PROFILE.md"
            if group_profile_file.exists():
                content = group_profile_file.read_text(encoding="utf-8")
                content = content.replace("{group_id}", group_id)
                content = content.replace("{uid}", uid)
                trimmer.add_part("group_profile", "# 群组公共身份与关系网 (Group PROFILE)\n" + content, 98)

        # [97] 群组工作流与行为守则 (G_GROUP.md) — 仅群聊加载，同一群内所有用户共享
        if group_id != "private":
            group_workflow_file = self.data_dir / "groups" / group_id / "G_GROUP.md"
            if group_workflow_file.exists():
                content = group_workflow_file.read_text(encoding="utf-8")
                content = content.replace("{group_id}", group_id)
                content = content.replace("{uid}", uid)
                trimmer.add_part("group_workflow", "# 群组工作流与行为守则 (GROUP)\n" + content, 97)

        # [96] 个人核心准则 (P_SOUL.md) — 个人专属，放在群组文件之后
        soul_file = profile_dir / "P_SOUL.md"
        if soul_file.exists():
            content = soul_file.read_text(encoding="utf-8")
            content = content.replace("{uid}", uid)
            content = content.replace("{自己的uid}", uid)
            trimmer.add_part("personal_soul", "# 个人核心准则 (Personal SOUL)\n" + content, 96)

        # [95] 个人画像与人设 (P_PROFILE.md) — 个人专属，包含隐私边界和社交人设
        profile_file = profile_dir / "P_PROFILE.md"
        if profile_file.exists():
            content = profile_file.read_text(encoding="utf-8")
            content = content.replace("{uid}", uid)
            trimmer.add_part("personal_profile", "# 个人画像与人设 (Personal PROFILE)\n" + content, 95)

        # [94] 个人行为准则与操作手册 (P_AGENTS.md) — 个人专属，会自主进化
        agents_file = profile_dir / "P_AGENTS.md"
        if agents_file.exists():
            content = agents_file.read_text(encoding="utf-8")
            content = content.replace("{uid}", uid)
            trimmer.add_part("personal_agents", "# 个人行为准则与操作手册 (Personal AGENTS)\n" + content, 94)

        # [90] 权限状态与社交感知（硬核注入，非文件，必须加载）
        is_super = self.identity_manager.is_super_admin(uid, self.config.admin_uids)
        is_group_admin = self.identity_manager.is_group_admin(uid, group_id)
        perm_desc = (
            f"你当前正在与 {'超级管理员' if is_super else '群组管理员' if is_group_admin else '普通用户'} 对话。\n"
        )

        social_perception = (
            "\n\n【社交感知与身份识别】\n"
            "1. **身份标识**：`user_数字`（如 user_547813589）是系统内部逻辑 ID。在对话中，请始终使用用户在 `P_PROFILE.md` 中定义的「主称呼」来称呼用户，仅在需要艾特（@）时使用 UID 格式。\n"
            "2. **提及识别**：消息中出现的 `[@昵称(UID:user_xxx)]` 表示该用户被提及。你可以通过 UID 关联其历史记忆。\n"
        )

        if group_id != "private":
            social_perception += (
                "3. **回复规范**：系统会自动引用提问者的消息，**请绝对不要在回复的开头使用 `[@昵称(UID:user_xxx)]` 提及提问者**。但如果你在行文中需要提及其他人，可以正常使用 @ 格式。\n"
                "4. **艾特规范**：在群聊中，如果你想提及或回复某位群成员，请直接在回复文本中使用 `[@昵称(UID:user_xxx)]` 格式（例如 `[@张三(UID:user_5478138)]`）。**严禁**使用 `send_message_to_user` 工具去尝试私聊群成员，除非用户明确要求私聊。\n"
                "5. **称呼规范（最高优先级）**：当你在 `P_PROFILE.md` 中看到用户的「主称呼」时，**必须始终使用该称呼**来称呼该用户，而不是使用其 QQ 群名片、昵称或其他别名。这是用户明确授权的公开身份标识，在所有场景（包括群聊）中都应优先使用。\n"
                "6. **新成员感知**：如果遇到从未见过的 UID，说明该成员尚未在你的记忆库中'建档'，你可以通过对话引导其自我介绍并记录。\n"
                "7. **权限管理**：如果你是超级管理员，你可以使用 `set_group_admin_tool` 工具，并传入目标用户的 UID 来管理群组权限。\n"
            )
        else:
            social_perception += (
                "3. **私聊规范**：当前为私聊场景，请直接称呼对方，**严禁**在回复中使用 `[@昵称(UID:user_xxx)]` 格式，这会显得生硬且不礼貌。\n"
                "4. **权限管理**：如果你是超级管理员，你可以使用 `set_group_admin_tool` 工具，并传入目标用户的 UID 来管理群组权限。\n"
            )

        trimmer.add_part("permissions", "# 权限与社交感知\n" + perm_desc + social_perception, 90)

        # ========== 第二层：动态加载区 (Progressive Zone) - 优先级 80~89 ==========

        # [89] ContextIndexer 目录索引（渐进式披露的核心）
        context_map = self.context_indexer.build_context_map(uid, group_id, include_skills=True)
        if context_map:
            trimmer.add_part("context_index", context_map, 89)

        # [80] 知识图谱关联（按需召回）
        graph_context = self._build_graph_context(uid, user_message)
        if graph_context:
            trimmer.add_part("graph_context", graph_context, 80)

        # ========== 第三段：绝对动态区 (Dynamic Zone) - 缓存破坏者 ==========
        # 这些内容频繁变动，必须放在最后

        # 7. 群体规则和成员（实时性要求高）
        if group_id != "private":
            group_context = self.group_manager.get_group_context(group_id, uid)

            if group_context.get("group_rules"):
                trimmer.add_part("group_rules", "# 群体规则\n" + group_context["group_rules"], 29)

            if group_context.get("members"):
                member_list = "\n".join(["- %s (%s)" % (m["alias"], m["role"]) for m in group_context["members"]])
                trimmer.add_part("group_members", "# 群体成员\n" + member_list, 28)

        # 9. TODO 热记忆上下文（必须加载，实时性要求高）
        todo_context = self._build_todo_context(uid, group_id)
        if todo_context:
            trimmer.add_part("todo_hot_memory", todo_context, 26)

        # 10. 首次引导（仅在首次使用时加载，之后删除）
        bootstrap_content = ""

        if group_id != "private":
            group_bootstrap_file = self.data_dir / "groups" / group_id / "G_BOOTSTRAP.md"
            if group_bootstrap_file.exists():
                bootstrap_content = group_bootstrap_file.read_text(encoding="utf-8")
                trimmer.add_part("bootstrap", "# 群组首次引导\n" + bootstrap_content, 25)

        if not bootstrap_content:
            bootstrap_file = profile_dir / "P_BOOTSTRAP.md"
            if bootstrap_file.exists():
                bootstrap_content = bootstrap_file.read_text(encoding="utf-8")
                trimmer.add_part("bootstrap", "# 个人首次引导\n" + bootstrap_content, 25)

        # ================================================================================

        selected_parts, total_tokens = trimmer.trim()

        if not selected_parts:
            return ""

        # 按照 priority 降序排列（即静态在前，动态在后）
        selected_parts.sort(key=lambda x: x.priority, reverse=True)

        prompt_parts = [part.content for part in selected_parts]
        final_prompt = "\n\n---\n\n".join(prompt_parts)

        # 附加绝对动态的上下文（工作区、档案、Sudo、情绪指令）
        final_prompt += self._build_working_context(uid, group_id)
        final_prompt += self._build_archives_context(uid, group_id)
        final_prompt += self._build_sudo_context(uid)
        final_prompt += self._build_emotion_instruction()

        if self.learning_manager:
            session_id = f"{uid}_{group_id}"
            learning_prompt = self.learning_manager.get_state_prompt_suffix(session_id)
            if learning_prompt:
                logger.debug(f"[PromptBuilder] 添加认知状态提示词 (session={session_id}): {learning_prompt[:100]}...")
                final_prompt += learning_prompt

        logger.info(
            "[Scriptor] 系统提示词构建完成(渐进式披露模式 + Prompt Caching 优化): "
            "使用 %s/%s 个记忆部分, "
            "估算 Token: %s/%s"
            % (len(selected_parts), len(trimmer.parts), total_tokens, self.config.max_system_prompt_tokens)
        )

        return final_prompt

    def load_soul(self, uid):
        """加载人格定义"""
        profile_dir = self.data_dir / "profiles" / uid
        soul_file = profile_dir / "P_SOUL.md"

        if soul_file.exists():
            content = soul_file.read_text(encoding="utf-8")
            content = content.replace("{uid}", uid)
            content = content.replace("{自己的uid}", uid)
            return content
        return ""

    def load_agents(self, uid):
        """加载行为规则"""
        profile_dir = self.data_dir / "profiles" / uid
        agents_file = profile_dir / "P_AGENTS.md"

        if agents_file.exists():
            content = agents_file.read_text(encoding="utf-8")
            content = content.replace("{uid}", uid)
            return content
        return ""

    def load_profile(self, uid):
        """加载个人画像"""
        profile_dir = self.data_dir / "profiles" / uid
        profile_file = profile_dir / "P_PROFILE.md"

        if profile_file.exists():
            content = profile_file.read_text(encoding="utf-8")
            content = content.replace("{uid}", uid)
            return content
        return ""

    def load_group_context(self, group_id, uid):
        """加载群体上下文"""
        group_context = self.group_manager.get_group_context(group_id, uid)

        parts = []

        if group_context.get("group_rules"):
            parts.append("## 群体规则\n" + group_context["group_rules"])

        if group_context.get("members"):
            member_list = "\n".join(["- %s (%s)" % (m["alias"], m["role"]) for m in group_context["members"]])
            parts.append("## 群体成员\n" + member_list)

        return "\n\n".join(parts)
