from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api.event import filter

from .base import BaseMixin

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


class CommandsMixin(BaseMixin):
    """
    其他命令 Mixin

    包含：
    - 帮助命令
    - 状态查询命令
    - WebUI 命令
    - 智能分段状态命令
    - 缓冲器状态命令
    - 会话锁状态命令

    注意：所有命令装饰器已移至 main.py 中注册，避免指令冲突
    """

    async def cmd_sc_help(self, event: AstrMessageEvent):
        """查看 Scriptor 插件指令帮助（公开版）"""
        msg = """📜 **Scriptor 插件指令指南**

👤 **【身份管理】**
- `/whoami` : 查看当前身份 ID 及绑定状态
- `/get_bind_code` : 生成从属绑定码（反向绑定）
- `/bind` : 输入绑定码，将本设备绑定为从属
- `/unbind` : 解绑当前设备（需二次确认，仅限多设备）

🧠 **【记忆与搜索】**
- `/mem_status` : 查看记忆系统状态
- `/search <关键词>` : 检索你的长期记忆脑区
- `/search <关键词> personal/group/cross` : 按范围检索记忆

📚 **【知识库】**
- `/kb_status` : 查看知识库状态
- `/学习状态` : 查看当前学习模式状态

🎓 **【学习模式】**（仅管理员）
- `/开始学习` : 进入学习模式，AI 将主动记录知识
- `/结束学习` : 退出学习模式
- `/开始授课` : 进入授课模式，知识库锁定为只读
- `/结束授课` : 退出授课模式

💬 **【系统状态】**
- `/buffer_status` : 查看消息缓冲器状态
- `/lock_status` : 查看会话锁管理器状态

⏰ **【主动事件】**
- 每日早安问候（08:00）
- 每日总结推送（22:00，AI 生成并私聊发送）

💡 **提示**：所有指令均支持私聊与群聊。
管理指令请使用 `/sc_admin` 查看。"""
        yield event.plain_result(msg)

    async def cmd_sc_admin(self, event: AstrMessageEvent):
        """查看 Scriptor 插件管理指令（隐藏版）"""
        msg = """🔐 **Scriptor 管理指令**

⚠️ **【高危操作】**
- `/reset_identity` : 彻底重置身份（三步验证，将清空所有记忆）

🔧 **【调试工具】**
- `/debug_identity` : 查看身份映射表（仅私聊或管理员）
- `/debug_memory` : 查看当前记忆状态（仅私聊或管理员）

🛠️ **【维护工具】**
- `/mem_report` : 生成记忆维护建议报告
- `/webui` : 启动 Web 管理界面

🎓 **【学习模式】**（仅管理员）
- `/开始学习` : 进入学习模式，AI 将主动记录知识
- `/结束学习` : 退出学习模式
- `/开始授课` : 进入授课模式，知识库锁定为只读
- `/结束授课` : 退出授课模式

💡 **警告**：管理指令涉及敏感操作，请谨慎使用。"""
        yield event.plain_result(msg)

    async def cmd_smart_split_status(self, event: AstrMessageEvent):
        """查看智能分段发送器状态"""
        stats = self.smart_sender.get_stats()

        msg = "## 📤 智能分段发送器状态\n\n"
        msg += f"- **启用状态**: {'✅' if stats['enabled'] else '❌'}\n"
        msg += f"- **仅分段LLM**: {'✅' if stats['only_llm'] else '❌'}\n"
        msg += f"- **活跃会话**: {stats['active_sessions']}\n"
        msg += f"- **分段正则**: `{stats['split_regex']}`\n"
        msg += f"- **打字速度**: {stats['typing_speed']} 秒/字符\n"
        msg += f"- **最小延迟**: {stats['min_delay']} 秒\n"
        msg += f"- **最大延迟**: {stats['max_delay']} 秒\n"
        msg += f"- **随机波动**: ±{stats['random_factor'] * 100:.0f}%\n"

        yield event.plain_result(msg)

    async def cmd_buffer_status(self, event: AstrMessageEvent):
        """查看消息缓冲器状态"""
        stats = self.message_buffer.get_stats()
        all_status = self.message_buffer.get_all_status()

        msg = "## 📥 消息缓冲器状态\n\n"
        msg += f"- **启用状态**: {'✅' if stats['enabled'] else '❌'}\n"
        msg += f"- **活跃会话**: {stats['active_sessions']}\n"
        msg += f"- **缓冲消息总数**: {stats['total_buffered']}\n"
        msg += f"- **耐心时间**: {stats['patience_seconds']}秒\n"

        if all_status:
            msg += "\n### 会话详情\n"
            for sid, status in all_status.items():
                if status:
                    msg += f"- `{sid[:20]}...`: {status['count']}条消息\n"

        yield event.plain_result(msg)

    async def cmd_lock_status(self, event: AstrMessageEvent):
        """查看会话锁管理器状态"""
        stats = self.session_lock_manager.get_stats()

        msg = "## 🔒 会话锁管理器状态\n\n"
        msg += f"- **启用状态**: {'✅' if stats['enabled'] else '❌'}\n"
        msg += f"- **总会话**: {stats['total_sessions']}\n"
        msg += f"- **处理中**: {stats['processing_sessions']}\n"
        msg += f"- **空闲**: {stats['idle_sessions']}\n"
        msg += f"- **等待中**: {stats['waiting_sessions']}\n"
        msg += f"- **待处理事件**: {stats['total_pending']}\n"

        yield event.plain_result(msg)

    async def cmd_sc_concurrency(self, event: AstrMessageEvent):
        """查看全局并发控制状态"""
        if not hasattr(self, "concurrency_guard") or not self.concurrency_guard:
            yield event.plain_result("❌ 并发控制器未初始化")
            return

        stats = self.concurrency_guard.get_stats()

        msg = "## ⚡ 全局并发控制状态\n\n"
        msg += f"- **启用状态**: {'✅' if self.config.concurrency_control_enabled else '❌'}\n"
        msg += f"- **最大并发数**: {stats['max_concurrent']}\n"
        msg += f"- **当前活跃**: {stats['active']}/{stats['max_concurrent']}\n"
        msg += f"- **排队中**: {stats['waiting']} 个请求\n"
        msg += f"- **平均等待时间**: {stats.get('avg_wait_time', 0)}s\n"
        msg += f"- **总请求数**: {stats.get('total_acquired', 0)}\n\n"

        if stats["active_sessions"]:
            msg += "### 🔄 活跃会话\n"
            for session_id in list(stats["active_sessions"])[:10]:
                msg += f"- `{session_id}`\n"

        if stats["waiting_details"]:
            msg += "\n### ⏳ 排队中的请求\n"
            for detail in stats["waiting_details"][:5]:
                msg += f"- `{detail['session_id']}` (优先级: {detail['priority']}, 等待: {detail['wait_seconds']}s)\n"

        yield event.plain_result(msg)

    async def cmd_webui(self, event: AstrMessageEvent):
        """启动 Web 管理界面"""
        is_private = event.is_private() if hasattr(event, "is_private") else False

        if not is_private:
            yield event.plain_result("⚠️ 此命令仅支持私聊使用，请私聊发送 /webui")
            return

        yield event.plain_result(
            "🌐 Scriptor Web 管理界面\n\n"
            "⚠️ **安全提示**：\n"
            "- 建议仅在**本地网络**或通过 **VPN/SSH 隧道** 访问 Web UI\n"
            "- 不要将端口暴露到公网，以免被恶意利用\n\n"
            "使用步骤：\n"
            "1. 确保已安装 Web UI 依赖：\n"
            "   ```bash\n"
            "   pip install fastapi uvicorn streamlit\n"
            "   ```\n\n"
            "2. 在插件目录下运行：\n"
            "   ```bash\n"
            "   # 启动 API 服务器\n"
            "   python -m uvicorn web.api:app --host 127.0.0.1 --port 8000\n\n"
            "   # 新开一个终端，启动 Streamlit UI\n"
            "   streamlit run web/app.py\n"
            "   ```\n\n"
            "3. 在浏览器中打开：\n"
            "   - Streamlit UI: http://localhost:8501\n"
            "   - API 文档: http://localhost:8000/docs\n\n"
            "📝 提示：Web UI 可以直接编辑 Markdown 记忆文件，编辑后会自动重新索引！"
        )

    async def cmd_confirm_delete(self, event: AstrMessageEvent):
        """
        确认执行待确认的删除操作。

        当 AI 尝试删除文件时，系统会挂起操作并等待用户确认。
        用户回复 /delete 即可确认删除，回复其他任何内容将取消操作。
        """
        from ..core.pending_tasks import get_pending_task_store
        from ..tools.common.file_ops import file_delete

        session_id = event.session_id if hasattr(event, "session_id") else str(id(event))
        store = get_pending_task_store()

        success, task = store.confirm_task(session_id)

        if not success:
            if task is None:
                yield event.plain_result(
                    "ℹ️ **没有等待确认的删除任务**\n\n"
                    "当前没有需要确认的删除操作。可能：\n"
                    "- 任务已超时（2分钟内未响应）\n"
                    "- 您已经确认或取消了该操作\n"
                    "- 从未发起过删除请求"
                )
            else:
                yield event.plain_result(
                    "❌ **确认失败**\n\n" f"任务已超时或状态异常（文件: `{task.file_path}`）。\n" "请重新发起删除请求。"
                )
            return

        file_path = task.file_path

        result = await file_delete(event, file_path, self, force=True)

        if isinstance(result, dict) and result.get("status") == "pending_confirmation":
            yield event.plain_result("⚠️ **系统异常**\n\n" "删除操作未能正常完成，请重试或联系管理员。")
            return

        if result.startswith("Error:") or result.startswith("❌"):
            yield event.plain_result(f"❌ **删除失败**\n\n{result}")
        else:
            yield event.plain_result(result)
