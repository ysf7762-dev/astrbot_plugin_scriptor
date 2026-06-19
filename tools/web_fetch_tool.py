# tools/web_fetch_tool.py
"""
WebFetch 工具 - 安全的 URL 内容获取器

功能：
1. HTTP/HTTPS 网页内容获取
2. HTML → Markdown 转换
3. SSRF 防护（阻止内网探测）
4. 内容长度限制与超时控制
5. 缓存机制（避免重复请求）
6. 速率限制
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    logger.warning("[WebFetch] aiohttp 未安装，WebFetch 功能将不可用")

try:
    from bs4 import BeautifulSoup

    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
    logger.warning("[WebFetch] beautifulsoup4 未安装，HTML 解析功能受限")

try:
    import markdownify

    MARKDOWNIFY_AVAILABLE = True
except ImportError:
    MARKDOWNIFY_AVAILABLE = False
    logger.warning("[WebFetch] markdownify 未安装，HTML→Markdown 转换功能受限")


@dataclass
class WebFetchConfig:
    """WebFetch 配置"""

    max_content_length: int = 100 * 1024  # 100KB
    timeout_seconds: float = 15.0
    max_redirects: int = 3
    allowed_schemes: tuple = ("http", "https")
    blocked_domains: set = field(
        default_factory=lambda: {
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
            "::1",
            "localhost.localdomain",
            "ip6-localhost",
            "ip6-loopback",
        }
    )
    blocked_networks: list = field(
        default_factory=lambda: [
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "169.254.0.0/16",
            "100.64.0.0/10",
            "198.18.0.0/15",
            "fc00::/7",
            "fe80::/10",
        ]
    )
    user_agent: str = "ScriptorBot/1.0 (Educational Purpose; https://github.com/astrbots/astrbot_plugin_scriptor)"
    cache_ttl_seconds: int = 300  # 5分钟缓存
    rate_limit_rpm: int = 20  # 每分钟最多 20 次


@dataclass
class WebFetchResult:
    """WebFetch 结果"""

    url: str
    title: str
    content: str
    metadata: Dict[str, Any]
    content_length: int
    fetched_at: datetime
    status_code: int = 200
    error: Optional[str] = None


class TokenBucketLimiter:
    """令牌桶速率限制器"""

    def __init__(self, rate_per_minute: int):
        self.rate = rate_per_minute
        self.tokens = rate_per_minute
        self.last_refill = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            self.tokens = min(self.rate, self.tokens + elapsed * (self.rate / 60))
            self.last_refill = now

            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


class WebFetcher:
    """网页内容获取器"""

    def __init__(self, config: Optional[WebFetchConfig] = None):
        self.config = config or WebFetchConfig()
        self._cache: Dict[str, tuple[WebFetchResult, float]] = {}
        self._rate_limiter = TokenBucketLimiter(self.config.rate_limit_rpm)
        self._request_count = 0

    def _validate_url(self, url: str) -> bool:
        """
        验证 URL 安全性

        检查：
        - 协议是否允许
        - 是否在黑名单域名中
        - 是否为内网地址
        """
        try:
            parsed = urlparse(url)

            if parsed.scheme not in self.config.allowed_schemes:
                raise ValueError(f"不支持的协议: {parsed.scheme}，仅支持 {self.config.allowed_schemes}")

            domain = parsed.hostname or ""

            if domain in self.config.blocked_domains:
                raise ValueError(f"被阻止的域名: {domain}")

            for blocked in self.config.blocked_domains:
                if domain.endswith(blocked) or domain == blocked:
                    raise ValueError(f"被阻止的域名: {domain}")

            import ipaddress

            try:
                ip = ipaddress.ip_address(domain)
                for network_str in self.config.blocked_networks:
                    network = ipaddress.ip_network(network_str, strict=False)
                    if ip in network:
                        raise ValueError(f"被阻止的内网地址: {ip}")
            except ValueError:
                pass
            except Exception as e:
                logger.debug(f"[WebFetch] IP 地址检查失败: {e}")

            return True

        except Exception as e:
            raise ValueError(f"URL 验证失败: {e}")

    def _get_from_cache(self, url: str) -> Optional[WebFetchResult]:
        """从缓存获取"""
        if url not in self._cache:
            return None

        result, cached_at = self._cache[url]
        if time.time() - cached_at > self.config.cache_ttl_seconds:
            del self._cache[url]
            return None

        return result

    def _put_cache(self, url: str, result: WebFetchResult):
        """存入缓存"""
        if len(self._cache) >= 100:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]

        self._cache[url] = (result, time.time())

    async def fetch(self, url: str) -> WebFetchResult:
        """
        获取并转换网页内容

        Args:
            url: 目标 URL

        Returns:
            WebFetchResult 包含 title, content, metadata
        """
        if not AIOHTTP_AVAILABLE:
            return WebFetchResult(
                url=url,
                title="",
                content="",
                metadata={},
                content_length=0,
                fetched_at=datetime.now(),
                error="aiohttp 未安装，请运行: pip install aiohttp",
            )

        self._validate_url(url)

        cached = self._get_from_cache(url)
        if cached:
            logger.debug(f"[WebFetch] 命中缓存: {url}")
            return cached

        acquired = await self._rate_limiter.acquire()
        if not acquired:
            return WebFetchResult(
                url=url,
                title="",
                content="",
                metadata={},
                content_length=0,
                fetched_at=datetime.now(),
                error="请求过于频繁，请稍后再试",
            )

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    headers={"User-Agent": self.config.user_agent},
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
                    max_redirects=self.config.max_redirects,
                ) as resp,
            ):
                self._request_count += 1

                html = await resp.read()

                if len(html) > self.config.max_content_length:
                    html = html[: self.config.max_content_length]

                content = self._html_to_markdown(html, url)
                title = self._extract_title(html)
                metadata = self._extract_metadata(html, url)

                result = WebFetchResult(
                    url=url,
                    title=title,
                    content=content,
                    metadata=metadata,
                    content_length=len(content),
                    fetched_at=datetime.now(),
                    status_code=resp.status,
                )

                self._put_cache(url, result)
                logger.info(f"[WebFetch] 成功获取: {url} ({len(content)} 字符)")
                return result

        except asyncio.TimeoutError:
            return WebFetchResult(
                url=url,
                title="",
                content="",
                metadata={},
                content_length=0,
                fetched_at=datetime.now(),
                error=f"请求超时 (> {self.config.timeout_seconds}s)",
            )
        except Exception as e:
            logger.error(f"[WebFetch] 获取失败: {url}, 错误: {e}")
            return WebFetchResult(
                url=url, title="", content="", metadata={}, content_length=0, fetched_at=datetime.now(), error=str(e)
            )

    def _html_to_markdown(self, html_bytes: bytes, base_url: str) -> str:
        """将 HTML 转换为精简 Markdown"""
        if not BS4_AVAILABLE or not MARKDOWNIFY_AVAILABLE:
            try:
                text = html_bytes.decode("utf-8", errors="ignore")
                text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"\s+", " ", text).strip()
                return text[:8000]
            except Exception as e:
                logger.debug(f"[WebFetch] HTML 解析失败: {e}")
                return "[HTML 解析依赖未安装，无法转换内容]"

        soup = BeautifulSoup(html_bytes, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript"]):
            tag.decompose()

        main_content = self._extract_main_content(soup)

        try:
            markdown = markdownify.markdownify(str(main_content), heading_style="ATX")
        except Exception as e:
            logger.warning(f"[WebFetch] Markdown 转换失败: {e}")
            markdown = main_content.get_text(separator="\n", strip=True)

        markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

        if len(markdown) > 8000:
            markdown = markdown[:8000] + "\n\n... (内容已截断，原文过长)"

        return markdown

    def _extract_main_content(self, soup) -> Any:
        """启发式提取主要内容区域"""
        for selector in ["article", "main", '[role="main"]', "#content", ".content", "#main"]:
            main = soup.select_one(selector)
            if main and len(main.get_text(strip=True)) > 100:
                return main

        body = soup.find("body")
        if body:
            return body

        return soup

    def _extract_title(self, html_bytes: bytes) -> str:
        """提取页面标题"""
        if not BS4_AVAILABLE:
            return ""

        try:
            soup = BeautifulSoup(html_bytes, "html.parser")
            title_tag = soup.find("title")
            if title_tag and title_tag.string:
                return title_tag.string.strip()
        except Exception as e:
            logger.debug(f"[WebFetch] 提取标题失败: {e}")

        return "(无标题)"

    def _extract_metadata(self, html_bytes: bytes, url: str) -> Dict[str, Any]:
        """提取元数据"""
        metadata = {}

        if not BS4_AVAILABLE:
            return metadata

        try:
            soup = BeautifulSoup(html_bytes, "html.parser")

            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                metadata["og_title"] = og_title["content"]

            og_description = soup.find("meta", property="og:description")
            if og_description and og_description.get("content"):
                metadata["og_description"] = og_description["content"]

            author_meta = soup.find("meta", attrs={"name": "author"})
            if author_meta and author_meta.get("content"):
                metadata["author"] = author_meta["content"]

        except Exception as e:
            logger.debug(f"[WebFetch] 元数据提取失败: {e}")

        metadata["source_url"] = url
        return metadata

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "cache_size": len(self._cache),
            "total_requests": self._request_count,
            "rate_limit": f"{self.config.rate_limit_rpm}/min",
            "config": {
                "max_content_kb": self.config.max_content_length // 1024,
                "timeout_seconds": self.config.timeout_seconds,
                "cache_ttl_seconds": self.config.cache_ttl_seconds,
            },
        }
