"""Fetch the platform's live documentation pages.

Two of these matter operationally:

* posting-gate.md must be re-read in the same action as every public text
  submission - not remembered from a previous run. The gate module depends on
  this fetcher for exactly that reason.
* skill.md and friends are read on demand, and their revision hash is stored so
  the daily skill-update run can tell the owner what actually changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .constants import DOC_PAGES, SITE_ORIGIN
from .errors import AitaolunNetworkError


@dataclass
class DocPage:
    """One fetched documentation page."""

    name: str
    url: str
    text: str
    fetched_at: float
    revision: str

    def excerpt(self, limit: int = 4000) -> str:
        if len(self.text) <= limit:
            return self.text
        return self.text[:limit] + f"\n\n...（已截断，全文 {len(self.text)} 字，可分段再读）"


def revision_of(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


class DocFetcher:
    """Fetches and caches the public docs, with a short freshness window."""

    def __init__(
        self,
        site_origin: str = SITE_ORIGIN,
        timeout_seconds: float = 20.0,
        cache_seconds: float = 0.0,
    ) -> None:
        self.site_origin = (site_origin or SITE_ORIGIN).rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout_seconds)))
        self._cache_seconds = max(0.0, float(cache_seconds))
        self._cache: dict[str, DocPage] = {}
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    def known_pages(self) -> list[str]:
        return sorted(DOC_PAGES)

    def url_for(self, name: str) -> str:
        key = (name or "").strip().lower()
        if key not in DOC_PAGES:
            raise KeyError(name)
        return f"{self.site_origin}{DOC_PAGES[key]}"

    async def _ensure_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self._timeout)
            return self._session

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def fetch(self, name: str, force: bool = True) -> DocPage:
        """Fetch a page. force=True bypasses the cache, which the gate needs."""

        key = (name or "").strip().lower()
        url = self.url_for(key)
        cached = self._cache.get(key)
        if (
            not force
            and cached is not None
            and self._cache_seconds > 0
            and time.time() - cached.fetched_at < self._cache_seconds
        ):
            return cached

        session = await self._ensure_session()
        try:
            async with session.get(
                url, headers={"Accept": "text/markdown, text/plain, */*"}
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise AitaolunNetworkError(
                        f"读取 {url} 失败：HTTP {response.status}"
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise AitaolunNetworkError(f"读取 {url} 失败：{exc}") from exc

        page = DocPage(
            name=key,
            url=url,
            text=text,
            fetched_at=time.time(),
            revision=revision_of(text),
        )
        self._cache[key] = page
        return page

    async def fetch_many(self, names: list[str]) -> dict[str, DocPage | Exception]:
        """Fetch several pages concurrently, keeping per-page failures local."""

        async def one(name: str) -> Any:
            try:
                return await self.fetch(name)
            except (AitaolunNetworkError, KeyError) as exc:
                return exc

        results = await asyncio.gather(*(one(name) for name in names))
        return dict(zip(names, results))
