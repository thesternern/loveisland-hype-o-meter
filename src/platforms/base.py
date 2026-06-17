"""Platform-scraper interface.

Every platform returns the SAME normalized post/comment shape so that entity
resolution, sentiment, and scoring stay platform-agnostic. Adding Instagram
Reels later means writing one more subclass that emits these same dicts.

Normalized post:
    {
      "platform": str, "id": str, "url": str, "caption": str,
      "author_id": str, "author_name": str,
      "likes": int, "views": int, "shares": int, "comment_count": int,
      "created_at": str (ISO8601), "hashtags": list[str], "search_query": str,
    }

Normalized comment:
    {
      "platform": str, "id": str, "post_id": str, "post_url": str,
      "text": str, "author_id": str, "likes": int, "created_at": str,
    }
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ScanResult:
    posts: list[dict] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)


class PlatformScraper(ABC):
    """One scan = discover posts for the given queries + pull their comments."""

    platform: str = "base"

    @abstractmethod
    def scan(
        self,
        queries: list[str],
        hashtags: list[str],
        scan_cfg: dict,
        dry_run: bool = False,
    ) -> ScanResult:
        """Run discovery + comment retrieval and return normalized records."""
        raise NotImplementedError

    @abstractmethod
    def estimate(self, n_sources: int, scan_cfg: dict, dry_run: bool = False) -> dict:
        """Estimate cost. n_sources = number of discovery sources (queries + hashtags).

        Return {'posts': int, 'comments': int, 'usd': float, 'breakdown': str}.
        """
        raise NotImplementedError
