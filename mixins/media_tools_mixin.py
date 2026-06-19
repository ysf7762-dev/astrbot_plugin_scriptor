"""
媒体库工具 Mixin

提供媒体库搜索、管理和发送功能。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List

from astrbot.api import logger
from astrbot.api.event import filter

from .base import BaseMixin

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


class MediaToolsMixin(BaseMixin):
    """
    媒体库工具 Mixin

    包含媒体库搜索、管理和发送相关的 @filter.llm_tool() 方法。

    媒体库搜索隐私分级（与记忆搜索一致）：

    | Scope   | 群聊场景                      | 私聊场景 | 需要理由 |
    |---------|-----------------------------|---------|---------|
    | group   | 仅当前群聊                    | ❌ 不可用 | ❌       |
    | personal| 当前群聊 + 私聊               | ❌ 不可用 | 群聊需要  |
    | cross   | 当前群聊 + 私聊 + 所有其他群聊 | 默认使用 | 群聊需要  |
    """

    @filter.llm_tool()
    async def search_media(
        self,
        event: AstrMessageEvent,
        query: str = "",
        scope: str = "group",
        media_type: str = "all",
        days: int = 30,
        limit: int = 10,
        cross_reason: str = "",
    ):
        """
        搜索媒体库中的图片和文件。

        当用户询问之前发送过的图片、文件，或需要查找特定媒体内容时使用此工具。

        Args:
            query (str): 搜索关键词（可选，搜索描述和文件名）
            scope (str): 搜索范围。
                - group: 仅搜索当前群聊的媒体库（群聊默认，私聊不可用）
                - personal: 当前群聊 + 私聊媒体库（群聊需提供理由，私聊不可用）
                - cross: 当前群聊 + 私聊 + 所有其他群聊（群聊需提供理由，私聊默认）
            media_type (str): 媒体类型。
                - all: 搜索所有类型（默认）
                - images: 仅搜索图片
                - files: 仅搜索文件
            days (int): 搜索最近多少天内的媒体（默认 30 天）
            limit (int): 返回结果数量（默认 10 个）
            cross_reason (str): 【重要】跨场景搜索理由。
                群聊场景下使用 personal 或 cross 时必填。
                私聊场景不需要此参数。

        Returns:
            匹配的媒体列表，包含文件名、类型、描述、时间等信息
        """
        await self._wait_for_ready()

        if not hasattr(self, "media_manager"):
            return "媒体管理功能未启用"

        uid, group_id, _ = self._get_identity(event)
        is_private_context = group_id == "private"

        if is_private_context:
            scope = "cross"
        elif scope not in ("personal", "group", "cross"):
            scope = "group"

        if scope == "group" and is_private_context:
            return "❌ 私聊场景不支持 group 范围搜索，自动使用 cross 范围。"

        if not is_private_context and scope in ("personal", "cross"):
            if not cross_reason or not cross_reason.strip():
                scope_desc = {"personal": "当前群聊 + 私聊", "cross": "当前群聊 + 私聊 + 所有其他群聊"}
                return (
                    f"❌ 跨场景搜索必须提供 cross_reason 参数。\n\n"
                    f"您请求了 {scope} 范围搜索（{scope_desc[scope]}），需要提供理由。\n"
                    f"请在 cross_reason 参数中引用用户的话，说明为什么需要跨场景搜索。"
                )

        try:
            all_results = []

            if scope == "group":
                results = await self.media_manager.search_media(
                    uid=uid, group_id=group_id, query=query, media_type=media_type, days=days, limit=limit
                )
                all_results.extend(results)

            elif scope == "personal":
                if not is_private_context:
                    results = await self.media_manager.search_media(
                        uid=uid, group_id=group_id, query=query, media_type=media_type, days=days, limit=limit
                    )
                    all_results.extend(results)

                private_results = await self.media_manager.search_media(
                    uid=uid, group_id="private", query=query, media_type=media_type, days=days, limit=limit
                )
                for r in private_results:
                    r["source"] = "私聊"
                all_results.extend(private_results)

            elif scope == "cross":
                results = await self.media_manager.search_media(
                    uid=uid, group_id=group_id, query=query, media_type=media_type, days=days, limit=limit
                )
                all_results.extend(results)

                if not is_private_context:
                    private_results = await self.media_manager.search_media(
                        uid=uid, group_id="private", query=query, media_type=media_type, days=days, limit=limit
                    )
                    for r in private_results:
                        r["source"] = "私聊"
                    all_results.extend(private_results)

                joined_groups = self.group_manager.get_user_joined_groups(uid)
                for gid in joined_groups:
                    if gid == group_id:
                        continue
                    group_results = await self.media_manager.search_media(
                        uid=uid, group_id=gid, query=query, media_type=media_type, days=days, limit=limit
                    )
                    for r in group_results:
                        r["source"] = f"群聊:{gid}"
                    all_results.extend(group_results)

            all_results = sorted(all_results, key=lambda x: x.get("timestamp", 0), reverse=True)
            all_results = all_results[:limit]

            if not all_results:
                return "未找到匹配的媒体文件"

            return self._format_media_results(all_results)

        except Exception as e:
            logger.error(f"[Scriptor] 媒体搜索失败: {e}")
            return f"媒体搜索失败: {e!s}"

    @filter.llm_tool()
    async def list_recent_media(
        self,
        event: AstrMessageEvent,
        scope: str = "group",
        media_type: str = "all",
        limit: int = 5,
        cross_reason: str = "",
    ):
        """
        列出最近保存的媒体文件。

        当用户想查看最近发送的图片或文件时使用此工具。

        Args:
            scope (str): 搜索范围。
                - group: 仅搜索当前群聊的媒体库（群聊默认，私聊不可用）
                - personal: 当前群聊 + 私聊媒体库（群聊需提供理由，私聊不可用）
                - cross: 当前群聊 + 私聊 + 所有其他群聊（群聊需提供理由，私聊默认）
            media_type (str): 媒体类型。
                - all: 所有类型（默认）
                - images: 仅图片
                - files: 仅文件
            limit (int): 返回结果数量（默认 5 个）
            cross_reason (str): 【重要】跨场景搜索理由。
                群聊场景下使用 personal 或 cross 时必填。
                私聊场景不需要此参数。

        Returns:
            最近的媒体文件列表
        """
        await self._wait_for_ready()

        if not hasattr(self, "media_manager"):
            return "媒体管理功能未启用"

        uid, group_id, _ = self._get_identity(event)
        is_private_context = group_id == "private"

        if is_private_context:
            scope = "cross"
        elif scope not in ("personal", "group", "cross"):
            scope = "group"

        if scope == "group" and is_private_context:
            return "❌ 私聊场景不支持 group 范围搜索，自动使用 cross 范围。"

        if not is_private_context and scope in ("personal", "cross"):
            if not cross_reason or not cross_reason.strip():
                scope_desc = {"personal": "当前群聊 + 私聊", "cross": "当前群聊 + 私聊 + 所有其他群聊"}
                return (
                    f"❌ 跨场景搜索必须提供 cross_reason 参数。\n\n"
                    f"您请求了 {scope} 范围搜索（{scope_desc[scope]}），需要提供理由。\n"
                    f"请在 cross_reason 参数中引用用户的话，说明为什么需要跨场景搜索。"
                )

        try:
            all_results = []

            if scope == "group":
                results = await self.media_manager.list_recent_media(
                    uid=uid, group_id=group_id, media_type=media_type, limit=limit
                )
                all_results.extend(results)

            elif scope == "personal":
                if not is_private_context:
                    results = await self.media_manager.list_recent_media(
                        uid=uid, group_id=group_id, media_type=media_type, limit=limit
                    )
                    all_results.extend(results)

                private_results = await self.media_manager.list_recent_media(
                    uid=uid, group_id="private", media_type=media_type, limit=limit
                )
                for r in private_results:
                    r["source"] = "私聊"
                all_results.extend(private_results)

            elif scope == "cross":
                results = await self.media_manager.list_recent_media(
                    uid=uid, group_id=group_id, media_type=media_type, limit=limit
                )
                all_results.extend(results)

                if not is_private_context:
                    private_results = await self.media_manager.list_recent_media(
                        uid=uid, group_id="private", media_type=media_type, limit=limit
                    )
                    for r in private_results:
                        r["source"] = "私聊"
                    all_results.extend(private_results)

                joined_groups = self.group_manager.get_user_joined_groups(uid)
                for gid in joined_groups:
                    if gid == group_id:
                        continue
                    group_results = await self.media_manager.list_recent_media(
                        uid=uid, group_id=gid, media_type=media_type, limit=limit
                    )
                    for r in group_results:
                        r["source"] = f"群聊:{gid}"
                    all_results.extend(group_results)

            all_results = sorted(all_results, key=lambda x: x.get("timestamp", 0), reverse=True)
            all_results = all_results[:limit]

            if not all_results:
                return "暂无保存的媒体文件"

            return self._format_media_results(all_results)

        except Exception as e:
            logger.error(f"[Scriptor] 列出媒体失败: {e}")
            return f"列出媒体失败: {e!s}"

    @filter.llm_tool()
    async def send_media(self, event: AstrMessageEvent, filename: str, scope: str = "group", cross_reason: str = ""):
        """
        发送媒体库中的图片或文件给用户。

        当用户要求发送、展示、分享之前保存的图片或文件时使用此工具。

        Args:
            filename (str): 媒体库中的文件名（从 search_media 或 list_recent_media 结果中获取）
            scope (str): 搜索范围（应与之前搜索时使用的 scope 一致）。
                - group: 仅在当前群聊媒体库中查找（群聊默认，私聊不可用）
                - personal: 当前群聊 + 私聊媒体库（群聊需提供理由，私聊不可用）
                - cross: 当前群聊 + 私聊 + 所有其他群聊（群聊需提供理由，私聊默认）
            cross_reason (str): 【重要】跨场景搜索理由。
                群聊场景下使用 personal 或 cross 时必填。
                私聊场景不需要此参数。

        Returns:
            发送结果
        """
        await self._wait_for_ready()

        if not hasattr(self, "media_manager"):
            return "媒体管理功能未启用"

        uid, group_id, _ = self._get_identity(event)
        is_private_context = group_id == "private"

        if is_private_context:
            scope = "cross"
        elif scope not in ("personal", "group", "cross"):
            scope = "group"

        if scope == "group" and is_private_context:
            return "❌ 私聊场景不支持 group 范围搜索，自动使用 cross 范围。"

        if not is_private_context and scope in ("personal", "cross"):
            if not cross_reason or not cross_reason.strip():
                scope_desc = {"personal": "当前群聊 + 私聊", "cross": "当前群聊 + 私聊 + 所有其他群聊"}
                return (
                    f"❌ 跨场景搜索必须提供 cross_reason 参数。\n\n"
                    f"您请求了 {scope} 范围搜索（{scope_desc[scope]}），需要提供理由。\n"
                    f"请在 cross_reason 参数中引用用户的话，说明为什么需要跨场景搜索。"
                )

        try:
            media_info = None

            if scope == "group":
                media_info = await self.media_manager.get_media_by_filename(
                    uid=uid, group_id=group_id, filename=filename
                )

            elif scope == "personal":
                if not is_private_context:
                    media_info = await self.media_manager.get_media_by_filename(
                        uid=uid, group_id=group_id, filename=filename
                    )

                if not media_info:
                    media_info = await self.media_manager.get_media_by_filename(
                        uid=uid, group_id="private", filename=filename
                    )
                    if media_info:
                        logger.info(f"[Scriptor] 从私聊媒体库找到文件: {filename}")

            elif scope == "cross":
                media_info = await self.media_manager.get_media_by_filename(
                    uid=uid, group_id=group_id, filename=filename
                )

                if not media_info and not is_private_context:
                    media_info = await self.media_manager.get_media_by_filename(
                        uid=uid, group_id="private", filename=filename
                    )
                    if media_info:
                        logger.info(f"[Scriptor] 从私聊媒体库找到文件: {filename}")

                if not media_info:
                    joined_groups = self.group_manager.get_user_joined_groups(uid)
                    for gid in joined_groups:
                        if gid == group_id:
                            continue
                        media_info = await self.media_manager.get_media_by_filename(
                            uid=uid, group_id=gid, filename=filename
                        )
                        if media_info:
                            logger.info(f"[Scriptor] 从群聊 {gid} 媒体库找到文件: {filename}")
                            break

            if not media_info:
                return f"未找到媒体文件: {filename}"

            media_path = media_info.get("path")
            media_type = media_info.get("media_type", "file")

            if not media_path or not os.path.exists(media_path):
                return f"媒体文件不存在: {filename}"

            from astrbot.api.all import MessageChain
            from astrbot.api.message_components import File, Image

            if media_type == "image":
                image = Image.fromFileSystem(media_path)
                message_chain = MessageChain([image])
            else:
                original_name = media_info.get("original_name", filename)
                file_component = File(name=original_name, file=media_path)
                message_chain = MessageChain([file_component])

            await event.send(message_chain)
            logger.info(f"[Scriptor] 已发送媒体文件: {filename}")

            return f"已发送: {filename}"

        except Exception as e:
            logger.error(f"[Scriptor] 发送媒体失败: {e}")
            return f"发送媒体失败: {e!s}"

    def _format_media_results(self, results: List[Dict[str, Any]]) -> str:
        """格式化媒体搜索结果"""
        lines = ["### 媒体库搜索结果\n"]

        for i, item in enumerate(results, 1):
            media_type = "图片" if item.get("type") == "image" else "文件"
            filename = item.get("filename", "未知")
            timestamp = item.get("timestamp", 0)
            description = item.get("description", "")
            size_bytes = item.get("size_bytes", 0)
            sender_name = item.get("sender_name", "")
            source = item.get("source", "")

            time_str = ""
            if timestamp:
                try:
                    dt = datetime.fromtimestamp(timestamp)
                    time_str = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    time_str = str(timestamp)

            size_str = self._format_file_size(size_bytes)

            lines.append(f"**{i}. [{media_type}] {filename}**")
            if time_str:
                lines.append(f"   - 时间: {time_str}")
            if sender_name:
                lines.append(f"   - 发送者: {sender_name}")
            if size_str:
                lines.append(f"   - 大小: {size_str}")
            if source:
                lines.append(f"   - 来源: {source}")
            if description:
                lines.append(f"   - 描述: {description}")
            lines.append("")

        return "\n".join(lines)

    def _format_file_size(self, size_bytes: int) -> str:
        """格式化文件大小"""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
