# tests/test_active_reply.py
"""
测试 ActiveReplyManager 模块
"""

import asyncio
import os
import sys
from pathlib import Path

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, plugin_dir)

import importlib.util

spec = importlib.util.spec_from_file_location(
    "active_reply_manager", os.path.join(plugin_dir, "core", "active_reply_manager.py")
)
active_reply_manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(active_reply_manager)

ActiveReplyManager = active_reply_manager.ActiveReplyManager
GroupStatus = active_reply_manager.GroupStatus
ReplyDecision = active_reply_manager.ReplyDecision
QueuedMessage = active_reply_manager.QueuedMessage
GroupState = active_reply_manager.GroupState


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
    ar_hard_stop_words = "退下,闭嘴,滚,算了,没事了,不用了"


class MockGroupManager:
    pass


class MockContext:
    async def llm_generate(self, chat_provider_id=None, prompt="", **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(completion_text="mock response")


async def test_basic_functionality():
    print("测试基本功能...")

    config = MockConfig()
    group_manager = MockGroupManager()
    context = MockContext()

    manager = ActiveReplyManager(
        config=config,
        group_manager=group_manager,
        context=context,
        data_dir=Path("/tmp"),
    )

    expected_words = {"退下", "闭嘴", "滚", "算了", "没事了", "不用了"}
    assert manager.hard_stop_words == expected_words
    print("OK - 硬打断词加载正确")

    state = manager._get_group_state("test_group")
    assert state.status == GroupStatus.IDLE
    print("OK - 初始状态为 IDLE")

    manager._activate_attention(state)
    assert state.status == GroupStatus.AWAKE
    assert state.expires_at is not None
    print("OK - 注意力窗口激活成功")

    manager._deactivate_attention(state, "测试")
    assert state.status == GroupStatus.IDLE
    print("OK - 注意力窗口关闭成功")


async def test_name_wakeup():
    print("\n测试名字唤醒...")

    config = MockConfig()
    manager = ActiveReplyManager(
        config=config,
        group_manager=MockGroupManager(),
        context=MockContext(),
        data_dir=Path("/tmp"),
    )

    assert manager._check_name_wakeup("灵笔司书你好", "test_group") == True
    assert manager._check_name_wakeup("司书在吗", "test_group") == True
    assert manager._check_name_wakeup("管家", "test_group") == True
    assert manager._check_name_wakeup("今天天气怎么样", "test_group") == False
    print("OK - 名字唤醒检测正确")


async def test_hard_stop():
    print("\n测试硬打断...")

    config = MockConfig()
    manager = ActiveReplyManager(
        config=config,
        group_manager=MockGroupManager(),
        context=MockContext(),
        data_dir=Path("/tmp"),
    )

    assert manager._check_hard_stop("退下") == True
    assert manager._check_hard_stop("闭嘴") == True
    assert manager._check_hard_stop("算了") == True
    assert manager._check_hard_stop("没事了") == True
    assert manager._check_hard_stop("不用了") == True
    assert manager._check_hard_stop("你好") == False
    print("OK - 硬打断检测正确")


async def main():
    print("=" * 50)
    print("ActiveReplyManager 单元测试")
    print("=" * 50)

    await test_basic_functionality()
    await test_name_wakeup()
    await test_hard_stop()

    print("\n" + "=" * 50)
    print("所有测试通过！")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
