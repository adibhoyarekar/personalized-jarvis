"""Web tools: search and opening websites."""

from __future__ import annotations

import logging
import webbrowser
from urllib.parse import quote_plus, urlparse

from tools import tool

log = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not urlparse(url).scheme:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Not a web address: {url!r}")
    return url


@tool
def open_url(url: str) -> str:
    """Open a website in the default browser.

    Use for "go to github.com", "open YouTube", or to show a page found by web_search.

    Args:
        url: Full URL or bare domain, e.g. "https://github.com" or "youtube.com".
    """
    target = _normalize_url(url)
    if not webbrowser.open(target):
        raise RuntimeError("No web browser available")
    return f"Opened {target}."


@tool
def web_search(query: str, max_results: int = 5) -> list | str:
    """Search the web with DuckDuckGo and return the top results (title, link, snippet).

    Use for facts you don't know, current events, prices, or anything recent.
    Summarise the answer in a sentence or two; don't read out URLs.

    Args:
        query: What to search for.
        max_results: Number of results to return (1-10).
    """
    max_results = max(1, min(int(max_results), 10))
    try:
        from ddgs import DDGS
    except ImportError:  # older package name
        try:
            from duckduckgo_search import DDGS  # type: ignore[no-redef]
        except ImportError as exc:
            raise RuntimeError("Web search needs the 'ddgs' package: pip install ddgs") from exc

    try:
        results = DDGS().text(query, max_results=max_results)
    except Exception as exc:  # noqa: BLE001 - network/rate-limit errors vary by version
        log.warning("web_search failed: %s", exc)
        raise RuntimeError(
            f"Search failed ({type(exc).__name__}). The internet may be down. "
            f"Fallback link: https://duckduckgo.com/?q={quote_plus(query)}"
        ) from exc

    if not results:
        return f"No results for '{query}'."
    return [
        {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
        for r in results
    ]
