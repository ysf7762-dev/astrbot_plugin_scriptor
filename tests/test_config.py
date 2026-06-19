# tests/test_config.py
"""Scriptor 配置模块测试"""

import pytest

# 使用直接导入方式（通过 conftest.py 设置的 sys.path）
try:
    from core.config_pydantic import ScriptorConfig, ScriptorConfigPydantic
except ImportError:
    from config_pydantic import ScriptorConfig


class TestScriptorConfig:
    """ScriptorConfig 配置类测试"""

    def test_default_config(self):
        """测试默认配置"""
        config = ScriptorConfig()

        # 注意：memory_compact_threshold 默认值为 50000（50KB）
        assert config.memory_compact_threshold == 50000
        assert config.daily_note_enabled is True
        assert config.cross_group_enabled is True
        assert config.embedding_enabled is True
        assert config.search_top_k == 5
        assert config.embedding_provider == "local"
        assert config.embedding_api_base == "http://localhost:11434/v1"
        assert config.embedding_api_key is None or config.embedding_api_key == ""
        assert config.embedding_model == "AI-ModelScope/bge-small-zh-v1.5"
        assert config.rerank_enabled is False
        assert config.rerank_provider == "api"
        assert config.rerank_top_k == 5

    def test_custom_config(self):
        """测试自定义配置（使用向后兼容的扁平格式）"""
        custom_config = {
            "memory_compact_threshold": 10000,
            "daily_note_enabled": False,
            "embedding_enabled": False,
            "search_top_k": 10,
            "embedding_provider": "api",
            "rerank_enabled": True,
            "rerank_top_k": 3,
        }

        # 使用 load_from_flat_dict 方法加载扁平化配置（向后兼容）
        config = ScriptorConfig.load_from_flat_dict(custom_config)

        assert config.memory_compact_threshold == 10000
        assert config.daily_note_enabled is False
        assert config.embedding_enabled is False
        assert config.search_top_k == 10
        assert config.embedding_provider == "api"
        assert config.rerank_enabled is True
        assert config.rerank_top_k == 3

    def test_to_dict(self):
        """测试配置转换为字典"""
        config = ScriptorConfig()
        config_dict = config.to_dict()

        assert isinstance(config_dict, dict)
        assert "memory_compact_threshold" in config_dict
        assert "daily_note_enabled" in config_dict
        assert "embedding_enabled" in config_dict
        assert "search_top_k" in config_dict
        assert "embedding_provider" in config_dict
        assert "rerank_enabled" in config_dict
        assert "rerank_top_k" in config_dict

    def test_to_dict_sensitive(self):
        """测试敏感信息脱敏"""
        config = ScriptorConfig.load_from_flat_dict(
            {"embedding_api_key": "secret_key_123", "rerank_api_key": "rerank_secret_456"}
        )

        # 默认不包含敏感信息（扁平化格式）
        config_dict = config.to_dict(include_sensitive=False)
        assert config_dict.get("embedding_api_key") == "***"
        assert config_dict.get("rerank_api_key") == "***"

        # 显式包含敏感信息
        config_dict_sensitive = config.to_dict(include_sensitive=True)
        assert config_dict_sensitive.get("embedding_api_key") == "secret_key_123"
        assert config_dict_sensitive.get("rerank_api_key") == "rerank_secret_456"

    def test_load_from_file(self, tmp_path):
        """测试从文件加载配置"""
        import json

        config_file = tmp_path / "config.json"

        test_config = {"scriptor": {"memory_compact_threshold": 12345, "search_top_k": 7}}

        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(test_config, f)

        config = ScriptorConfig.load_from_file(config_file)

        assert config.memory_compact_threshold == 12345
        assert config.search_top_k == 7

    def test_performance_tuning_params(self):
        """测试性能调优参数"""
        config = ScriptorConfig()

        assert config.llm_extraction_threshold == 10
        assert config.max_file_locks == 100
        assert config.index_cache_timeout == 300

    def test_custom_performance_params(self):
        """测试自定义性能参数"""
        custom_config = {
            "llm_extraction_threshold": 20,
            "max_file_locks": 200,
            "index_cache_timeout": 600,
        }

        config = ScriptorConfig.load_from_flat_dict(custom_config)

        assert config.llm_extraction_threshold == 20
        assert config.max_file_locks == 200
        assert config.index_cache_timeout == 600

    def test_pydantic_validation(self):
        """测试Pydantic验证（宽容策略：非法值自动回退到默认值，不抛异常）"""
        # 测试 embedding_provider 验证：非法值自动回退为 "local"
        config = ScriptorConfig.load_from_flat_dict({"embedding_provider": "invalid"})
        assert config.embedding.embedding_provider == "local"

        # 测试 rerank_provider 验证：非法值自动回退为 "api"
        config = ScriptorConfig.load_from_flat_dict({"rerank_provider": "invalid"})
        assert config.rerank.rerank_provider == "api"

        # 测试 rerank_top_k > search_top_k 验证（这个仍然抛异常，因为是逻辑一致性检查）
        with pytest.raises(Exception):
            ScriptorConfig.load_from_flat_dict({"search_top_k": 5, "rerank_top_k": 10, "rerank_enabled": True})

    def test_pydantic_schema(self):
        """测试Pydantic Schema生成（嵌套结构）"""
        schema = ScriptorConfig.get_schema()

        assert isinstance(schema, dict)
        assert "properties" in schema
        # 新版本使用嵌套配置，检查嵌套字段是否存在
        assert "memory" in schema["properties"]
        assert "embedding" in schema["properties"]
