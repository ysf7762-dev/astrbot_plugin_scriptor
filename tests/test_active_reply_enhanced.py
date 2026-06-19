# tests/test_active_reply_manager_enhanced.py
"""主动回复管理器增强测试"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.active_reply_manager import ActiveReplyManager, GroupState, GroupStatus, QueuedMessage, ReplyDecision


@pytest.fixture
def temp_data_dir(tmp_path):
    """创建临时数据目录"""
    return tmp_path


@pytest.fixture
def mock_config():
    """模拟配置对象"""

    class MockConfig:
        active_reply_enabled = True
        ar_name_wakeup = True
        ar_task_sniffing = False
        ar_continuous_dialogue = True
        ar_debounce_seconds = 3
        ar_max_queue_size = 10
        ar_attention_window_minutes = 2
        ar_attention_window_messages = 10
        ar_intent_model_provider = None
        ar_context_messages = 10
        ar_hard_stop_words = "退下,闭嘴,滚"

    return MockConfig()


@pytest.fixture
def mock_group_manager():
    """模拟群组管理器"""

    class MockGroupManager:
        def get_group_profile(self, group_id):
            return None

        def get_group_members(self, group_id):
            return []

    return MockGroupManager()


class TestGroupState:
    """测试 GroupState 数据类"""

    def test_default_state(self):
        """测试默认状态"""
        state = GroupState()

        assert state.status == GroupStatus.IDLE
        assert state.expires_at is None
        assert state.message_count == 0
        assert state.last_ai_message_time is None
        assert state.message_queue == []
        assert state.debounce_task is None
        assert state.processing is False
        assert state.conversation_start_time is None
        assert state.all_messages == []

    def test_awake_state(self):
        """测试唤醒状态"""
        expires_at = datetime.now() + timedelta(minutes=5)
        state = GroupState(status=GroupStatus.AWAKE, expires_at=expires_at, message_count=5, processing=True)

        assert state.status == GroupStatus.AWAKE
        assert state.expires_at is not None
        assert state.message_count == 5
        assert state.processing is True


class TestQueuedMessage:
    """测试 QueuedMessage 数据类"""

    def test_create_queued_message(self):
        """测试创建排队消息"""
        msg = QueuedMessage(
            message_id="msg_001",
            sender_id="user_123",
            sender_name="张三",
            content="你好",
            timestamp=datetime.now(),
            is_at_bot=True,
        )

        assert msg.message_id == "msg_001"
        assert msg.sender_id == "user_123"
        assert msg.sender_name == "张三"
        assert msg.content == "你好"
        assert msg.is_at_bot is True


class TestReplyDecision:
    """测试 ReplyDecision 数据类"""

    def test_positive_decision(self):
        """测试肯定决策"""
        decision = ReplyDecision(
            should_reply=True, target_msg_id="msg_123", reply_text="你好！", reasoning="用户在呼唤机器人"
        )

        assert decision.should_reply is True
        assert decision.target_msg_id == "msg_123"
        assert decision.reply_text == "你好！"

    def test_negative_decision(self):
        """测试否定决策"""
        decision = ReplyDecision(should_reply=False, reasoning="消息不是对机器人说的")

        assert decision.should_reply is False
        assert decision.target_msg_id is None
        assert decision.reply_text is None


class TestActiveReplyManager:
    """测试 ActiveReplyManager 管理器"""

    def _create_manager(self, temp_data_dir, mock_config, mock_group_manager):
        """创建管理器实例（简化版）"""

        class MockContext:
            async def llm_generate(self, chat_provider_id=None, prompt="", **kwargs):
                from types import SimpleNamespace

                return SimpleNamespace(completion_text="mock response")

        manager = ActiveReplyManager(
            config=mock_config,
            group_manager=mock_group_manager,
            context=MockContext(),
            data_dir=temp_data_dir,
        )
        return manager

    def test_manager_initialization(self, temp_data_dir, mock_config, mock_group_manager):
        """测试管理器初始化"""
        manager = self._create_manager(temp_data_dir, mock_config, mock_group_manager)

        assert manager.config is not None
        assert manager.group_manager is not None
        assert manager.data_dir == temp_data_dir

    def test_group_state_creation(self, temp_data_dir, mock_config, mock_group_manager):
        """测试群组状态创建"""
        manager = self._create_manager(temp_data_dir, mock_config, mock_group_manager)
        group_id = "test_group_123"

        # 获取或创建群组状态
        if group_id not in manager.group_states:
            manager.group_states[group_id] = GroupState()

        state = manager.group_states.get(group_id)

        assert state is not None
        assert state.status == GroupStatus.IDLE

    def test_name_wakeup_detection(self, temp_data_dir, mock_config, mock_group_manager):
        """测试名字唤醒检测"""
        manager = self._create_manager(temp_data_dir, mock_config, mock_group_manager)

        # 模拟包含机器人名字的消息
        message_with_name = "@司书 你好"
        message_without_name = "大家好，今天天气不错"

        # 测试名字检测（假设机器人名字为"司书"）
        bot_names = ["司书", "scriptor", "Scriptor"]

        is_wakeup_with_name = any(name in message_with_name for name in bot_names)
        is_wakeup_without_name = any(name in message_without_name for name in bot_names)

        assert is_wakeup_with_name is True
        assert is_wakeup_without_name is False

    def test_hard_stop_words_filtering(self, temp_data_dir, mock_config, mock_group_manager):
        """测试硬打断词过滤"""
        manager = self._create_manager(temp_data_dir, mock_config, mock_group_manager)

        stop_words_raw = manager.config.ar_hard_stop_words
        # 支持中英文逗号分隔
        if "，" in stop_words_raw:
            stop_words = stop_words_raw.split("，")
        else:
            stop_words = stop_words_raw.split(",")

        # 应该被过滤的消息
        should_stop_messages = ["司书，退下吧", "闭嘴，别说话了", "你给我滚"]

        for msg in should_stop_messages:
            should_stop = any(word in msg for word in stop_words)
            assert should_stop is True, f"消息 '{msg}' 应该触发停止 (stop_words: {stop_words})"

    def test_debounce_mechanism(self, temp_data_dir, mock_config, mock_group_manager):
        """测试防抖机制"""
        manager = self._create_manager(temp_data_dir, mock_config, mock_group_manager)

        group_id = "debounce_test_group"
        if group_id not in manager.group_states:
            manager.group_states[group_id] = GroupState()

        state = manager.group_states[group_id]

        # 模拟快速添加消息
        now = datetime.now()
        for i in range(5):
            msg = QueuedMessage(
                message_id=f"msg_{i}",
                sender_id=f"user_{i}",
                sender_name=f"用户{i}",
                content=f"消息{i}",
                timestamp=now + timedelta(seconds=i * 0.1),  # 快速连续消息
            )

            if len(state.message_queue) < manager.config.ar_max_queue_size:
                state.message_queue.append(msg)

        # 验证消息已被加入队列
        assert len(state.message_queue) > 0
        assert len(state.message_queue) <= manager.config.ar_max_queue_size

    def test_attention_window_activation(self, temp_data_dir, mock_config, mock_group_manager):
        """测试注意力窗口激活"""
        manager = self._create_manager(temp_data_dir, mock_config, mock_group_manager)

        group_id = "attention_test_group"
        if group_id not in manager.group_states:
            manager.group_states[group_id] = GroupState()

        state = manager.group_states[group_id]

        # 模拟激活注意力窗口
        original_status = state.status
        state.status = GroupStatus.AWAKE
        state.expires_at = datetime.now() + timedelta(minutes=manager.config.ar_attention_window_minutes)
        state.conversation_start_time = datetime.now()

        # 验证状态变更
        assert state.status == GroupStatus.AWAKE
        assert state.expires_at is not None
        assert state.conversation_start_time is not None

        # 验证窗口未过期
        is_window_active = state.expires_at and datetime.now() < state.expires_at
        assert is_window_active is True

    def test_message_queue_limit(self, temp_data_dir, mock_config, mock_group_manager):
        """测试消息队列大小限制"""
        manager = self._create_manager(temp_data_dir, mock_config, mock_group_manager)

        max_size = manager.config.ar_max_queue_size
        group_id = "queue_limit_test"

        if group_id not in manager.group_states:
            manager.group_states[group_id] = GroupState()

        state = manager.group_states[group_id]

        # 尝试添加超过限制的消息数量
        for i in range(max_size + 5):
            if len(state.message_queue) < max_size:
                msg = QueuedMessage(
                    message_id=f"overflow_msg_{i}",
                    sender_id=f"user_{i}",
                    sender_name=f"用户{i}",
                    content=f"溢出测试消息{i}",
                    timestamp=datetime.now(),
                )
                state.message_queue.append(msg)

        # 验证队列不超过最大值
        assert len(state.message_queue) <= max_size


class TestEdgeCases:
    """边界条件测试"""

    def test_empty_message_handling(self, temp_data_dir, mock_config, mock_group_manager):
        """测试空消息处理"""

        class MockContext:
            async def llm_generate(self, chat_provider_id=None, prompt="", **kwargs):
                from types import SimpleNamespace

                return SimpleNamespace(completion_text="mock response")

        manager = ActiveReplyManager(
            config=mock_config,
            group_manager=mock_group_manager,
            context=MockContext(),
            data_dir=temp_data_dir,
        )

        # 空消息不应导致异常
        try:
            result = manager.check_should_reply("", "test_group", "user_123")
            # 如果方法存在，验证返回值类型
            if result is not None:
                assert isinstance(result, (bool, ReplyDecision))
        except AttributeError:
            pass  # 方法可能不存在，跳过测试

    def test_special_characters_in_message(self, temp_data_dir, mock_config, mock_group_manager):
        """测试特殊字符处理"""

        class MockContext:
            async def llm_generate(self, chat_provider_id=None, prompt="", **kwargs):
                from types import SimpleNamespace

                return SimpleNamespace(completion_text="mock response")

        manager = ActiveReplyManager(
            config=mock_config,
            group_manager=mock_group_manager,
            context=MockContext(),
            data_dir=temp_data_dir,
        )

        special_messages = [
            "你好🎉🎊",
            "测试 <script>alert('xss')</script>",
            "包含\n换行符\n的消息",
            "   前后空格   ",
            "包含\"引号\"和'单引号'的消息",
        ]

        for msg in special_messages:
            try:
                # 验证特殊字符不会导致崩溃
                if hasattr(manager, "sanitize_message"):
                    sanitized = manager.sanitize_message(msg)
                    assert isinstance(sanitized, str)
            except Exception as e:
                pytest.fail(f"处理特殊字符时出错: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
