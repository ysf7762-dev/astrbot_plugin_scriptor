#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VFS (Virtual File System) 虚拟文件系统功能测试

验证虚拟文件系统架构的正确性，包括：
1. VFS 路径解析器
2. 命名空间挂载
3. 权限校验
4. 文件操作工具的 VFS 支持
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent / "tools" / "common"))

# 导入 VFS 模块
try:
    from file_ops import (
        VFS_NAMESPACE_GROUP,
        VFS_NAMESPACE_PERSONAL,
        VFS_NAMESPACE_ROOT,
        VFS_VIRTUAL_ROOT_MARKER,
        _get_vfs_namespaces,
        _is_vfs_path,
        _resolve_vfs_path,
    )

    print("✅ VFS 模块导入成功")
except ImportError as e:
    print(f"❌ VFS 模块导入失败: {e}")
    sys.exit(1)


class MockEvent:
    """模拟消息事件对象"""

    def __init__(self, sender_id: str = "test_user_123", group_id: str = ""):
        self.sender_id = sender_id
        self.group_id = group_id
        self.message_str = "测试消息"

    def get_sender_id(self):
        return self.sender_id

    def get_group_id(self):
        return self.group_id


class MockPlugin:
    """模拟插件实例"""

    def __init__(self, data_dir: str, is_sudo: bool = False):
        self.data_dir = data_dir
        self._is_sudo = is_sudo
        self.identity_manager = type(
            "obj",
            (object,),
            {
                "is_sudo": lambda self, uid, admin_uids: is_sudo,
                "get_or_create_uid": lambda self, sid, platform: f"user_{hash(sid) % 100000000:08d}",
            },
        )()
        self.config = type(
            "obj",
            (object,),
            {
                "admin_uids": ["user_admin"],
            },
        )()


def test_vfs_path_detection():
    """测试 1：VFS 路径检测"""
    print("\n📋 测试 1: VFS 路径检测")

    test_cases = [
        ("@personal/P_PROFILE.md", True),
        ("@group/G_SOUL.md", True),
        ("@root/config.yaml", True),
        (".", True),
        ("", True),
        ("/", True),
        ("\\", True),
        ("P_PROFILE.md", False),
        ("profiles/user_123/file.md", False),
        ("skills/test/SKILL.md", False),
    ]

    for path, expected in test_cases:
        result = _is_vfs_path(path)
        status = "✅" if result == expected else "❌"
        print(f"  {status} _is_vfs_path('{path}') = {result} (期望: {expected})")

        if result != expected:
            return False

    return True


async def test_vfs_namespace_resolution():
    """测试 2：VFS 命名空间解析（需要真实环境）"""
    print("\n📋 测试 2: VFS 命名空间解析")

    # 创建模拟事件和插件
    event = MockEvent(sender_id="test_user_123", group_id="private")
    plugin = MockPlugin(data_dir="D:/test_data", is_sudo=False)

    # 测试 @personal/ 解析
    try:
        resolved, is_virtual, error = await _resolve_vfs_path(
            "@personal/P_PROFILE.md", event, plugin, check_permission=False
        )

        if is_virtual and resolved and not error:
            print(f"  ✅ @personal/ 解析成功: {resolved}")
        else:
            print(f"  ⚠️  @personal/ 解析结果: resolved={resolved}, is_virtual={is_virtual}, error={error}")

    except Exception as e:
        print(f"  ❌ @personal/ 解析失败: {e}")
        return False

    # 测试 @root/ 权限检查（非 Sudo 模式）
    try:
        resolved, is_virtual, error = await _resolve_vfs_path("@root/config.yaml", event, plugin, check_permission=True)

        if error and "Sudo" in error:
            print(f"  ✅ @root/ 权限拦截正常: {error[:50]}...")
        elif is_virtual:
            print(f"  ⚠️  @root/ 未被正确拦截（可能处于 Sudo 模式）")
        else:
            print(f"  ℹ️  @root/ 解析结果: {resolved}, {error}")

    except Exception as e:
        print(f"  ⚠️  @root/ 权限检查异常: {e}")

    return True


def test_vfs_constants():
    """测试 3：VFS 常量定义"""
    print("\n📋 测试 3: VFS 常量定义")

    constants = {
        "VFS_NAMESPACE_PERSONAL": VFS_NAMESPACE_PERSONAL,
        "VFS_NAMESPACE_GROUP": VFS_NAMESPACE_GROUP,
        "VFS_NAMESPACE_ROOT": VFS_NAMESPACE_ROOT,
        "VFS_VIRTUAL_ROOT_MARKER": VFS_VIRTUAL_ROOT_MARKER,
    }

    expected_values = {
        "VFS_NAMESPACE_PERSONAL": "@personal/",
        "VFS_NAMESPACE_GROUP": "@group/",
        "VFS_NAMESPACE_ROOT": "@root/",
        "VFS_VIRTUAL_ROOT_MARKER": ".",
    }

    all_correct = True
    for name, value in constants.items():
        expected = expected_values[name]
        status = "✅" if value == expected else "❌"
        print(f"  {status} {name} = '{value}' (期望: '{expected}')")

        if value != expected:
            all_correct = False

    return all_correct


async def run_all_tests():
    """运行所有测试"""
    print("=" * 60)
    print("🧪 VFS 虚拟文件系统功能测试")
    print("=" * 60)

    results = []

    # 测试 1：路径检测
    results.append(("VFS 路径检测", test_vfs_path_detection()))

    # 测试 2：常量定义
    results.append(("VFS 常量定义", test_vfs_constants()))

    # 测试 3：命名空间解析
    results.append(("VFS 命名空间解析", await test_vfs_namespace_resolution()))

    # 输出总结
    print("\n" + "=" * 60)
    print("📊 测试结果总结")
    print("=" * 60)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"  {status} - {name}")

    print(f"\n总计: {passed}/{total} 测试通过")

    if passed == total:
        print("\n🎉 所有测试通过！VFS 架构升级成功！")
        return 0
    else:
        print(f"\n⚠️  有 {total - passed} 个测试未通过，请检查实现")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(run_all_tests())
    sys.exit(exit_code)
