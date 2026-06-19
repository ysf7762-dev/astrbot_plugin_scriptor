from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import filter

from .base import BaseMixin

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


class IdentityMixin(BaseMixin):
    """
    身份与权限管理 Mixin

    包含：
    - 身份绑定/解绑/重置命令
    - 身份信息查询命令
    - 权限管理工具

    注意：所有命令装饰器已移至 main.py 中注册，避免指令冲突
    """

    async def cmd_whoami(self, event: AstrMessageEvent):
        """查看当前身份信息"""
        uid, group_id, physical_id = self._get_identity(event)

        user_name = self.identity_manager.get_user_primary_name(uid)
        user_groups = self.identity_manager.get_user_groups(uid)
        device_count = self.identity_manager.get_bound_device_count(uid)

        msg = f"""## 👤 身份信息

- **物理ID**: {physical_id}
- **逻辑UID**: {uid}
- **显示名**: {user_name}
- **当前群体**: {group_id}
- **参与群体数**: {len(user_groups)}
- **绑定设备数**: {device_count}
"""
        yield event.plain_result(msg)

    async def cmd_get_bind_code(self, event: AstrMessageEvent):
        """生成从属绑定码（本设备将作为从属，记忆将被清空）

        使用方式：
        1. 在你想要清空的设备上输入此指令获取绑定码
        2. 在你想要保留记忆的主设备上输入 /bind {绑定码}
        """
        uid, group_id, physical_id = self._get_identity(event)
        umo = event.unified_msg_origin
        platform = umo.split(":")[0] if umo else "unknown"

        if not uid:
            yield event.plain_result("❌ 无法识别的身份，请先发送消息建立身份。")
            return

        import random
        import string

        chars = string.digits + string.ascii_uppercase
        bind_code = "".join(random.choices(chars, k=8))
        self.identity_manager.create_bind_token(bind_code, uid, role="secondary")

        msg = f"""🔑 **您的【从属绑定码】是：`{bind_code}`**

⚠️ **重要提示（请仔细阅读）：**
- 本设备已被标记为【**从属设备**】
- 绑定成功后，本设备的**原有记忆将被清空**
- 本设备的身份将合并到输入此码的主设备

📋 **绑定操作指南：**
1. 在本设备获取绑定码：`{bind_code}`
2. 在您想要**保留记忆的主设备**上发送：`/bind {bind_code}`
3. 主设备确认后，绑定即完成

⏰ 有效期：5分钟（错误3次后锁定5分钟）

💡 **安全说明：**
- 绑定码等同于"从属契约"，输入他人的绑定码意味着您同意将主设备的数据共享给生成码的设备
- 如果有人诱导您输入绑定码，请务必确认对方身份
"""
        yield event.plain_result(msg)

    async def cmd_bind(self, event: AstrMessageEvent, bind_code: str = None, confirm_token: str = None):
        """绑定设备（反向绑定模式）

        用法：
        - /bind {绑定码} - 发起绑定请求（需二次确认）
        - /bind confirm {确认码} - 确认绑定

        注意：反向绑定规则
        - 输入绑定码的设备 = 主号（保留记忆）
        - 生成绑定码的设备 = 从属（记忆被清空）
        """
        self_uid, _, _ = self._get_identity(event)

        umo = event.unified_msg_origin
        platform = umo.split(":")[0] if umo else "unknown"
        sender_id = str(event.get_sender_id())

        if confirm_token:
            async for result in self._confirm_bind(event, confirm_token, platform, sender_id):
                yield result
            return

        if not bind_code:
            yield event.plain_result("❌ 请提供绑定码。用法：\n/bind {绑定码}\n/bind confirm {确认码}")
            return

        bind_result = self.identity_manager.consume_bind_token(bind_code)

        if not bind_result:
            yield event.plain_result("❌ 绑定码无效或已过期。")
            return

        secondary_uid = bind_result["uid"]
        secondary_platform = ""
        for key, mapped_uid in self.identity_manager.identity_map.items():
            if mapped_uid == secondary_uid:
                parts = key.split(":")
                if len(parts) >= 2:
                    secondary_platform = parts[0]
                break

        secondary_meta = self.identity_manager.uid_metadata.get(secondary_uid, {})
        secondary_name = secondary_meta.get("primary_name", secondary_uid[-6:])
        secondary_device_count = self.identity_manager.get_bound_device_count(secondary_uid)

        self_meta = self.identity_manager.uid_metadata.get(self_uid, {})
        self_name = self_meta.get("primary_name", self_uid[-6:] if self_uid else "未知")

        confirm_token = self.identity_manager.create_pending_bind_confirmation(self_uid, secondary_uid)

        msg = f"""⚠️ **身份合并确认**

您正在作为【**主设备**】接纳一个从属设备！

📊 **设备信息：**
├─ 【主设备】{self_name} (当前设备, {platform}:{sender_id})
└─ 【从属设备】{secondary_name} ({secondary_platform}, 绑定设备数: {secondary_device_count})

⚠️ **数据影响：**
├─ 从属设备的记忆 → 将被**永久删除** ⚠️
├─ 从属设备的数据 → 将完全合并到主设备
├─ 主设备的记忆 → **保持不变**
└─ 合并后两设备将共享同一记忆脑区

🔐 **反向绑定说明：**
- 本设备作为主号，将共享记忆给从属设备
- 从属设备的原有记忆将被清空

⏰ 确认码有效期：5分钟

**确认合并请发送：**
`/bind confirm {confirm_token}`

**输入其他内容将取消绑定。**"""

        yield event.plain_result(msg)

    async def _confirm_bind(self, event: AstrMessageEvent, confirm_token: str, platform: str, sender_id: str):
        """执行绑定确认（反向绑定）"""
        bind_info = self.identity_manager.confirm_bind(confirm_token)

        if not bind_info:
            yield event.plain_result("❌ 确认码无效或已过期，请重新发起绑定。")
            return

        primary_uid = bind_info["primary_uid"]
        secondary_uid = bind_info["secondary_uid"]

        self.identity_manager.merge_identities(primary_uid, secondary_uid, cleanup_old=True)

        await self._cleanup_secondary_sessions(secondary_uid)

        primary_meta = self.identity_manager.uid_metadata.get(primary_uid, {})
        primary_name = primary_meta.get("primary_name", primary_uid[-6:])

        yield event.plain_result(
            f"🎉 绑定成功！\n\n当前设备已作为主号接入记忆脑区：{primary_name}\n从属设备的记忆已被清空并合并到本脑区。"
        )

    async def _cleanup_secondary_sessions(self, secondary_uid: str):
        """清理从属 UID 的所有会话历史

        在跨用户绑定后，必须清空从属设备的数据库会话记录，
        否则大模型会读取绑定前的旧对话历史，导致"失忆"问题。
        """
        cleaned_count = 0
        for umo, mapped_uid in list(self.identity_manager.identity_map.items()):
            if mapped_uid == secondary_uid:
                try:
                    await self.context.conversation_manager.delete_conversations_by_user_id(umo)
                    cleaned_count += 1
                    logger.info(f"[Scriptor] 已清理从属设备会话：{umo}")
                except Exception as e:
                    logger.error(f"[Scriptor] 清理从属设备会话失败 {umo}: {e}")

        if cleaned_count > 0:
            logger.info(f"[Scriptor] 绑定后清理完成：共清理 {cleaned_count} 个从属设备会话")
        else:
            logger.warning(f"[Scriptor] 未找到需要清理的从属设备会话 (secondary_uid={secondary_uid})")

    async def cmd_unbind(self, event: AstrMessageEvent, unbind_token: str = None, confirm_token: str = None):
        """解绑当前设备（两步确认）

        用法：
        - /unbind - 发起解绑（需二次确认）
        - /unbind confirm {unbind_token} - 确认解绑
        """
        uid, group_id, physical_id = self._get_identity(event)
        umo = event.unified_msg_origin
        platform = umo.split(":")[0] if umo else "unknown"
        sender_id = str(event.get_sender_id())

        if confirm_token:
            async for result in self._confirm_unbind(event, confirm_token, platform, sender_id):
                yield result
            return

        if unbind_token:
            async for result in self._confirm_unbind(event, unbind_token, platform, sender_id):
                yield result
            return

        async for result in self._handle_unbind_or_reset(event, uid, platform, sender_id):
            yield result

    async def _handle_unbind_or_reset(self, event: AstrMessageEvent, uid: str, platform: str, sender_id: str):
        """处理解绑或重置的第一步"""
        if not uid:
            yield event.plain_result("❌ 无法识别的身份，请先发送消息建立身份。")
            return

        device_count = self.identity_manager.get_bound_device_count(uid)

        if device_count <= 1:
            yield event.plain_result("❌ 当前身份仅绑定了唯一设备，无法解绑。如需清空记忆，请使用 /reset_identity。")
            return

        unbind_token = self.identity_manager.create_pending_unbind_confirmation(sender_id, platform, uid)

        if not unbind_token:
            yield event.plain_result("❌ 当前身份仅绑定了唯一设备，无法解绑。")
            return

        msg = f"""⚠️ **身份解绑警告**

📱 当前设备: {platform}:{sender_id}
🧠 当前脑区: {uid}

执行解绑后，本设备将：
1. 彻底脱离当前记忆脑区
2. 失去所有历史对话和记忆的访问权限
3. 获得一个全新的空白身份重新开始
（原脑区的数据将保留给其他已绑定的设备使用）

⏰ 确认码有效期：5分钟

确认解绑请发送：
`/unbind confirm {unbind_token}`

**输入其他内容将取消解绑。**"""

        yield event.plain_result(msg)

    async def _confirm_unbind(self, event: AstrMessageEvent, unbind_token: str, platform: str, sender_id: str):
        """执行解绑确认"""
        result = self.identity_manager.confirm_unbind(unbind_token)

        if not result:
            yield event.plain_result("❌ 确认码无效或已过期，请重新发起解绑。")
            return

        new_uid = result["new_uid"]
        yield event.plain_result(f"🎉 解绑成功！已为您分配全新的记忆脑区: {new_uid}，一切从零开始。")

    async def cmd_reset_identity(
        self, event: AstrMessageEvent, reset_token: str = None, step: str = None, code: str = None
    ):
        """重置当前身份的记忆（三次验证）

        用法：
        - /reset_identity - 发起重置请求
        - /reset_identity step1 {6位验证码} - 第一步验证
        - /reset_identity step2 {声明} - 第二步验证
        - /reset_identity confirm {reset_token} - 最终确认
        """
        uid, group_id, physical_id = self._get_identity(event)

        if not uid:
            yield event.plain_result("❌ 无法识别的身份，请先发送消息建立身份。")
            return

        if step == "step1" and code:
            async for result in self._confirm_reset_step1(event, reset_token, code):
                yield result
            return

        if step == "step2":
            async for result in self._confirm_reset_step2(event, reset_token, code if code else ""):
                yield result
            return

        if reset_token:
            async for result in self._confirm_reset_step3(event, reset_token, uid):
                yield result
            return

        async for result in self._handle_identity_reset(event, uid):
            yield result

    async def _handle_identity_reset(self, event: AstrMessageEvent, uid: str):
        """发起身份重置（三次验证第一步）"""
        reset_token, code = self.identity_manager.create_pending_reset_confirmation(uid)

        msg = f"""🚨 【危险操作警告】🚨

您正在请求【永久销毁】当前记忆脑区 ({uid}) 的所有数据！
此操作将导致：
1. 所有绑定到此脑区的设备全部失去记忆
2. 历史对话、任务、个人设定被物理删除
3. 数据【无法恢复】！

⚠️ 此操作不可逆！

如果您确知风险，请在 3 分钟内回复：
`/reset_identity step1 {code}`

**回复其他内容将取消此次重置请求。**"""

        yield event.plain_result(msg)

    async def _confirm_reset_step1(self, event: AstrMessageEvent, reset_token: str, code: str):
        """验证第一次确认码"""
        if not self.identity_manager.confirm_reset_step1(reset_token, code):
            yield event.plain_result("❌ 验证码错误或已过期，请重新发起重置请求。")
            return

        msg = """✅ 验证通过 (1/3)。

为了确保您不是误操作，请手动输入以下完整句子（不要包含引号）：
"我确认永久销毁所有记忆数据且无法恢复"

请在 3 分钟内直接回复上述句子。"""

        yield event.plain_result(msg)

    async def _confirm_reset_step2(self, event: AstrMessageEvent, reset_token: str, statement: str):
        """验证第二次声明输入"""
        if not self.identity_manager.confirm_reset_step2(reset_token, statement):
            yield event.plain_result("❌ 声明内容不正确，请手动输入完整句子。")
            return

        msg = """✅ 验证通过 (2/3)。

⚠️ 最后一步：确认执行重置。

请回复以下精确字符串以最终执行：
`/reset_identity confirm {reset_token}`

**此操作将立即永久删除所有记忆数据！**""".replace("{reset_token}", reset_token)

        yield event.plain_result(msg)

    async def _confirm_reset_step3(self, event: AstrMessageEvent, reset_token: str, uid: str):
        """执行最终重置"""
        result = self.identity_manager.perform_identity_reset(reset_token)

        if not result:
            yield event.plain_result("❌ 重置确认码无效或已过期，请重新发起重置请求。")
            return

        yield event.plain_result(
            f"💥 记忆脑区已彻底格式化。\n\n当前身份 ({uid}) 已重置为出厂状态，所有绑定设备将从零开始。"
        )

    async def cmd_debug_identity(self, event: AstrMessageEvent):
        """[调试] 查看当前用户的身份映射详情（仅私聊或管理员可用）"""
        is_private = event.is_private() if hasattr(event, "is_private") else False

        uid, group_id, physical_id = self._get_identity(event)
        umo = event.unified_msg_origin
        platform = umo.split(":")[0] if umo else "unknown"

        admin_uids = getattr(self.config, "admin_uids", [])
        if not is_private and str(uid) not in admin_uids:
            yield event.plain_result("⚠️ 此指令仅限私聊或管理员使用。")
            return

        if not uid:
            yield event.plain_result("❌ 无法识别的身份。")
            return

        im = self.identity_manager
        current_key = f"{platform}:{physical_id}"

        msg = f"""🔍 **身份映射调试信息**

**当前设备**
- 物理ID: `{physical_id}`
- 平台: `{platform}`
- 映射键: `{current_key}`
- 逻辑UID: `{uid}`

**UID 元数据**
"""
        meta = im.uid_metadata.get(uid, {})
        if meta:
            msg += f"- 主设备名: `{meta.get('primary_name', '未设置')}`\n"
            msg += f"- 创建时间: `{meta.get('created_at', '未知')}`\n"
            msg += f"- 最后活跃: `{meta.get('last_active', '未知')}`\n"

            devices = meta.get("devices", [])
            msg += f"- 绑定设备数: `{len(devices)}`\n"
            if devices:
                msg += "\n**绑定设备列表**\n"
                for dev in devices:
                    dev_platform = dev.get("platform", "unknown")
                    dev_physical = dev.get("physical_id", "?")
                    dev_is_primary = dev.get("is_primary", False)
                    marker = "👑" if dev_is_primary else "📱"
                    msg += f"{marker} {dev_platform}:{dev_physical[:12]}...\n"

            groups = meta.get("groups", [])
            msg += f"\n- 参与群体数: `{len(groups)}`\n"
            if groups:
                msg += "\n**群体列表**\n"
                for grp in groups[:5]:
                    msg += f"- `{grp}`\n"
                if len(groups) > 5:
                    msg += f"... 还有 {len(groups) - 5} 个群体\n"
        else:
            msg += "_无元数据_\n"

        msg += f"""
**映射表统计**
- 映射总数: `{len(im.identity_map)}`
- 已知UID数: `{len(im.uid_metadata)}`
- 待确认绑定: `{len(im._pending_bind_confirm)}`
- 待确认解绑: `{len(im._pending_unbind_confirm)}`
- 待确认重置: `{len(im._pending_reset_confirm)}`
"""

        yield event.plain_result(msg)
