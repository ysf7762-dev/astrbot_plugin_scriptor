# core/knowledge_base.py
"""
知识库系统模块 - 借鉴 Angel Memory 设计理念
核心特点：
1. 条目设计（1000字以内，支持复杂业务逻辑）
2. 主动学习而非被动RAG
3. research_topic 研究工具
4. 知识库不是RAG，而是主动学习源
优化：搜索功能复用 SearchEngine (Tantivy/BM25)，避免低效的字符串遍历
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


class KnowledgeType(Enum):
    """知识类型"""

    FACT = "fact"  # 事实知识
    SKILL = "skill"  # 技能
    PREFERENCE = "preference"  # 偏好
    RULE = "rule"  # 规则
    EXPERIENCE = "experience"  # 经验
    REFERENCE = "reference"  # 参考资料


@dataclass
class KnowledgeItem:
    """知识条目 - 支持1000字以内的完整业务逻辑描述"""

    id: str = ""
    title: str = ""  # 标题（一句话概括）
    content: str = ""  # 内容（1000字以内）
    knowledge_type: KnowledgeType = KnowledgeType.FACT
    tags: List[str] = field(default_factory=list)
    category: str = ""  # 分类标签
    created_at: str = ""
    updated_at: str = ""
    is_active: bool = True  # 主动知识，永不衰减
    source: str = ""  # 来源（必填，支持溯源）
    useful_count: int = 0  # 被使用次数
    useful_score: float = 5.0  # 有用性评分

    def is_short_enough(self) -> bool:
        """检查是否是合格的条目（1000字以内）"""
        return len(self.content) <= 1000

    def to_markdown(self) -> str:
        """转换为 Markdown 格式"""
        lines = []

        # 标题行
        type_tag = f"({self.knowledge_type.value})"
        active_tag = " [Active: true]" if self.is_active else ""
        header = f"## {self.title}"
        lines.append(header)

        # 元数据
        if self.tags:
            tag_str = ", ".join(f"[{tag}]" for tag in self.tags)
            lines.append(f"**标签**: {tag_str}")

        if self.category:
            lines.append(f"**分类**: {self.category}")

        # 内容
        lines.append(f"\n{self.content}")

        # 元数据行
        meta_parts = []
        if self.created_at:
            meta_parts.append(f"创建: {self.created_at}")
        if self.useful_count > 0:
            meta_parts.append(f"使用: {self.useful_count}次")
        if self.source:
            meta_parts.append(f"来源: {self.source}")

        if meta_parts:
            lines.append(f"\n---\n*{' | '.join(meta_parts)}")

        return "\n".join(lines) + "\n"

    @classmethod
    def create(
        cls,
        title: str,
        content: str,
        knowledge_type: KnowledgeType = KnowledgeType.FACT,
        tags: List[str] = None,
        category: str = "",
        is_active: bool = True,
        source: str = "",
    ) -> "KnowledgeItem":
        """创建知识条目"""
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

        if len(content) > 1000:
            logger.warning(f"[KnowledgeBase] 内容超过1000字，建议精简: {len(content)}字")

        return cls(
            id=cls._generate_id(title, content),
            title=title,
            content=content,
            knowledge_type=knowledge_type,
            tags=tags or [],
            category=category,
            created_at=timestamp,
            updated_at=timestamp,
            is_active=is_active,
            source=source,
            useful_count=0,
            useful_score=5.0,
        )

    @staticmethod
    def _generate_id(title: str, content: str) -> str:
        """生成知识ID"""
        import hashlib

        combined = f"{title}_{content[:50]}"
        return hashlib.md5(combined.encode()).hexdigest()[:12]


class KnowledgeBase:
    """知识库 - 主动学习源，而非被动RAG

    存储路径：global/knowledge/（全局共享）
    优化：支持注入 SearchEngine 以使用 Tantivy/BM25 加速搜索
    """

    MAX_CONTENT_LENGTH = 1000  # 最大1000字，支持复杂业务逻辑的完整描述

    def __init__(self, data_dir: Path, search_engine=None):
        self.data_dir = data_dir

        # 全局目录路径
        self.global_dir = data_dir / "global"
        self.global_dir.mkdir(parents=True, exist_ok=True)

        # 新路径：global/knowledge/
        self.knowledge_dir = self.global_dir / "knowledge"
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

        # 迁移旧数据
        self._migrate_from_legacy_path()

        self._items: Dict[str, KnowledgeItem] = {}
        self._search_engine = search_engine  # 可选：SearchEngine 实例
        self._load_all()

    def _migrate_from_legacy_path(self):
        """迁移旧路径的数据到 global/ 目录"""
        legacy_dir = self.data_dir / "knowledge"

        if legacy_dir.exists() and legacy_dir != self.knowledge_dir:
            # 检查旧目录是否有内容
            legacy_kb_file = legacy_dir / "KNOWLEDGE_BASE.md"
            if legacy_kb_file.exists():
                target_file = self.knowledge_dir / "KNOWLEDGE_BASE.md"
                if not target_file.exists():
                    try:
                        import shutil

                        shutil.copy2(str(legacy_kb_file), str(target_file))
                        logger.info("[KnowledgeBase] 已迁移知识库数据到 global/ 目录")
                        # 保留旧文件作为备份，但不删除
                    except Exception as e:
                        logger.warning(f"[KnowledgeBase] 迁移知识库数据失败: {e}")

    def set_search_engine(self, search_engine):
        """注入 SearchEngine 实例以启用 BM25 加速搜索"""
        self._search_engine = search_engine
        logger.info("[KnowledgeBase] SearchEngine 已注入，启用 BM25 加速搜索")

    def _load_all(self):
        """加载所有知识条目"""
        kb_file = self.knowledge_dir / "KNOWLEDGE_BASE.md"

        if not kb_file.exists():
            return

        try:
            content = kb_file.read_text(encoding="utf-8")
            self._parse_markdown(content)
            logger.info(f"[KnowledgeBase] 知识库加载完成: {len(self._items)} 条")

            # 启动时自动去重
            self._deduplicate()
        except Exception as e:
            logger.error(f"[KnowledgeBase] 加载知识库失败: {e}")

    def _strip_title_suffix(self, title: str) -> str:
        """去除标题中的括号后缀（如"更新版""修订版"）"""
        import re as _re

        return _re.sub(r"[（\(][^）\)]*[）\)]", "", title).strip()

    def _deduplicate(self):
        """启动时自动去重：相似标题保留最新条目"""
        if len(self._items) <= 1:
            return

        remove_ids = set()
        items_list = list(self._items.values())

        for i in range(len(items_list)):
            if items_list[i].id in remove_ids:
                continue
            for j in range(i + 1, len(items_list)):
                if items_list[j].id in remove_ids:
                    continue

                item_a = items_list[i]
                item_b = items_list[j]

                base_a = self._strip_title_suffix(item_a.title).lower()
                base_b = self._strip_title_suffix(item_b.title).lower()

                is_duplicate = False
                if base_a == base_b and base_a:
                    is_duplicate = True
                elif item_a.title == item_b.title:
                    is_duplicate = True

                if not is_duplicate:
                    continue

                # 保留更新的条目（按 created_at 比较）
                time_a = item_a.created_at or ""
                time_b = item_b.created_at or ""

                if time_b >= time_a:
                    loser = item_a
                    winner = item_b
                else:
                    loser = item_b
                    winner = item_a

                remove_ids.add(loser.id)
                logger.info(
                    f"[KnowledgeBase] 去重: 移除旧条目「{loser.title}」(创建: {loser.created_at}), "
                    f"保留新条目「{winner.title}」(创建: {winner.created_at})"
                )

        if remove_ids:
            for rid in remove_ids:
                del self._items[rid]
            self._save_all()
            logger.info(f"[KnowledgeBase] 去重完成: 移除 {len(remove_ids)} 条重复条目，剩余 {len(self._items)} 条")

    def _parse_markdown(self, content: str):
        """解析Markdown格式的知识库"""
        self._items = {}

        sections = re.split(r"\n---\n", content)

        for section in sections:
            section = section.strip()
            if not section:
                continue

            item = self._parse_section(section)
            if item:
                self._items[item.id] = item

    def _parse_section(self, section: str) -> Optional[KnowledgeItem]:
        """解析单个知识条目"""
        try:
            lines = section.split("\n")
            title = ""
            content_lines = []
            tags = []
            category = ""
            knowledge_type = KnowledgeType.FACT
            is_active = True
            source = ""
            useful_count = 0
            useful_score = 5.0
            created_at = ""
            updated_at = ""

            in_meta = False
            meta_line = ""

            for line in lines:
                line = line.strip()

                if line.startswith("## "):
                    title = line[3:].strip()
                elif line.startswith("**标签**:"):
                    tag_str = line[len("**标签**:") :].strip()
                    tags = [t.strip("[] ") for t in tag_str.split(",") if t.strip()]
                elif line.startswith("**分类**:"):
                    category = line[len("**分类**:") :].strip()
                elif line.startswith("---"):
                    in_meta = True
                elif in_meta and line.startswith("*"):
                    meta_line = line[1:].strip()
                elif not in_meta and line and not line.startswith("#"):
                    content_lines.append(line)

            content = "\n".join(content_lines).strip()

            if meta_line:
                meta_parts = [p.strip() for p in meta_line.split("|")]
                for part in meta_parts:
                    if part.startswith("创建:"):
                        created_at = part[len("创建:") :].strip()
                    elif part.startswith("使用:"):
                        use_str = part[len("使用:") :].strip()
                        if use_str.endswith("次"):
                            use_str = use_str[:-1]
                        try:
                            useful_count = int(use_str)
                        except:
                            pass
                    elif part.startswith("来源:"):
                        source = part[len("来源:") :].strip()

            if not title:
                return None

            if not created_at:
                created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if not updated_at:
                updated_at = created_at

            item = KnowledgeItem(
                id=KnowledgeItem._generate_id(title, content),
                title=title,
                content=content,
                knowledge_type=knowledge_type,
                tags=tags,
                category=category,
                created_at=created_at,
                updated_at=updated_at,
                is_active=is_active,
                source=source,
                useful_count=useful_count,
                useful_score=useful_score,
            )

            return item

        except Exception as e:
            logger.warning(f"[KnowledgeBase] 解析知识条目失败: {e}, section={section[:50]}...")
            return None

    def _save_all(self):
        """保存所有知识条目"""
        kb_file = self.knowledge_dir / "KNOWLEDGE_BASE.md"

        try:
            lines = ["# Scriptor 知识库\n"]
            lines.append("> 条目设计，每条不超过1000字，主动学习源\n\n")

            for item in self._items.values():
                lines.append(item.to_markdown())
                lines.append("\n---\n")

            kb_file.write_text("\n".join(lines), encoding="utf-8")
            logger.debug(f"[KnowledgeBase] 知识库已保存: {len(self._items)} 条")
        except Exception as e:
            logger.error(f"[KnowledgeBase] 保存知识库失败: {e}")
            raise

    def add_item(self, item: KnowledgeItem) -> bool:
        """添加知识条目"""
        if not item.is_short_enough():
            logger.warning(f"[KnowledgeBase] 建议精简内容: {len(item.content)}字")

        try:
            self._items[item.id] = item
            self._save_all()
            logger.info(f"[KnowledgeBase] 添加知识: {item.title}")
            return True
        except Exception as e:
            logger.error(f"[KnowledgeBase] 添加知识条目失败: {e}")
            if item.id in self._items:
                del self._items[item.id]
            return False

    def get_item(self, item_id: str) -> Optional[KnowledgeItem]:
        """获取知识条目"""
        return self._items.get(item_id)

    def search(self, query: str, limit: int = 5) -> List[KnowledgeItem]:
        """搜索知识库（优先使用 SearchEngine 加速，否则回退到本地匹配）"""
        has_chinese = any("\u4e00" <= char <= "\u9fff" for char in query)
        if has_chinese:
            return self._search_local(query, limit)

        if self._search_engine and hasattr(self._search_engine, "bm25"):
            return self._search_bm25(query, limit)

        return self._search_local(query, limit)

    def _search_bm25(self, query: str, limit: int) -> List[KnowledgeItem]:
        """使用 SearchEngine 的 BM25 索引搜索"""
        try:
            docs = []
            for item in self._items.values():
                docs.append(item.title + " " + item.content)

            if not docs:
                return []

            # 分词（简单的空格分词）
            query_tokens = query.lower().split()
            corpus_tokens = [doc.lower().split() for doc in docs]

            # 使用 SearchEngine 的 BM25
            self._search_engine.bm25.fit(corpus_tokens)
            scores = self._search_engine.bm25.get_scores(query_tokens)

            # 排序并返回结果
            items_list = list(self._items.values())
            scored_results = [(score, items_list[i]) for i, score in enumerate(scores) if score > 0]
            scored_results.sort(key=lambda x: (x[0], x[1].useful_score, x[1].useful_count), reverse=True)

            return [item for (_, item) in scored_results[:limit]]
        except Exception as e:
            logger.warning(f"[KnowledgeBase] BM25 搜索失败，回退到本地搜索: {e}")
            return self._search_local(query, limit)

    def _search_local(self, query: str, limit: int = 5) -> List[KnowledgeItem]:
        """本地字符串匹配搜索（回退方案）"""
        results = []
        query_lower = query.lower()

        for item in self._items.values():
            score = 0.0

            if query_lower in item.title.lower():
                score += 3.0

            if query_lower in item.content.lower():
                score += 2.0

            for tag in item.tags:
                if query_lower in tag.lower():
                    score += 1.5

            if item.category and query_lower in item.category.lower():
                score += 1.0

            if score > 0:
                results.append((score, item))

        results.sort(key=lambda x: (x[0], x[1].useful_score, x[1].useful_count), reverse=True)

        return [item for (score, item) in results[:limit]]

    def record_usage(self, item_id: str):
        """记录知识使用（强化学习）"""
        if item_id in self._items:
            item = self._items[item_id]
            item.useful_count += 1
            item.useful_score = min(10.0, item.useful_score + 0.5)
            item.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_all()
            logger.debug(f"[KnowledgeBase] 知识使用: {item.title}")

    def get_all_categories(self) -> List[str]:
        """获取所有分类"""
        categories = set()
        for item in self._items.values():
            if item.category:
                categories.add(item.category)
        return sorted(list(categories))

    def get_by_category(self, category: str) -> List[KnowledgeItem]:
        """按分类获取知识"""
        return [item for item in self._items.values() if item.category == category]

    @staticmethod
    def validate_content(content: str) -> tuple[bool, str]:
        """验证内容是否符合短条目要求"""
        if len(content) > KnowledgeBase.MAX_CONTENT_LENGTH:
            return False, f"内容过长: {len(content)} 字，建议不超过 {KnowledgeBase.MAX_CONTENT_LENGTH} 字"

        if len(content.strip()) == 0:
            return False, "内容不能为空"

        return True, "OK"

    def get_all_items(self) -> List[KnowledgeItem]:
        """获取所有知识条目"""
        return list(self._items.values())

    def delete_item(self, item_id: str) -> bool:
        """删除知识条目"""
        if item_id in self._items:
            del self._items[item_id]
            self._save_all()
            logger.info(f"[KnowledgeBase] 删除知识: {item_id}")
            return True
        return False

    def update_item(self, item_id: str, **kwargs) -> bool:
        """更新知识条目"""
        if item_id not in self._items:
            return False

        item = self._items[item_id]
        valid_fields = ["title", "content", "knowledge_type", "tags", "category", "is_active", "source", "useful_score"]

        for key, value in kwargs.items():
            if key in valid_fields:
                setattr(item, key, value)

        item.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_all()
        logger.info(f"[KnowledgeBase] 更新知识: {item.title}")
        return True

    def get_stats(self) -> Dict[str, Any]:
        """获取知识库统计信息"""
        items = list(self._items.values())
        return {
            "total": len(items),
            "active": sum(1 for item in items if item.is_active),
            "total_uses": sum(item.useful_count for item in items),
            "categories": len(self.get_all_categories()),
            "types": {t.value: sum(1 for item in items if item.knowledge_type == t) for t in KnowledgeType},
        }
