"""
Scriptor 权限管理系统测试

测试细粒度权限控制功能
"""

import pytest

# 使用包导入方式，兼容相对导入
# 使用直接导入方式（通过 conftest.py 设置的 sys.path）
try:
    from core.permission_manager import Permission, PermissionManager, Role, get_permission_manager
except ImportError:
    from permission_manager import Permission, PermissionManager, Role, get_permission_manager


class TestPermissionManager:
    """权限管理器测试"""

    @pytest.fixture
    def perm_manager(self):
        """创建权限管理器实例"""
        manager = PermissionManager()
        manager._admin_uids.clear()
        manager._user_permissions.clear()
        return manager

    def test_admin_uids(self, perm_manager):
        """测试管理员设置"""
        perm_manager.set_admin_uids(["admin1", "admin2"])

        assert perm_manager.is_admin("admin1")
        assert perm_manager.is_admin("admin2")
        assert not perm_manager.is_admin("normal_user")

    def test_add_remove_admin(self, perm_manager):
        """测试添加移除管理员"""
        perm_manager.add_admin("new_admin")
        assert perm_manager.is_admin("new_admin")

        perm_manager.remove_admin("new_admin")
        assert not perm_manager.is_admin("new_admin")

    def test_get_user_role_admin(self, perm_manager):
        """测试管理员角色"""
        perm_manager.add_admin("admin_user")
        assert perm_manager.get_user_role("admin_user") == Role.ADMIN

    def test_get_user_role_member(self, perm_manager):
        """测试成员角色"""
        role = perm_manager.get_user_role("user", group_role="member")
        assert role == Role.MEMBER

    def test_get_user_role_owner(self, perm_manager):
        """测试所有者角色"""
        role = perm_manager.get_user_role("owner", group_role="owner")
        assert role == Role.OWNER

    def test_get_user_role_default(self, perm_manager):
        """测试默认角色"""
        role = perm_manager.get_user_role("normal_user")
        assert role == Role.USER

    def test_check_permission_admin(self, perm_manager):
        """测试管理员权限检查"""
        perm_manager.add_admin("admin")

        assert perm_manager.check_permission("admin", Permission.DEBUG)
        assert perm_manager.check_permission("admin", Permission.DELETE)
        assert perm_manager.check_permission("admin", Permission.BACKUP)

    def test_check_permission_user(self, perm_manager):
        """测试普通用户权限检查"""
        assert perm_manager.check_permission("user", Permission.VIEW)
        assert perm_manager.check_permission("user", Permission.SEARCH)
        assert perm_manager.check_permission("user", Permission.RECORD)

    def test_check_permission_guest(self, perm_manager):
        """测试访客权限检查"""
        assert perm_manager.check_permission("guest", Permission.VIEW)

    def test_grant_permission(self, perm_manager):
        """测试授予权限"""
        perm_manager.grant_permission("special_user", Permission.DEBUG)

        assert perm_manager.check_permission("special_user", Permission.DEBUG)

    def test_revoke_permission(self, perm_manager):
        """测试撤销权限"""
        perm_manager.grant_permission("user", Permission.DEBUG)
        perm_manager.revoke_permission("user", Permission.DEBUG)

        assert not perm_manager.check_permission("user", Permission.DEBUG)

    def test_get_user_permissions(self, perm_manager):
        """测试获取用户权限列表"""
        permissions = perm_manager.get_user_permissions("test_user")

        assert Permission.VIEW in permissions

    def test_can_access_memory_private_own(self, perm_manager):
        """测试访问自己的私有记忆"""
        assert perm_manager.can_access_memory("user1", "private", "user1")

    def test_can_access_memory_private_other(self, perm_manager):
        """测试访问他人的私有记忆"""
        assert not perm_manager.can_access_memory("user1", "private", "user2")

    def test_can_access_memory_global(self, perm_manager):
        """测试访问全局记忆"""
        assert perm_manager.can_access_memory("any_user", "global", "any_scope")

    def test_can_access_memory_group(self, perm_manager):
        """测试访问群组记忆"""
        assert perm_manager.can_access_memory("user1", "group", "user1", group_id="user1")
        assert not perm_manager.can_access_memory("user2", "group", "user1", group_id="user2")

    def test_can_access_memory_admin(self, perm_manager):
        """测试管理员可以访问所有记忆"""
        perm_manager.add_admin("admin")

        assert perm_manager.can_access_memory("admin", "private", "other_user")


class TestPermissionEnum:
    """权限枚举测试"""

    def test_permission_values(self):
        """测试权限枚举值"""
        assert Permission.VIEW.value == "view"
        assert Permission.SEARCH.value == "search"
        assert Permission.DEBUG.value == "debug"
        assert Permission.DELETE.value == "delete"

    def test_role_values(self):
        """测试角色枚举值"""
        assert Role.GUEST.value == "guest"
        assert Role.USER.value == "user"
        assert Role.ADMIN.value == "admin"


class TestGlobalFunctions:
    """全局函数测试"""

    def test_get_permission_manager(self):
        """测试获取权限管理器单例"""
        manager1 = get_permission_manager()
        manager2 = get_permission_manager()

        assert manager1 is manager2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
