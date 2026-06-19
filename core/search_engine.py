# core/search_engine.py
"""Scriptor 检索引擎模块"""

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from tools.common.bm25 import SimpleBM25
from tools.common.text_utils import jaccard_similarity, tokenize_for_bm25
from tools.integration.embedding import DualTrackEmbeddingFunction


@dataclass
class SearchResult:
    """搜索结果"""

    content: str
    source: str
    source_type: str  # personal, group, memory, note
    score: float
    date: str = ""
    uid: str = ""  # 用于隐私边界校验
    group_id: str = ""  # 用于隐私边界校验
    scope: str = ""  # personal 或 group
    privacy_level: str = ""  # private 或 global


class SearchEngine:
    """混合检索引擎 - 向量搜索 + 关键词搜索 + 文件搜索

    存储路径：
    - ChromaDB: global/chroma_db/
    - Tantivy: global/tantivy_index/
    - 索引缓存: global/index_cache.json
    """

    def __init__(self, data_dir, config, identity_manager, group_manager, memory_manager):
        self.data_dir = data_dir
        self.config = config
        self.identity_manager = identity_manager
        self.group_manager = group_manager
        self.memory_manager = memory_manager

        self.global_dir = Path(data_dir) / "global"
        self.global_dir.mkdir(parents=True, exist_ok=True)

        self.chroma_client = None
        self.collection = None
        self.embedding_fn = None
        self._init_task = None
        self._is_ready = False

        self.bm25 = SimpleBM25()

        self.tantivy_index = None
        self.tantivy_searcher = None
        self._tantivy_writer = None
        self._tantivy_ready = False

        self._bm25_corpus_cache: Optional[List[List[str]]] = None
        self._bm25_corpus_ids: Optional[List[str]] = None

        self._init_caches()
        self._migrate_from_legacy_path()

        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                self._init_task = loop.create_task(self._lazy_init_engines())
            else:
                self._init_task = None
        except RuntimeError:
            self._init_task = None
            logger.warning("[Scriptor] SearchEngine: 无法创建异步任务，将在首次使用时完成初始化")

    def _get_memory_file(self, directory: Path, scope: str = "personal") -> Path:
        """获取记忆文件路径（使用新的短命名格式）"""
        if scope == "global":
            return directory / "MEMORY.md"
        elif scope == "group":
            new_file = directory / "G_MEMORY.md"
            if new_file.exists():
                return new_file
            return directory / "MEMORY.md"
        else:
            new_file = directory / "P_MEMORY.md"
            if new_file.exists():
                return new_file
            return directory / "MEMORY.md"

    def _get_profile_file(self, profile_dir: Path) -> Path:
        """获取画像文件路径（使用新的短命名格式）"""
        new_file = profile_dir / "P_PROFILE.md"
        if new_file.exists():
            return new_file
        return profile_dir / "PROFILE.md"

    def _get_soul_file(self, directory: Path, scope: str = "personal") -> Path:
        """获取灵魂文件路径（使用新的短命名格式）"""
        if scope == "global":
            return directory / "SOUL.md"
        elif scope == "group":
            new_file = directory / "G_SOUL.md"
            if new_file.exists():
                return new_file
            return directory / "SOUL.md"
        else:
            new_file = directory / "P_SOUL.md"
            if new_file.exists():
                return new_file
            return directory / "SOUL.md"

    def _get_group_profile_file(self, group_dir: Path) -> Path:
        """获取群组画像文件路径（使用新的短命名格式）"""
        new_file = group_dir / "G_PROFILE.md"
        if new_file.exists():
            return new_file
        return group_dir / "GROUP_PROFILE.md"

    def _init_caches(self):
        """初始化缓存"""
        self._index_cache: Dict[str, float] = {}
        self._indexed_content_hashes: Dict[str, str] = {}
        self._INDEX_CACHE_TIMEOUT = 300
        self._INDEX_CACHE_FILE = self.global_dir / "index_cache.json"

    def _migrate_from_legacy_path(self):
        """迁移旧路径的数据到 global/ 目录"""
        import shutil

        # 迁移 ChromaDB
        legacy_chroma = Path(self.data_dir) / "chroma_db"
        new_chroma = self.global_dir / "chroma_db"
        if legacy_chroma.exists() and not new_chroma.exists():
            try:
                shutil.move(str(legacy_chroma), str(new_chroma))
                logger.info("[SearchEngine] 已迁移 ChromaDB 数据到 global/ 目录")
            except Exception as e:
                logger.warning(f"[SearchEngine] 迁移 ChromaDB 数据失败: {e}")

        # 迁移索引缓存
        legacy_cache = Path(self.data_dir) / "index_cache.json"
        if legacy_cache.exists() and not self._INDEX_CACHE_FILE.exists():
            try:
                shutil.move(str(legacy_cache), str(self._INDEX_CACHE_FILE))
                logger.info("[SearchEngine] 已迁移索引缓存到 global/ 目录")
            except Exception as e:
                logger.warning(f"[SearchEngine] 迁移索引缓存失败: {e}")

    async def _lazy_init_engines(self):
        """后台懒加载初始化所有检索引擎"""
        # 1. 初始化 Tantivy (纯文本主力)
        await self._init_tantivy()

        # 2. 初始化 ChromaDB (向量补充，可选)
        if self.config.embedding_enabled:
            await self._lazy_init_chromadb()
        else:
            self._is_ready = True

        # 3. 启动中央索引维护管线 (后台循环)
        asyncio.create_task(self._maintenance_pipeline())

    async def _maintenance_pipeline(self):
        """中央索引维护管线 - 次日0点后无活动X分钟触发"""
        import json

        maintenance_state_file = self.data_dir / "maintenance_state.json"
        LAST_MAINTENANCE_KEY = "last_maintenance_date"

        def load_maintenanceState():
            if maintenance_state_file.exists():
                try:
                    return json.loads(maintenance_state_file.read_text(encoding="utf-8"))
                except:
                    pass
            return {"last_backup": 0, "last_sync": 0, "last_rebuild": 0, "last_maintenance_date": ""}

        def save_maintenanceState(state):
            maintenance_state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

        while True:
            try:
                if not getattr(self.config, "nightly_maintenance_enabled", True):
                    await asyncio.sleep(3600)
                    continue

                now = datetime.now()
                inactivity_minutes = getattr(self.config, "nightly_maintenance_inactivity_minutes", 60)
                inactivity_seconds = inactivity_minutes * 60

                last_activity = 0
                if hasattr(self, "memory_manager") and self.memory_manager:
                    activity_times = self.memory_manager._last_active_time.values()
                    last_activity = max(activity_times) if activity_times else 0

                current_date_str = now.strftime("%Y-%m-%d")
                state = load_maintenanceState()
                last_maintenance_date = state.get(LAST_MAINTENANCE_KEY, "")

                # 动态维护触发逻辑：
                # 1. 凌晨窗口 (2-6点) 且空闲 60 分钟
                # 2. 非凌晨时段，但空闲时间极长 (例如 6 小时以上)
                is_midnight_window = 2 <= now.hour <= 6
                is_long_idle = (time.time() - last_activity) > (6 * 3600) if last_activity > 0 else False

                no_activity = (time.time() - last_activity) > inactivity_seconds if last_activity > 0 else True
                already_ran_today = current_date_str == last_maintenance_date

                should_trigger = False
                if not already_ran_today:
                    if is_midnight_window and no_activity:
                        should_trigger = True
                        logger.info(f"[Scriptor] 触发凌晨维护窗口 (2-6点，空闲 {inactivity_minutes} 分钟)")
                    elif is_long_idle:
                        should_trigger = True
                        logger.info("[Scriptor] 触发全天候长空闲维护 (空闲超过 6 小时)")

                if should_trigger:
                    state["last_maintenance_date"] = current_date_str
                    now_timestamp = time.time()

                    if now_timestamp - state.get("last_backup", 0) > 86400:
                        logger.info("[Scriptor] 执行数据备份...")
                        await self._backup_data()
                        state["last_backup"] = now_timestamp

                    if now_timestamp - state.get("last_sync", 0) > 259200:
                        logger.info("[Scriptor] 执行 Markdown 到向量库同步...")
                        await self._sync_markdown_to_vector_db()
                        state["last_sync"] = now_timestamp

                    if now_timestamp - state.get("last_rebuild", 0) > 604800:
                        if self._tantivy_ready and not self.config.embedding_enabled:
                            logger.info("[Scriptor] 重建 Tantivy 索引...")
                            await self._init_tantivy()
                            all_docs = await self._collect_all_docs_for_indexing("*", "*", "all")
                            if all_docs:
                                await self.index_documents(all_docs)
                        state["last_rebuild"] = now_timestamp

                    logger.info("[Scriptor] 执行记忆合并优化检查...")
                    await self._optimize_duplicate_memories()

                    save_maintenanceState(state)
                    logger.info("[Scriptor] 夜间维护管线执行完成！")

                await asyncio.sleep(300)

            except Exception as e:
                logger.error(f"[Scriptor] 维护管线执行失败: {e}")
                await asyncio.sleep(3600)

    async def _sync_markdown_to_vector_db(self):
        """
        将 Markdown 文件中的记忆同步到向量数据库 (流式处理，避免内存峰值)
        边读取边写入，不一次性加载所有文档到内存
        """
        if not self.config.embedding_enabled or not self.collection:
            return

        logger.info("[Scriptor] 正在执行 Markdown 到向量库的双向同步（流式处理）...")

        # 1. 先收集数据库中已有的记忆 ID
        existing_ids = set()
        try:
            # 获取向量库中所有文档（分批次获取，避免一次获取太多）
            offset = 0
            batch_size = 100
            while True:

                def _get_batch():
                    return self.collection.get(limit=batch_size, offset=offset)

                result = await asyncio.to_thread(_get_batch)
                if not result or not result["ids"]:
                    break
                existing_ids.update(result["ids"])
                offset += batch_size
                if len(result["ids"]) < batch_size:
                    break
        except Exception as e:
            logger.warning(f"[Scriptor] 获取现有向量库 ID 失败，将进行全量重建: {e}")
            existing_ids = set()

        current_ids = set()
        total_synced = 0

        try:
            # 流式批处理：边读取边写入，避免内存峰值
            BATCH_SIZE = 50
            current_batch = []

            def _flush_batch():
                """将当前批次写入向量库"""
                nonlocal total_synced
                if current_batch:
                    try:
                        self.collection.upsert(
                            ids=[doc["id"] for doc in current_batch],
                            documents=[doc["content"] for doc in current_batch],
                            metadatas=[doc["metadata"] for doc in current_batch],
                        )
                        total_synced += len(current_batch)
                        logger.debug(f"[Scriptor] 已同步批次 {total_synced // BATCH_SIZE}")
                    except Exception as e:
                        logger.error(f"[Scriptor] 同步批次失败: {e}")
                    current_batch.clear()

            # 同步所有个人记忆
            profiles_dir = self.data_dir / "profiles"
            if profiles_dir.exists():
                for p_dir in profiles_dir.iterdir():
                    if not p_dir.is_dir():
                        continue
                    uid = p_dir.name

                    mem_file = self._get_memory_file(p_dir, "personal")
                    if mem_file.exists():
                        docs_from_file = await self._index_memory_file(mem_file, uid, "private")
                        for doc in docs_from_file:
                            current_batch.append(doc)
                            current_ids.add(doc["id"])
                            if len(current_batch) >= BATCH_SIZE:
                                _flush_batch()

                    profile_file = self._get_profile_file(p_dir)
                    if profile_file.exists():
                        doc_id = hashlib.md5(f"profile_{uid}".encode()).hexdigest()
                        content = profile_file.read_text(encoding="utf-8")
                        doc = {
                            "id": doc_id,
                            "content": content,
                            "metadata": {
                                "uid": uid,
                                "group_id": "private",
                                "source": profile_file.name,
                                "source_type": "personal",
                                "memory_type": "profile",
                                "privacy_level": "private",
                                "date": datetime.now().strftime("%Y-%m-%d"),
                            },
                        }
                        current_batch.append(doc)
                        current_ids.add(doc_id)
                        if len(current_batch) >= BATCH_SIZE:
                            _flush_batch()

            groups_dir = self.data_dir / "groups"
            if groups_dir.exists():
                for g_dir in groups_dir.iterdir():
                    if not g_dir.is_dir():
                        continue
                    group_id = g_dir.name

                    mem_file = self._get_memory_file(g_dir, "group")
                    if mem_file.exists():
                        docs_from_file = await self._index_memory_file(mem_file, "*", group_id)
                        for doc in docs_from_file:
                            current_batch.append(doc)
                            current_ids.add(doc["id"])
                            if len(current_batch) >= BATCH_SIZE:
                                _flush_batch()

                    group_file = g_dir / "GROUP.md"
                    if group_file.exists():
                        doc_id = hashlib.md5(f"group_{group_id}".encode()).hexdigest()
                        content = group_file.read_text(encoding="utf-8")
                        doc = {
                            "id": doc_id,
                            "content": content,
                            "metadata": {
                                "uid": "*",
                                "group_id": group_id,
                                "source": "GROUP.md",
                                "source_type": "group",
                                "memory_type": "group_info",
                                "privacy_level": "group",
                                "date": datetime.now().strftime("%Y-%m-%d"),
                            },
                        }
                        current_batch.append(doc)
                        current_ids.add(doc_id)
                        if len(current_batch) >= BATCH_SIZE:
                            _flush_batch()

            # 刷新最后一批
            _flush_batch()

            # 3. 计算需要删除的 ID（在数据库中但不在 Markdown 中）
            ids_to_delete = list(existing_ids - current_ids)
            if ids_to_delete:
                logger.info(f"[Scriptor] 将删除 {len(ids_to_delete)} 条不再存在的向量记忆")
                try:
                    # 分批删除，避免一次删除太多
                    delete_batch_size = 100
                    for i in range(0, len(ids_to_delete), delete_batch_size):
                        delete_batch = ids_to_delete[i : i + delete_batch_size]
                        self.collection.delete(ids=delete_batch)
                        logger.debug(f"[Scriptor] 已删除批次 {i//delete_batch_size + 1}")
                except Exception as e:
                    logger.warning(f"[Scriptor] 删除过期向量记忆失败: {e}")

            logger.info(
                f"[Scriptor] Markdown ↔ 向量库双向同步完成！同步了 {total_synced} 条，删除了 {len(ids_to_delete)} 条"
            )

        except Exception as e:
            logger.error(f"[Scriptor] 同步 Markdown 到向量库失败: {e}")

    async def _index_memory_file(self, file_path: Path, uid: str, group_id: str) -> List[Dict]:
        """解析并索引单个 MEMORY.md 文件，返回文档列表（不直接写入，用于批量处理）"""
        docs = []
        try:
            content = file_path.read_text(encoding="utf-8")
            blocks = re.split(r"(?=### \[)", content)

            for block_idx, block in enumerate(blocks):
                if not block.strip():
                    continue

                # 提取元数据和内容
                # 格式: ### [时间] (类型) [Status: xxx] [Privacy: 级别] [Strength: 1.0] [Score: 5.0]
                meta_match = re.search(
                    r"### \[(.*?)\] \((.*?)\)(?: \[Status: (.*?)\])? \[Privacy: (.*?)\] \[Strength: (.*?)\] \[Score: (.*?)\]",
                    block,
                )
                if meta_match:
                    timestamp, m_type, status, privacy, strength, score = meta_match.groups()
                    # 提取内容（跳过第一行元数据）
                    content_lines = block.split("\n")[1:]
                    doc_content = "\n".join(content_lines).strip()
                    if not doc_content:
                        continue

                    doc_id = hashlib.md5(f"{uid}_{group_id}_{timestamp}_{doc_content[:50]}".encode()).hexdigest()

                    docs.append(
                        {
                            "id": doc_id,
                            "content": doc_content,
                            "metadata": {
                                "uid": uid,
                                "group_id": group_id,
                                "source": "MEMORY.md",
                                "source_type": "memory",
                                "memory_type": m_type,
                                "privacy_level": privacy,
                                "strength": float(strength),
                                "useful_score": float(score),
                                "status": status or "active",
                                "date": timestamp.split(" ")[0],
                            },
                        }
                    )
                else:
                    # 旧格式或不匹配的块，也尝试索引
                    doc_id = hashlib.md5(f"{uid}_{group_id}_{file_path.name}_{block_idx}".encode()).hexdigest()
                    docs.append(
                        {
                            "id": doc_id,
                            "content": block.strip(),
                            "metadata": {
                                "uid": uid,
                                "group_id": group_id,
                                "source": file_path.name,
                                "source_type": "memory",
                                "memory_type": "unknown",
                                "privacy_level": "private" if group_id == "private" else "group",
                                "date": datetime.now().strftime("%Y-%m-%d"),
                            },
                        }
                    )
        except Exception as e:
            logger.error(f"[Scriptor] 解析记忆文件失败 {file_path}: {e}")

        return docs

    async def _backup_data(self):
        """备份记忆数据为 JSON（流式处理，避免内存溢出）"""
        try:
            import json

            backup_dir = self.data_dir / "backups"
            backup_dir.mkdir(exist_ok=True)

            backup_file = backup_dir / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

            # 使用流式写入，避免一次性加载所有数据到内存
            with open(backup_file, "w", encoding="utf-8") as f:
                # 写入 JSON 头部
                f.write("{\n")
                f.write(f'  "timestamp": "{datetime.now().isoformat()}",\n')
                f.write('  "version": "1.0",\n')

                # 1. 流式备份个人资料
                f.write('  "profiles": {\n')
                profiles_dir = self.data_dir / "profiles"
                profile_count = 0
                if profiles_dir.exists():
                    for profile_path in profiles_dir.iterdir():
                        if not profile_path.is_dir():
                            continue

                        uid = profile_path.name
                        if profile_count > 0:
                            f.write(",\n")
                        f.write(f'    "{uid}": {{')

                        file_count = 0
                        personal_files = [
                            ("P_PROFILE.md", "PROFILE.md"),
                            ("P_SOUL.md", "SOUL.md"),
                            ("P_MEMORY.md", "MEMORY.md"),
                            ("P_AGENTS.md", "AGENTS.md"),
                            ("P_SOP.md", "SOP.md"),
                            ("ARCHIVE.md", "ARCHIVE.md"),
                        ]
                        for new_name, old_name in personal_files:
                            file_path = profile_path / new_name
                            if not file_path.exists():
                                file_path = profile_path / old_name
                            if file_path.exists():
                                if file_count > 0:
                                    f.write(", ")
                                content = file_path.read_text(encoding="utf-8")
                                escaped_content = json.dumps(content, ensure_ascii=False)[1:-1]
                                f.write(f'"{file_path.name}": "{escaped_content}"')
                                file_count += 1

                        # 备份日记目录（限制最近30天）
                        memory_dir = profile_path / "memory"
                        if memory_dir.exists():
                            if file_count > 0:
                                f.write(", ")
                            f.write('"memory": {')
                            mem_count = 0
                            for md_file in sorted(memory_dir.glob("*.md"), reverse=True)[:30]:
                                if mem_count > 0:
                                    f.write(", ")
                                content = md_file.read_text(encoding="utf-8")
                                escaped_content = json.dumps(content, ensure_ascii=False)[1:-1]
                                f.write(f'"{md_file.name}": "{escaped_content}"')
                                mem_count += 1
                            f.write("}")

                        f.write("}")
                        profile_count += 1
                f.write("\n  },\n")

                # 2. 流式备份群体资料
                f.write('  "groups": {\n')
                groups_dir = self.data_dir / "groups"
                group_count = 0
                if groups_dir.exists():
                    for group_path in groups_dir.iterdir():
                        if not group_path.is_dir() or group_path.name == "group_map.json":
                            continue

                        group_id = group_path.name
                        if group_count > 0:
                            f.write(",\n")
                        f.write(f'    "{group_id}": {{')

                        file_count = 0
                        group_files = [
                            ("GROUP.md", "GROUP.md"),
                            ("G_MEMORY.md", "MEMORY.md"),
                            ("G_PROFILE.md", "GROUP_PROFILE.md"),
                            ("G_SOP.md", "SOP.md"),
                            ("ARCHIVE.md", "ARCHIVE.md"),
                        ]
                        for new_name, old_name in group_files:
                            file_path = group_path / new_name
                            if not file_path.exists():
                                file_path = group_path / old_name
                            if file_path.exists():
                                if file_count > 0:
                                    f.write(", ")
                                content = file_path.read_text(encoding="utf-8")
                                escaped_content = json.dumps(content, ensure_ascii=False)[1:-1]
                                f.write(f'"{file_path.name}": "{escaped_content}"')
                                file_count += 1

                        f.write("}}")
                        group_count += 1
                f.write("\n  },\n")

                # 3. 备份身份映射
                f.write('  "identity_map": ')
                identity_map_file = self.data_dir / "identity_map.json"
                if identity_map_file.exists():
                    identity_data = json.loads(identity_map_file.read_text(encoding="utf-8"))
                    # 只备份必要的字段，敏感字段可能需要过滤
                    safe_data = {
                        "identity_map": identity_data.get("identity_map", {}),
                        "uid_metadata": {
                            uid: {
                                "created_at": meta.get("created_at"),
                                "last_active": meta.get("last_active"),
                                "primary_name": meta.get("primary_name", ""),
                            }
                            for uid, meta in identity_data.get("uid_metadata", {}).items()
                        },
                    }
                    f.write(json.dumps(safe_data, ensure_ascii=False))
                else:
                    f.write("{}")
                f.write(",\n")

                # 4. 备份跨群消息（限制最近100条）
                f.write('  "cross_group_messages": ')
                cross_group_file = self.data_dir / "cross_group_messages.json"
                if cross_group_file.exists():
                    try:
                        cg_data = json.loads(cross_group_file.read_text(encoding="utf-8"))
                        # 只保留最近的100条
                        recent_messages = cg_data[-100:] if len(cg_data) > 100 else cg_data
                        f.write(json.dumps(recent_messages, ensure_ascii=False))
                    except:
                        f.write("[]")
                else:
                    f.write("[]")

                # 写入 JSON 尾部
                f.write("\n}")

            logger.info(f"[Scriptor] 数据备份完成: {backup_file}")

            retention_days = getattr(self.config, "backup_retention_days", 7)
            backup_files = sorted(backup_dir.glob("backup_*.json"), reverse=True)
            for old_backup in backup_files[retention_days:]:
                old_backup.unlink()
                logger.info(f"[Scriptor] 已删除旧备份: {old_backup.name}")

        except Exception as e:
            logger.error(f"[Scriptor] 数据备份失败: {e}")

    async def _init_tantivy(self):
        """初始化 Tantivy 内存索引"""
        try:
            import tantivy

            # 定义 Schema
            schema_builder = tantivy.SchemaBuilder()
            schema_builder.add_text_field("id", stored=True)
            schema_builder.add_text_field("content", stored=True, tokenizer_name="default")
            schema_builder.add_text_field("source", stored=True)
            schema_builder.add_text_field("source_type", stored=True)
            schema_builder.add_text_field("uid", stored=True)
            schema_builder.add_text_field("group_id", stored=True)
            schema_builder.add_text_field("date", stored=True)
            schema_builder.add_text_field("scope", stored=True)
            schema_builder.add_text_field("privacy_level", stored=True)
            schema = schema_builder.build()

            # 创建内存索引 (保持文件即记忆，不持久化 Tantivy)
            self.tantivy_index = tantivy.Index(schema)

            # 创建 IndexWriter
            self._tantivy_writer = self.tantivy_index.writer()

            self._tantivy_ready = True
            logger.info("[Scriptor] Tantivy BM25 引擎初始化成功")

        except ImportError:
            logger.warning("[Scriptor] 未安装 tantivy 库，将降级使用纯 Python BM25。请运行 pip install tantivy")
            self._tantivy_ready = False
        except Exception as e:
            logger.error(f"[Scriptor] Tantivy 初始化失败: {e}")
            self._tantivy_ready = False

        # 加载索引缓存
        self._load_index_cache()

    def _load_index_cache(self):
        """加载索引缓存"""
        try:
            if self._INDEX_CACHE_FILE.exists():
                cache_data = json.loads(self._INDEX_CACHE_FILE.read_text(encoding="utf-8"))
                self._index_cache = cache_data.get("index_cache", {})
                self._indexed_content_hashes = cache_data.get("content_hashes", {})
                logger.debug(f"[Scriptor] 加载索引缓存: {len(self._index_cache)} 条记录")
        except Exception as e:
            logger.warning(f"[Scriptor] 加载索引缓存失败: {e}")
            self._index_cache = {}
            self._indexed_content_hashes = {}

    def _save_index_cache(self):
        """保存索引缓存到文件"""
        try:
            cache_data = {"index_cache": self._index_cache, "content_hashes": self._indexed_content_hashes}
            self._INDEX_CACHE_FILE.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[Scriptor] 保存索引缓存失败: {e}")

    async def _check_and_update_index(self, uid: str, group_id: str, scope: str, cache_key: str) -> bool:
        """
        检查索引是否需要更新

        Returns:
            True 如果需要重新索引，False 如果可以使用缓存
        """
        import hashlib

        # 检查缓存是否过期
        last_indexed = self._index_cache.get(cache_key, 0)
        if time.time() - last_indexed < self._INDEX_CACHE_TIMEOUT:
            # 缓存未过期，检查文件内容是否有变化
            pass
        else:
            # 缓存已过期，需要重新索引
            return True

        # 检查源文件是否有变化
        files_to_check = []

        if scope == "group":
            group_dir = self.data_dir / "groups" / group_id
            if group_dir.exists():
                group_files = ["G_GROUP.md", "G_MEMORY.md", "MEMORY.md"]
                for md_file in group_files:
                    file_path = group_dir / md_file
                    if file_path.exists():
                        files_to_check.append(file_path)
                memory_dir = group_dir / "memory"
                if memory_dir.exists():
                    files_to_check.extend(sorted(memory_dir.glob("*.md"))[:14])
                md_files_dir = group_dir / "md_files"
                if md_files_dir.exists():
                    files_to_check.extend(sorted(md_files_dir.glob("*.md"))[:14])

        elif scope == "personal":
            profile_dir = self.data_dir / "profiles" / uid
            if profile_dir.exists():
                personal_files = [
                    "P_PROFILE.md",
                    "PROFILE.md",
                    "P_SOUL.md",
                    "SOUL.md",
                    "P_AGENTS.md",
                    "AGENTS.md",
                    "P_MEMORY.md",
                    "MEMORY.md",
                ]
                for md_file in personal_files:
                    file_path = profile_dir / md_file
                    if file_path.exists():
                        files_to_check.append(file_path)
                memory_dir = profile_dir / "memory"
                if memory_dir.exists():
                    files_to_check.extend(sorted(memory_dir.glob("*.md"))[:14])
                md_files_dir = profile_dir / "md_files"
                if md_files_dir.exists():
                    files_to_check.extend(sorted(md_files_dir.glob("*.md"))[:14])

            group_dir = self.data_dir / "groups" / group_id
            if group_dir.exists():
                group_files = ["G_GROUP.md", "G_MEMORY.md", "MEMORY.md"]
                for md_file in group_files:
                    file_path = group_dir / md_file
                    if file_path.exists():
                        files_to_check.append(file_path)
                memory_dir = group_dir / "memory"
                if memory_dir.exists():
                    files_to_check.extend(sorted(memory_dir.glob("*.md"))[:14])
                md_files_dir = group_dir / "md_files"
                if md_files_dir.exists():
                    files_to_check.extend(sorted(md_files_dir.glob("*.md"))[:14])

        elif scope == "cross":
            profile_dir = self.data_dir / "profiles" / uid
            if profile_dir.exists():
                personal_files = [
                    "P_PROFILE.md",
                    "PROFILE.md",
                    "P_SOUL.md",
                    "SOUL.md",
                    "P_AGENTS.md",
                    "AGENTS.md",
                    "P_MEMORY.md",
                    "MEMORY.md",
                ]
                for md_file in personal_files:
                    file_path = profile_dir / md_file
                    if file_path.exists():
                        files_to_check.append(file_path)
                memory_dir = profile_dir / "memory"
                if memory_dir.exists():
                    files_to_check.extend(sorted(memory_dir.glob("*.md"))[:14])
                md_files_dir = profile_dir / "md_files"
                if md_files_dir.exists():
                    files_to_check.extend(sorted(md_files_dir.glob("*.md"))[:14])
            joined_groups = self.group_manager.get_user_joined_groups(uid)
            for gid in joined_groups:
                group_dir = self.data_dir / "groups" / gid
                if group_dir.exists():
                    for md_file in ["GROUP.md", "MEMORY.md"]:
                        files_to_check.append(group_dir / md_file)
                    memory_dir = group_dir / "memory"
                    if memory_dir.exists():
                        files_to_check.extend(sorted(memory_dir.glob("*.md"))[:14])
                    md_files_dir = group_dir / "md_files"
                    if md_files_dir.exists():
                        files_to_check.extend(sorted(md_files_dir.glob("*.md"))[:14])

        # 检查文件内容哈希
        for file_path in files_to_check:
            if not file_path.exists():
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
                content_hash = hashlib.md5(content.encode()).hexdigest()

                file_key = str(file_path)
                if self._indexed_content_hashes.get(file_key) != content_hash:
                    return True
            except Exception as e:
                logger.debug(f"[SearchEngine] 读取文件失败 {file_path}: {e}")
                continue

        # 缓存有效且文件无变化，不需要重新索引
        return False

    def _tokenize(self, text: str) -> str:
        """简单的中英文分词"""
        return " ".join(tokenize_for_bm25(text))

    async def index_documents(self, documents: List[SearchResult]):
        """将文档批量索引到 Tantivy"""
        if not self._tantivy_ready or not self.tantivy_index:
            return

        try:
            from tantivy import Document

            def _batch_index():
                if self._tantivy_writer is None:
                    try:
                        self._tantivy_writer = self.tantivy_index.writer()
                    except Exception as e:
                        logger.error(f"[Scriptor] 获取 Tantivy Writer 失败 (可能锁未释放): {e}")
                        return

                try:
                    for doc in documents:
                        doc_scope = doc.scope or ("personal" if doc.group_id == "personal" else "group")
                        d = Document(
                            id=doc.source,
                            content=self._tokenize(doc.content),
                            source=doc.source,
                            source_type=doc.source_type,
                            uid=doc.uid,
                            group_id=doc.group_id,
                            date=doc.date,
                            scope=doc_scope,
                            privacy_level=doc.privacy_level or "",
                        )
                        self._tantivy_writer.add_document(d)
                    self._tantivy_writer.commit()
                except Exception as e:
                    if self._tantivy_writer:
                        self._tantivy_writer.commit()
                    logger.error(f"[Scriptor] Tantivy 批量索引失败: {e}")

            await asyncio.to_thread(_batch_index)
            logger.info(f"[Scriptor] 已索引 {len(documents)} 个文档到 Tantivy")
        except Exception as e:
            logger.error(f"[Scriptor] Tantivy 索引文档失败: {e}")

    async def _search_tantivy(self, query: str, uid: str, group_id: str, scope: str, limit: int) -> List[SearchResult]:
        """使用 Tantivy BM25 搜索（带隐私边界过滤）"""
        results = []

        if not self._tantivy_ready or not self.tantivy_index:
            return results

        is_private_context = group_id == "private"

        try:

            tokenized_query = self._tokenize(query)

            def _search():
                searcher = self.tantivy_index.searcher()
                try:
                    parsed_query = self.tantivy_index.parse_query(tokenized_query)
                    search_result = searcher.search(parsed_query, limit=limit * 3)

                    for hit in search_result.hits:
                        score = hit[0]
                        doc_address = hit[1]
                        retrieved_doc = searcher.doc(doc_address)

                        doc_uid = retrieved_doc.get_first("uid") or ""
                        doc_group_id = retrieved_doc.get_first("group_id") or ""
                        doc_source_type = retrieved_doc.get_first("source_type") or ""
                        doc_scope = retrieved_doc.get_first("scope") or ""
                        doc_privacy_level = retrieved_doc.get_first("privacy_level") or ""

                        if scope == "group":
                            if doc_scope == "group" and doc_group_id == group_id:
                                results.append(
                                    SearchResult(
                                        content=retrieved_doc.get_first("content"),
                                        source=retrieved_doc.get_first("source"),
                                        source_type=doc_source_type,
                                        score=score,
                                        date=retrieved_doc.get_first("date"),
                                        uid=doc_uid,
                                        group_id=doc_group_id,
                                        scope=doc_scope,
                                        privacy_level=doc_privacy_level,
                                    )
                                )
                                continue

                        elif scope == "personal":
                            is_personal_match = doc_scope == "personal" and doc_uid == uid
                            is_current_group_match = doc_scope == "group" and doc_group_id == group_id

                            if is_personal_match or is_current_group_match:
                                if is_personal_match and doc_privacy_level == "private":
                                    continue
                                results.append(
                                    SearchResult(
                                        content=retrieved_doc.get_first("content"),
                                        source=retrieved_doc.get_first("source"),
                                        source_type=doc_source_type,
                                        score=score,
                                        date=retrieved_doc.get_first("date"),
                                        uid=doc_uid,
                                        group_id=doc_group_id,
                                        scope=doc_scope,
                                        privacy_level=doc_privacy_level,
                                    )
                                )
                                continue

                        elif scope == "cross":
                            joined_groups = self.group_manager.get_user_joined_groups(uid)
                            is_personal_match = doc_scope == "personal" and doc_uid == uid
                            is_group_match = doc_scope == "group" and doc_group_id in joined_groups

                            if is_personal_match and doc_privacy_level == "private":
                                continue

                            if is_personal_match or is_group_match:
                                results.append(
                                    SearchResult(
                                        content=retrieved_doc.get_first("content"),
                                        source=retrieved_doc.get_first("source"),
                                        source_type=doc_source_type,
                                        score=score,
                                        date=retrieved_doc.get_first("date"),
                                        uid=doc_uid,
                                        group_id=doc_group_id,
                                        scope=doc_scope,
                                        privacy_level=doc_privacy_level,
                                    )
                                )
                                continue

                except Exception as e:
                    logger.error(f"[Scriptor] Tantivy 搜索执行异常: {e}")

            await asyncio.to_thread(_search)

        except Exception as e:
            logger.error(f"[Scriptor] Tantivy 搜索失败: {e}")

        return results[:limit]

    async def _lazy_init_chromadb(self):
        """后台懒加载初始化 ChromaDB"""
        try:
            # 模拟等待其他组件就绪 (可选)
            await asyncio.sleep(1)

            import chromadb
            from chromadb.config import Settings

            # 初始化双轨制 Embedding 引擎
            self.embedding_fn = DualTrackEmbeddingFunction(
                provider=self.config.embedding_provider,
                api_base=self.config.embedding_api_base,
                api_key=self.config.embedding_api_key,
                model=self.config.embedding_model,
            )

            # 初始化 ChromaDB 客户端 (持久化存储到 global/chroma_db)
            chroma_dir = self.global_dir / "chroma_db"
            chroma_dir.mkdir(parents=True, exist_ok=True)

            # 将耗时的同步操作放入线程池执行，避免阻塞主事件循环
            def _init_db():
                client = chromadb.PersistentClient(path=str(chroma_dir), settings=Settings(anonymized_telemetry=False))
                collection = client.get_or_create_collection(
                    name="scriptor_memories",
                    embedding_function=self.embedding_fn,
                    metadata={"hnsw:space": "cosine"},  # 使用余弦相似度
                )
                return client, collection

            self.chroma_client, self.collection = await asyncio.to_thread(_init_db)
            self._is_ready = True
            logger.info("[Scriptor] ChromaDB 向量数据库后台初始化成功")

        except Exception as e:
            logger.error(f"[Scriptor] ChromaDB 初始化失败: {e}。将降级为纯文本搜索。")
            self.config.embedding_enabled = False
            self._is_ready = True

    async def _wait_for_ready(self, timeout: float = 60.0):
        """
        等待检索引擎就绪（带超时机制防止死锁）

        Args:
            timeout: 超时时间（秒），默认 60 秒

        Raises:
            TimeoutError: 如果超过超时时间仍未就绪
        """
        import time

        start_time = time.time()

        while not self._is_ready:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                logger.error(f"[Scriptor] 检索引擎初始化超时 ({timeout}秒)，将降级为纯文本搜索")
                self.config.embedding_enabled = False
                self._is_ready = True
                return

            await asyncio.sleep(0.1)

    async def add_to_vector_db(self, doc_id: str, content: str, metadata: dict):
        """将记忆添加到向量数据库"""
        await self._wait_for_ready()

        if not self.config.embedding_enabled or not self.collection:
            return

        try:
            # 向量库写入操作可能耗时，放入线程池
            def _upsert():
                self.collection.upsert(documents=[content], metadatas=[metadata], ids=[doc_id])

            await asyncio.to_thread(_upsert)
        except Exception as e:
            logger.error(f"[Scriptor] 添加向量记忆失败: {e}")

    async def search(
        self,
        query: str,
        uid: str,
        group_id: str,
        scope: str = "group",
        limit: int = 5,
        user_raw_message: str = "",
        cross_reason: str = "",
    ) -> List[SearchResult]:
        """
        搜索记忆 (混合检索策略引擎) - 强制执行隐私边界

        Args:
            query: 搜索关键词
            uid: 当前用户ID
            group_id: 当前群体ID
            scope: 搜索范围 (group/personal/cross)
                - group: 仅当前群聊（群聊默认，私聊不可用）
                - personal: 当前群聊 + 私聊（群聊需理由，私聊不可用）
                - cross: 当前群聊 + 私聊 + 所有其他群聊（群聊需理由，私聊默认）
            limit: 返回结果数量
            user_raw_message: 用户的原始消息（用于日志记录）
            cross_reason: 跨界搜索理由（LLM 自证清白）

        Returns:
            搜索结果列表
        """
        await self._wait_for_ready()

        is_private_context = group_id == "private"

        if is_private_context:
            scope = "cross"
            logger.debug(f"[Scriptor] 私聊场景自动使用 cross 范围搜索。" f"Query: {query}, User: {uid}")
        elif scope not in ("personal", "group", "cross"):
            scope = "group"

        if scope == "group" and is_private_context:
            logger.warning(
                f"[Scriptor] 私聊场景不支持 group 范围搜索，自动切换为 cross。" f"Query: {query}, User: {uid}"
            )
            scope = "cross"

        if not is_private_context and scope in ("personal", "cross"):
            if not cross_reason or not cross_reason.strip():
                scope_desc = {"personal": "当前群聊 + 私聊", "cross": "当前群聊 + 私聊 + 所有其他群聊"}
                logger.warning(
                    f"[Scriptor] 拒绝跨场景搜索：未提供理由。" f"Query: {query}, User: {uid}, Group: {group_id}"
                )
                return [
                    SearchResult(
                        content=f"❌ **跨场景搜索权限不足**\n\n"
                        f"您请求了 {scope} 范围搜索（{scope_desc[scope]}），但未提供跨界理由（cross_reason）。\n\n"
                        f"**请重新调用工具，并在 cross_reason 参数中说明：**\n"
                        f"- 为什么需要跨场景搜索？\n"
                        f"- 用户的哪句话表明了跨场景意图？\n\n"
                        f"**示例：**\n"
                        f"```\n"
                        f'cross_reason: "用户说：找找我私聊里说的那个事"\n'
                        f"```\n\n"
                        f"如果用户没有明确表达跨场景意图，请使用 group 范围搜索。",
                        source="system",
                        source_type="error",
                        score=0.0,
                        date="",
                        uid="",
                        group_id="",
                    )
                ]
            else:
                logger.info(
                    f"[Scriptor] 允许跨场景搜索（{scope}）。理由: {cross_reason} | "
                    f"Query: {query}, User: {uid}, Group: {group_id}"
                )

        results = []

        if self.config.embedding_enabled and self.collection:
            # 实体优先的双重检索 (Entity-First Dual Retrieval)
            # 1. 实体/关键词搜索优先 (高精确度)
            text_results = await self._search_text(query, uid, group_id, scope, limit)
            results.extend(text_results)

            # 2. 向量搜索补充 (高召回率)
            if len(results) < limit:
                vector_limit = limit - len(results)
                vector_results = await self._search_vector_db(query, uid, group_id, scope, vector_limit)

                # 去重
                existing_contents = {r.content for r in results}
                for vr in vector_results:
                    if vr.content not in existing_contents:
                        results.append(vr)

            # 增加被检索到的记忆的强度 (活体文件机制)
            for res in results:
                if res.source_type in ["memory", "note"] and hasattr(self.memory_manager, "increase_memory_strength"):
                    # 默认被检索到即视为有用 (is_useful=True)
                    await self.memory_manager.increase_memory_strength(
                        uid, group_id, res.content, res.source, is_useful=True
                    )
        else:
            # 纯文本搜索 (自动降级)
            logger.debug("[Scriptor] 向量引擎不可用，自动降级为纯文本搜索")
            results = await self._search_text(query, uid, group_id, scope, limit)

        results = self._rank_results(results, query)

        return results[:limit]

    async def _search_vector_db(
        self, query: str, uid: str, group_id: str, scope: str, limit: int
    ) -> List[SearchResult]:
        """使用 ChromaDB 进行向量搜索"""
        results = []
        if not self.collection:
            return results

        try:
            where_filter = {}
            if scope == "group":
                where_filter = {"$and": [{"group_id": group_id}, {"scope": "group"}]}
            elif scope == "personal":
                where_filter = {
                    "$or": [
                        {"$and": [{"uid": uid}, {"scope": "personal"}, {"privacy_level": "global"}]},
                        {"$and": [{"group_id": group_id}, {"scope": "group"}]},
                    ]
                }
            elif scope == "cross":
                joined_groups = self.group_manager.get_user_joined_groups(uid)

                if joined_groups:
                    where_filter = {
                        "$or": [
                            {"$and": [{"uid": uid}, {"scope": "personal"}]},
                            {"$and": [{"group_id": {"$in": joined_groups}}, {"scope": "group"}]},
                        ]
                    }
                else:
                    where_filter = {"$and": [{"uid": uid}, {"scope": "personal"}]}
            else:
                where_filter = {"$and": [{"uid": uid}, {"scope": "personal"}]}

            # 执行查询
            query_result = self.collection.query(query_texts=[query], n_results=limit, where=where_filter)

            if query_result and query_result["documents"] and query_result["documents"][0]:
                docs = query_result["documents"][0]
                metas = query_result["metadatas"][0]
                distances = query_result["distances"][0] if "distances" in query_result else [0] * len(docs)

                for doc, meta, dist in zip(docs, metas, distances):
                    score = max(0, 1.0 - dist)
                    doc_scope = meta.get("scope", "")
                    doc_privacy_level = meta.get("privacy_level", "")

                    results.append(
                        SearchResult(
                            content=doc,
                            source=meta.get("source", "vector_db"),
                            source_type=meta.get("source_type", "memory"),
                            score=score * 2.0,
                            date=meta.get("date", ""),
                            uid=meta.get("uid", ""),
                            group_id=meta.get("group_id", ""),
                            scope=doc_scope,
                            privacy_level=doc_privacy_level,
                        )
                    )
        except Exception as e:
            logger.error(f"[Scriptor] 向量搜索失败: {e}")

        return results

    async def _search_text(self, query: str, uid: str, group_id: str, scope: str, limit: int) -> List[SearchResult]:
        """纯文本关键词搜索 (优先使用 Tantivy BM25)"""
        # 1. 优先使用 Tantivy (高性能)
        if self._tantivy_ready and self.tantivy_index:
            # 检查索引是否需要更新（使用缓存机制）
            cache_key = f"{uid}_{group_id}_{scope}"
            needs_reindex = await self._check_and_update_index(uid, group_id, scope, cache_key)

            if needs_reindex:
                all_docs = await self._collect_all_docs_for_indexing(uid, group_id, scope)
                if all_docs:
                    await self.index_documents(all_docs)
                    # 更新缓存
                    self._index_cache[cache_key] = time.time()
                    self._save_index_cache()

            # 执行 Tantivy 搜索
            tantivy_results = await self._search_tantivy(query, uid, group_id, scope, limit)
            if tantivy_results:
                return tantivy_results

        # 2. 降级使用 Python BM25
        results = []
        all_docs = []

        # 1. 收集所有相关文档
        if scope == "group":
            all_docs.extend(await self._collect_group_docs(group_id))
        elif scope == "personal":
            all_docs.extend(await self._collect_group_docs(group_id))
            all_docs.extend(await self._collect_personal_docs(uid, group_id))
        elif scope == "cross":
            all_docs.extend(await self._collect_personal_docs(uid, group_id))
            joined_groups = self.group_manager.get_user_joined_groups(uid)
            for gid in joined_groups:
                all_docs.extend(await self._collect_group_docs(gid))

        if not all_docs:
            return results

        # 2. 准备 BM25 语料库
        corpus = [tokenize_for_bm25(doc.content) for doc in all_docs]
        query_tokens = tokenize_for_bm25(query)

        if not query_tokens:
            return results

        # 3. 使用缓存的 BM25 实例
        cache_key = f"{uid}_{group_id}_{scope}"
        if (
            self._bm25_corpus_cache is None
            or self._bm25_corpus_ids is None
            or len(self._bm25_corpus_cache) != len(corpus)
        ):
            self._bm25_corpus_cache = corpus
            self._bm25_corpus_ids = [doc.source for doc in all_docs]
            self.bm25.fit(self._bm25_corpus_cache)

        scores = self.bm25.get_scores(query_tokens)

        # 4. 组装结果并过滤零分
        for doc, score in zip(all_docs, scores):
            if score > 0:
                doc.score = score
                results.append(doc)

        # 5. 排序并截断
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:limit]

    async def _collect_all_docs_for_indexing(self, uid: str, group_id: str, scope: str) -> List[SearchResult]:
        """收集所有需要索引的文档"""
        all_docs = []

        if scope == "group":
            all_docs.extend(await self._collect_group_docs(group_id))
        elif scope == "personal":
            all_docs.extend(await self._collect_group_docs(group_id))
            all_docs.extend(await self._collect_personal_docs(uid, group_id))
        elif scope == "cross":
            all_docs.extend(await self._collect_personal_docs(uid, group_id))
            joined_groups = self.group_manager.get_user_joined_groups(uid)
            for gid in joined_groups:
                all_docs.extend(await self._collect_group_docs(gid))

        return all_docs

    async def _collect_personal_docs(self, uid: str, current_group_id: str = "private") -> List[SearchResult]:
        """收集个人记忆文档（优化：限制单文件大小，防止内存峰值）

        Args:
            uid: 用户ID
            current_group_id: 当前群聊ID，用于判断是否需要过滤 private 级别记忆
        """
        docs = []
        profile_dir = self.data_dir / "profiles" / uid
        if not profile_dir.exists():
            return docs

        MAX_FILE_SIZE = 1024 * 1024
        is_group_context = current_group_id != "private"

        personal_files = [
            ("P_PROFILE.md", "PROFILE.md"),
            ("P_SOUL.md", "SOUL.md"),
            ("P_AGENTS.md", "AGENTS.md"),
            ("P_MEMORY.md", "MEMORY.md"),
        ]

        for new_name, old_name in personal_files:
            file_path = profile_dir / new_name
            if not file_path.exists():
                file_path = profile_dir / old_name
            if file_path.exists():
                content = self._read_file_with_limit(file_path, MAX_FILE_SIZE)

                if is_group_context and "MEMORY" in file_path.name.upper():
                    content = self._filter_private_memories(content)

                if content.strip():
                    docs.append(
                        SearchResult(
                            content=content,
                            source=file_path.name,
                            source_type="personal",
                            score=0.0,
                            date=datetime.now().strftime("%Y-%m-%d"),
                            uid=uid,
                            group_id="personal",
                            scope="personal",
                            privacy_level="global" if is_group_context else "private",
                        )
                    )

        memory_dir = profile_dir / "memory"
        if memory_dir.exists():
            for md_file in sorted(memory_dir.glob("*.md"), reverse=True)[:14]:
                content = self._read_file_with_limit(md_file, MAX_FILE_SIZE)

                if is_group_context:
                    content = self._filter_private_memories(content)

                if content.strip():
                    docs.append(
                        SearchResult(
                            content=content,
                            source=f"memory/{md_file.name}",
                            source_type="note",
                            score=0.0,
                            date=md_file.stem,
                            uid=uid,
                            group_id="personal",
                            scope="personal",
                            privacy_level="global" if is_group_context else "private",
                        )
                    )

        return docs

    def _filter_private_memories(self, content: str) -> str:
        """过滤掉 [Privacy: private] 标记的记忆块（用于群聊场景保护隐私）"""
        blocks = re.split(r"(?=### \[)", content)
        filtered_blocks = []

        for block in blocks:
            if not block.strip():
                continue
            # 检查是否包含 [Privacy: private] 标记
            if "[Privacy: private]" not in block:
                filtered_blocks.append(block)

        return "\n".join(filtered_blocks)

    def _read_file_with_limit(self, file_path: Path, max_size: int = 1024 * 1024) -> str:
        """安全读取文件，超过阈值则截断并记录警告"""
        try:
            file_size = file_path.stat().st_size
            if file_size > max_size:
                logger.warning(f"[Scriptor] 文件过大已截断: {file_path.name} ({file_size} bytes)")
                with open(file_path, "r", encoding="utf-8") as f:
                    return f.read(max_size) + f"\n\n[... 内容已截断，原始大小: {file_size} bytes ...]"
            else:
                return file_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"[Scriptor] 读取文件失败 {file_path}: {e}")
            return ""

    async def _collect_group_docs(self, group_id: str) -> List[SearchResult]:
        """收集群体记忆文档（优化：限制单文件大小）"""
        docs = []
        group_dir = self.data_dir / "groups" / group_id
        if not group_dir.exists():
            return docs

        MAX_FILE_SIZE = 1024 * 1024

        group_files = [("G_GROUP.md", "GROUP.md"), ("G_MEMORY.md", "MEMORY.md")]

        for new_name, old_name in group_files:
            file_path = group_dir / new_name
            if not file_path.exists():
                file_path = group_dir / old_name
            if file_path.exists():
                content = self._read_file_with_limit(file_path, MAX_FILE_SIZE)
                docs.append(
                    SearchResult(
                        content=content,
                        source=file_path.name,
                        source_type="group",
                        score=0.0,
                        date=datetime.now().strftime("%Y-%m-%d"),
                        uid="",
                        group_id=group_id,
                        scope="group",
                        privacy_level="group",
                    )
                )

        memory_dir = group_dir / "memory"
        if memory_dir.exists():
            for md_file in sorted(memory_dir.glob("*.md"), reverse=True)[:14]:
                content = self._read_file_with_limit(md_file, MAX_FILE_SIZE)
                docs.append(
                    SearchResult(
                        content=content,
                        source=f"memory/{md_file.name}",
                        source_type="note",
                        score=0.0,
                        date=md_file.stem,
                        uid="",
                        group_id=group_id,
                        scope="group",
                        privacy_level="group",
                    )
                )

        return docs

    def _rank_results(self, results: List[SearchResult], query: str) -> List[SearchResult]:
        """排序结果 (包含三档衰减策略和可选的 Rerank)"""
        query_lower = query.lower()

        for result in results:
            # 实体优先：个人画像匹配权重提升
            if result.source_type == "personal" and query_lower in result.content.lower():
                result.score *= 1.5

            # 提取 useful_score (如果有)
            useful_score = 5.0  # 默认值
            score_match = re.search(r"\[Score:\s*([\d\.]+)\]", result.content)
            if score_match:
                useful_score = float(score_match.group(1))

            # 时间衰减策略 (结合 useful_score)
            if "memory" in result.source.lower() and result.date:
                try:
                    file_date = datetime.strptime(result.date, "%Y-%m-%d")
                    days_ago = (datetime.now() - file_date).days

                    # 细化三档衰减策略 (基于有用性)
                    if useful_score >= 10.0:
                        # T2 (永存档): 极慢衰减或不衰减
                        decay_factor = max(0.9, 1.0 - days_ago * 0.001)
                    elif useful_score >= 5.0:
                        # T1 (待证档): 缓慢衰减
                        decay_factor = max(0.6, 1.0 - days_ago * 0.01)
                    else:
                        # T0 (易逝档): 快速衰减
                        decay_factor = max(0.2, 1.0 - days_ago * 0.05)

                    result.score *= decay_factor
                except:
                    pass

            # 动态权重调整：根据查询意图调整分数
            # 如果查询包含时间词，提升近期记忆的权重
            time_keywords = ["最近", "刚才", "今天", "昨天", "前几天", "上周"]
            if any(kw in query_lower for kw in time_keywords) and result.date:
                try:
                    file_date = datetime.strptime(result.date, "%Y-%m-%d")
                    days_ago = (datetime.now() - file_date).days
                    if days_ago <= 7:
                        result.score *= 1.3
                except:
                    pass

            # 如果查询包含偏好词，提升个人画像和偏好记忆的权重
            preference_keywords = ["喜欢", "讨厌", "偏好", "习惯", "爱"]
            if any(kw in query_lower for kw in preference_keywords):
                if result.source_type == "personal" or "preference" in result.content.lower():
                    result.score *= 1.4

        # 初步排序
        sorted_results = sorted(results, key=lambda x: x.score, reverse=True)

        # 可选的 Rerank 重排序 (如果配置了 rerank 模型)
        if hasattr(self.config, "rerank_enabled") and self.config.rerank_enabled:
            sorted_results = self._apply_rerank(query, sorted_results)

        return sorted_results

    def _apply_rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        """应用 Rerank 模型进行重排序"""
        if not self.config.rerank_enabled or len(results) <= 1:
            return results

        try:
            # 准备 rerank 输入
            documents = [result.content for result in results]

            # 尝试使用 API 方式调用 rerank 模型
            if self.config.rerank_provider == "api":
                reranked_results = self._rerank_via_api(query, documents, results)
                if reranked_results:
                    logger.info(f"[Scriptor] Rerank 完成，已重排序 {len(reranked_results)} 条结果")
                    return reranked_results

            # 如果 API 方式失败或未配置，使用简单的启发式 rerank 作为降级方案
            logger.debug("[Scriptor] 使用启发式 rerank 作为降级方案")
            return self._heuristic_rerank(query, results)

        except Exception as e:
            logger.error(f"[Scriptor] Rerank 失败: {e}，使用原始结果")
            return results

    def _heuristic_rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        """启发式 rerank（基于关键词匹配的简单重排序）"""
        query_lower = query.lower()
        query_tokens = set(re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fa5]", query_lower))

        for result in results:
            content_lower = result.content.lower()
            # 计算关键词匹配度
            content_tokens = set(re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fa5]", content_lower))
            overlap = len(query_tokens & content_tokens)
            # 增加匹配度权重
            result.score += overlap * 0.5

            # 完全匹配的关键词额外加分
            for token in query_tokens:
                if token in content_lower:
                    result.score += 1.0

        # 重新排序
        results.sort(key=lambda x: x.score, reverse=True)
        return results

    def _rerank_via_api(
        self, query: str, documents: List[str], original_results: List[SearchResult]
    ) -> Optional[List[SearchResult]]:
        """通过 API 调用 rerank 模型"""
        try:
            import threading

            import httpx

            api_base = self.config.rerank_api_base.rstrip("/")
            api_key = self.config.rerank_api_key
            model = self.config.rerank_model

            # 构建请求
            url = f"{api_base}/rerank"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "query": query,
                "documents": documents,
                "top_n": min(self.config.rerank_top_k, len(original_results)),
            }

            # 使用线程执行同步 HTTP 请求，避免阻塞主线程
            result_holder = []
            exception_holder = []

            def _sync_request():
                try:
                    with httpx.Client(timeout=30.0) as client:
                        response = client.post(url, json=payload, headers=headers)
                        response.raise_for_status()
                        result_holder.append(response.json())
                except Exception as e:
                    exception_holder.append(e)

            thread = threading.Thread(target=_sync_request, daemon=True)
            thread.start()
            thread.join(timeout=30)

            if exception_holder:
                raise exception_holder[0]

            if not result_holder:
                return None

            data = result_holder[0]

            # 解析 rerank 结果
            if "results" in data:
                # 创建索引映射
                doc_index_map = {doc: idx for idx, doc in enumerate(documents)}
                reranked = []

                for item in data["results"]:
                    idx = item.get("index")
                    if idx is not None and 0 <= idx < len(original_results):
                        result = original_results[idx]
                        # 更新分数
                        result.score = item.get("relevance_score", result.score)
                        reranked.append(result)

                if reranked:
                    return reranked

            return None

        except ImportError:
            logger.warning("[Scriptor] 未安装 httpx，无法使用 API rerank")
            return None
        except Exception as e:
            logger.debug(f"[Scriptor] API rerank 调用失败: {e}")
            return None

    def format_results(self, results: List[SearchResult], group_id: str = "private") -> str:
        """格式化搜索结果

        Args:
            results: 搜索结果列表
            group_id: 当前群聊ID，用于判断是否需要分组输出（群聊场景下保护隐私）
        """
        if not results:
            return "（未找到相关记录）"

        is_group_context = group_id != "private"

        personal_results = [r for r in results if r.source_type in ("personal", "note")]
        group_results = [r for r in results if r.source_type in ("group", "cross_group")]

        if is_group_context and personal_results:
            return self._format_grouped_results(personal_results, group_results)

        return self._format_flat_results(results)

    def _format_flat_results(self, results: List[SearchResult]) -> str:
        """扁平化输出格式（原有逻辑）"""
        parts = []
        for i, result in enumerate(results, 1):
            source_label = {"personal": "个人画像", "group": "群体记忆", "note": "日记", "cross_group": "跨群记忆"}.get(
                result.source_type, result.source_type
            )

            parts.append(f"### {i}. [{source_label}] {result.source}\n" f"{result.content}\n")

        return "\n\n".join(parts)

    def _format_grouped_results(self, personal_results: List[SearchResult], group_results: List[SearchResult]) -> str:
        """分组输出格式（群聊场景下保护隐私）"""
        parts = ["【搜索结果汇总】\n"]

        if group_results:
            parts.append("=== 🟢 群聊记忆 (可公开引用) ===")
            for i, result in enumerate(group_results, 1):
                parts.append(
                    f"{i}. [{result.date}] (来源: {result.source})\n"
                    f"   {result.content[:500]}{'...' if len(result.content) > 500 else ''}"
                )
            parts.append("")

        if personal_results:
            parts.append("=== 🔴 [⚠️私有] 个人记忆 (仅供内部理解，严禁泄露) ===")
            for i, result in enumerate(personal_results, 1):
                parts.append(
                    f"{i}. [{result.date}] (来源: {result.source})\n"
                    f"   {result.content[:500]}{'...' if len(result.content) > 500 else ''}"
                )
            parts.append("\n⚠️ 以上私有信息可用于理解用户意图，但绝不可在回复中直接透露具体内容。")

        return "\n".join(parts)

    async def _optimize_duplicate_memories(self):
        """优化重复记忆（检测相似记忆并生成合并建议）"""
        try:
            if not self.config.embedding_enabled or not self.collection:
                return

            logger.info("[Scriptor] 正在执行记忆优化检查...")

            # 1. 收集所有记忆
            all_memories = []
            try:
                offset = 0
                batch_size = 100
                while True:
                    result = self.collection.get(limit=batch_size, offset=offset, include=["metadatas", "documents"])
                    if not result or not result["ids"]:
                        break

                    for idx, doc_id in enumerate(result["ids"]):
                        metadata = result["metadatas"][idx] if result["metadatas"] else {}
                        content = result["documents"][idx] if result["documents"] else ""

                        # 只关注 MEMORY.md 中的记忆，跳过 PROFILE.md 和 GROUP.md
                        if metadata.get("source_type") == "memory" and metadata.get("source") == "MEMORY.md":
                            all_memories.append(
                                {
                                    "id": doc_id,
                                    "content": content,
                                    "metadata": metadata,
                                    "uid": metadata.get("uid", "*"),
                                    "group_id": metadata.get("group_id", "*"),
                                }
                            )

                    offset += batch_size
                    if len(result["ids"]) < batch_size:
                        break
            except Exception as e:
                logger.warning(f"[Scriptor] 获取所有记忆失败: {e}")
                return

            if len(all_memories) < 2:
                logger.info("[Scriptor] 记忆数量不足，跳过相似性检测")
                return

            logger.info(f"[Scriptor] 开始检测 {len(all_memories)} 条记忆的相似性...")

            # 2. 按用户和群组分组，只比较同一 scope 内的记忆
            from collections import defaultdict

            memory_groups = defaultdict(list)
            for mem in all_memories:
                scope_key = f"{mem['uid']}_{mem['group_id']}"
                memory_groups[scope_key].append(mem)

            # 3. 使用向量数据库的相似度检索替代 O(N²) 双重循环
            # 每个分组内，对于每条记忆，查询最相似的 N 条，效率从 O(N²) 降至 O(N)
            duplicate_candidates = []

            for scope_key, memories in memory_groups.items():
                if len(memories) < 2:
                    continue

                # 获取该分组内所有记忆的 ID 和内容
                group_ids = [mem["id"] for mem in memories]
                group_contents = {mem["id"]: mem["content"] for mem in memories}

                # 批量查询每个记忆的相似记忆（使用向量数据库的 query 功能）
                # 每次查询 top_k=5，效率远高于双重循环
                try:
                    query_results = self.collection.query(
                        query_texts=[mem["content"] for mem in memories],
                        n_results=5,
                        where={
                            "$and": [
                                {"uid": scope_key.split("_")[0]},
                                {"group_id": scope_key.split("_", 1)[1] if "_" in scope_key else "*"},
                            ]
                        },
                        include=["metadatas", "documents", "distances"],
                    )

                    # 解析查询结果
                    for i, query_id in enumerate(group_ids):
                        if not query_results or not query_results.get("ids") or i >= len(query_results["ids"]):
                            continue

                        result_ids = query_results["ids"][i]
                        distances = query_results["distances"][i] if "distances" in query_results else []

                        for j, result_id in enumerate(result_ids):
                            # 跳过自己
                            if result_id == query_id:
                                continue

                            # 计算相似度（距离越小越相似，1 - distance = 相似度）
                            if j < len(distances):
                                similarity = 1.0 - distances[j]
                            else:
                                # 如果没有距离信息，跳过
                                continue

                            # 相似度阈值判断
                            if similarity > 0.85:  # 提高阈值以减少误判
                                # 检查是否已经存在于候选中（避免重复添加）
                                exists = any(
                                    (c["mem1"]["id"] == query_id and c["mem2"]["id"] == result_id)
                                    or (c["mem1"]["id"] == result_id and c["mem2"]["id"] == query_id)
                                    for c in duplicate_candidates
                                )

                                if not exists:
                                    duplicate_candidates.append(
                                        {
                                            "mem1": memories[i],
                                            "mem2": {
                                                **memories[i],
                                                "id": result_id,
                                                "content": group_contents.get(result_id, ""),
                                            },
                                            "similarity": similarity,
                                            "scope_key": scope_key,
                                        }
                                    )

                except Exception as e:
                    logger.warning(f"[Scriptor] 向量相似度检索失败，回退到简单比较: {e}")

            # 4. 生成优化报告
            if duplicate_candidates:
                logger.warning(f"[Scriptor] 发现 {len(duplicate_candidates)} 对潜在重复记忆！")

                # 保存重复记忆建议到文件
                try:
                    import json

                    report_file = self.data_dir / "duplicate_suggestions.json"
                    report_data = {
                        "timestamp": datetime.now().isoformat(),
                        "total_candidates": len(duplicate_candidates),
                        "suggestions": [
                            {
                                "similarity": cand["similarity"],
                                "scope": cand["scope_key"],
                                "mem1_id": cand["mem1"]["id"],
                                "mem1_content": cand["mem1"]["content"][:100],
                                "mem2_id": cand["mem2"]["id"],
                                "mem2_content": cand["mem2"]["content"][:100],
                                "action": "建议合并或删除其中一条",
                            }
                            for cand in duplicate_candidates[:20]  # 只保存前20条
                        ],
                    }

                    with open(report_file, "w", encoding="utf-8") as f:
                        json.dump(report_data, f, ensure_ascii=False, indent=2)

                    logger.info(f"[Scriptor] 重复记忆建议已保存到: {report_file}")
                except Exception as e:
                    logger.error(f"[Scriptor] 保存重复记忆建议失败: {e}")
            else:
                logger.info("[Scriptor] 未发现明显的重复记忆")

            logger.info("[Scriptor] 记忆优化检查完成！")

        except Exception as e:
            logger.error(f"[Scriptor] 记忆优化检查失败: {e}")

    def _calculate_simple_similarity(self, text1: str, text2: str) -> float:
        """计算两个文本的简单相似度（基于关键词重叠）"""
        return jaccard_similarity(text1, text2)
