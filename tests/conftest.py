# tests/conftest.py
"""
pytest 配置文件
用于设置测试环境，解决模块导入问题
"""

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── Mock astrbot 模块（必须在任何其他导入之前） ─
# CI 环境中没有安装 astrbot，需要创建 mock 模块
_astrbot = types.ModuleType("astrbot")
_astrbot_api = types.ModuleType("astrbot.api")
_astrbot_api.logger = MagicMock()
_astrbot_api.provider = types.ModuleType("astrbot.api.provider")
_astrbot_api.provider.Provider = MagicMock()
_astrbot_api.star = types.ModuleType("astrbot.api.star")
_astrbot_api.star.Context = MagicMock()
_astrbot_api.star.Star = MagicMock()
_astrbot_api.star.register = MagicMock()
_astrbot_api.star.filter = MagicMock()
_astrbot_api.star.Context = MagicMock()
_astrbot_api.event = types.ModuleType("astrbot.api.event")
_astrbot_api.event.filter = MagicMock()
_astrbot_api.event.EventMessageType = MagicMock()
_astrbot_api.event.EventMessageType.ALL = "all"
_astrbot_api.platform = types.ModuleType("astrbot.api.platform")
_astrbot_api.platform.Platform = MagicMock()
_astrbot_api.message = types.ModuleType("astrbot.api.message")
_astrbot_api.message.MessageChain = MagicMock()
_astrbot_api.message.Plain = MagicMock()
_astrbot_api.message.Image = MagicMock()
_astrbot_api.message.At = MagicMock()
_astrbot_api.message.AtAll = MagicMock()
_astrbot_api.message.Forward = MagicMock()
_astrbot_api.message.ForwardMessageNode = MagicMock()
_astrbot_api.message.MessageEvent = MagicMock()
_astrbot_api.message.GroupMessageEvent = MagicMock()
_astrbot_api.message.FriendMessageEvent = MagicMock()
_astrbot_api.message.Group = MagicMock()
_astrbot_api.message.Member = MagicMock()
_astrbot_api.message.Person = MagicMock()
_astrbot_api.message.Self = MagicMock()
_astrbot_api.star_tools = types.ModuleType("astrbot.api.star_tools")
_astrbot_api.star_tools.StarTools = MagicMock()
_astrbot_api.provider.llm_tools = types.ModuleType("astrbot.api.provider.llm_tools")
_astrbot_api.provider.llm_tools.func_tool = MagicMock()
_astrbot_api.provider.llm_tools.FuncTool = MagicMock()
_astrbot_api.provider.llm_tools.BaseLLMTool = MagicMock()
_astrbot_api.provider.llm_tools.ToolParam = MagicMock()
_astrbot_api.provider.llm_tools.ToolParamType = MagicMock()
_astrbot_api.provider.llm_tools.ToolReturn = MagicMock()
_astrbot_api.provider.llm_tools.LLMResponse = MagicMock()
_astrbot_api.provider.llm_tools.LLMResponse.result = ""
_astrbot_api.provider.llm_tools.LLMResponse.role = "assistant"
_astrbot_api.provider.llm_tools.LLMResponse.type = "assistant"
_astrbot_api.provider.llm_tools.LLMResponse._metadata = {}
_astrbot_api.provider.llm_tools.LLMResponse.metadata = MagicMock()
_astrbot_api.provider.llm_tools.LLMResponse.metadata.get = MagicMock(return_value=None)
_astrbot_api.provider.llm_tools.LLMResponse.metadata.__getitem__ = MagicMock(side_effect=KeyError)
_astrbot_api.provider.llm_tools.LLMResponse.metadata.__contains__ = MagicMock(return_value=False)
_astrbot_api.provider.llm_tools.LLMResponse.metadata.keys = MagicMock(return_value=[])
_astrbot_api.provider.llm_tools.LLMResponse.metadata.values = MagicMock(return_value=[])
_astrbot_api.provider.llm_tools.LLMResponse.metadata.items = MagicMock(return_value=[])

_astrbot.api = _astrbot_api
_astrbot_api.__package__ = "astrbot.api"
_astrbot_api.provider.__package__ = "astrbot.api.provider"
_astrbot_api.star.__package__ = "astrbot.api.star"
_astrbot_api.event.__package__ = "astrbot.api.event"
_astrbot_api.platform.__package__ = "astrbot.api.platform"
_astrbot_api.message.__package__ = "astrbot.api.message"
_astrbot_api.star_tools.__package__ = "astrbot.api.star_tools"
_astrbot_api.provider.llm_tools.__package__ = "astrbot.api.provider.llm_tools"

sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _astrbot_api
sys.modules["astrbot.api.provider"] = _astrbot_api.provider
sys.modules["astrbot.api.star"] = _astrbot_api.star
sys.modules["astrbot.api.event"] = _astrbot_api.event
sys.modules["astrbot.api.platform"] = _astrbot_api.platform
sys.modules["astrbot.api.message"] = _astrbot_api.message
sys.modules["astrbot.api.star_tools"] = _astrbot_api.star_tools
sys.modules["astrbot.api.provider.llm_tools"] = _astrbot_api.provider.llm_tools

# 项目根目录
project_root = Path(__file__).parent.parent
core_dir = project_root / "core"
tools_dir = project_root / "tools"

# 在 pytest 启动时设置 sys.path（优先级最高）
# 确保项目根目录在最前面，这样可以直接导入 core.xxx
sys.path.insert(0, str(project_root))

# 同时也添加 core 和 tools 目录，支持直接导入模块
if str(core_dir) not in sys.path:
    sys.path.insert(1, str(core_dir))
if str(tools_dir) not in sys.path:
    sys.path.insert(2, str(tools_dir))

# 设置环境变量
os.environ.setdefault("SCRIPT_OR_TEST_MODE", "1")


def pytest_configure(config):
    config.addinivalue_line("markers", "unit: 单元测试（快速执行）")
    config.addinivalue_line("markers", "integration: 集成测试（较慢）")
    config.addinivalue_line("markers", "slow: 慢速测试")


def pytest_collection_modifyitems(items):
    """在收集测试后处理每个测试文件"""

    # 需要确保 core 模块能够正确导入
    # 清理可能导致问题的缓存模块
    modules_to_remove = [
        key
        for key in sys.modules.keys()
        if key.startswith("core.") or key == "core" or key == "astrbot_plugin_scriptor"
    ]
    for mod in modules_to_remove:
        if mod in sys.modules:
            del sys.modules[mod]


@pytest.fixture(scope="session", autouse=True)
def setup_syspath():
    """确保 sys.path 在所有测试前正确设置"""
    return sys.path[:]
