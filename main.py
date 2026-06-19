# Scriptor (灵笔司书)
# Copyright (C) 2026 ysf7762-dev
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Scriptor (灵笔司书) - 基于 Scriptor 体系的多角色跨群体 AI 智能管家记忆系统

特性：
- 跨平台身份聚合
- 群体记忆管理
- 跨群信息传递
- 主动式记忆管理
- 文件即记忆

架构说明：
- main.py: 核心入口，组件初始化，生命周期管理
- mixins/: 按业务领域划分的功能模块
  - HelpersMixin: 内部辅助方法
  - IdentityMixin: 身份与权限管理
  - MemoryMixin: 记忆管理
  - LearningMixin: 学习/授课模式
  - KnowledgeMixin: 知识库管理
  - EventsMixin: 事件拦截
  - ToolsMixin: LLM 工具
  - CommandsMixin: 其他命令
"""

import asyncio
from pathlib import Path
from typing import Dict, Set

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .core.active_reply_manager import ActiveReplyManager
from .core.archives.ingestor import DataIngestor
from .core.archives.manager import ArchiveManager
from .core.archives.router import ArchiveIndex, ArchiveRouter, migrate_legacy_archive
from .core.background_tasks import BackgroundTasks
from .core.compactor import Compactor
from .core.config_pydantic import ScriptorConfigPydantic
from .core.conversation_ledger import ConversationLedger
from .core.file_manager import FileManager
from .core.file_monitor import FileMonitor
from .core.group_manager import GroupManager
from .core.identity_manager import IdentityManager
from .core.knowledge_base import KnowledgeBase
from .core.knowledge_graph import KnowledgeGraph

from .core.learning_manager import LearningManager  # isort: split
from .core.learning_manager import CognitiveState
from .core.memory_manager import MemoryManager
from .core.message_buffering import MessageBuffer
from .core.message_sanitizer import MessageSanitizer
from .core.prompt_builder import PromptBuilder
from .core.research_tool import ResearchTool
from .core.scheduler import TaskScheduler
from .core.search_engine import SearchEngine
from .core.session_locks import SessionLockManager
from .core.smart_sender import create_smart_sender_from_config
from .core.tool_decoration import ToolDecorator
from .core.usage_docs import UsageDocsKnowledgeBase
from .mixins import (
    AdminMixin,
    CommandsMixin,
    EventsMixin,
    HelpersMixin,
    IdentityMixin,
    KnowledgeMixin,
    LearningMixin,
    MediaToolsMixin,
    MemoryMixin,
    ToolsMixin,
)
from .web.shared_state import set_shared_state


@register(
    "astrbot_plugin_scriptor",
    "Scriptor",
    "基于 Scriptor 的多角色跨群体 AI 智能管家记忆系统",
    "1.0.7",
    "https://github.com/astrbots/astrbot_plugin_scriptor",
)
class ScriptorPlugin(
    Star,
    HelpersMixin,
    IdentityMixin,
    MemoryMixin,
    LearningMixin,
    KnowledgeMixin,
    EventsMixin,
    ToolsMixin,
    MediaToolsMixin,
    CommandsMixin,
    AdminMixin,
):
    """
    Scriptor 插件主类

    通过 Mixin 模式按业务领域划分功能模块，保持代码的可维护性和可读性。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_scriptor")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 首先尝试从正确的配置文件加载配置
        import json
        import os

        # 检查是否存在配置文件（按优先级查找）
        # 首选: data/config/astrbot_plugin_scriptor_config.json（AstrBot 规范）
        # 备选: data/config/Scriptor_config.json（旧版本兼容）
        # self.data_dir = data/plugin_data/astrbot_plugin_scriptor
        # self.data_dir.parent.parent = data
        config_candidates = [
            self.data_dir.parent.parent / "config" / "astrbot_plugin_scriptor_config.json",
            self.data_dir.parent.parent / "config" / "Scriptor_config.json",
        ]

        correct_config_path = None
        for candidate in config_candidates:
            logger.info(f"[Scriptor] 尝试加载配置文件：{candidate}")
            if candidate.exists():
                correct_config_path = candidate
                try:
                    # 使用 utf-8-sig 编码读取，自动处理 BOM
                    with open(correct_config_path, "r", encoding="utf-8-sig") as f:
                        correct_config = json.load(f)
                    logger.info(f"[Scriptor] 从配置文件加载成功：{correct_config_path}")
                    logger.info(f"[Scriptor] web_search_enabled: {correct_config.get('web_search_enabled', 'N/A')}")
                    logger.info(f"[Scriptor] admin_uids: {correct_config.get('admin_uids', 'N/A')}")
                    break
                except Exception as e:
                    logger.warning(f"[Scriptor] 从配置文件加载失败：{e}")
                    correct_config = None
                    correct_config_path = None

        if correct_config is None and correct_config_path is None:
            logger.warning(f"[Scriptor] 所有候选配置文件都不存在，使用默认配置")
            correct_config = None

        # 使用正确的配置或默认配置
        if correct_config:
            # 使用 load_from_flat_dict 方法将扁平字典转换为嵌套配置
            self.config = ScriptorConfigPydantic.load_from_flat_dict(correct_config)

            # 同步配置到 Scriptor 配置文件（用于 Scriptor WebUI）
            try:
                scriptor_config_file = self.data_dir / "config.json"
                scriptor_config_file.parent.mkdir(parents=True, exist_ok=True)

                # 将嵌套配置转换为扁平配置
                flat_config = {}
                for section_name, section_config in self.config.dict().items():
                    if isinstance(section_config, dict):
                        for key, value in section_config.items():
                            flat_config[key] = value
                    else:
                        flat_config[section_name] = section_config

                with open(scriptor_config_file, "w", encoding="utf-8") as f:
                    json.dump(flat_config, f, ensure_ascii=False, indent=2)
                logger.info(f"[Scriptor] 配置已同步到 Scriptor 配置文件: {scriptor_config_file}")
            except Exception as e:
                logger.warning(f"[Scriptor] 同步配置到 Scriptor 配置文件失败: {e}")
        else:
            self.config = ScriptorConfigPydantic.load_from_flat_dict(config)

        from .tools.common.text_utils import set_global_config

        set_global_config(self.config)

        # 配置同步跟踪
        self._last_config_sync_time = 0
        self._config_sync_interval = 300  # 5 分钟

        logger.info("[Scriptor] 初始化中...")

        self._init_core_components()
        self._init_knowledge_system()
        self._init_archive_system()
        self._init_tool_system()
        self._init_state_variables()
        self._init_background_tasks()
        self._init_skill_system()
        self.background_tasks = BackgroundTasks(self)

        logger.info(f"[Scriptor] 插件已启动！数据目录: {self.data_dir}")

    def _init_core_components(self):
        """初始化核心组件：身份、群组、记忆、会话等"""
        self.identity_manager = IdentityManager(self.data_dir)
        self.group_manager = GroupManager(self.data_dir, self.identity_manager)
        self.memory_manager = MemoryManager(self.data_dir, self.config, self.identity_manager, self.group_manager)
        self.compactor = Compactor(self.config, self.context)
        self.conversation_ledger = ConversationLedger(self.data_dir)
        self.message_sanitizer = MessageSanitizer()
        self.message_buffer = MessageBuffer()
        self.tool_decorator = ToolDecorator()
        self.session_lock_manager = SessionLockManager()

        from .core.pending_tasks import init_pending_task_store

        init_pending_task_store()
        logger.info("[Scriptor] 待确认任务池已初始化")

        from .core.concurrency_guard import ConcurrencyGuard

        max_concurrent = getattr(self.config, "max_concurrent_llm", 5)
        self.concurrency_guard = ConcurrencyGuard(max_concurrent=max_concurrent)
        logger.info(f"[Scriptor] 并发控制器已初始化 (最大并发: {max_concurrent})")

        self.smart_sender = create_smart_sender_from_config(self.config)
        self.scheduler = TaskScheduler(self.data_dir)
        self.scheduler.start()

    def _init_knowledge_system(self):
        """初始化知识系统：知识库、学习管理器、知识图谱等"""
        self.knowledge_base = KnowledgeBase(self.data_dir)
        self.research_tool = ResearchTool(self.knowledge_base)
        self.usage_docs_kb = UsageDocsKnowledgeBase(self.data_dir)

        self.learning_manager = LearningManager(
            knowledge_base=self.knowledge_base,
            knowledge_graph=None,
            admin_uids=self.config.admin_uids,
            identity_manager=self.identity_manager,
        )
        logger.info("[Scriptor] 学习管理器已初始化")

        self.knowledge_graph = KnowledgeGraph(self.data_dir)
        self.learning_manager.kg = self.knowledge_graph
        logger.info("[Scriptor] 知识图谱已注入学习管理器")

    def _init_archive_system(self):
        """初始化档案馆系统"""
        if migrate_legacy_archive(self.data_dir):
            logger.info("[Scriptor] 已迁移旧版档案馆到 global 目录")

        self.archive_router = ArchiveRouter(self.data_dir)
        self.archive_index = ArchiveIndex(self.archive_router)
        self.archive_manager = ArchiveManager(str(self.data_dir / "global" / "archives.db"))
        self.data_ingestor = DataIngestor(self.archive_manager)
        self.file_manager = FileManager(self.data_dir)

    def _init_tool_system(self):
        """初始化工具系统：搜索、媒体、主动回复等"""
        from .core.media_manager import MediaManager

        self.media_manager = MediaManager(self.data_dir, self.config)
        logger.info("[Scriptor] 媒体资源管理器已初始化")

        # 调试日志：输出配置值
        logger.info(f"[Scriptor] 配置检查 - web_search_enabled: {self.config.web_search_enabled}")
        logger.info(f"[Scriptor] 配置检查 - searxng_base_url: {self.config.searxng_base_url}")

        self.web_search_tool = None
        if self.config.web_search_enabled:
            try:
                from .tools.web_search_tool import WebSearchTool

                searxng_url = self.config.searxng_base_url
                if not searxng_url:
                    logger.warning("[Scriptor] SearXNG 地址未配置，网页搜索功能将不可用")
                    self.web_search_tool = None
                else:
                    self.web_search_tool = WebSearchTool(
                        searxng_base_url=searxng_url,
                        searxng_secret=self.config.searxng_secret,
                        max_results=self.config.searxng_max_results,
                        timeout=self.config.searxng_timeout,
                        archive_enabled=self.config.search_archive_enabled,
                        archive_threshold=self.config.search_archive_threshold,
                        fetch_top_n=self.config.web_fetch_top_n,
                        default_engines=self.config.searxng_default_engines,
                    )
                    logger.info(
                        f"[Scriptor] 网页搜索工具已初始化 (SearXNG: {searxng_url}, 自动读取: {self.config.web_fetch_top_n} 个网页)"
                    )
            except Exception as e:
                logger.error(f"[Scriptor] 网页搜索工具初始化失败：{e}")
                self.web_search_tool = None

        # 初始化 WebFetch 工具（访问完整网页）
        self.web_fetch_tool = None
        try:
            from .tools.web_fetch_tool import WebFetcher

            self.web_fetch_tool = WebFetcher()
            logger.info("[Scriptor] WebFetch 工具已初始化（可访问完整网页）")
        except Exception as e:
            logger.error(f"[Scriptor] WebFetch 工具初始化失败：{e}")
            self.web_fetch_tool = None

        self.active_reply_manager = ActiveReplyManager(
            config=self.config,
            group_manager=self.group_manager,
            context=self.context,
            data_dir=self.data_dir,
        )

        self.search_engine = None
        self.prompt_builder = None
        self.file_monitor = None

    def _init_state_variables(self):
        """初始化状态变量"""
        self._web_api_process = None
        self._is_ready = False
        self._background_tasks: Set[asyncio.Task] = set()
        self._is_terminating = False
        self._group_states: Dict[str, str] = {}
        self._group_last_active: Dict[str, float] = {}
        self._last_interaction_time: Dict[str, float] = {}
        self._has_new_content: Dict[str, bool] = {}

    def _init_background_tasks(self):
        """初始化后台任务"""
        self._background_tasks.add(asyncio.create_task(self._lazy_init_components()))
        self._background_tasks.add(asyncio.create_task(self._scheduler_loop()))
        self._background_tasks.add(asyncio.create_task(self._nightly_graph_consolidation()))
        self._background_tasks.add(asyncio.create_task(self._heartbeat_loop()))

        self._rebind_mixin_tool_handlers()
        self._register_scriptor_skills()

        from .core.architecture_enhancements import get_architecture_manager

        self.arch_manager = get_architecture_manager()
        self._background_tasks.add(asyncio.create_task(self._init_architecture_enhancements()))

    def _init_skill_system(self):
        """初始化技能系统"""
        from .tools.skill_tool import initialize_skill_system

        plugin_dir = Path(__file__).parent
        skills_dir = plugin_dir / "skills"

        custom_skills_path = None
        if self.config.custom_skills_dir:
            custom_skills_path = Path(self.config.custom_skills_dir)
            if not custom_skills_path.is_absolute():
                custom_skills_path = self.data_dir / custom_skills_path

        self.skill_registry, self.skill_executor = initialize_skill_system(
            skills_dir, self, custom_skills_dir=custom_skills_path
        )
        logger.info("[Scriptor] 技能宏调用系统已初始化")

    def _rebind_mixin_tool_handlers(self):
        """
        重新绑定 Mixin 工具的 handler 到 self。

        AstrBot 框架在绑定 handler 时检查 handler.__module__ == plugin_module_path，
        但 Mixin 类的方法来自不同的模块，导致绑定条件不满足。
        此方法手动将 Mixin 工具的 handler 替换为绑定到 self 的方法。
        """

        llm_tool_manager = self.context.get_llm_tool_manager()

        main_module_path = self.__class__.__module__
        mixin_modules = {
            "mixins.tools_mixin",
            "mixins.knowledge_mixin",
            "mixins.media_tools_mixin",
        }

        rebound_count = 0
        for func_tool in llm_tool_manager.func_list:
            if func_tool.handler is None:
                continue

            handler_module = getattr(func_tool.handler, "__module__", "")

            if any(handler_module.endswith(m) or handler_module == m for m in mixin_modules):
                handler_name = func_tool.handler.__name__
                bound_handler = getattr(self, handler_name, None)

                if bound_handler is not None:
                    func_tool.handler = bound_handler
                    func_tool.handler_module_path = main_module_path
                    rebound_count += 1
                    logger.debug(f"[Scriptor] 重新绑定工具 handler: {func_tool.name} -> {handler_name}")

        if rebound_count > 0:
            logger.info(f"[Scriptor] 已重新绑定 {rebound_count} 个 Mixin 工具的 handler")

    def _register_scriptor_skills(self):
        """
        注册 Scriptor 专属 Skills 到 AstrBot 的 SkillManager。

        将 skills/ 目录下的 5 个技能包复制到 AstrBot 全局 skills 目录，
        让 AstrBot 的 SkillManager 能够自动发现和加载这些技能。
        """
        import shutil
        from pathlib import Path

        try:
            plugin_dir = Path(__file__).parent
            scriptor_skills_dir = plugin_dir / "skills"

            if not scriptor_skills_dir.exists():
                logger.warning(f"[Scriptor] Skills 目录不存在: {scriptor_skills_dir}")
                return

            from astrbot.core.utils.astrbot_path import get_astrbot_skills_path

            astrbot_skills_root = Path(get_astrbot_skills_path())
            astrbot_skills_root.mkdir(parents=True, exist_ok=True)

            skill_names = [
                "scriptor-knowledge-research",
                "scriptor-todo-schedule",
                "scriptor-archive-manager",
                "scriptor-media-gallery",
                "scriptor-group-admin",
            ]

            registered_count = 0
            for skill_name in skill_names:
                src_dir = scriptor_skills_dir / skill_name
                dst_dir = astrbot_skills_root / skill_name

                if not src_dir.exists():
                    logger.warning(f"[Scriptor] Skill 文件夹不存在: {src_dir}")
                    continue

                skill_md = src_dir / "SKILL.md"
                if not skill_md.exists():
                    logger.warning(f"[Scriptor] SKILL.md 不存在: {skill_md}")
                    continue

                try:
                    if dst_dir.exists():
                        shutil.rmtree(dst_dir)
                    shutil.copytree(src_dir, dst_dir)
                    registered_count += 1
                    logger.info(f"[Scriptor] 已注册 Skill: {skill_name}")
                except Exception as e:
                    logger.error(f"[Scriptor] 注册 Skill 失败 {skill_name}: {e}")

            if registered_count > 0:
                logger.info(f"[Scriptor] 成功注册 {registered_count}/{len(skill_names)} 个 Scriptor Skills")
            else:
                logger.warning("[Scriptor] 未能注册任何 Skill")

        except ImportError:
            logger.warning("[Scriptor] 无法导入 AstrBot Skills 路径工具，跳过 Skill 注册")
        except Exception as e:
            logger.error(f"[Scriptor] 注册 Skills 时发生错误: {e}")

    def _get_intent_provider(self):
        """获取意图判定小模型提供商"""
        if not self.config.ar_intent_model_provider:
            return None

        try:
            if hasattr(self.context, "get_provider_by_id"):
                return self.context.get_provider_by_id(self.config.ar_intent_model_provider)
        except Exception as e:
            logger.warning(f"[Scriptor] 获取小模型提供商失败: {e}")

        return None

    def _get_current_provider(self):
        """获取当前使用的 LLM 提供商"""
        try:
            if hasattr(self.context, "get_using_provider"):
                return self.context.get_using_provider()
        except Exception as e:
            logger.warning(f"[Scriptor] 获取当前提供商失败: {e}")

        return None

    async def _init_architecture_enhancements(self):
        """初始化架构增强组件（斜杠命令路由器、工具降级、权限引擎）"""
        try:
            await self.arch_manager.initialize(plugin_instance=self)
            self.arch_manager.register_enhanced_commands()
            await self.arch_manager.start_background_services()

            logger.info("[Scriptor] ✅ 架构增强组件已初始化（命令路由/工具降级/权限引擎）")
        except Exception as e:
            logger.error(f"[Scriptor] 架构增强组件初始化失败：{e}", exc_info=True)

    async def _lazy_init_components(self):
        """后台懒加载初始化重量级组件"""
        try:
            self._cleanup_invalid_group_directories()

            logger.info("[Scriptor] 初始化媒体资源管理器...")
            await self.media_manager.initialize()

            logger.info("[Scriptor] 开始后台初始化 SearchEngine...")
            self.search_engine = SearchEngine(
                self.data_dir, self.config, self.identity_manager, self.group_manager, self.memory_manager
            )

            self.knowledge_base.set_search_engine(self.search_engine)

            self.usage_docs_kb.set_search_engine(self.search_engine)

            logger.info("[Scriptor] 开始后台初始化 PromptBuilder...")
            self.prompt_builder = PromptBuilder(
                self.data_dir,
                self.config,
                self.identity_manager,
                self.group_manager,
                self.memory_manager,
                self.file_manager,
                self.archive_manager,
                self.knowledge_graph,
                self.learning_manager,
                self.archive_router,
            )

            logger.info("[Scriptor] 开始初始化文件监控...")
            self.file_monitor = FileMonitor(self.data_dir, self._handle_file_change)
            asyncio.create_task(self.file_monitor.start())

            logger.info("[Scriptor] 启动任务记忆监控...")
            await self.memory_manager.start_task_monitor()

            logger.info("[Scriptor] 设置 Web UI 共享状态...")
            set_shared_state(
                self.data_dir,
                self.search_engine,
                self.identity_manager,
                self.group_manager,
                self.memory_manager,
                self.config,
                self.knowledge_base,
                self.research_tool,
                self.archive_manager,
                self.archive_router,
                self.data_ingestor,
            )

            logger.info("[Scriptor] 启动会话锁管理器...")
            await self.session_lock_manager.start()

            await self._start_web_ui()

            self._is_ready = True
            logger.info("[Scriptor] 后台初始化完成！")

        except asyncio.CancelledError:
            logger.warning("[Scriptor] 后台初始化被取消")
        except (OSError, RuntimeError) as e:
            logger.error(f"[Scriptor] 后台初始化失败：{e}")

    async def _sync_config_from_disk(self):
        """
        从磁盘同步配置（用于捕获通过 AstrBot 官方 Web UI 进行的配置更改）

        检查配置文件的修改时间，如果有变化则重新加载配置
        """
        import time

        current_time = time.time()
        if current_time - self._last_config_sync_time < self._config_sync_interval:
            return

        try:
            config_file = self.data_dir / "config.json"
            if not config_file.exists():
                return

            # 检查文件修改时间
            config_mtime = config_file.stat().st_mtime
            if config_mtime <= self._last_config_sync_time:
                return

            import json

            with open(config_file, "r", encoding="utf-8") as f:
                new_config = json.load(f)

            # 检查配置是否变化
            old_config_dict = self.config.dict()
            if new_config == old_config_dict:
                self._last_config_sync_time = current_time
                return

            logger.info("[Scriptor] 检测到配置更新，重新加载配置...")
            self.config = ScriptorConfigPydantic(**new_config)
            self._last_config_sync_time = current_time

            # 更新全局配置
            from .tools.common.text_utils import set_global_config

            set_global_config(self.config)

            # 同步更新 shared_state 中的配置引用
            from .web.shared_state import _shared_state, set_shared_state

            _shared_state["config"] = self.config

            # 同步配置到 WebUI 的 config.json 文件
            try:
                config_dump = self.data_dir / "config.json"
                config_dump.write_text(json.dumps(self.config.dict(), indent=4, ensure_ascii=False), encoding="utf-8")
                logger.info("[Scriptor] 已同步配置到 Web UI config.json")
            except Exception as sync_err:
                logger.warning(f"[Scriptor] 同步配置到 Web UI 失败: {sync_err}")

            # 重新初始化依赖配置的组件
            await self._reload_config_dependent_components()

            logger.info("[Scriptor] 配置已重新加载")

        except Exception as e:
            logger.error(f"[Scriptor] 配置同步失败：{e}")

    async def _reload_config_dependent_components(self):
        """
        重新加载依赖配置的组件

        目前主要重新初始化网页搜索工具
        """
        try:
            # 重新初始化网页搜索工具
            if self.config.web_search_enabled:
                from .tools.web_search_tool import WebSearchTool

                searxng_url = self.config.searxng_base_url
                if searxng_url:
                    # 关闭旧的搜索工具
                    if self.web_search_tool and hasattr(self.web_search_tool, "client"):
                        try:
                            await self.web_search_tool.client.close()
                        except Exception:
                            pass

                    # 创建新的搜索工具
                    self.web_search_tool = WebSearchTool(
                        searxng_base_url=searxng_url,
                        searxng_secret=self.config.searxng_secret,
                        max_results=self.config.searxng_max_results,
                        timeout=self.config.searxng_timeout,
                        archive_enabled=self.config.search_archive_enabled,
                        archive_threshold=self.config.search_archive_threshold,
                        fetch_top_n=self.config.web_fetch_top_n,
                        default_engines=self.config.searxng_default_engines,
                    )
                    logger.info(f"[Scriptor] 网页搜索工具已重新初始化 (SearXNG: {searxng_url})")
                else:
                    logger.warning("[Scriptor] SearXNG 地址未配置，网页搜索功能将不可用")
                    self.web_search_tool = None
            else:
                if self.web_search_tool and hasattr(self.web_search_tool, "client"):
                    try:
                        await self.web_search_tool.client.close()
                    except Exception:
                        pass
                self.web_search_tool = None
                logger.info("[Scriptor] 网页搜索功能已禁用")

            # 重新初始化 SmartSender
            if hasattr(self, "smart_sender"):
                from .core.smart_sender import SmartSender

                self.smart_sender = SmartSender(
                    enabled=self.config.smart_split_enabled,
                    only_llm=self.config.smart_split_only_llm,
                    context=self.context,
                    regex_pattern=self.config.smart_split_regex,
                    cleanup_pattern=self.config.smart_split_cleanup_regex,
                    typing_speed=self.config.smart_split_typing_speed,
                    min_delay=self.config.smart_split_min_delay,
                    max_delay=self.config.smart_split_max_delay,
                    random_factor=self.config.smart_split_random_factor,
                    long_text_threshold=self.config.smart_split_long_text_threshold,
                    long_text_pattern=self.config.smart_split_long_text_pattern,
                    group_reply=self.config.smart_split_group_reply,
                )
                logger.info("[Scriptor] SmartSender 已重新初始化")

            # 可以在这里添加其他需要重新初始化的组件

        except Exception as e:
            logger.error(f"[Scriptor] 重新加载组件失败：{e}")

    async def _start_web_ui(self):
        """启动 Web UI 服务（后端 API + 前端 Streamlit）"""
        import os
        import subprocess
        import sys

        web_ui_enabled = getattr(self.config, "web_ui_enabled", True)
        if not web_ui_enabled:
            logger.info("[Scriptor] Web UI 已禁用，跳过启动")
            return

        missing_deps = []
        try:
            import fastapi
        except ImportError:
            missing_deps.append("fastapi")
        try:
            import uvicorn
        except ImportError:
            missing_deps.append("uvicorn")
        try:
            import slowapi
        except ImportError:
            missing_deps.append("slowapi")
        try:
            import psutil
        except ImportError:
            missing_deps.append("psutil")
        try:
            import bcrypt
        except ImportError:
            missing_deps.append("bcrypt")

        if missing_deps:
            logger.error(f"[Scriptor] Web UI 启动失败：缺少必要依赖 {missing_deps}")
            logger.error("[Scriptor] 请在终端运行以下命令安装依赖：")
            logger.error(f"[Scriptor] .\\.venv\\Scripts\\pip install {' '.join(missing_deps)}")
            return

        plugin_dir = Path(__file__).parent
        web_dir = plugin_dir / "web"
        api_script = web_dir / "api.py"
        vue_dist_dir = web_dir / "dist"

        api_port = getattr(self.config, "web_api_port", 18111)

        try:
            config_dump = self.data_dir / "config.json"
            import json

            config_dump.write_text(json.dumps(self.config.dict(), indent=4, ensure_ascii=False), encoding="utf-8")
            logger.info(f"[Scriptor] 已同步配置到 Web UI: {config_dump}")
        except Exception as e:
            logger.warning(f"[Scriptor] 无法同步配置到 Web UI: {e}")

        env = os.environ.copy()
        env["SCRIPTOR_API_URL"] = f"http://127.0.0.1:{api_port}/api"

        astrbot_root = plugin_dir.parent.parent
        python_path = env.get("PYTHONPATH", "")
        paths = [str(astrbot_root), str(plugin_dir)]
        if python_path:
            paths.append(python_path)
        env["PYTHONPATH"] = os.pathsep.join(paths)

        password_file = self.data_dir / ".web_ui_password"
        key_file = self.data_dir / ".web_ui_key"

        old_password_file = plugin_dir / ".web_ui_password"
        old_key_file = plugin_dir / ".web_ui_key"

        if old_password_file.exists() and not password_file.exists():
            try:
                old_content = old_password_file.read_text(encoding="utf-8").strip()
                if old_content and not old_content.startswith("$2b$"):
                    import bcrypt

                    hashed = bcrypt.hashpw(old_content.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                    password_file.parent.mkdir(parents=True, exist_ok=True)
                    password_file.write_text(hashed, encoding="utf-8")
                    logger.info("[Scriptor] 已迁移并加密旧密码文件到新位置")
                    old_password_file.unlink()
                elif old_content:
                    password_file.parent.mkdir(parents=True, exist_ok=True)
                    password_file.write_text(old_content, encoding="utf-8")
                    logger.info("[Scriptor] 已迁移密码文件到新位置")
                    old_password_file.unlink()
            except Exception as e:
                logger.warning(f"[Scriptor] 迁移密码文件失败: {e}")

        if old_key_file.exists() and not key_file.exists() and not password_file.exists():
            try:
                key_file.parent.mkdir(parents=True, exist_ok=True)
                key_file.write_text(old_key_file.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info("[Scriptor] 已迁移临时密钥文件到新位置")
                old_key_file.unlink()
            except Exception as e:
                logger.warning(f"[Scriptor] 迁移密钥文件失败: {e}")

        has_custom_password = password_file.exists()
        has_temp_key = key_file.exists()

        if has_custom_password:
            logger.info("[Scriptor] 检测到自定义密码配置")
        elif has_temp_key:
            logger.info("[Scriptor] 检测到临时密钥配置")
            logger.warning("[Scriptor] 建议在 Web UI 配置中心设置新密码")
        else:
            logger.warning("[Scriptor] 未设置密码，请在 Web UI 首次访问时设置密码")

        try:
            if api_script.exists():
                logger.info(f"[Scriptor] 启动 Web 服务 (端口: {api_port})...")

                api_err_log = self.data_dir / "web_api_error.log"
                api_err_file = open(api_err_log, "w", encoding="utf-8")

                self._web_api_process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "web.api:app", "--host", "0.0.0.0", "--port", str(api_port)],
                    cwd=str(plugin_dir),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=api_err_file,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                logger.info(f"[Scriptor] Web 服务已启动 (PID: {self._web_api_process.pid})")

                if vue_dist_dir.exists():
                    logger.info(f"[Scriptor] Vue 前端已就绪，访问地址: http://127.0.0.1:{api_port}")
                else:
                    logger.warning("[Scriptor] Vue 前端未构建，请运行: cd web && npm run build")
                    logger.info(f"[Scriptor] API 文档地址: http://127.0.0.1:{api_port}/docs")
            else:
                logger.warning(f"[Scriptor] Web API 脚本不存在: {api_script}")

        except Exception as e:
            logger.error(f"[Scriptor] 启动 Web UI 失败: {e}")

    async def _stop_web_ui(self):
        """停止 Web UI 服务"""
        if self._web_api_process:
            try:
                logger.info("[Scriptor] 停止 Web 服务...")
                self._web_api_process.terminate()
                self._web_api_process.wait(timeout=5)
            except Exception as e:
                logger.warning(f"[Scriptor] 停止 Web 服务时出错: {e}")
                try:
                    self._web_api_process.kill()
                except Exception as kill_err:
                    logger.debug(f"[Scriptor] 强制终止进程失败: {kill_err}")
            self._web_api_process = None

    # ==================== 后台任务（委托到 BackgroundTasks） ====================

    async def _scheduler_loop(self):
        """定时任务触发检查循环"""
        await self.background_tasks.scheduler_loop()

    async def _send_scheduled_message(self, task):
        """发送定时任务消息或处理内部事件"""
        await self.background_tasks.send_scheduled_message(task)

    async def _nightly_graph_consolidation(self):
        """夜间知识图谱整理任务"""
        await self.background_tasks.nightly_graph_consolidation()

    async def _heartbeat_loop(self):
        """定时执行 Heartbeat 任务"""
        await self.background_tasks.run_heartbeat_loop()

    async def _run_graph_consolidation(self):
        """执行知识图谱整合（带超时保护）"""
        await self.background_tasks.run_graph_consolidation()

    async def _consolidate_user_graph(self, uid_dir: Path) -> int:
        """整合单个用户的知识图谱（增量 + 限量）"""
        return await self.background_tasks.consolidate_user_graph(uid_dir)

    async def _process_diary_for_graph(self, diary_file: Path, uid: str) -> bool:
        """处理单个日记文件，提取实体和关系"""
        return await self.background_tasks.process_diary_for_graph(diary_file, uid)

    async def _handle_proactive_event(self, event_type: str, uid: str, group_id: str):
        """处理主动事件（如早安、晚安、复盘）"""
        await self.background_tasks.handle_proactive_event(event_type, uid, group_id)

    async def _run_idle_consolidation(self):
        """执行后台闲时整理任务"""
        await self.background_tasks.run_idle_consolidation()

    async def _consolidate_working_files(self, uid: str, group_id: str):
        """为特定用户/群组执行闲时文件整理"""
        await self.background_tasks.consolidate_working_files(uid, group_id)

    async def _handle_web_search(self, event, query: str, depth: str = "normal", save_to_memory: bool = False):
        """处理网页搜索请求"""
        return await self.background_tasks.handle_web_search(event, query, depth, save_to_memory)

    def _build_graph_extraction_prompt(self, content: str, uid: str, user_name: str) -> str:
        """构建知识图谱提取 Prompt"""
        return self.background_tasks.build_graph_extraction_prompt(content, uid, user_name)

    def _get_user_preferred_name(self, uid: str) -> str:
        """获取用户偏好名称"""
        return self.background_tasks._get_user_preferred_name(uid)

    # ==================== 事件钩子代理方法 ====================
    # 注意：AstrBot 框架只扫描最终类的方法，不会扫描 Mixin 类的方法
    # 因此需要在主类中创建代理方法，调用 Mixin 类中的实际实现
    # ⚠️ global_recorder 使用 yield（异步生成器），其他钩子使用 return（普通协程）

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def global_recorder(self, event: AstrMessageEvent):
        """全局消息记录器（代理到 EventsMixin）"""
        async for result in super().global_recorder(event):
            yield result

    @filter.on_llm_request()
    async def before_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 请求前钩子（代理到 EventsMixin）"""
        await super().before_llm_request(event, req)

    @filter.on_llm_response()
    async def after_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """LLM 响应后钩子（代理到 EventsMixin）"""
        await super().after_response(event, resp)

    @filter.on_llm_tool_respond()
    async def on_tool_respond(self, event: AstrMessageEvent, tool, tool_args, tool_result):
        """工具执行后钩子（代理到 EventsMixin）"""
        await super().on_tool_respond(event, tool, tool_args, tool_result)

    @filter.on_using_llm_tool()
    async def on_tool_call(self, event: AstrMessageEvent, tool, tool_args):
        """工具调用前钩子（代理到 EventsMixin）"""
        await super().on_tool_call(event, tool, tool_args)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """消息装饰器钩子（代理到 EventsMixin）"""
        await super().on_decorating_result(event)

    # ==================== 配置同步事件处理器 ====================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _config_sync_handler(self, event: AstrMessageEvent):
        """
        配置同步事件处理器

        定期检查并同步配置文件（每 5 分钟最多一次）
        这样可以捕获通过 AstrBot 官方 Web UI 进行的配置更改
        """
        try:
            await self._sync_config_from_disk()
        except Exception as e:
            # 静默失败，避免影响正常功能
            logger.debug(f"[Scriptor] 配置同步检查失败：{e}")

    # ==================== 命令代理方法 ====================
    # 注意：AstrBot 的 @filter.command 装饰器需要在类定义时静态应用才能被扫描到
    # 因此我们回退到手动定义方案，以确保所有命令都能正常工作

    @filter.command("sc_help")
    async def sc_help(self, event: AstrMessageEvent):
        async for result in super().cmd_sc_help(event):
            yield result

    @filter.command("sc_admin")
    async def sc_admin(self, event: AstrMessageEvent):
        async for result in super().cmd_sc_admin(event):
            yield result

    @filter.command("smart_split_status")
    async def smart_split_status(self, event: AstrMessageEvent):
        async for result in super().cmd_smart_split_status(event):
            yield result

    @filter.command("buffer_status")
    async def buffer_status(self, event: AstrMessageEvent):
        async for result in super().cmd_buffer_status(event):
            yield result

    @filter.command("lock_status")
    async def lock_status(self, event: AstrMessageEvent):
        async for result in super().cmd_lock_status(event):
            yield result

    @filter.command("sc_concurrency")
    async def sc_concurrency(self, event: AstrMessageEvent):
        async for result in super().cmd_sc_concurrency(event):
            yield result

    @filter.command("webui")
    async def webui(self, event: AstrMessageEvent):
        async for result in super().cmd_webui(event):
            yield result

    @filter.command("kb_status")
    async def kb_status(self, event: AstrMessageEvent):
        async for result in super().cmd_kb_status(event):
            yield result

    @filter.command("开始学习")
    async def 开始学习(self, event: AstrMessageEvent):
        async for result in super().cmd_start_learning(event):
            yield result

    @filter.command("结束学习")
    async def 结束学习(self, event: AstrMessageEvent):
        async for result in super().cmd_end_learning(event):
            yield result

    @filter.command("开始授课")
    async def 开始授课(self, event: AstrMessageEvent):
        async for result in super().cmd_start_teaching(event):
            yield result

    @filter.command("结束授课")
    async def 结束授课(self, event: AstrMessageEvent):
        async for result in super().cmd_end_teaching(event):
            yield result

    @filter.command("学习状态")
    async def 学习状态(self, event: AstrMessageEvent):
        async for result in super().cmd_learning_status(event):
            yield result

    @filter.command("whoami")
    async def whoami(self, event: AstrMessageEvent):
        async for result in super().cmd_whoami(event):
            yield result

    @filter.command("get_bind_code")
    async def get_bind_code(self, event: AstrMessageEvent):
        async for result in super().cmd_get_bind_code(event):
            yield result

    @filter.command("debug_identity")
    async def debug_identity(self, event: AstrMessageEvent):
        async for result in super().cmd_debug_identity(event):
            yield result

    @filter.command("mem_status")
    async def mem_status(self, event: AstrMessageEvent):
        async for result in super().cmd_status(event):
            yield result

    @filter.command("debug_memory")
    async def debug_memory(self, event: AstrMessageEvent):
        async for result in super().cmd_debug_memory(event):
            yield result

    @filter.command("mem_report")
    async def mem_report(self, event: AstrMessageEvent):
        async for result in super().cmd_mem_report(event):
            yield result

    @filter.command("sudo_state_up")
    async def sudo_state_up(self, event: AstrMessageEvent):
        async for result in super().cmd_sudo_state_up(event):
            yield result

    @filter.command("sudo_state_down")
    async def sudo_state_down(self, event: AstrMessageEvent):
        async for result in super().cmd_sudo_state_down(event):
            yield result

    @filter.command("sudo_status")
    async def sudo_status(self, event: AstrMessageEvent):
        async for result in super().cmd_sudo_status(event):
            yield result

    @filter.command("sudo_sessions")
    async def sudo_sessions(self, event: AstrMessageEvent):
        async for result in super().cmd_sudo_sessions(event):
            yield result

    @filter.command("sudo_audit")
    async def sudo_audit(self, event: AstrMessageEvent):
        async for result in super().cmd_sudo_audit(event):
            yield result

    @filter.command("confirm_delete")
    async def confirm_delete(self, event: AstrMessageEvent):
        async for result in super().cmd_confirm_delete(event):
            yield result

    # 带参数的命令
    @filter.command("bind")
    async def bind(self, event: AstrMessageEvent, bind_code: str = None, confirm_token: str = None):
        async for result in super().cmd_bind(event, bind_code=bind_code, confirm_token=confirm_token):
            yield result

    @filter.command("unbind")
    async def unbind(self, event: AstrMessageEvent, unbind_token: str = None, confirm_token: str = None):
        async for result in super().cmd_unbind(event, unbind_token=unbind_token, confirm_token=confirm_token):
            yield result

    @filter.command("reset_identity")
    async def reset_identity(
        self, event: AstrMessageEvent, reset_token: str = None, step: str = None, code: str = None
    ):
        async for result in super().cmd_reset_identity(event, reset_token=reset_token, step=step, code=code):
            yield result

    @filter.command("search")
    async def search(self, event: AstrMessageEvent, *, remainder: str = ""):
        async for result in super().cmd_search(event, remainder=remainder):
            yield result

    # ==================== 生命周期方法 ====================

    async def terminate(self) -> None:
        """插件卸载时的清理工作"""
        try:
            logger.info("[Scriptor] 插件正在关闭...")
            self._is_terminating = True

            await self._stop_web_ui()

            if self.file_monitor:
                logger.info("[Scriptor] 停止文件监控...")
                await self.file_monitor.stop()

            if hasattr(self.memory_manager, "stop_task_monitor"):
                logger.info("[Scriptor] 停止任务记忆监控...")
                await self.memory_manager.stop_task_monitor()

            logger.info("[Scriptor] 停止会话锁管理器...")
            await self.session_lock_manager.stop()

            if self.scheduler:
                logger.info("[Scriptor] 停止定时任务调度器...")
                self.scheduler.stop()

            if hasattr(self, "arch_manager") and self.arch_manager:
                logger.info("[Scriptor] 停止架构增强后台服务...")
                await self.arch_manager.stop_background_services()

            pending_tasks = [t for t in self._background_tasks if not t.done()]
            if pending_tasks:
                logger.info(f"检测到待收束后台任务: {len(pending_tasks)} 个，开始取消")
                for task in pending_tasks:
                    task.cancel()
                await asyncio.gather(*pending_tasks, return_exceptions=True)
                logger.info("插件内后台任务已收束")

            logger.info("[Scriptor] 插件已关闭")

        except asyncio.CancelledError:
            logger.warning("[Scriptor] 插件卸载被取消")
        except (OSError, RuntimeError) as e:
            logger.error(f"[Scriptor] 插件卸载清理失败: {e}")
