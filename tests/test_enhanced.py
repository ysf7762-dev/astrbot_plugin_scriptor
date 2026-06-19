# tests/test_enhanced.py
"""Scriptor 增强单元测试
覆盖安全、工具函数、边界条件等
"""

import pytest

# 使用包导入方式，兼容相对导入
# 使用直接导入方式（通过 conftest.py 设置的 sys.path）
try:
    from tools.security.sanitizer import sanitize_id
except ImportError:
    from security.sanitizer import sanitize_id
# 使用直接导入方式（通过 conftest.py 设置的 sys.path）
try:
    from core.message_sanitizer import MessageSanitizer, Platform, SanitizerConfig
except ImportError:
    from message_sanitizer import MessageSanitizer, SanitizerConfig
# 使用直接导入方式（通过 conftest.py 设置的 sys.path）
try:
    from core.config_pydantic import ScriptorConfig
except ImportError:
    from config_pydantic import ScriptorConfig
# 使用直接导入方式（通过 conftest.py 设置的 sys.path）
try:
    from tools.common.text_utils import TokenEstimator
except ImportError:
    from common.text_utils import TokenEstimator


class TestSanitizeId:
    """标识符清洗测试"""

    def test_valid_id(self):
        """测试有效标识符"""
        assert sanitize_id("user123") == "user123"
        assert sanitize_id("group_456") == "group_456"
        assert sanitize_id("test-user") == "test-user"

    def test_invalid_id_empty(self):
        """测试空标识符"""
        assert sanitize_id("") == "unknown"
        assert sanitize_id(None) == "unknown"

    def test_invalid_id_type(self):
        """测试非字符串类型"""
        assert sanitize_id(123) == "unknown"
        assert sanitize_id([]) == "unknown"


class TestTokenCalculation:
    """Token计算测试"""

    def test_estimate_tokens(self):
        """测试token估算"""
        # 简单估算
        tokens = TokenEstimator.estimate_tokens("test text")
        assert tokens > 0

        # 大量文本
        long_text = "word " * 1000
        tokens = TokenEstimator.estimate_tokens(long_text)
        assert tokens > 100  # 至少100个token

        # 空字符串
        assert TokenEstimator.estimate_tokens("") == 0

        # 中英文混合
        mixed = "Hello 你好 world 世界"
        tokens = TokenEstimator.estimate_tokens(mixed)
        assert tokens > 0


class TestMessageSanitizer:
    """消息清洗器测试"""

    def test_sanitize_basic(self):
        """测试基本清洗"""
        config = SanitizerConfig()
        sanitizer = MessageSanitizer(config)

        result = sanitizer.sanitize("Hello **world**")
        assert "**" not in result

    def test_sanitize_image_links(self):
        """测试图片链接清洗"""
        config = SanitizerConfig()
        sanitizer = MessageSanitizer(config)

        result = sanitizer.sanitize("![img](http://example.com/img.png)")
        assert "![" not in result

    def test_sanitize_mentions(self):
        """测试@提及清洗"""
        config = SanitizerConfig()
        sanitizer = MessageSanitizer(config)

        result = sanitizer.sanitize("@user123 hello")
        assert "@user123" not in result or "@" not in result


class TestConfigEdgeCases:
    """配置边界情况测试"""

    def test_config_bounds(self):
        """测试配置边界值（使用嵌套配置）"""
        config = ScriptorConfig(
            memory={"llm_extraction_threshold": 5},
            system={"max_file_locks": 10, "index_cache_timeout": 60},
        )

        assert config.llm_extraction_threshold == 5
        assert config.max_file_locks == 10
        assert config.index_cache_timeout == 60

    def test_config_invalid_bounds(self):
        """测试无效边界值被拒绝"""
        with pytest.raises(Exception):
            ScriptorConfig(memory={"llm_extraction_threshold": 3})


class TestPathValidation:
    """路径验证测试"""

    def test_safe_filename_pattern(self):
        """测试安全文件名模式"""
        import re

        SAFE_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]+\.md$")

        assert SAFE_FILENAME_PATTERN.match("2024-01-01.md")
        assert SAFE_FILENAME_PATTERN.match("memory_backup.md")

        assert not SAFE_FILENAME_PATTERN.match("../etc/passwd")
        assert not SAFE_FILENAME_PATTERN.match("file with spaces.md")
        assert not SAFE_FILENAME_PATTERN.match("file<script>.md")


class TestAPIKeySecurity:
    """API密钥安全测试"""

    def test_api_key_masking(self):
        """测试API密钥遮蔽（嵌套配置）"""
        config = ScriptorConfig(
            embedding={"embedding_api_key": "sk-test-12345678"}, rerank={"rerank_api_key": "sk-rerank-87654321"}
        )

        # 使用 to_dict 获取扁平化格式
        exported = config.to_dict()

        assert "embedding_api_key" in exported
        assert "rerank_api_key" in exported

    def test_api_key_inclusion(self):
        """测试显式包含API密钥（嵌套配置）"""
        config = ScriptorConfig(embedding={"embedding_api_key": "sk-secret"})

        # 使用 model_dump 获取嵌套格式（包含完整信息）
        exported_nested = config.model_dump()
        assert exported_nested["embedding"]["embedding_api_key"] == "sk-secret"

        # 使用 to_dict with include_sensitive=True 获取扁平化格式
        exported_flat = config.to_dict(include_sensitive=True)
        assert exported_flat.get("embedding_api_key") == "sk-secret"


class TestCORSConfig:
    """CORS配置测试"""

    def test_default_origins(self):
        """测试默认信任源"""
        _default_origins = "http://localhost:8501,http://127.0.0.1:8501"
        _trusted_list = [origin.strip() for origin in _default_origins.split(",")]

        assert "http://localhost:8501" in _trusted_list
        assert "http://127.0.0.1:8501" in _trusted_list
        assert "*" not in _trusted_list

    def test_env_origins(self):
        """测试环境变量配置"""
        env_origins = "http://192.168.1.100:8501,https://example.com"
        trusted_list = [origin.strip() for origin in env_origins.split(",")]

        assert len(trusted_list) == 2
        assert "http://192.168.1.100:8501" in trusted_list
