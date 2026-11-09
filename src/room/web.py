"""Outbound reads: one page, or one search on a public JSON API. Everything that comes back is
untrusted text written by strangers, and is handed to models marked as such."""

import html
import ipaddress
import re
import socket
import time
from urllib.parse import urldefrag, urlparse

import httpx

BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}


class FetchError(Exception):
    pass


def normalize(url: str) -> str:
    url, _ = urldefrag(url.strip())
    return url.rstrip("/")


def check_public(url: str):
    """Refuse anything that is not plain http(s) to a public address: a page cannot point an agent
    at this machine, its network or a cloud metadata endpoint."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FetchError(f"only http(s) URLs can be fetched: {url}")
    host = p.hostname.lower()
    if host in BLOCKED_HOSTS or host.endswith((".local", ".internal", ".ts.net")):
        raise FetchError(f"refusing a private host: {host}")
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror as e:
        raise FetchError(f"cannot resolve {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FetchError(f"refusing a non-public address for {host}: {ip}")


_DROP = re.compile(r"<(script|style|noscript|svg|nav|footer|header|form)\b.*?</\1>", re.S | re.I)
_BLOCK = re.compile(r"</?(p|div|section|article|li|ul|ol|tr|table|h[1-6]|pre|br|hr)\b[^>]*>", re.I)
_HEAD = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.S | re.I)
_LINK = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(doc: str) -> str:
    title = re.search(r"<title[^>]*>(.*?)</title>", doc, re.S | re.I)
    doc = _DROP.sub(" ", doc)
    doc = _HEAD.sub(lambda m: f"\n\n{'#' * int(m.group(1))} {m.group(2)}\n", doc)
    doc = _LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})", doc)
    doc = _BLOCK.sub("\n", doc)
    doc = html.unescape(_TAG.sub("", doc))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in doc.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if title:
        text = f"Title: {html.unescape(_TAG.sub('', title.group(1))).strip()}\n\n{text}"
    return text


def client(user_agent: str) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent, "Accept-Language": "en"},
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=False,
    )


def fetch(url: str, *, user_agent: str, max_bytes: int, max_redirects: int = 5) -> tuple[str, str]:
    """GET one page, checking every redirect hop. Returns (final url, text)."""
    with client(user_agent) as c:
        for _ in range(max_redirects + 1):
            check_public(url)
            with c.stream("GET", url) as r:
                if r.is_redirect:
                    url = str(r.url.join(r.headers["location"]))
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code} for {url}")
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) >= max_bytes:
                        break
                ctype = r.headers.get("content-type", "")
                text = body.decode(r.encoding or "utf-8", errors="replace")
                if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
                    text = html_to_text(text)
                return str(r.url), text
    raise FetchError(f"too many redirects from {url}")


def get_json(url: str, *, user_agent: str, params=None):
    check_public(url)
    with client(user_agent) as c:
        r = c.get(url, params=params, headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()


# ── Searches on public APIs. Each returns compact dicts: title, url, score, extra. ─────────────


def search_hn(query, *, days, limit, user_agent, min_points=10):
    since = int(time.time() - days * 86400)
    data = get_json(
        "https://hn.algolia.com/api/v1/search",
        user_agent=user_agent,
        params={
            "query": query,
            "tags": "story",
            "numericFilters": f"created_at_i>{since},points>{min_points}",
            "hitsPerPage": limit,
        },
    )
    return [
        {
            "source": "hn",
            "title": h.get("title") or "",
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
            "discussion": f"https://news.ycombinator.com/item?id={h['objectID']}",
            "score": h.get("points", 0),
            "comments": h.get("num_comments", 0),
            "query": query,
        }
        for h in data.get("hits", [])
    ]


def _when(text):
    """RSS pubDate or Atom ISO date -> aware datetime, or None."""
    from datetime import datetime
    from email.utils import parsedate_to_datetime

    text = (text or "").strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _summary(text, n=280):
    text = html.unescape(_TAG.sub(" ", text or ""))
    return re.sub(r"\s+", " ", text).strip()[:n]


def read_feed(url, *, name, days, limit, user_agent):
    """Items from one RSS 2.0 or Atom feed, newest first, from the last `days` days."""
    import xml.etree.ElementTree as ET  # expat refuses entity-expansion bombs (Python >= 3.7.1)
    from datetime import UTC, datetime, timedelta

    check_public(url)
    with client(user_agent) as c:
        r = c.get(url, follow_redirects=True)
        r.raise_for_status()
    root = ET.fromstring(r.content)
    atom = "{http://www.w3.org/2005/Atom}"
    items = []
    if root.tag == f"{atom}feed":
        for e in root.findall(f"{atom}entry"):
            link = next((lk.get("href") for lk in e.findall(f"{atom}link")
                         if lk.get("rel", "alternate") == "alternate"), None)  # fmt: skip
            items.append((
                e.findtext(f"{atom}title", ""),
                link or "",
                e.findtext(f"{atom}published") or e.findtext(f"{atom}updated"),
                e.findtext(f"{atom}summary") or e.findtext(f"{atom}content") or "",
                (e.find(f"{atom}author/{atom}name").text
                 if e.find(f"{atom}author/{atom}name") is not None else ""),
            ))  # fmt: skip
    else:
        for e in root.iter("item"):
            items.append((
                e.findtext("title", ""),
                e.findtext("link", ""),
                e.findtext("pubDate"),
                e.findtext("description", ""),
                e.findtext("{http://purl.org/dc/elements/1.1/}creator", ""),
            ))  # fmt: skip
    since = datetime.now(UTC) - timedelta(days=days)
    out = []
    for title, link, when, summary, author in items:
        dt = _when(when)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        if dt and dt < since:
            continue
        out.append({
            "source": name,
            "title": _summary(title, 200),
            "url": link.strip(),
            "date": dt.date().isoformat() if dt else "",
            "author": (author or "").strip(),
            "summary": _summary(summary),
            "score": 0,
        })  # fmt: skip
    return out[:limit]


def planet_kafka(*, name, days, limit, user_agent, pages=2):
    """Aiven's Planet Kafka (aiven.io/planet-kafka). The page builds its list from a JSON API,
    ten articles a page, newest first."""
    from datetime import UTC, datetime, timedelta

    since = datetime.now(UTC) - timedelta(days=days)
    out = []
    for page in range(1, pages + 1):
        data = get_json("https://aiven.io/api/planet-kafka", user_agent=user_agent,
                        params={"page": page} if page > 1 else None)  # fmt: skip
        for a in data.get("articles", []):
            dt = _when(a.get("date"))
            if dt and dt < since:
                return out[:limit]
            out.append({
                "source": name,
                "title": _summary(a.get("title"), 200),
                "url": a.get("link", ""),
                "date": dt.date().isoformat() if dt else "",
                "author": a.get("author", ""),
                "summary": _summary(a.get("content")) + (
                    f" (via {a['feedName']})" if a.get("feedName") else ""),
                "score": 0,
            })  # fmt: skip
        if not data.get("hasLoadMore"):
            break
    return out[:limit]


def search_github(query, *, days, limit, user_agent):
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    data = get_json(
        "https://api.github.com/search/repositories",
        user_agent=user_agent,
        params={
            "q": f"{query} pushed:>{since}",
            "sort": "stars",
            "order": "desc",
            "per_page": limit,
        },
    )
    return [
        {
            "source": "github",
            "title": f"{r['full_name']}: {r.get('description') or ''}".strip(": "),
            "url": r["html_url"],
            "score": r.get("stargazers_count", 0),
            "created": r.get("created_at", "")[:10],
            "topics": r.get("topics", [])[:8],
        }
        for r in data.get("items", [])
    ]


def aiven_docs_index(terms, *, user_agent, cache, limit=40):
    """Aiven docs pages whose URL contains every term. The sitemap is the index: cheap, and it
    only ever names real pages. `cache` is a file path; the sitemap is refetched after an hour."""
    from pathlib import Path

    cache = Path(cache)
    if not cache.exists() or time.time() - cache.stat().st_mtime > 3600:
        with client(user_agent) as c:
            r = c.get("https://aiven.io/docs/sitemap.xml")
            r.raise_for_status()
        cache.write_text("\n".join(re.findall(r"<loc>([^<]+)</loc>", r.text)))
    terms = [t.lower() for t in terms if t]
    urls = cache.read_text().splitlines()
    return [u for u in urls if all(t in u.lower() for t in terms)][:limit]
