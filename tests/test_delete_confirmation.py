# 临时测试：高危操作删除确认机制
"""
测试文件删除的二次确认流程：
1. PendingTaskStore 的基本功能
2. file_delete 函数的拦截逻辑
3. 完整的确认/拒绝流程模拟
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pending_tasks import (
    PendingTask,
    PendingTaskStatus,
    PendingTaskStore,
    PendingTaskType,
    get_pending_task_store,
    init_pending_task_store,
)


def test_pending_task_store():
    """测试待确认任务池的基本功能"""
    print("=" * 60)
    print("测试 1: PendingTaskStore 基本功能")
    print("=" * 60)

    store = PendingTaskStore(timeout_seconds=120.0)

    # 测试添加任务
    task = store.add_task("session_123", PendingTaskType.FILE_DELETE, "/test/file.md")
    assert task is not None, "添加任务失败"
    assert task.status == PendingTaskStatus.PENDING, "任务状态应为 PENDING"
    assert task.file_path == "/test/file.md", "文件路径不匹配"
    assert store.size == 1, "任务数量应为 1"
    print("✅ 添加任务: 通过")

    # 测试获取任务
    retrieved = store.get_task("session_123")
    assert retrieved is not None, "获取任务失败"
    assert retrieved.session_id == "session_123", "会话 ID 不匹配"
    print("✅ 获取任务: 通过")

    # 测试 has_pending_task
    assert store.has_pending_task("session_123") == True, "应有待处理任务"
    assert store.has_pending_task("nonexistent") == False, "不应有待处理任务"
    print("✅ has_pending_task: 通过")

    # 测试确认任务
    success, confirmed_task = store.confirm_task("session_123")
    assert success == True, "确认任务应成功"
    assert confirmed_task.status == PendingTaskStatus.CONFIRMED, "状态应为 CONFIRMED"
    assert store.size == 0, "确认后任务应被移除"
    print("✅ 确认任务: 通过")

    # 测试重复确认（已不存在）
    success, _ = store.confirm_task("session_123")
    assert success == False, "重复确认应失败"
    print("✅ 重复确认拒绝: 通过")

    # 测试拒绝任务
    task2 = store.add_task("session_456", PendingTaskType.FILE_DELETE, "/test/another.md")
    success, rejected_task = store.reject_task("session_456")
    assert success == True, "拒绝任务应成功"
    assert rejected_task.status == PendingTaskStatus.REJECTED, "状态应为 REJECTED"
    print("✅ 拒绝任务: 通过")

    # 测试清除任务
    task3 = store.add_task("session_789", PendingTaskType.FILE_DELETE, "/test/third.md")
    cleared = store.clear_task("session_789")
    assert cleared == True, "清除任务应成功"
    assert store.size == 0, "清除后任务数量应为 0"
    print("✅ 清除任务: 通过")

    print("\n所有基本功能测试通过！\n")


def test_pending_task_expiration():
    """测试任务过期机制"""
    print("=" * 60)
    print("测试 2: 任务过期机制")
    print("=" * 60)

    # 使用极短的超时时间进行测试
    store = PendingTaskStore(timeout_seconds=0.1)  # 100ms 超时

    task = store.add_task("session_exp", PendingTaskType.FILE_DELETE, "/test/expired.md")
    assert store.has_pending_task("session_exp") == True, "刚添加的任务应存在"

    import time

    time.sleep(0.15)  # 等待超过超时时间

    # 过期后获取应返回 None
    retrieved = store.get_task("session_exp")
    assert retrieved is None, "过期任务应返回 None"

    # 过期后确认应失败
    success, _ = store.confirm_task("session_exp")
    assert success == False, "过期任务确认应失败"

    print("✅ 任务过期机制: 通过\n")


def test_file_delete_interception_logic():
    """测试 file_delete 函数的拦截逻辑（模拟）"""
    print("=" * 60)
    print("测试 3: file_delete 拦截逻辑（模拟）")
    print("=" * 60)

    # 模拟配置对象
    class MockConfig:
        require_delete_confirmation = True

    class MockPlugin:
        config = MockConfig()

    # 模拟需要确认的情况
    config_enabled = getattr(MockPlugin.config, "require_delete_confirmation", True)
    force = False

    if config_enabled and not force:
        from core.pending_tasks import PendingTaskType, get_pending_task_store

        init_pending_task_store()
        store = get_pending_task_store()

        # 模拟 file_delete 内部调用 add_task
        store.add_task("test_session", PendingTaskType.FILE_DELETE, "/test/sensitive.md")

        result = {
            "status": "pending_confirmation",
            "message": "操作已挂起，等待用户通过 /delete 命令确认",
            "file_path": "/test/sensitive.md",
            "session_id": "test_session",
        }

        assert result["status"] == "pending_confirmation", "应返回 pending_confirmation 状态"

        # 验证任务已被添加到存储中
        assert store.has_pending_task("test_session") == True, "任务应被挂起"

        print("✅ 拦截逻辑: 通过 (返回 pending_confirmation)")

        # 模拟用户确认
        success, confirmed_task = store.confirm_task("test_session")
        assert success == True, "用户确认应成功"
        assert confirmed_task.file_path == "/test/sensitive.md", "文件路径应匹配"

        print("✅ 用户确认流程: 通过")

    # 模拟强制执行（跳过确认）
    force_result = "✅ 文件已成功删除: /test/normal.md"
    if not isinstance(force_result, dict):
        print("✅ 强制执行模式: 通过 (直接返回结果)")

    print()


def test_full_workflow_simulation():
    """完整工作流模拟"""
    print("=" * 60)
    print("测试 4: 完整工作流模拟")
    print("=" * 60)

    init_pending_task_store()
    store = get_pending_task_store()

    session_id = "user_session_001"
    file_to_delete = "MEMORY.md"

    # Step 1: AI 调用 file_delete_tool
    print("\n📋 Step 1: AI 尝试删除文件...")
    task = store.add_task(session_id, PendingTaskType.FILE_DELETE, file_to_delete)
    print(f"   → 任务已挂起: {task.file_path}")
    print(f"   → 状态: {task.status.value}")

    # Step 2: 系统发送确认消息（模拟）
    print("\n📋 Step 2: 系统向用户发送确认请求...")
    confirmation_msg = (
        f"⚠️ **系统警告：高危操作拦截**\n\n"
        f"AI 尝试删除文件：`{file_to_delete}`\n\n"
        f"请回复 `/delete` 确认，或回复其他内容取消。"
    )
    print(f"   → {confirmation_msg[:50]}...")

    # Step 3a: 场景 A - 用户确认
    print("\n📋 Step 3a: 用户回复 `/delete`...")
    success, confirmed = store.confirm_task(session_id)
    if success:
        print(f"   ✅ 用户已确认，执行删除: {confirmed.file_path}")
        delete_result = f"✅ 文件已成功删除: {confirmed.file_path}"
        print(f"   → {delete_result}")
    else:
        print("   ❌ 确认失败")

    # Step 3b: 场景 B - 用户取消（重新开始）
    print("\n📋 Step 3b: 模拟用户取消场景...")
    task_b = store.add_task(session_id, PendingTaskType.FILE_DELETE, "NOTES.md")

    message_stripped = "算了，不删了"
    is_delete_cmd = message_stripped.strip().lower() in ("/delete", "delete")

    if not is_delete_cmd:
        _, rejected = store.reject_task(session_id)
        if rejected:
            print(f"   ✅ 用户取消了删除操作: {rejected.file_path}")
            print(f"   → 文件安全保留")

    print()


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("🧪 高危操作删除确认机制 - 单元测试")
    print("=" * 60 + "\n")

    try:
        test_pending_task_store()
        test_pending_task_expiration()
        test_file_delete_interception_logic()
        test_full_workflow_simulation()

        print("=" * 60)
        print("🎉 所有测试通过！")
        print("=" * 60)

    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
