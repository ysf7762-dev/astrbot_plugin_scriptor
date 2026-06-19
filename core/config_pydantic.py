# core/config_pydantic.py
"""
Scriptor 配置管理模块 - Pydantic 版本（嵌套模型优化版）

使用 Pydantic 进行配置验证和 Schema 生成
按功能域拆分为嵌套模型，提升可维护性和可读性
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MemoryConfig(BaseModel):
    """记忆系统配置"""

    memory_compact_threshold: int = Field(
        50000, ge=1000, le=500000, description="记忆压缩阈值（字符数），超过此值触发自动压缩"
    )
    daily_note_enabled: bool = Field(True, description="是否启用日记")
    cross_group_enabled: bool = Field(True, description="是否启用跨群功能")
    reflection_message_threshold: int = Field(15, ge=5, le=100, description="触发记忆反思的消息数量阈值")
    reflection_time_threshold: int = Field(1800, ge=60, le=7200, description="触发记忆反思的时间阈值（秒）")
    reflection_topic_threshold: float = Field(0.7, ge=0.0, le=1.0, description="话题识别阈值")
    reflection_recent_messages_limit: int = Field(20, ge=5, le=100, description="反思时考虑的最新消息数量")
    memory_archive_score_cap: float = Field(15.0, ge=5.0, le=20.0, description="记忆归档分数上限")
    llm_extraction_threshold: int = Field(10, ge=5, le=50, description="触发LLM记忆提取的消息数量阈值")
    memory_encryption_enabled: bool = Field(False, description="是否启用记忆内容加密存储")


class EmbeddingConfig(BaseModel):
    """嵌入向量配置"""

    embedding_enabled: bool = Field(True, description="是否启用嵌入")
    embedding_provider: str = Field("local", description="嵌入提供者: local 或 api")
    embedding_api_base: str = Field("http://localhost:11434/v1", description="嵌入 API 地址")
    embedding_api_key: Optional[str] = Field(default=None, description="嵌入 API 密钥")
    embedding_model: str = Field("AI-ModelScope/bge-small-zh-v1.5", description="嵌入模型名称")

    @field_validator("embedding_provider")
    @classmethod
    def validate_embedding_provider(cls, v):
        if v not in ["local", "api"]:
            import logging

            logging.getLogger("scriptor").warning(
                f"[Scriptor] embedding_provider 值 '{v}' 无效，已自动回退为 'local'。" f"有效值: local, api"
            )
            return "local"
        return v


class RerankConfig(BaseModel):
    """重排配置"""

    rerank_enabled: bool = Field(False, description="是否启用重排")
    rerank_provider: str = Field("api", description="重排提供者: api 或 local")
    rerank_api_base: str = Field("http://localhost:11434/v1", description="重排 API 地址")
    rerank_api_key: Optional[str] = Field(default=None, description="重排 API 密钥")
    rerank_model: str = Field("bge-reranker-v2-m3", description="重排模型名称")
    rerank_top_k: int = Field(5, ge=1, le=20, description="重排返回结果数")

    @field_validator("rerank_provider")
    @classmethod
    def validate_rerank_provider(cls, v):
        if v not in ["local", "api"]:
            import logging

            logging.getLogger("scriptor").warning(
                f"[Scriptor] rerank_provider 值 '{v}' 无效，已自动回退为 'api'。" f"有效值: local, api"
            )
            return "api"
        return v


class PriorityConfig(BaseModel):
    """上下文优先级配置"""

    max_system_prompt_tokens: int = Field(100000, ge=1000, le=500000, description="系统提示词最大 Token 数")
    enable_token_control: bool = Field(True, description="是否启用 Token 控制")
    soul_priority: int = Field(10, ge=1, le=20, description="人设优先级")
    agents_priority: int = Field(9, ge=1, le=20, description="代理/工具优先级")
    profile_priority: int = Field(8, ge=1, le=20, description="用户档案优先级")
    group_rules_priority: int = Field(7, ge=1, le=20, description="群规则优先级")
    group_members_priority: int = Field(6, ge=1, le=20, description="群成员优先级")
    cross_group_tasks_priority: int = Field(5, ge=1, le=20, description="跨群任务优先级")
    recent_notes_priority: int = Field(4, ge=1, le=20, description="近期笔记优先级")
    sop_priority: int = Field(3, ge=1, le=20, description="SOP 优先级")
    retrieval_guidance_priority: int = Field(2, ge=1, le=20, description="检索指导优先级")
    graph_recall_priority: int = Field(10, ge=1, le=20, description="知识图谱召回优先级")
    graph_recall_limit: int = Field(15, ge=1, le=50, description="知识图谱单次召回的最大关系条数")
    graph_keyword_search_limit: int = Field(3, ge=1, le=10, description="知识图谱每个关键词召回的实体数量限制")


class SearchConfig(BaseModel):
    """搜索配置"""

    search_top_k: int = Field(5, ge=1, le=50, description="搜索返回结果数")
    web_search_enabled: bool = Field(False, description="是否启用网页搜索工具（需手动开启）")
    searxng_base_url: str = Field("", description="SearXNG 搜索引擎地址（需自行配置）")
    searxng_secret: Optional[str] = Field(default=None, description="SearXNG 密钥")
    searxng_default_engines: str = Field(
        "google,baidu,wikipedia,sogou,quark,brave,startpage", description="默认启用的搜索引擎"
    )
    searxng_max_results: int = Field(20, ge=1, le=50, description="SearXNG 最大搜索结果数")
    searxng_timeout: int = Field(10, ge=5, le=60, description="SearXNG 请求超时时间（秒）")
    search_archive_enabled: bool = Field(True, description="是否启用搜索结果归档")
    search_archive_threshold: float = Field(0.8, ge=0.5, le=0.95, description="归档判定阈值")
    web_fetch_top_n: int = Field(5, ge=0, le=10, description="搜索后自动读取前 N 个网页内容（0 表示不读取）")


class LearningConfig(BaseModel):
    """学习/授课模式配置"""

    learning_mode_enabled: bool = Field(True, description="是否启用学习模式功能")
    learning_auto_extract_entities: bool = Field(True, description="自动提取实体和关系")
    learning_conflict_detection: bool = Field(True, description="知识冲突检测")
    learning_max_pending_items: int = Field(10, ge=1, le=100, description="最大待确认知识数量")
    teaching_mode_enabled: bool = Field(True, description="是否启用授课模式功能")
    teaching_strict_mode: bool = Field(True, description="授课模式下严格禁止知识库修改")
    nightly_maintenance_inactivity_minutes: int = Field(60, ge=10, le=240, description="夜间维护无活动时间阈值")
    nightly_maintenance_enabled: bool = Field(True, description="是否启用夜间中央索引维护管线")


class SmartSplitConfig(BaseModel):
    """智能分段发送配置"""

    smart_split_enabled: bool = Field(True, description="是否启用智能分段发送")
    smart_split_only_llm: bool = Field(True, description="仅对 LLM 结果进行分段")
    smart_split_regex: str = Field(".*?(?:\\n+|[。？！~…]{2,})|.+$", description="智能分段正则表达式")
    smart_split_cleanup_regex: str = Field("^\\s+|\\s+$", description="清理正则表达式")
    smart_split_typing_speed: float = Field(0.08, ge=0.01, le=0.5, description="模拟打字速度（秒/字符）")
    smart_split_min_delay: float = Field(1.5, ge=0.1, le=3.0, description="分段最小延迟（秒）")
    smart_split_max_delay: float = Field(3.5, ge=1.0, le=10.0, description="分段最大延迟（秒）")
    smart_split_random_factor: float = Field(0.2, ge=0.0, le=0.5, description="延迟随机波动因子")
    smart_split_long_text_threshold: int = Field(150, ge=50, le=1000, description="长文本判定阈值")
    smart_split_long_text_pattern: str = Field("\\n{2,}", description="长文本分段正则表达式")
    smart_split_group_reply: bool = Field(True, description="群聊引用功能总开关")


class ActiveReplyConfig(BaseModel):
    """主动回复配置"""

    active_reply_enabled: bool = Field(False, description="启用群聊主动回复（主开关）")
    ar_name_wakeup: bool = Field(True, description="启用群内称呼唤醒")
    ar_task_sniffing: bool = Field(False, description="启用活跃任务嗅探")
    ar_continuous_dialogue: bool = Field(True, description="启用连续对话智能判定")
    ar_debounce_seconds: int = Field(3, ge=1, le=30, description="防抖窗口时间（秒）")
    ar_max_queue_size: int = Field(10, ge=1, le=50, description="打包队列最大消息数")
    ar_attention_window_minutes: int = Field(2, ge=1, le=10, description="注意力窗口滑动时间（分钟）")
    ar_attention_window_messages: int = Field(10, ge=1, le=50, description="注意力窗口消息条数上限")
    ar_intent_model_provider: Optional[str] = Field(default=None, description="意图判定小模型提供商ID")
    ar_context_messages: int = Field(10, ge=5, le=30, description="意图判定时提供的上下文消息数量")
    ar_hard_stop_words: str = Field("退下，闭嘴，滚，消失，别说话，不用了，算了，没事了", description="硬打断词列表")


class ToolConfig(BaseModel):
    """工具系统配置"""

    tool_compact_enabled: bool = Field(True, description="是否启用工具结果微压缩防爆机制")
    tool_max_tokens: int = Field(
        10000, ge=1000, le=128000, description="工具返回结果的最大 Token 数量（超过此值将触发微压缩）"
    )
    tool_default_strategy: str = Field(
        "head_tail", description="默认压缩策略: truncate (截断尾部) 或 head_tail (保留头尾)"
    )


class SkillConfig(BaseModel):
    """技能系统配置 (v2.1)"""

    custom_skills_dir: Optional[str] = Field(default=None, description="自定义技能目录路径（用于覆盖或扩展内置技能）")
    enable_skill_recommendation: bool = Field(True, description="是否启用技能智能推荐注入")
    skill_recommendation_limit: int = Field(2, ge=1, le=5, description="每次推荐的最大技能数量")
    enable_tool_whitelist: bool = Field(
        True, description="是否启用技能工具白名单（限制 LLM 在执行技能时只能使用指定工具）"
    )


class MediaConfig(BaseModel):
    media_auto_save_enabled: bool = Field(True, description="自动保存收到的图片和文件")
    media_save_to_memory: bool = Field(False, description="保存媒体时调用 Vision API 生成描述")
    media_max_image_size_mb: int = Field(20, ge=1, le=100, description="单张图片最大大小（MB）")
    media_max_file_size_mb: int = Field(20, ge=1, le=500, description="单个文件最大大小（MB）")
    media_allowed_file_types: str = Field(
        "txt,md,doc,docx,wps,xls,xlsx,et,csv,ppt,pptx,pdf,jpg,jpeg,png,gif,webp,bmp", description="允许保存的文件类型"
    )
    media_retention_days: int = Field(30, ge=0, le=3650, description="媒体文件保留天数")


class WebUIConfig(BaseModel):
    """Web UI 管理面板配置"""

    web_ui_enabled: bool = Field(True, description="是否启用 Web UI 管理面板")
    web_api_port: int = Field(18111, ge=1024, le=65535, description="Web UI 后端 API 端口")
    web_frontend_port: int = Field(19111, ge=1024, le=65535, description="Web UI 前端界面端口")


class SystemConfig(BaseModel):
    """系统级配置"""

    message_sanitizer_enabled: bool = Field(True, description="是否启用消息清洗器")
    message_buffer_enabled: bool = Field(True, description="是否启用消息缓冲器")
    tool_decoration_enabled: bool = Field(True, description="是否启用工具装饰器")
    session_locks_enabled: bool = Field(True, description="是否启用会话锁")
    concurrency_control_enabled: bool = Field(True, description="是否启用全局并发控制")
    max_concurrent_llm: int = Field(5, ge=1, le=20, description="最大并发 LLM 请求数")
    session_timeout_seconds: float = Field(3600.0, ge=60.0, le=86400.0, description="会话超时时间（秒）")
    max_pending_per_session: int = Field(10, ge=1, le=50, description="每个会话最大排队数")
    backup_retention_days: int = Field(7, ge=1, le=30, description="备份文件保留天数")
    max_file_locks: int = Field(100, ge=10, le=500, description="文件锁缓存最大数量")
    index_cache_timeout: int = Field(300, ge=60, le=3600, description="索引缓存超时时间（秒）")
    admin_uids: List[str] = Field(default=[], description="管理员 UID 列表")
    debug_mode: bool = Field(True, description="是否启用调试模式")
    heartbeat_inactivity_threshold: int = Field(3600, ge=300, le=7200, description="Heartbeat 复盘不活跃阈值（秒）")
    graph_consolidation_max_time: int = Field(25, ge=5, le=60, description="知识图谱整合最大时间（分钟）")
    graph_consolidation_max_diaries: int = Field(10, ge=1, le=50, description="每晚处理的最大日记数")
    llm_call_timeout: int = Field(60, ge=10, le=300, description="LLM 调用超时时间（秒）")
    require_delete_confirmation: bool = Field(True, description="删除文件时是否需要用户通过 /delete 命令二次确认")


class ScriptorConfigPydantic(BaseModel):
    """
    Scriptor 插件主配置 - 嵌套模型版本

    支持:
    - 类型安全
    - 值范围验证
    - 默认值
    - JSON Schema 生成
    - 功能域隔离
    """

    model_config = ConfigDict(
        extra="ignore",  # 忽略未知字段，保持向后兼容
    )

    # ========== 嵌套配置模块 ==========
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="记忆系统配置")
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig, description="嵌入向量配置")
    rerank: RerankConfig = Field(default_factory=RerankConfig, description="重排配置")
    priority: PriorityConfig = Field(default_factory=PriorityConfig, description="上下文优先级配置")
    search: SearchConfig = Field(default_factory=SearchConfig, description="搜索配置")
    learning: LearningConfig = Field(default_factory=LearningConfig, description="学习/授课模式配置")
    smart_split: SmartSplitConfig = Field(default_factory=SmartSplitConfig, description="智能分段发送配置")
    active_reply: ActiveReplyConfig = Field(default_factory=ActiveReplyConfig, description="主动回复配置")
    media: MediaConfig = Field(default_factory=MediaConfig, description="媒体资源管理配置")
    tool: ToolConfig = Field(default_factory=ToolConfig, description="工具系统配置")
    skill: SkillConfig = Field(default_factory=SkillConfig, description="技能系统配置")
    web_ui: WebUIConfig = Field(default_factory=WebUIConfig, description="Web UI 管理面板配置")
    system: SystemConfig = Field(default_factory=SystemConfig, description="系统级配置")

    # ========== 动态属性代理（向后兼容） ==========
    # 使用 __getattr__ 和 __setattr__ 实现扁平化属性访问
    # 支持通过 config.xxx 方式直接访问嵌套配置项

    def __getattr__(self, name: str) -> Any:
        """动态获取嵌套配置项的属性"""
        if name.startswith("_"):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

        nested_configs = (
            "memory",
            "embedding",
            "rerank",
            "priority",
            "search",
            "learning",
            "smart_split",
            "active_reply",
            "media",
            "tool",
            "skill",
            "web_ui",
            "system",
        )

        for config_name in nested_configs:
            try:
                nested_config = object.__getattribute__(self, config_name)
                if nested_config is not None and hasattr(nested_config, name):
                    return getattr(nested_config, name)
            except AttributeError:
                continue

        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        """动态设置嵌套配置项的属性"""
        nested_configs = (
            "memory",
            "embedding",
            "rerank",
            "priority",
            "search",
            "learning",
            "smart_split",
            "active_reply",
            "media",
            "tool",
            "skill",
            "web_ui",
            "system",
        )

        if name in nested_configs or (name.startswith("_") and name != "__dict__"):
            super().__setattr__(name, value)
            return

        for config_name in nested_configs:
            try:
                nested_config = object.__getattribute__(self, config_name)
                if nested_config is not None and hasattr(nested_config, name):
                    setattr(nested_config, name, value)
                    return
            except AttributeError:
                continue

        super().__setattr__(name, value)

    @model_validator(mode="after")
    def validate_consistency(self):
        if self.rerank.rerank_enabled and self.rerank.rerank_top_k > self.search.search_top_k:
            raise ValueError("rerank_top_k 不能大于 search_top_k")
        return self

    @classmethod
    def load_from_flat_dict(cls, config_dict: Dict[str, Any]) -> "ScriptorConfigPydantic":
        """
        从扁平字典加载配置（向后兼容）

        支持旧的扁平格式和新的嵌套格式混合使用
        """
        nested_configs = {
            "memory": {},
            "embedding": {},
            "rerank": {},
            "priority": {},
            "search": {},
            "learning": {},
            "smart_split": {},
            "active_reply": {},
            "media": {},
            "tool": {},
            "web_ui": {},
            "system": {},
        }

        # 字段到子配置的映射
        field_mapping = {
            "memory": [
                "memory_compact_threshold",
                "daily_note_enabled",
                "cross_group_enabled",
                "reflection_message_threshold",
                "reflection_time_threshold",
                "reflection_topic_threshold",
                "reflection_recent_messages_limit",
                "memory_archive_score_cap",
                "llm_extraction_threshold",
                "memory_encryption_enabled",
            ],
            "embedding": [
                "embedding_enabled",
                "embedding_provider",
                "embedding_api_base",
                "embedding_api_key",
                "embedding_model",
            ],
            "rerank": [
                "rerank_enabled",
                "rerank_provider",
                "rerank_api_base",
                "rerank_api_key",
                "rerank_model",
                "rerank_top_k",
            ],
            "priority": [
                "max_system_prompt_tokens",
                "enable_token_control",
                "soul_priority",
                "agents_priority",
                "profile_priority",
                "group_rules_priority",
                "group_members_priority",
                "cross_group_tasks_priority",
                "recent_notes_priority",
                "sop_priority",
                "retrieval_guidance_priority",
                "graph_recall_priority",
                "graph_recall_limit",
                "graph_keyword_search_limit",
            ],
            "search": [
                "search_top_k",
                "web_search_enabled",
                "searxng_base_url",
                "searxng_secret",
                "searxng_default_engines",
                "searxng_max_results",
                "searxng_timeout",
                "search_archive_enabled",
                "search_archive_threshold",
            ],
            "learning": [
                "learning_mode_enabled",
                "learning_auto_extract_entities",
                "learning_conflict_detection",
                "learning_max_pending_items",
                "teaching_mode_enabled",
                "teaching_strict_mode",
                "nightly_maintenance_inactivity_minutes",
                "nightly_maintenance_enabled",
            ],
            "smart_split": [
                "smart_split_enabled",
                "smart_split_only_llm",
                "smart_split_regex",
                "smart_split_cleanup_regex",
                "smart_split_typing_speed",
                "smart_split_min_delay",
                "smart_split_max_delay",
                "smart_split_random_factor",
                "smart_split_long_text_threshold",
                "smart_split_long_text_pattern",
                "smart_split_group_reply",
            ],
            "active_reply": [
                "active_reply_enabled",
                "ar_name_wakeup",
                "ar_task_sniffing",
                "ar_continuous_dialogue",
                "ar_debounce_seconds",
                "ar_max_queue_size",
                "ar_attention_window_minutes",
                "ar_attention_window_messages",
                "ar_intent_model_provider",
                "ar_context_messages",
                "ar_hard_stop_words",
            ],
            "media": [
                "media_auto_save_enabled",
                "media_save_to_memory",
                "media_max_image_size_mb",
                "media_max_file_size_mb",
                "media_allowed_file_types",
                "media_retention_days",
            ],
            "tool": ["tool_compact_enabled", "tool_max_tokens", "tool_default_strategy"],
            "web_ui": ["web_ui_enabled", "web_api_port", "web_frontend_port"],
            "system": [
                "message_sanitizer_enabled",
                "message_buffer_enabled",
                "tool_decoration_enabled",
                "session_locks_enabled",
                "backup_retention_days",
                "max_file_locks",
                "index_cache_timeout",
                "admin_uids",
                "debug_mode",
            ],
        }

        for config_name, fields in field_mapping.items():
            for field in fields:
                if field in config_dict:
                    nested_configs[config_name][field] = config_dict.pop(field)

        # 处理嵌套格式
        for key in list(config_dict.keys()):
            if key in nested_configs and isinstance(config_dict[key], dict):
                nested_configs[key].update(config_dict.pop(key))

        return cls(**nested_configs)

    @classmethod
    def load_from_file(cls, config_path: Path) -> "ScriptorConfigPydantic":
        """从配置文件加载（含容错机制）

        如果配置文件损坏或包含非法值，自动回退到默认配置，
        而不是抛出异常导致插件永久无法加载。
        """
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config_json = json.load(f)
                config_dict = config_json.get("scriptor", config_json)
                return cls.load_from_flat_dict(config_dict)
            except Exception as e:
                import logging

                logging.getLogger("scriptor").warning(f"[Scriptor] 配置文件加载失败，已回退到默认配置: {e}")
                return cls()
        return cls()

    def to_dict(self, include_sensitive: bool = False) -> Dict[str, Any]:
        """
        转换为字典（扁平格式，向后兼容）

        Args:
            include_sensitive: 是否包含敏感信息（如API密钥）

        Returns:
            扁平化的配置字典
        """
        data = {}

        # 导出所有嵌套配置为扁平格式
        for config_name in [
            "memory",
            "embedding",
            "rerank",
            "priority",
            "search",
            "learning",
            "smart_split",
            "active_reply",
            "media",
            "tool",
            "web_ui",
            "system",
        ]:
            sub_config = getattr(self, config_name)
            if hasattr(sub_config, "model_dump"):
                data.update(sub_config.model_dump())

        # 敏感字段列表
        sensitive_fields = ["embedding_api_key", "rerank_api_key"]

        if not include_sensitive:
            for field in sensitive_fields:
                if data.get(field):
                    data[field] = "***"

        return data

    @classmethod
    def get_schema(cls) -> Dict[str, Any]:
        """获取 JSON Schema"""
        return cls.model_json_schema()

    def save_to_file(self, config_path: Path, include_sensitive: bool = False):
        """
        保存到配置文件

        Args:
            config_path: 配置文件路径
            include_sensitive: 是否包含敏感信息（如API密钥），默认 False（安全优先）
        """
        config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"scriptor": self.to_dict(include_sensitive=include_sensitive)}
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


ScriptorConfig = ScriptorConfigPydantic
