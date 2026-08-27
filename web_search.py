"""Real web search: optional search API first, key-free HTML scraping as fallback.

Why this module exists
----------------------
The original implementation relied on Gemini's `google_search` grounding tool,
which is NOT available on many free Google AI plans / `*-flash-lite` models.
So we perform an actual HTTP search and hand clean text snippets to Gemini for
summarization, using only the Python standard library (no bs4 / requests).

Search strategy
---------------
1. If `WEB_SEARCH_API_KEY` is set, query a real search API first. The provider is
   auto-detected from `WEB_SEARCH_BASE_URL`, or forced with `WEB_SEARCH_PROVIDER`.
2. If the API is not configured, errors out, or returns nothing, fall back to the
   key-free Bing HTML scrape and then DuckDuckGo -- exactly the old behaviour.
   Set `WEB_SEARCH_STRICT=1` to disable that fallback and fail loudly instead.

Environment variables (all optional)
------------------------------------
- WEB_SEARCH_API_KEY      API key/token. Empty or missing = scraping only.
- WEB_SEARCH_BASE_URL     Endpoint URL. Sensible default per known provider.
- WEB_SEARCH_PROVIDER     tavily | serper | brave | bing | google_cse | serpapi |
                          exa | searxng | generic  (default: auto-detect)
- WEB_SEARCH_CX           Search-engine id, Google Custom Search only.
- WEB_SEARCH_STRICT       1 = never fall back to scraping.
- WEB_SEARCH_METHOD       GET | POST, `generic` provider only.
- WEB_SEARCH_QUERY_PARAM  Query field name, `generic` provider only (default: q).
- WEB_SEARCH_AUTH_HEADER  Header name for the key, `generic` provider only
                          (default: Authorization, sent as "Bearer <key>").

Public API
----------
- `web_search.search(query)`         -> (results, error)
- `web_search.search_async(query)`   -> (results, error)   [awaitable]
- `format_context(results)`          -> compact text block for the LLM

`main.py` imports the singleton object (`from web_search import web_search,
format_context`), so the object wrapper below MUST stay in place; the plain
functions are kept as well for backwards compatibility.
"""

import asyncio
import gzip
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}

# Default endpoint per provider, used when WEB_SEARCH_BASE_URL is not given.
DEFAULT_ENDPOINTS = {
    "tavily": "https://api.tavily.com/search",
    "serper": "https://google.serper.dev/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "bing": "https://api.bing.microsoft.com/v7.0/search",
    "google_cse": "https://www.googleapis.com/customsearch/v1",
    "serpapi": "https://serpapi.com/search.json",
    "exa": "https://api.exa.ai/search",
}

# Keys we look for when pulling fields out of an unknown JSON shape.
_TITLE_KEYS = ("title", "name", "heading", "header")
_SNIPPET_KEYS = ("snippet", "description", "content", "text", "body", "abstract",
                 "summary", "excerpt", "highlight")
_URL_KEYS = ("url", "link", "href", "source_url", "displayUrl", "display_url")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _clean(text: str) -> str:
    """Strip tags/entities and collapse whitespace."""
    txt = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", txt).strip()


def _decode_body(raw: bytes, encoding: str) -> str:
    enc = (encoding or "").lower()
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    elif "deflate" in enc:
        try:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
    return raw.decode("utf-8", "ignore")


def _http_get(url: str, timeout: int = 15) -> str:
    """GET a URL and return decoded HTML (handles gzip/deflate)."""
    req = urllib.request.Request(url, headers=BASE_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = resp.headers.get("Content-Encoding") or ""
    return _decode_body(raw, enc)


def _http_json(url: str, method: str = "GET", headers=None, payload=None, timeout: int = 15):
    """Perform a JSON request and return the decoded object.

    Raises on HTTP/network errors; the caller decides whether to fall back.
    """
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    hdrs.update(headers or {})

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = _decode_body(resp.read(), resp.headers.get("Content-Encoding") or "")
    except urllib.error.HTTPError as e:
        # Surface the API's own error message -- it is usually the actual reason
        # (bad key, quota, wrong endpoint) and saves a lot of guessing.
        try:
            detail = _decode_body(e.read(), e.headers.get("Content-Encoding") or "")
        except Exception:
            detail = ""
        raise RuntimeError(f"HTTP {e.code}: {(detail or str(e))[:300]}") from None

    body = (body or "").strip()
    if not body:
        raise RuntimeError("empty response body")
    return json.loads(body)


def _unwrap_ddg_url(url: str) -> str:
    """DuckDuckGo wraps result links in /l/?uddg=<encoded> redirects."""
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if "uddg=" in url:
        try:
            qs = urllib.parse.urlparse(url).query
            target = urllib.parse.parse_qs(qs).get("uddg", [""])[0]
            if target:
                return target
        except Exception:
            pass
    return url


# ----------------------------------------------------------------------
# Search API layer (only active when WEB_SEARCH_API_KEY is set)
# ----------------------------------------------------------------------
def _detect_provider(base_url: str) -> str:
    """Guess the provider from the endpoint host."""
    explicit = _env("WEB_SEARCH_PROVIDER").lower()
    if explicit:
        return explicit

    host = ""
    try:
        host = urllib.parse.urlparse(base_url).netloc.lower()
    except Exception:
        pass

    if "tavily" in host:
        return "tavily"
    if "serper" in host:
        return "serper"
    if "serpapi" in host:
        return "serpapi"
    if "brave" in host:
        return "brave"
    if "bing" in host or "cognitiveservices" in host:
        return "bing"
    if "googleapis" in host:
        return "google_cse"
    if "exa.ai" in host:
        return "exa"
    if "searx" in host:
        return "searxng"
    return "generic"


def _api_settings():
    """Read the API configuration from the environment on every call.

    Returns None when no key is configured, which keeps the old scraping-only
    behaviour intact.
    """
    key = _env("WEB_SEARCH_API_KEY")
    base_url = _env("WEB_SEARCH_BASE_URL")
    if not key and not base_url:
        return None

    provider = _detect_provider(base_url)
    if not base_url:
        base_url = DEFAULT_ENDPOINTS.get(provider, "")
    if not base_url:
        return None
    # SearXNG needs no key at all, every other provider does.
    if not key and provider != "searxng":
        return None

    return {"key": key, "base_url": base_url, "provider": provider}


def _with_params(base_url: str, params: dict) -> str:
    """Append query params, preserving any already present in the base URL."""
    parts = urllib.parse.urlsplit(base_url)
    existing = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    merged = existing + [(k, str(v)) for k, v in params.items() if v not in (None, "")]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(merged), parts.fragment)
    )


def _build_request(cfg: dict, query: str, max_results: int):
    """Return (url, method, headers, payload) for the configured provider."""
    provider, key, base_url = cfg["provider"], cfg["key"], cfg["base_url"]

    if provider == "tavily":
        return base_url, "POST", {"Authorization": f"Bearer {key}"}, {
            "api_key": key,  # older Tavily API versions expect it in the body
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }

    if provider == "serper":
        return base_url, "POST", {"X-API-KEY": key}, {"q": query, "num": max_results, "hl": "fa"}

    if provider == "exa":
        return base_url, "POST", {"x-api-key": key}, {
            "query": query,
            "numResults": max_results,
            "contents": {"text": {"maxCharacters": 600}},
        }

    if provider == "brave":
        url = _with_params(base_url, {"q": query, "count": max_results})
        return url, "GET", {"X-Subscription-Token": key, "Accept-Encoding": "gzip"}, None

    if provider == "bing":
        url = _with_params(base_url, {"q": query, "count": max_results, "mkt": "fa-IR"})
        return url, "GET", {"Ocp-Apim-Subscription-Key": key}, None

    if provider == "google_cse":
        url = _with_params(base_url, {
            "key": key,
            "cx": _env("WEB_SEARCH_CX"),
            "q": query,
            "num": min(max_results, 10),
        })
        return url, "GET", {}, None

    if provider == "serpapi":
        url = _with_params(base_url, {"api_key": key, "q": query, "num": max_results})
        return url, "GET", {}, None

    if provider == "searxng":
        url = _with_params(base_url, {"q": query, "format": "json", "language": "fa"})
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return url, "GET", headers, None

    # ---- generic JSON endpoint (fully configurable) ----
    method = (_env("WEB_SEARCH_METHOD", "GET") or "GET").upper()
    query_param = _env("WEB_SEARCH_QUERY_PARAM", "q") or "q"
    auth_header = _env("WEB_SEARCH_AUTH_HEADER", "Authorization") or "Authorization"
    headers = {}
    if key:
        headers[auth_header] = f"Bearer {key}" if auth_header.lower() == "authorization" else key

    if method == "POST":
        return base_url, "POST", headers, {query_param: query, "max_results": max_results}
    return _with_params(base_url, {query_param: query, "count": max_results}), "GET", headers, None


def _first_str(item: dict, keys) -> str:
    for k in keys:
        val = item.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            inner = val.get("text") or val.get("value")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        if isinstance(val, list) and val and isinstance(val[0], str):
            return " ".join(val)[:800].strip()
    return ""


def _looks_like_results(value) -> bool:
    return (
        isinstance(value, list)
        and value
        and isinstance(value[0], dict)
        and any(k in value[0] for k in _URL_KEYS + _TITLE_KEYS + _SNIPPET_KEYS)
    )


def _find_result_list(data, depth: int = 0):
    """Walk arbitrary JSON and return the most plausible list of results."""
    if depth > 5:
        return []
    if _looks_like_results(data):
        return data
    if isinstance(data, dict):
        # Prefer conventional key names before brute-forcing the whole document.
        for key in ("results", "organic", "organic_results", "items", "data",
                    "value", "webPages", "web", "documents", "hits", "answers"):
            if key in data:
                found = _find_result_list(data[key], depth + 1)
                if found:
                    return found
        for value in data.values():
            found = _find_result_list(value, depth + 1)
            if found:
                return found
    return []


def _normalize_results(data, max_results: int):
    """Map an arbitrary API response onto {title, snippet, url} dicts."""
    raw_list = _find_result_list(data)
    out = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        title = _clean(_first_str(item, _TITLE_KEYS))
        snippet = _clean(_first_str(item, _SNIPPET_KEYS))[:800]
        url = _first_str(item, _URL_KEYS)
        if title or snippet:
            out.append({"title": title, "snippet": snippet, "url": url})
        if len(out) >= max_results:
            break
    return out


def _search_api(query: str, max_results: int = 5, timeout: int = 15):
    """Query the configured search API. Returns (results, error).

    error is None when the API answered with usable results, or when the API is
    simply not configured.
    """
    cfg = _api_settings()
    if not cfg:
        return [], None  # not configured -> not an error, just skip this layer

    provider = cfg["provider"]
    if provider == "google_cse" and not _env("WEB_SEARCH_CX"):
        return [], "api(google_cse): WEB_SEARCH_CX is required for Google Custom Search"

    try:
        url, method, headers, payload = _build_request(cfg, query, max_results)
        data = _http_json(url, method, headers, payload, timeout)
    except Exception as e:
        return [], f"api({provider}): {type(e).__name__}: {e}"

    results = _normalize_results(data, max_results)
    if not results:
        return [], f"api({provider}): response parsed but contained no results"

    print(f"Web search via API provider '{provider}' returned {len(results)} results")
    return results, None


# ----------------------------------------------------------------------
# Key-free scraping engines (fallback / default)
# ----------------------------------------------------------------------
def _search_bing(query: str, max_results: int = 5, timeout: int = 15):
    """Scrape Bing's `li.b_algo` result blocks (Persian market results)."""
    url = (
        "https://www.bing.com/search?q="
        + urllib.parse.quote(query)
        + "&setlang=fa-IR&mkt=fa-IR"
    )
    page = _http_get(url, timeout)

    out = []
    blocks = re.findall(r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>', page, re.S)
    for blk in blocks[:max_results]:
        h2 = re.search(r"<h2[^>]*>(.*?)</h2>", blk, re.S)
        p = re.search(r"<p[^>]*>(.*?)</p>", blk, re.S)
        a = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"', blk, re.S)
        title = _clean(h2.group(1)) if h2 else ""
        snippet = _clean(p.group(1)) if p else ""
        link = a.group(1) if a else ""
        if title or snippet:
            out.append({"title": title, "snippet": snippet, "url": link})
    return out


def _search_duckduckgo(query: str, max_results: int = 5, timeout: int = 15):
    """Fallback: DuckDuckGo HTML endpoints (no JS, no API key)."""
    endpoints = (
        "https://html.duckduckgo.com/html/?kl=wt-wt&q=",
        "https://lite.duckduckgo.com/lite/?q=",
    )
    for base in endpoints:
        try:
            page = _http_get(base + urllib.parse.quote(query), timeout)
        except Exception:
            continue

        out = []

        # html.duckduckgo.com layout
        titles = re.findall(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            page,
            re.S,
        )
        snippets = re.findall(
            r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', page, re.S
        )
        for i, (link, title) in enumerate(titles[:max_results]):
            snippet = _clean(snippets[i]) if i < len(snippets) else ""
            out.append(
                {
                    "title": _clean(title),
                    "snippet": snippet,
                    "url": _unwrap_ddg_url(link),
                }
            )

        # lite.duckduckgo.com layout
        if not out:
            lite = re.findall(
                r'<a[^>]*class="[^"]*result-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                page,
                re.S,
            )
            lite_snips = re.findall(
                r'<td[^>]*class="[^"]*result-snippet[^"]*"[^>]*>(.*?)</td>', page, re.S
            )
            for i, (link, title) in enumerate(lite[:max_results]):
                snippet = _clean(lite_snips[i]) if i < len(lite_snips) else ""
                out.append(
                    {
                        "title": _clean(title),
                        "snippet": snippet,
                        "url": _unwrap_ddg_url(link),
                    }
                )

        out = [r for r in out if r["title"] or r["snippet"]]
        if out:
            return out
    return []


# ----------------------------------------------------------------------
# Public functions
# ----------------------------------------------------------------------
def search(query: str, max_results: int = 5, timeout: int = 15):
    """Blocking search.

    Returns `(results, error)` where results is a list of
    `{"title", "snippet", "url"}` dicts and error is None on success.

    Order: configured search API (if any) -> Bing scrape -> DuckDuckGo.
    """
    query = (query or "").strip()
    if not query:
        return [], "EmptyQuery: no search terms provided"

    errors = []

    # 1) Search API, only when WEB_SEARCH_API_KEY / BASE_URL are configured
    api_results, api_error = _search_api(query, max_results, timeout)
    if api_results:
        return api_results, None
    if api_error:
        errors.append(api_error)
        print(f"Search API failed: {api_error}")
        if _env("WEB_SEARCH_STRICT", "0").lower() in ("1", "true", "yes"):
            return [], api_error

    # 2) Key-free scraping (the original behaviour)
    for engine_name, engine in (("bing", _search_bing), ("duckduckgo", _search_duckduckgo)):
        try:
            results = engine(query, max_results, timeout)
            if results:
                return results, None
            errors.append(f"{engine_name}: no results parsed")
        except Exception as e:  # network error, block page, timeout...
            errors.append(f"{engine_name}: {type(e).__name__}: {e}")

    return [], " | ".join(errors) or "no results"


async def search_async(query: str, max_results: int = 5, timeout: int = 15):
    """Async wrapper so the Telethon event loop is never blocked."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: search(query, max_results, timeout)
    )


def format_context(results, max_chars: int = 2500) -> str:
    """Flatten search results into a compact text block for the LLM."""
    if not results:
        return ""
    lines = []
    total = 0
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        url = (r.get("url") or "").strip()
        block = f"[{i}] {title}\n{snippet}".strip()
        if url:
            block += f"\n({url})"
        if total + len(block) > max_chars:
            break
        lines.append(block)
        total += len(block)
    return "\n\n".join(lines)


class WebSearch:
    """Object facade kept because `main.py` imports the `web_search` singleton."""

    @staticmethod
    def search(query: str, max_results: int = 5, timeout: int = 15):
        return search(query, max_results, timeout)

    @staticmethod
    async def search_async(query: str, max_results: int = 5, timeout: int = 15):
        return await search_async(query, max_results, timeout)

    @staticmethod
    def format_context(results, max_chars: int = 2500) -> str:
        return format_context(results, max_chars)

    @staticmethod
    def active_provider() -> str:
        """Which layer will be used right now: the API name, or 'scrape'."""
        cfg = _api_settings()
        return cfg["provider"] if cfg else "scrape"


# Global singleton used by main.py
web_search = WebSearch()
