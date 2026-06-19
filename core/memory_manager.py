# core/memory_manager.py
"""Scriptor 记忆管理模块"""

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from .search_engine import SearchResult

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from tools.common.image_utils import compute_dhash, compute_md5_hash
from tools.config.memory_patterns import MEMORY_KEYWORDS, MEMORY_TYPES
from tools.security.encryption import get_memory_encryption
from tools.security.sanitizer import sanitize_id

from .interfaces import MemoryRecordParams


class MemoryManager:
    """主动式记忆管理系统"""

    MEMORY_KEYWORDS = MEMORY_KEYWORDS

    MEMORY_TYPES = MEMORY_TYPES

    _MEMORY_BLOCK_SPLITTER = re.compile(r"(?=### \[)")

    def __init__(self, data_dir, config, identity_manager, group_manager, context=None):
        self.data_dir = data_dir
        self.config = config
        self.identity_manager = identity_manager
        self.group_manager = group_manager
        self._context = context

        self._unprocessed_messages = {}
        self.LLM_EXTRACTION_THRESHOLD = 10

        self._last_active_time: Dict[str, float] = {}
        self._last_active_file: Dict[str, str] = {}
        self._state_lock = asyncio.Lock()

        self._file_locks = {}
        self._lock_access_times = {}
        self._MAX_LOCKS = 100
        self._locks_lock = asyncio.Lock()

        self._task_check_interval = 3600
        self._task_check_task = None

        # P1-3: 初始化加密器
        encryption = get_memory_encryption()
        encryption_enabled = getattr(config, "memory_encryption_enabled", False)
        encryption_key = getattr(config, "memory_encryption_key", None)
        encryption.initialize(encryption_key, encryption_enabled)
        self._encryption = encryption

    def _decrypt_content_if_needed(self, content: str) -> str:
        """
        P1-3: 如果内容被加密，则解密

        Args:
            content: 可能加密的内容

        Returns:
            解密后的内容或原始内容
        """
        if not self._encryption.is_enabled:
            return content

        if content.startswith("gAAAAA") or content.startswith("gAAAAA"):
            return self._encryption.decrypt(content)

        return content

    def _decrypt_memory_block(self, block: str) -> str:
        """
        P1-3: 解密记忆块中的加密内容

        处理包含 [Encrypted] 标记的记忆块
        """
        if not self._encryption.is_enabled:
            return block

        if "[Encrypted]" not in block:
            return block

        parts = block.split("\n", 1)
        if len(parts) < 2:
            return block

        header = parts[0]
        content = parts[1]

        decrypted_content = self._decrypt_content_if_needed(content)
        return header + "\n" + decrypted_content

    async def _cleanup_old_locks_async(self):
        """异步清理最久未使用的锁（LRU 策略）"""
        if len(self._file_locks) <= self._MAX_LOCKS:
            return

        try:
            async with self._locks_lock:
                if len(self._file_locks) <= self._MAX_LOCKS:
                    return

                sorted_locks = sorted(self._lock_access_times.items(), key=lambda x: x[1])

                remove_count = max(1, len(sorted_locks) // 5)
                for path_str, _ in sorted_locks[:remove_count]:
                    if path_str in self._file_locks:
                        del self._file_locks[path_str]
                    if path_str in self._lock_access_times:
                        del self._lock_access_times[path_str]

                logger.debug(f"[Scriptor] 锁缓存清理完成，移除 {remove_count} 个锁")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[Scriptor] 锁缓存清理失败: {e}")

    async def _get_lock(self, file_path: Path) -> asyncio.Lock:
        """获取文件级别的异步锁（带 LRU 清理）"""
        path_str = str(file_path)

        async with self._locks_lock:
            if path_str not in self._file_locks:
                if len(self._file_locks) >= self._MAX_LOCKS:
                    self._file_locks.pop(next(iter(self._file_locks)), None)
                    self._lock_access_times.pop(next(iter(self._lock_access_times)), None)
                self._file_locks[path_str] = asyncio.Lock()

            self._lock_access_times[path_str] = time.time()
            return self._file_locks[path_str]

    def _get_profile_dir(self, uid: str) -> Path:
        """获取用户个人目录 (带安全校验)"""
        uid = sanitize_id(uid)
        return self.data_dir / "profiles" / uid

    def _get_group_dir(self, group_id: str) -> Path:
        """获取群体目录 (带安全校验)"""
        group_id = sanitize_id(group_id, default="unknown_group")
        return self.data_dir / "groups" / group_id

    def _get_global_dir(self) -> Path:
        """获取全局目录"""
        return self.data_dir / "global"

    def _get_memory_file(self, uid: str = None, group_id: str = "private", is_global: bool = False) -> Path:
        """获取记忆文件路径（使用新版命名格式）"""
        if is_global:
            global_dir = self._get_global_dir()
            return global_dir / "MEMORY.md"
        elif group_id == "private":
            profile_dir = self._get_profile_dir(uid)
            return profile_dir / "P_MEMORY.md"
        else:
            group_dir = self._get_group_dir(group_id)
            return group_dir / "G_MEMORY.md"

    def _get_profile_file(self, uid: str) -> Path:
        """获取画像文件路径（使用新命名格式）"""
        profile_dir = self._get_profile_dir(uid)
        return profile_dir / "P_PROFILE.md"

    def _get_group_profile_file(self, group_id: str) -> Path:
        """获取群组画像文件路径（使用新命名格式）"""
        group_dir = self._get_group_dir(group_id)
        return group_dir / "G_PROFILE.md"

    def _compute_dhash(self, image_data: bytes) -> str:
        """计算图片的差值哈希 (dHash) - 已迁移到tools"""
        return compute_dhash(image_data)

    def _compute_md5_hash(self, image_data: bytes) -> str:
        """计算图片的 MD5 哈希（用于精确匹配）- 已迁移到tools"""
        return compute_md5_hash(image_data)

    async def get_image_paraphrase(self, image_data: bytes, provider=None, max_age_days: int = 30) -> str:
        """
        获取图片转述 (带双层缓存机制)

        缓存策略：
        1. 先检查 MD5 精确匹配（完全相同的图片）
        2. 再检查 dHash 模糊匹配（相似图片）
        3. 缓存过期自动清理（默认 30 天）
        """
        # 确保缓存目录存在
        cache_dir = self.data_dir / "image_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # 计算两种哈希
        md5_hash = self._compute_md5_hash(image_data)
        dhash_hash = self._compute_dhash(image_data)

        # 先尝试精确匹配（MD5）
        md5_cache_file = cache_dir / f"md5_{md5_hash}.txt"
        if md5_cache_file.exists():
            cache_age = time.time() - md5_cache_file.stat().st_mtime
            if cache_age < max_age_days * 86400:
                logger.debug(f"[Scriptor] 命中图片 MD5 精确缓存: {md5_hash}")
                return md5_cache_file.read_text(encoding="utf-8")
            else:
                logger.debug("[Scriptor] MD5 缓存已过期，将重新生成")
                md5_cache_file.unlink(missing_ok=True)

        # 再尝试模糊匹配（dHash）
        dhash_cache_file = cache_dir / f"dhash_{dhash_hash}.txt"
        if dhash_cache_file.exists():
            cache_age = time.time() - dhash_cache_file.stat().st_mtime
            if cache_age < max_age_days * 86400:
                logger.debug(f"[Scriptor] 命中图片 dHash 模糊缓存: {dhash_hash}")
                paraphrase = dhash_cache_file.read_text(encoding="utf-8")
                # 同时更新 MD5 缓存，加速下次访问
                md5_cache_file.write_text(paraphrase, encoding="utf-8")
                return paraphrase
            else:
                logger.debug("[Scriptor] dHash 缓存已过期，将重新生成")
                dhash_cache_file.unlink(missing_ok=True)

        # 清理过期缓存
        await self._cleanup_expired_cache(cache_dir, max_age_days)

        # 如果没有缓存，调用 Vision LLM 进行转述
        paraphrase = await self._call_vision_provider(
            provider, image_data, md5_hash, dhash_hash, md5_cache_file, dhash_cache_file
        )
        return paraphrase

    async def _call_vision_provider(
        self,
        provider,
        image_data: bytes,
        md5_hash: str,
        dhash_hash: str,
        md5_cache_file: Path,
        dhash_cache_file: Path,
        max_age_days: int = 30,
    ) -> str:
        """
        调用视觉 Provider 进行图片转述

        使用 AstrBot v4.x 推荐的 llm_generate 接口（支持图片输入）
        """
        if not provider:
            return await self._save_placeholder(md5_hash, dhash_hash, md5_cache_file, dhash_cache_file)

        prompt = "请简要描述这张图片的内容，不超过 100 字"

        try:
            import base64

            image_b64 = base64.b64encode(image_data).decode("utf-8")
            image_url = f"data:image/jpeg;base64,{image_b64}"

            # 使用 AstrBot v4.x 推荐的 llm_generate 接口
            context = self._context if hasattr(self, "_context") else None
            if context:
                provider_id = await context.get_current_chat_provider_id(None)
                response = await context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    image_urls=[image_url],
                )
                if response and response.completion_text:
                    paraphrase = response.completion_text.strip()
                    if paraphrase:
                        await self._cache_paraphrase(paraphrase, md5_hash, dhash_hash, md5_cache_file, dhash_cache_file)
                        logger.info(f"[Scriptor] 图片转述已生成并缓存: {md5_hash}")
                        return paraphrase
        except Exception as e:
            logger.debug(f"[Scriptor] llm_generate 图片转述失败: {e}")

        # 所有方式都失败，使用占位符
        return await self._save_placeholder(md5_hash, dhash_hash, md5_cache_file, dhash_cache_file)

    def _extract_vision_response(self, response) -> Optional[str]:
        """从 Vision Provider 响应中提取文本内容"""
        if not response:
            return None

        # 尝试多种响应格式
        if isinstance(response, str):
            return response.strip()

        if hasattr(response, "completion_text"):
            return response.completion_text.strip()

        if hasattr(response, "text"):
            return response.text.strip()

        if hasattr(response, "content"):
            content = response.content
            if isinstance(content, str):
                return content.strip()
            if hasattr(content, "text"):
                return content.text.strip()

        if hasattr(response, "message"):
            msg = response.message
            if isinstance(msg, str):
                return msg.strip()
            if hasattr(msg, "content"):
                return msg.content.strip()

        return None

    async def _cache_paraphrase(
        self, paraphrase: str, md5_hash: str, dhash_hash: str, md5_cache_file: Path, dhash_cache_file: Path
    ):
        """缓存转述内容"""
        try:
            md5_cache_file.write_text(paraphrase, encoding="utf-8")
            dhash_cache_file.write_text(paraphrase, encoding="utf-8")
        except OSError as e:
            logger.error(f"[Scriptor] 缓存转述失败: {e}")

    async def _save_placeholder(
        self, md5_hash: str, dhash_hash: str, md5_cache_file: Path, dhash_cache_file: Path
    ) -> str:
        """保存占位符并返回"""
        paraphrase = "[图片]"
        try:
            md5_cache_file.write_text(paraphrase, encoding="utf-8")
            dhash_cache_file.write_text(paraphrase, encoding="utf-8")
        except OSError as e:
            logger.error(f"[Scriptor] 保存图片占位符失败: {e}")
        return paraphrase

    async def _cleanup_expired_cache(self, cache_dir: Path, max_age_days: int):
        """清理过期的图片缓存"""
        try:
            now = time.time()
            max_age_seconds = max_age_days * 86400
            deleted_count = 0

            for cache_file in cache_dir.glob("*.txt"):
                if now - cache_file.stat().st_mtime > max_age_seconds:
                    cache_file.unlink()
                    deleted_count += 1

            if deleted_count > 0:
                logger.info(f"[Scriptor] 清理了 {deleted_count} 个过期图片缓存")

        except OSError as e:
            logger.error(f"[Scriptor] 清理图片缓存失败: {e}")

    async def record_interaction(self, uid: str, group_id: str, role: str, content: str):
        """
        记录交互到日记

        采用“软截断”机制：如果当前时间距离上一次记录超过 60 分钟，
        且已经跨天，则开启新的日记文件；否则继续追加到上一个活跃的日记文件中。
        """
        now = time.time()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        today_str = time.strftime("%Y-%m-%d", time.localtime(now))

        session_key = f"{uid}_{group_id}"
        is_new_session = False

        # 只有私聊才写入日记文件，群聊日记由 group_manager 负责
        if group_id == "private":
            target_dir = self._get_profile_dir(uid) / "memory"
            target_dir.mkdir(parents=True, exist_ok=True)

            daily_note_file = target_dir / f"{today_str}.md"

            async with self._state_lock:
                last_active = self._last_active_time.get(session_key, 0)
                last_file = self._last_active_file.get(session_key)
                if now - last_active > 3600 or not last_file:
                    if not last_file or today_str != last_file.replace(".md", ""):
                        active_file_name = f"{today_str}.md"
                        is_new_session = True
                    else:
                        active_file_name = last_file
                else:
                    active_file_name = last_file

                self._last_active_time[session_key] = now
                self._last_active_file[session_key] = active_file_name
                daily_note_file = target_dir / active_file_name

            # 记录绝对时间，精确到秒，防止时间幻觉
            entry = f"### [{timestamp}] {role}\n{content}\n\n"

            # FIX P0-2: 使用文件锁保证并发安全，同时保持原子性
            lock = await self._get_lock(daily_note_file)
            async with lock:
                # 在锁内再次检查并更新状态（双重检查锁定模式）
                async with self._state_lock:
                    current_file = self._last_active_file.get(session_key)
                    if current_file != active_file_name:
                        active_file_name = current_file
                        daily_note_file = target_dir / active_file_name

                # 检查日记文件大小，如果超过 1MB，则进行轮转归档
                if daily_note_file.exists() and daily_note_file.stat().st_size > 1024 * 1024:
                    archive_file = daily_note_file.with_name(f"{daily_note_file.stem}_ARCHIVE_{int(now)}.md")
                    daily_note_file.rename(archive_file)
                    logger.info(
                        f"[Scriptor] 日记文件 {daily_note_file.name} 超过 1MB，已轮转归档至 {archive_file.name}"
                    )

                with open(daily_note_file, "a", encoding="utf-8") as f:
                    f.write(entry)
        else:
            # 群聊场景：只更新时间戳，不写入日记文件
            async with self._state_lock:
                self._last_active_time[session_key] = now
                # 检查是否是新会话（用于记忆提取触发）
                last_active = self._last_active_time.get(session_key, 0)
                last_file = self._last_active_file.get(session_key)
                if now - last_active > 3600 or not last_file:
                    if not last_file or today_str != (last_file or "").replace(".md", ""):
                        is_new_session = True

        # 增加未处理消息计数（私聊和群聊都需要，用于记忆提取）
        if session_key not in self._unprocessed_messages:
            self._unprocessed_messages[session_key] = []
        self._unprocessed_messages[session_key].append({"role": role, "content": content})

        logger.debug(f"[Scriptor] 记录交互: uid={uid}, group={group_id}")

        return is_new_session

    def get_unprocessed_messages(self, uid: str, group_id: str) -> list:
        """获取未处理的消息列表"""
        session_key = f"{uid}_{group_id}"
        return self._unprocessed_messages.get(session_key, [])

    def clear_unprocessed_messages(self, uid: str, group_id: str):
        """清空未处理的消息列表"""
        session_key = f"{uid}_{group_id}"
        if session_key in self._unprocessed_messages:
            self._unprocessed_messages[session_key] = []

    def should_trigger_llm_extraction(self, uid: str, group_id: str) -> bool:
        """判断是否应该触发 LLM 记忆提取"""
        session_key = f"{uid}_{group_id}"
        messages = self._unprocessed_messages.get(session_key, [])
        return len(messages) >= self.LLM_EXTRACTION_THRESHOLD

    def should_extract_memory(self, content: str) -> bool:
        """
        判断是否需要提取为长期记忆

        采用主动式策略：关键词触发 + 内容长度 + 决策检测
        """
        content_lower = content.lower()

        if any(kw in content_lower for kw in self.MEMORY_KEYWORDS):
            return True

        if len(content) > 500:
            return True

        decision_keywords = ["决定", "选择", "计划", "目标", "决定好了", "就这么办"]
        if any(kw in content for kw in decision_keywords):
            return True

        return False

    def extract_memory_type(self, content: str) -> Optional[str]:
        """提取记忆类型"""
        for mem_type, keywords in self.MEMORY_TYPES.items():
            if any(kw in content for kw in keywords):
                return mem_type
        return None

    async def record_long_term_memory(self, params: MemoryRecordParams, search_engine=None):
        """记录长期记忆到 MEMORY.md 并同步到向量数据库

        Args:
            params: 记忆记录参数（MemoryRecordParams 数据类）
            search_engine: 搜索引擎实例（可选）
        """
        uid = params.uid
        group_id = params.group_id
        content = params.content
        memory_type = params.memory_type
        privacy_level = params.privacy_level
        strength = params.strength
        useful_score = params.useful_score
        status = params.status
        is_sudo = params.is_sudo

        now = time.time()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        today_str = time.strftime("%Y-%m-%d", time.localtime(now))

        if is_sudo:
            memory_file = self._get_memory_file(is_global=True)
            scope = "global"
            privacy_level = "global"
        elif group_id == "private":
            memory_file = self._get_memory_file(uid=uid, group_id="private")
            scope = "personal"
            if privacy_level not in ["private", "global"]:
                privacy_level = "private"
        else:
            memory_file = self._get_memory_file(group_id=group_id)
            scope = "group"
            privacy_level = "group"

        status_tag = f" [Status: {status}]" if memory_type == "task" else ""

        content_to_store = content
        is_encrypted = False
        if self._encryption.is_enabled and privacy_level in ["private", "group"]:
            content_to_store = self._encryption.encrypt(content)
            is_encrypted = True

        encrypted_tag = " [Encrypted]" if is_encrypted else ""
        entry = f"\n### [{timestamp}] ({memory_type}){status_tag}{encrypted_tag} [Privacy: {privacy_level}] [Strength: {strength:.1f}] [Score: {useful_score:.1f}]\n{content_to_store}\n"

        lock = await self._get_lock(memory_file)
        async with lock:
            if memory_file.exists() and memory_file.stat().st_size > 1024 * 1024:
                archive_file = memory_file.with_name(f"MEMORY_ARCHIVE_{int(now)}.md")
                memory_file.rename(archive_file)
                logger.info(f"[Scriptor] MEMORY.md 超过 1MB，已轮转归档至 {archive_file.name}")

            with open(memory_file, "a", encoding="utf-8") as f:
                f.write(entry)

        if search_engine and hasattr(search_engine, "add_to_vector_db"):
            doc_id = f"mem_{scope}_{uid}_{group_id}_{int(now)}"
            source_name = memory_file.name
            metadata = {
                "uid": uid,
                "group_id": group_id,
                "scope": scope,
                "source": source_name,
                "source_type": "memory",
                "memory_type": memory_type,
                "privacy_level": privacy_level,
                "strength": strength,
                "useful_score": useful_score,
                "status": status,
                "date": today_str,
            }
            await search_engine.add_to_vector_db(doc_id, content, metadata)

        logger.info(f"[Scriptor] 记录长期记忆: uid={uid[:8]}..., scope={scope}, type={memory_type}, status={status}")

    async def update_task_status(self, uid: str, group_id: str, task_content: str, new_status: str):
        """更新任务记忆的状态 (防止堆积)"""
        try:
            target_file = self._get_memory_file(uid=uid, group_id=group_id)

            lock = await self._get_lock(target_file)
            async with lock:
                if not target_file.exists():
                    return

                file_content = target_file.read_text(encoding="utf-8")
                match_content = task_content[:50].strip()

                blocks = self._MEMORY_BLOCK_SPLITTER.split(file_content)
                updated_blocks = []
                changed = False

                for block in blocks:
                    if match_content in block and "(task)" in block:
                        if "[Status:" in block:
                            block = re.sub(r"\[Status:\s*\w+\]", f"[Status: {new_status}]", block)
                        else:
                            block = block.replace("(task)", f"(task) [Status: {new_status}]")
                        changed = True
                    updated_blocks.append(block)

                if changed:
                    target_file.write_text("".join(updated_blocks), encoding="utf-8")
                    logger.info(f"[Scriptor] 任务状态已更新为 {new_status}: {match_content}...")
        except OSError as e:
            logger.error(f"[Scriptor] 更新任务状态失败: {e}")

    async def resolve_memory_conflict(
        self, uid: str, group_id: str, new_memory: str, old_memories: List["SearchResult"], compactor
    ) -> str:
        """
        解决记忆冲突 - 当新记忆与旧记忆矛盾时调用 LLM 进行智能合并

        Args:
            uid: 用户ID
            group_id: 群组ID
            new_memory: 新记忆内容
            old_memories: 冲突的旧记忆列表
            compactor: 压缩器实例（用于调用 LLM）

        Returns:
            解决后的记忆内容
        """
        try:
            old_contents = [m.content for m in old_memories]
            resolved = await compactor.resolve_conflict(new_memory, old_contents)
            logger.info(f"[Scriptor] 记忆冲突已解决: {new_memory[:30]}...")
            return resolved
        except (OSError, RuntimeError) as e:
            logger.error(f"[Scriptor] 解决记忆冲突失败: {e}")
            return new_memory

    async def merge_and_cleanup_memories(
        self, uid: str, group_id: str, memories_to_merge: List["SearchResult"], compactor
    ):
        """合并相似记忆并清理旧记忆 (借鉴 Angel Memory)"""
        if len(memories_to_merge) < 2:
            return

        logger.info(f"[Scriptor] 正在合并 {len(memories_to_merge)} 条相似记忆...")

        # 1. 调用 LLM 进行合并
        contents = [m.content for m in memories_to_merge]
        merged_content, total_score = await compactor.merge_memories(contents)

        if not merged_content:
            return

        # 2. 记录新记忆 (合并后的)
        await self.record_long_term_memory(
            MemoryRecordParams(
                uid=uid,
                group_id=group_id,
                content=merged_content,
                memory_type="consolidated",
                useful_score=total_score,
                strength=2.0,
            )
        )

        # 3. 清理旧记忆 (从 Markdown 文件中删除或标记为归档)
        # 这里为了安全，先将其移动到 ARCHIVE.md
        for m in memories_to_merge:
            await self._archive_and_remove_memory(uid, group_id, m.content, m.source)

    async def _archive_and_remove_memory(self, uid: str, group_id: str, content: str, source: str):
        """从源文件中移除记忆并存入归档"""
        try:
            if group_id == "private":
                target_file = self._get_profile_dir(uid) / source
            else:
                target_file = self._get_group_dir(group_id) / source

            lock = await self._get_lock(target_file)
            async with lock:
                if not target_file.exists():
                    return

                file_content = target_file.read_text(encoding="utf-8")
                match_content = content[:50].strip()

                blocks = self._MEMORY_BLOCK_SPLITTER.split(file_content)
                remaining_blocks = []
                removed_block = ""

                for block in blocks:
                    if match_content in block:
                        removed_block = block
                    else:
                        remaining_blocks.append(block)

                if removed_block:
                    # 写入剩余内容
                    target_file.write_text("".join(remaining_blocks), encoding="utf-8")
                    # 写入归档
                    await self._archive_memory(uid, group_id, removed_block.replace("### [", "### [MERGED_ARCHIVE] ["))

        except OSError as e:
            logger.error(f"[Scriptor] 移除旧记忆失败: {e}")

    async def _archive_memory(self, uid: str, group_id: str, content: str):
        """将记忆写入归档文件"""
        try:
            if group_id == "private":
                archive_file = self._get_profile_dir(uid) / "ARCHIVE.md"
            else:
                archive_file = self._get_group_dir(group_id) / "ARCHIVE.md"

            lock = await self._get_lock(archive_file)
            async with lock:
                with open(archive_file, "a", encoding="utf-8") as f:
                    f.write(f"\n{content}\n")
        except OSError as e:
            logger.error(f"[Scriptor] 归档记忆失败: {e}")

    async def update_profile(self, uid: str, group_id: str, new_facts: str, scope: str = "personal"):
        """
        更新画像信息（支持个人画像和群组画像）

        Args:
            uid: 用户 ID
            group_id: 群组 ID（私聊时为 "private"）
            new_facts: 需要更新的事实信息
            scope: 更新范围，"personal" 或 "group"
        """
        if scope == "group":
            if group_id == "private":
                logger.warning("[Scriptor] 无法在私聊中更新群组画像")
                return

            profile_file = self._get_group_profile_file(group_id)
            # 新模板 G_PROFILE.md 结构：追加到 ## 3. 场景集体记忆
            target_heading = "## 3. 场景集体记忆"
            log_context = f"group={group_id}"
        else:
            profile_file = self._get_profile_file(uid)
            # 新模板 P_PROFILE.md 结构：追加到 ## 5. 持续关注点
            target_heading = "## 5. 持续关注点"
            log_context = f"uid={uid}"

        lock = await self._get_lock(profile_file)
        async with lock:
            if not profile_file.exists():
                logger.warning(f"[Scriptor] 画像文件不存在: {profile_file}")
                return

            existing_content = profile_file.read_text(encoding="utf-8")

            # 检查是否已经存在动态更新区域，如果不存在则创建
            dynamic_section_marker = "### 动态更新记录"
            new_entry = f"- {time.strftime('%Y-%m-%d')}: {new_facts}"

            if target_heading in existing_content:
                parts = existing_content.split(target_heading)
                if len(parts) > 1:
                    section_content = parts[1]
                    # 寻找下一个二级标题，确定当前章节的边界
                    next_heading_match = re.search(r"\n##\s", section_content)

                    if next_heading_match:
                        # 在当前章节末尾（下一个章节之前）追加
                        end_idx = next_heading_match.start()
                        current_section = section_content[:end_idx]
                        remainder = section_content[end_idx:]

                        if dynamic_section_marker not in current_section:
                            updated_section = current_section.rstrip() + f"\n\n{dynamic_section_marker}\n{new_entry}\n"
                        else:
                            updated_section = current_section.rstrip() + f"\n{new_entry}\n"

                        updated = parts[0] + target_heading + updated_section + remainder
                    else:
                        # 如果是最后一个章节，直接追加到末尾
                        if dynamic_section_marker not in section_content:
                            updated_section = section_content.rstrip() + f"\n\n{dynamic_section_marker}\n{new_entry}\n"
                        else:
                            updated_section = section_content.rstrip() + f"\n{new_entry}\n"

                        updated = parts[0] + target_heading + updated_section

                    profile_file.write_text(updated, encoding="utf-8")
            else:
                # 如果目标章节不存在，追加到文件末尾并创建章节
                with open(profile_file, "a", encoding="utf-8") as f:
                    f.write(f"\n\n{target_heading}\n\n{dynamic_section_marker}\n{new_entry}\n")

        logger.info(f"[Scriptor] 更新画像: {log_context}, scope={scope}")

    async def increase_memory_strength(
        self, uid: str, group_id: str, content: str, source: str, is_useful: bool = True
    ):
        """
        增加被检索到的记忆的强度和有用性评分 (活体文件机制)
        """
        try:
            # 确定目标文件
            if group_id == "private":
                target_dir = self._get_profile_dir(uid)
            else:
                target_dir = self._get_group_dir(group_id)

            if source.startswith("memory/"):
                target_file = target_dir / source
            else:
                target_file = target_dir / source

            # 使用异步锁保证并发安全
            lock = await self._get_lock(target_file)
            async with lock:
                if not target_file.exists():
                    return

                file_content = target_file.read_text(encoding="utf-8")

                # 简单的强度增加逻辑：查找包含 content 的块，并更新其 Strength 和 Score
                # 假设格式为: ### [时间] (类型) [Privacy: 级别] [Strength: 1.0] [Score: 5.0]

                # 提取内容的前几十个字符作为匹配特征，避免换行等问题
                match_content = content[:50].strip()
                if not match_content:
                    return

                # 寻找包含该内容的块
                blocks = self._MEMORY_BLOCK_SPLITTER.split(file_content)
                updated_blocks = []
                changed = False

                for block in blocks:
                    if match_content in block:
                        # 更新 Strength
                        if "[Strength:" in block:
                            strength_match = re.search(r"\[Strength:\s*([\d\.]+)\]", block)
                            if strength_match:
                                current_strength = float(strength_match.group(1))
                                new_strength = min(5.0, current_strength + 0.5)
                                block = re.sub(r"\[Strength:\s*[\d\.]+\]", f"[Strength: {new_strength:.1f}]", block)
                                changed = True

                        # 更新 Score (三档衰减策略的核心)
                        if "[Score:" in block:
                            score_match = re.search(r"\[Score:\s*([\d\.]+)\]", block)
                            if score_match:
                                current_score = float(score_match.group(1))
                                if is_useful:
                                    # 有用则加分，最高 15.0 (T2 永存档)
                                    new_score = min(15.0, current_score + 1.0)
                                else:
                                    # 无用则扣分，加速遗忘
                                    new_score = max(0.0, current_score - 2.0)
                                block = re.sub(r"\[Score:\s*[\d\.]+\]", f"[Score: {new_score:.1f}]", block)
                                changed = True
                        elif is_useful:
                            # 如果没有 Score 标签，但被证明有用，则添加一个初始高分
                            block = block.replace("\n", " [Score: 6.0]\n", 1)
                            changed = True

                    updated_blocks.append(block)

                if changed:
                    target_file.write_text("".join(updated_blocks), encoding="utf-8")
                    logger.debug(f"[Scriptor] 记忆强度/评分已更新: {source}")

        except OSError as e:
            logger.error(f"[Scriptor] 更新记忆强度失败: {e}")

    def get_daily_notes(self, uid: str, group_id: str, days: int = 7) -> List[Dict]:
        """获取最近日记"""
        notes = []

        if group_id == "private":
            note_dir = self._get_profile_dir(uid) / "memory"
        else:
            note_dir = self._get_group_dir(group_id) / "memory"

        if not note_dir.exists():
            return notes

        for md_file in sorted(note_dir.glob("*.md"), reverse=True)[:days]:
            content = md_file.read_text(encoding="utf-8")
            notes.append({"date": md_file.stem, "content": content, "path": str(md_file)})

        return notes

    def get_recent_notes_text(self, uid: str, group_id: str, limit: int = 3) -> str:
        """获取最近日记文本"""
        notes = self.get_daily_notes(uid, group_id, limit)

        if not notes:
            return ""

        parts = []
        for note in notes:
            parts.append(f"## {note['date']}\n{note['content']}")

        return "\n\n".join(parts)

    def get_hot_memory(self, uid: str, group_id: str) -> str:
        """获取热记忆（用于提示词注入）"""
        parts = []

        profile_dir = self._get_profile_dir(uid)
        profile_file = self._get_profile_file(uid)
        if profile_file.exists():
            parts.append(f"# 个人画像\n{profile_file.read_text(encoding='utf-8')}")

        # 引入知识笔记/SOP (Knowledge Base)
        sop_file = profile_dir / "P_SOP.md"
        if sop_file.exists():
            parts.append(f"# 个人知识库与标准流程(SOP)\n{sop_file.read_text(encoding='utf-8')}")

        if group_id != "private":
            group_context = self.group_manager.get_group_context(group_id, uid)
            if group_context.get("group_rules"):
                parts.append(f"# 群体规则\n{group_context['group_rules']}")

            if group_context.get("members"):
                member_list = "\n".join([f"- {m['alias']} ({m['role']})" for m in group_context["members"]])
                parts.append(f"# 群体成员\n{member_list}")

            # 群组知识库
            group_sop_file = self._get_group_dir(group_id) / "G_SOP.md"
            if group_sop_file.exists():
                parts.append(f"# 群组知识库与标准流程(SOP)\n{group_sop_file.read_text(encoding='utf-8')}")

        recent_notes = self.get_recent_notes_text(uid, group_id, limit=2)
        if recent_notes:
            parts.append(f"# 最近日记\n{recent_notes}")

        return "\n\n---\n\n".join(parts) if parts else ""

    async def start_task_monitor(self):
        """启动任务记忆监控"""
        if self._task_check_task is None or self._task_check_task.done():
            self._task_check_task = asyncio.create_task(self._task_consolidation_loop())
            logger.info("[Scriptor] 任务记忆监控已启动")

    async def stop_task_monitor(self):
        """停止任务记忆监控"""
        if self._task_check_task and not self._task_check_task.done():
            self._task_check_task.cancel()
            try:
                await self._task_check_task
            except asyncio.CancelledError:
                pass
            logger.info("[Scriptor] 任务记忆监控已停止")

    async def _task_consolidation_loop(self):
        """任务记忆巩固循环"""
        while True:
            try:
                await asyncio.sleep(self._task_check_interval)
                logger.debug("[Scriptor] 执行任务记忆定期检查...")
                await self._consolidate_all_tasks()
            except asyncio.CancelledError:
                logger.info("[Scriptor] 任务记忆监控已取消")
                break
            except (OSError, RuntimeError) as e:
                logger.error(f"[Scriptor] 任务记忆巩固出错: {e}")

    async def _consolidate_all_tasks(self):
        """巩固所有用户和群组的任务记忆"""
        # 检查所有个人任务
        profiles_dir = self.data_dir / "profiles"
        if profiles_dir.exists():
            for profile_dir in profiles_dir.iterdir():
                if profile_dir.is_dir():
                    uid = profile_dir.name
                    await self._consolidate_tasks_for_uid(uid, "private")

        # 检查所有群组任务
        groups_dir = self.data_dir / "groups"
        if groups_dir.exists():
            for group_dir in groups_dir.iterdir():
                if group_dir.is_dir():
                    group_id = group_dir.name
                    await self._consolidate_tasks_for_uid("*", group_id)

    async def _consolidate_tasks_for_uid(self, uid: str, group_id: str):
        """巩固指定用户或群组的任务记忆"""
        try:
            memory_file = self._get_memory_file(uid=uid, group_id=group_id)

            if not memory_file.exists():
                return

            lock = await self._get_lock(memory_file)
            async with lock:
                content = memory_file.read_text(encoding="utf-8")
                blocks = self._MEMORY_BLOCK_SPLITTER.split(content)

                updated_blocks = []
                changed = False
                now = datetime.now()

                for block in blocks:
                    if not block.strip():
                        updated_blocks.append(block)
                        continue

                    # 检查是否为任务类型
                    if "(task)" in block:
                        # 提取任务状态和时间
                        status_match = re.search(r"\[Status:\s*(\w+)\]", block)
                        date_match = re.search(r"### \[(.*?)\]", block)

                        current_status = status_match.group(1) if status_match else "pending"
                        task_date = None

                        if date_match:
                            try:
                                task_date = datetime.strptime(date_match.group(1), "%Y-%m-%d %H:%M:%S")
                            except (ValueError, TypeError):
                                pass

                        # 自动状态转换逻辑
                        new_status = current_status

                        if current_status == "pending" and task_date:
                            # 任务超过 7 天未动，标记为 stalled
                            if (now - task_date).days > 7:
                                new_status = "stalled"
                            # 任务超过 30 天，标记为 archived
                            elif (now - task_date).days > 30:
                                new_status = "archived"

                        elif current_status == "completed":
                            # 完成的任务超过 30 天，标记为 archived
                            if task_date and (now - task_date).days > 30:
                                new_status = "archived"

                        # 更新状态
                        if new_status != current_status:
                            if "[Status:" in block:
                                block = re.sub(r"\[Status:\s*\w+\]", f"[Status: {new_status}]", block)
                            else:
                                block = block.replace("(task)", f"(task) [Status: {new_status}]")
                            changed = True
                            logger.info(f"[Scriptor] 任务状态更新: {current_status} → {new_status}")

                    updated_blocks.append(block)

                if changed:
                    memory_file.write_text("".join(updated_blocks), encoding="utf-8")
                    logger.info(f"[Scriptor] 任务记忆巩固完成: uid={uid}, group={group_id}")

        except (OSError, RuntimeError) as e:
            logger.error(f"[Scriptor] 巩固任务记忆失败: uid={uid}, group={group_id}, error={e}")

    async def get_pending_tasks(self, uid: str, group_id: str) -> List[Dict]:
        """获取待处理的任务列表"""
        tasks = []

        try:
            memory_file = self._get_memory_file(uid=uid, group_id=group_id)

            if not memory_file.exists():
                return tasks

            content = memory_file.read_text(encoding="utf-8")
            blocks = self._MEMORY_BLOCK_SPLITTER.split(content)

            for block in blocks:
                if "(task)" in block:
                    status_match = re.search(r"\[Status:\s*(\w+)\]", block)
                    status = status_match.group(1) if status_match else "pending"

                    if status in ["pending", "in_progress", "stalled"]:
                        content_match = re.search(r"\n(.*?)(?:\n|$)", block)
                        task_content = content_match.group(1) if content_match else block.strip()

                        tasks.append({"content": task_content, "status": status, "raw_block": block})

        except OSError as e:
            logger.error(f"[Scriptor] 获取待处理任务失败: {e}")

        return tasks
