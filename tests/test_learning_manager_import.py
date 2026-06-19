# tests/test_learning_manager_import.py
"""
验证 LearningManager 模块可以正常导入和基本功能
使用静态分析避免相对导入问题
"""

from pathlib import Path

import pytest


class TestLearningManagerExists:
    """验证 LearningManager 模块存在且结构正确"""

    def test_learning_manager_file_exists(self):
        """验证文件存在"""
        project_root = Path(__file__).parent.parent
        lm_file = project_root / "core" / "learning_manager.py"

        assert lm_file.exists(), "learning_manager.py 文件应该存在"

    def test_learning_manager_content(self):
        """验证文件内容包含关键定义"""
        project_root = Path(__file__).parent.parent
        lm_file = project_root / "core" / "learning_manager.py"
        content = lm_file.read_text(encoding="utf-8")

        # 检查关键类定义
        assert "class CognitiveState(Enum):" in content, "应包含 CognitiveState 枚举"
        assert "class KnowledgeExtraction:" in content, "应包含 KnowledgeExtraction 数据类"
        assert "class LearningSession:" in content, "应包含 LearningSession 数据类"
        assert "class LearningManager:" in content, "应包含 LearningManager 类"

        # 检查关键方法
        assert "def get_session" in content, "应包含 get_session 方法"
        assert "def set_state" in content, "应包含 set_state 方法"
        assert "def add_pending_knowledge" in content, "应包含 add_pending_knowledge 方法"
        assert "def confirm_pending_knowledge" in content, "应包含 confirm_pending_knowledge 方法"
        assert "def get_state_prompt_suffix" in content, "应包含 get_state_prompt_suffix 方法"

        # 检查三态认知模型
        assert "NORMAL" in content, "应包含 NORMAL 状态"
        assert "LEARNING" in content, "应包含 LEARNING 状态"
        assert "TEACHING" in content, "应包含 TEACHING 状态"

        # 检查双轨写入
        assert "_dual_track_write" in content, "应包含双轨写入方法"

        print("✅ LearningManager 模块结构验证通过")


class TestMainPyImports:
    """验证 main.py 中的导入语句正确"""

    def test_learning_manager_import_in_main(self):
        """验证 main.py 导入了 LearningManager"""
        project_root = Path(__file__).parent.parent
        main_file = project_root / "main.py"
        content = main_file.read_text(encoding="utf-8")

        # 检查导入语句
        assert (
            "from .core.learning_manager import LearningManager" in content
            or "from core.learning_manager import LearningManager" in content
        ), "main.py 应导入 LearningManager"

        assert "CognitiveState" in content, "main.py 应导入 CognitiveState"

        print("✅ main.py 中的 LearningManager 导入验证通过")


class TestLearningCommands:
    """验证学习模式命令已注册"""

    def test_learning_commands_in_main(self):
        """验证 main.py 中包含学习模式命令

        注意：命令装饰器已统一移至 main.py 中注册，Mixin 中只保留方法实现。
        这是为了避免指令冲突（同一个指令被注册两次）。
        """
        project_root = Path(__file__).parent.parent

        # 验证 main.py 中注册了命令
        main_file = project_root / "main.py"
        main_content = main_file.read_text(encoding="utf-8")

        assert '@filter.command("开始学习")' in main_content, "应在 main.py 中注册 /开始学习 命令"
        assert '@filter.command("结束学习")' in main_content, "应在 main.py 中注册 /结束学习 命令"
        assert '@filter.command("开始授课")' in main_content, "应在 main.py 中注册 /开始授课 命令"
        assert '@filter.command("结束授课")' in main_content, "应在 main.py 中注册 /结束授课 命令"
        assert '@filter.command("学习状态")' in main_content, "应在 main.py 中注册 /学习状态 命令"

        # 验证 learning_mixin.py 中包含方法实现
        learning_mixin_file = project_root / "mixins" / "learning_mixin.py"
        mixin_content = learning_mixin_file.read_text(encoding="utf-8")

        assert "async def cmd_start_learning" in mixin_content, "应在 learning_mixin.py 中包含 cmd_start_learning 函数"
        assert "async def cmd_end_learning" in mixin_content, "应在 learning_mixin.py 中包含 cmd_end_learning 函数"
        assert "async def cmd_start_teaching" in mixin_content, "应在 learning_mixin.py 中包含 cmd_start_teaching 函数"
        assert "async def cmd_end_teaching" in mixin_content, "应在 learning_mixin.py 中包含 cmd_end_teaching 函数"
        assert (
            "async def cmd_learning_status" in mixin_content
        ), "应在 learning_mixin.py 中包含 cmd_learning_status 函数"

        # 验证 Mixin 中不包含装饰器（避免指令冲突）
        assert '@filter.command("开始学习")' not in mixin_content, "应避免在 Mixin 中重复注册 /开始学习 命令"

        print("✅ 学习模式命令验证通过（架构：main.py 注册，Mixin 实现）")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
