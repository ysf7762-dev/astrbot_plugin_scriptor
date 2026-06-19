# core/__init__.py
"""
Scriptor 核心模块

使用惰性加载（lazy loading），避免在导入时触发所有子模块的加载。
这样 `from core.memory_manager import MemoryManager` 不会因为其他子模块的问题而失败。
"""

import importlib
import os
import sys
from pathlib import Path

_core_dir = Path(__file__).parent
_plugin_root = _core_dir.parent
_tools_dir = _plugin_root / "tools"

if str(_tools_dir) not in sys.path:
    sys.path.insert(0, str(_tools_dir))
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))

# 惰性加载映射：属性名 -> 模块路径
_LAZY_IMPORTS = {
    # 直接子模块
    "ActiveReplyManager": ("core.active_reply_manager", "ActiveReplyManager"),
    "GroupState": ("core.active_reply_manager", "GroupState"),
    "GroupStatus": ("core.active_reply_manager", "GroupStatus"),
    "QueuedMessage": ("core.active_reply_manager", "QueuedMessage"),
    "ReplyDecision": ("core.active_reply_manager", "ReplyDecision"),
    "ScriptorConfig": ("core.config_pydantic", "ScriptorConfig"),
    "ScriptorConfigPydantic": ("core.config_pydantic", "ScriptorConfigPydantic"),
    "ChainedRecall": ("core.enhanced_features", "ChainedRecall"),
    "ConflictResolver": ("core.enhanced_features", "ConflictResolver"),
    "EnhancedMemorySystem": ("core.enhanced_features", "EnhancedMemorySystem"),
    "LightweightJudger": ("core.enhanced_features", "LightweightJudger"),
    "ReflectionScheduler": ("core.enhanced_features", "ReflectionScheduler"),
    "FeedbackQueue": ("core.feedback_queue", "FeedbackQueue"),
    "FeedbackTask": ("core.feedback_queue", "FeedbackTask"),
    "FeedbackType": ("core.feedback_queue", "FeedbackType"),
    "get_feedback_queue": ("core.feedback_queue", "get_feedback_queue"),
    "init_feedback_queue": ("core.feedback_queue", "init_feedback_queue"),
    "KnowledgeBase": ("core.knowledge_base", "KnowledgeBase"),
    "KnowledgeItem": ("core.knowledge_base", "KnowledgeItem"),
    "KnowledgeType": ("core.knowledge_base", "KnowledgeType"),
    "StructuredMemory": ("core.memory_struct", "StructuredMemory"),
    "BufferConfig": ("core.message_buffering", "BufferConfig"),
    "BufferedMessage": ("core.message_buffering", "BufferedMessage"),
    "MessageBuffer": ("core.message_buffering", "MessageBuffer"),
    "get_message_buffer": ("core.message_buffering", "get_message_buffer"),
    "set_message_buffer": ("core.message_buffering", "set_message_buffer"),
    "MessageSanitizer": ("core.message_sanitizer", "MessageSanitizer"),
    "Platform": ("core.message_sanitizer", "Platform"),
    "SanitizerConfig": ("core.message_sanitizer", "SanitizerConfig"),
    "get_sanitizer": ("core.message_sanitizer", "get_sanitizer"),
    "set_sanitizer": ("core.message_sanitizer", "set_sanitizer"),
    "ResearchDepth": ("core.research_tool", "ResearchDepth"),
    "ResearchNote": ("core.research_tool", "ResearchNote"),
    "ResearchStatus": ("core.research_tool", "ResearchStatus"),
    "ResearchTask": ("core.research_tool", "ResearchTask"),
    "ResearchTool": ("core.research_tool", "ResearchTool"),
    "LockManagerConfig": ("core.session_locks", "LockManagerConfig"),
    "SessionContext": ("core.session_locks", "SessionContext"),
    "SessionLockManager": ("core.session_locks", "SessionLockManager"),
    "SessionState": ("core.session_locks", "SessionState"),
    "get_session_lock_manager": ("core.session_locks", "get_session_lock_manager"),
    "set_session_lock_manager": ("core.session_locks", "set_session_lock_manager"),
    "DecorationConfig": ("core.tool_decoration", "DecorationConfig"),
    "ToolCategory": ("core.tool_decoration", "ToolCategory"),
    "ToolDecoration": ("core.tool_decoration", "ToolDecoration"),
    "ToolDecorator": ("core.tool_decoration", "ToolDecorator"),
    "get_tool_decorator": ("core.tool_decoration", "get_tool_decorator"),
    "set_tool_decorator": ("core.tool_decoration", "set_tool_decorator"),
    # tools 兼容导入
    "sanitize_id": ("tools.security.sanitizer", "sanitize_id"),
    "sanitize_filename": ("tools.security.sanitizer", "sanitize_filename"),
    "sanitize_log_message": ("tools.security.sanitizer", "sanitize_log_message"),
    "SAFE_FILENAME_PATTERN": ("tools.security.sanitizer", "SAFE_FILENAME_PATTERN"),
    "safe_json_loads": ("tools.common.json_parser", "safe_json_loads"),
    "extract_json_from_llm_output": ("tools.common.json_parser", "extract_json_from_llm_output"),
    "async_read_json": ("tools.common.async_io", "async_read_json"),
    "async_write_json": ("tools.common.async_io", "async_write_json"),
    "async_read_text": ("tools.common.async_io", "async_read_text"),
    "async_write_text": ("tools.common.async_io", "async_write_text"),
    "async_append_text": ("tools.common.async_io", "async_append_text"),
}

# 尝试导入 DebouncedWriter（可能失败）
try:
    from tools.storage.debounced_writer import DebouncedWriter

    _LAZY_IMPORTS["DebouncedWriter"] = ("tools.storage.debounced_writer", "DebouncedWriter")
except ImportError:
    pass


def __getattr__(name):
    """惰性加载：只在访问属性时才导入对应模块"""
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        try:
            module = importlib.import_module(module_path)
            return getattr(module, attr_name)
        except ImportError:
            # 尝试从 tools 包直接导入（兼容旧路径）
            alt_path = module_path.replace("tools.", "", 1)
            try:
                module = importlib.import_module(alt_path)
                return getattr(module, attr_name)
            except ImportError:
                raise AttributeError(f"module 'core' has no attribute {name!r}")
    raise AttributeError(f"module 'core' has no attribute {name!r}")


def __dir__():
    return list(_LAZY_IMPORTS.keys())


__all__ = list(_LAZY_IMPORTS.keys())
