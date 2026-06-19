# tools/web_search_tool.py
"""
网页搜索工具 - LLM 可调用
功能：
1. 调用 SearXNG 进行网页搜索
2. 生成搜索结果摘要
3. 自动判定是否归档重要信息
4. 可选：自动读取前 N 个网页的完整内容
"""

import asyncio
from enum import Enum
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from ..core.search_providers import SearchSummarizer, SearXNGClient


class SearchDepth(Enum):
    """搜索深度"""

    QUICK = "quick"  # 快速搜索（3 条结果）
    NORMAL = "normal"  # 标准搜索（5-8 条结果）
    DEEP = "deep"  # 深度搜索（10-15 条结果）


class WebSearchTool:
    """网页搜索工具"""

    def __init__(
        self,
        searxng_base_url: str = "",
        searxng_secret: Optional[str] = None,
        max_results: int = 10,
        timeout: int = 10,
        archive_enabled: bool = True,
        archive_threshold: float = 0.8,
        fetch_top_n: int = 0,
        default_engines: Optional[str] = None,
    ):
        """
        初始化网页搜索工具

        Args:
            searxng_base_url: SearXNG 服务器地址
            searxng_secret: SearXNG 密钥（可选）
            max_results: 最大搜索结果数
            timeout: 请求超时时间（秒）
            archive_enabled: 是否启用归档
            archive_threshold: 归档判定阈值
            fetch_top_n: 自动读取前 N 个网页内容（0 表示不读取）
            default_engines: 默认启用的搜索引擎（逗号分隔，如 "google,baidu,wikipedia"）
        """
        self.client = SearXNGClient(
            base_url=searxng_base_url, timeout=timeout, max_results=max_results, secret=searxng_secret
        )

        self.summarizer = SearchSummarizer(archive_threshold=archive_threshold)

        self.archive_enabled = archive_enabled
        self.fetch_top_n = fetch_top_n
        self.default_engines = [e.strip() for e in default_engines.split(",") if e.strip()] if default_engines else None
        self._search_count = 0
        self._archive_count = 0

    async def _fetch_url_content(self, url: str, index: int) -> tuple[int, str, str]:
        """
        获取单个 URL 的内容

        Args:
            url: 要获取的 URL
            index: 结果索引

        Returns:
            (index, url, content_or_error)
        """
        from .web_fetch_tool import WebFetchConfig, WebFetcher

        try:
            config = WebFetchConfig()
            fetcher = WebFetcher(config)
            result = await fetcher.fetch(url)

            if result.error:
                return (index, url, f"❌ 获取失败: {result.error}")

            # 截取内容摘要（避免太长，10000 字符适合研究文章）
            content = result.content[:10000] if len(result.content) > 10000 else result.content
            return (index, url, f"📄 **{result.title}**\n\n{content}")

        except Exception as e:
            return (index, url, f"❌ 获取失败: {e!s}")

    async def search(
        self,
        query: str,
        depth: SearchDepth = SearchDepth.NORMAL,
        categories: Optional[List[str]] = None,
        engines: Optional[List[str]] = None,
        save_to_memory: bool = False,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        执行网页搜索

        Args:
            query: 搜索关键词
            depth: 搜索深度
            categories: 搜索类别
            engines: 指定搜索引擎
            save_to_memory: 是否保存到记忆（由归档判定决定）
            user_context: 用户上下文

        Returns:
            格式化的搜索结果摘要
        """
        self._search_count += 1

        max_results_map = {SearchDepth.QUICK: 3, SearchDepth.NORMAL: 8, SearchDepth.DEEP: 15}

        actual_max_results = max_results_map.get(depth, 8)

        actual_engines = engines if engines is not None else self.default_engines

        try:
            logger.info(f"[WebSearchTool] 开始搜索：'{query}' (深度：{depth.value})")

            results = await self.client.search_with_retry(
                query=query, categories=categories, engines=actual_engines, max_retries=2
            )

            if not results:
                logger.warning(f"[WebSearchTool] 搜索未返回结果：'{query}'")
                return f'未找到关于"{query}"的相关信息'

            summary = await self.summarizer.summarize(
                results=results, original_query=query, max_results=min(actual_max_results, len(results))
            )

            # 自动读取前 N 个网页的完整内容
            fetched_contents = []
            if self.fetch_top_n > 0 and results:
                top_n = min(self.fetch_top_n, len(results))
                logger.info(f"[WebSearchTool] 正在读取前 {top_n} 个网页的完整内容...")

                # 并行获取多个 URL
                tasks = []
                for i, result in enumerate(results[:top_n]):
                    if result.url:
                        tasks.append(self._fetch_url_content(result.url, i + 1))

                if tasks:
                    fetched_results = await asyncio.gather(*tasks)

                    for idx, url, content in sorted(fetched_results):
                        fetched_contents.append(f"\n---\n\n### 📖 网页 {idx}: {url}\n\n{content}")

                logger.info(f"[WebSearchTool] 已读取 {len(fetched_contents)} 个网页")

            # 合并搜索摘要和网页内容
            if fetched_contents:
                summary = f"{summary}\n\n{'='*20}\n\n## 📚 深度阅读（前 {len(fetched_contents)} 个网页）\n{''.join(fetched_contents)}"

            if self.archive_enabled and save_to_memory:
                decision, confidence, reason = self.summarizer.should_archive(
                    query=query, results=results, user_context=user_context
                )

                if decision == ArchiveDecision.ARCHIVE:
                    facts = self.summarizer.extract_facts(results, query)
                    archive_data = self.summarizer.format_for_archive(
                        query=query, summary=summary, decision_reason=reason, facts=facts
                    )

                    self._archive_count += 1
                    logger.info(
                        f"[WebSearchTool] 归档重要信息 (#{self._archive_count}): "
                        f"置信度 {confidence:.2f}, 理由：{reason}"
                    )

                    return f"{summary}\n\n📌 **已归档重要信息**\n理由：{reason}"

            return summary

        except TimeoutError as e:
            logger.error(f"[WebSearchTool] 搜索超时：{e}")
            return f"⚠️ **搜索超时**\n\n无法连接到搜索引擎，请稍后重试。\n错误信息：{e!s}"
        except Exception as e:
            logger.error(f"[WebSearchTool] 搜索失败：{e}")
            return f"⚠️ **搜索失败**\n\n发生错误：{e!s}"

    async def health_check(self) -> bool:
        """检查搜索引擎是否可用"""
        return await self.client.health_check()

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "total_searches": self._search_count,
            "total_archives": self._archive_count,
            "archive_rate": (self._archive_count / self._search_count * 100 if self._search_count > 0 else 0.0),
        }

    async def close(self):
        """关闭客户端"""
        await self.client.close()

    async def __aenter__(self):
        """异步上下文管理器入口"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()


from ..core.search_providers.summarizer import ArchiveDecision
