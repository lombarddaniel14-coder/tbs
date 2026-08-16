"""Web search and page fetching.

Deliberately dependency-free: searches go through DuckDuckGo's keyless HTML
endpoint and pages are fetched with urllib, so there is no second API key to
manage. Results are plain text, trimmed to something a model can read cheaply.
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
TIMEOUT = 20
MAX_PAGE_BYTES = 800_000
MAX_TEXT_CHARS = 6_000


TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web and return titles, URLs, and snippets. Use this for "
            "anything current or outside your knowledge: news, prices, hours, "
            "campus info, documentation, sports scores, weather."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "max_results": {
                    "type": "integer",
                    "description": "How many results to return (1-10, default 5).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Download a web page and return its readable text. Use after "
            "web_search when a result looks like it holds the actual answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full http(s) URL to fetch."}
            },
            "required": ["url"],
        },
    },
]

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")
_RESULT_RE = re.compile(
    r'(?is)<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
)
_SNIPPET_RE = re.compile(r'(?is)<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>')


def _strip_html(raw: str) -> str:
    text = _SCRIPT_RE.sub(" ", raw)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|br)\s*>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_RE.sub("\n\n", text).strip()


def _unwrap_ddg(url: str) -> str:
    """DuckDuckGo wraps some results in /l/?uddg=<encoded>."""
    if "duckduckgo.com/l/" not in url and not url.startswith("//duckduckgo.com/l/"):
        return url
    parsed = urllib.parse.urlparse(url if "://" in url else "https:" + url)
    target = urllib.parse.parse_qs(parsed.query).get("uddg", [])
    return urllib.parse.unquote(target[0]) if target else url


def _request(url: str, data: bytes | None = None) -> str:
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read(MAX_PAGE_BYTES)
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def web_search(tool_input: dict) -> str:
    query = str(tool_input.get("query", "")).strip()
    if not query:
        return "No search query was given."
    try:
        limit = int(tool_input.get("max_results", 5))
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 10))

    payload = urllib.parse.urlencode({"q": query}).encode("utf-8")
    try:
        body = _request(SEARCH_ENDPOINT, data=payload)
    except urllib.error.URLError as exc:
        return f"Web search failed (no connection or blocked): {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return f"Web search failed: {exc}"

    titles = list(_RESULT_RE.finditer(body))
    snippets = [_strip_html(m.group(1)) for m in _SNIPPET_RE.finditer(body)]
    if not titles:
        return f"No results found for '{query}'."

    lines = [f"Search results for '{query}':"]
    for index, match in enumerate(titles[:limit]):
        title = _strip_html(match.group("title"))
        url = _unwrap_ddg(html.unescape(match.group("url")))
        snippet = snippets[index] if index < len(snippets) else ""
        lines.append(f"\n{index + 1}. {title}\n   {url}")
        if snippet:
            lines.append(f"   {snippet[:400]}")
    return "\n".join(lines)


def web_fetch(tool_input: dict) -> str:
    url = str(tool_input.get("url", "")).strip()
    if not url:
        return "No URL was given."
    if "://" not in url:
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Refusing to fetch a '{parsed.scheme}' URL. Only http and https are allowed."

    try:
        body = _request(url)
    except urllib.error.HTTPError as exc:
        return f"Fetch failed: HTTP {exc.code} from {parsed.netloc}."
    except urllib.error.URLError as exc:
        return f"Fetch failed (no connection or blocked): {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return f"Fetch failed: {exc}"

    text = _strip_html(body)
    if not text:
        return f"{url} returned no readable text (it may be a script-driven page)."
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n\n[...truncated...]"
    return f"Content of {url}:\n\n{text}"


def handle(name: str, tool_input: dict) -> str:
    return HANDLERS[name](tool_input)


HANDLERS = {
    "web_search": web_search,
    "web_fetch": web_fetch,
}
