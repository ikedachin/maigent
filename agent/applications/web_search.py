import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger("agent")

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


@dataclass(frozen=True)
class WebSearchItem:
    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True)
class WebSearchResult:
    ok: bool
    query: str
    results: tuple[WebSearchItem, ...] = field(default_factory=tuple)
    message: str = ""


def search_web(config, query: str) -> WebSearchResult:
    query = query.strip()
    if not query:
        return WebSearchResult(ok=False, query=query, message="検索クエリが空です。")
    api_key = getattr(config, "web_search_api_key", "")
    if not api_key:
        return WebSearchResult(
            ok=False,
            query=query,
            message="web_searchのAPIキーが未設定です。tools.web_search.api_key または環境変数 TAVILY_API_KEY を設定してください。",
        )
    max_results = getattr(config, "web_search_max_results", 5)
    timeout_seconds = getattr(config, "web_search_timeout_seconds", 10)
    payload = json.dumps(
        {
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TAVILY_SEARCH_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        logger.debug("web_search_error thread=http status=%s", exc.code)
        return WebSearchResult(ok=False, query=query, message=f"Web検索APIがエラーを返しました(status={exc.code})。")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.debug("web_search_error thread=network detail=%s", exc)
        return WebSearchResult(ok=False, query=query, message=f"Web検索に接続できませんでした: {exc}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        logger.debug("web_search_error thread=parse")
        return WebSearchResult(ok=False, query=query, message="Web検索APIの応答を解析できませんでした。")
    raw_results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw_results, list):
        raw_results = []
    items = []
    for entry in raw_results[:max_results]:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not title and not url:
            continue
        snippet = str(entry.get("content") or "").strip()
        items.append(WebSearchItem(title=title or url, url=url, snippet=snippet[:500]))
    if not items:
        return WebSearchResult(ok=False, query=query, message="Web検索で関連する結果が見つかりませんでした。")
    logger.debug("web_search_done query=%r results=%s", query, len(items))
    return WebSearchResult(ok=True, query=query, results=tuple(items))
