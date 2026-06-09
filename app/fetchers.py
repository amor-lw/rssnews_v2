from __future__ import annotations

import dataclasses
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree
import re

import requests

from app.helpers import canonicalize_url, hn_guid, now_utc, stable_guid
from app.models import Article

HN_BASE = "https://hacker-news.firebaseio.com/v0"
NEWSAPI_BASE = "https://newsapi.org/v2"
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
HN_ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"
CISA_KEV_JSON = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


def safe_params(params: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if params is None:
        return None
    sanitized = dict(params)
    for key in list(sanitized):
        if key.lower() in {"apikey", "api_key", "token", "key"}:
            sanitized[key] = "***"
    return sanitized


def http_get_json(
    url: str,
    params: Dict[str, Any] | None = None,
    headers: Dict[str, str] | None = None,
    timeout: int = 15,
    retries: int = 3,
) -> Any:
    last_err = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
            if response.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_err = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"GET failed: {url} params={safe_params(params)} err={last_err}")


def http_get_text(
    url: str,
    params: Dict[str, Any] | None = None,
    headers: Dict[str, str] | None = None,
    timeout: int = 15,
    retries: int = 3,
) -> str:
    request_headers = {"User-Agent": "rssnews/1.0 (+personal RSS builder)"}
    if headers:
        request_headers.update(headers)

    last_err = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, headers=request_headers, timeout=timeout)
            if response.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            return response.text
        except Exception as exc:
            last_err = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"GET failed: {url} params={safe_params(params)} err={last_err}")


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(element: ElementTree.Element, names: tuple[str, ...]) -> str:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if xml_local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def atom_link(entry: ElementTree.Element) -> str:
    fallback = ""
    for child in list(entry):
        if xml_local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        if not href:
            continue
        if child.attrib.get("rel", "alternate") == "alternate":
            return href
        fallback = fallback or href
    return fallback


def parse_feed_datetime(value: str) -> datetime:
    if not value:
        return now_utc()
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return now_utc()


def fetch_rss_feed(
    name: str,
    url: str,
    limit: int = 30,
    max_age_hours: int | None = None,
    kind: str = "news",
) -> List[Article]:
    text = http_get_text(url)
    root = ElementTree.fromstring(text)
    root_name = xml_local_name(root.tag)
    cutoff = now_utc() - timedelta(hours=max_age_hours) if max_age_hours and max_age_hours > 0 else None
    articles: List[Article] = []

    if root_name == "feed":
        entries = [child for child in list(root) if xml_local_name(child.tag) == "entry"]
        for entry in entries[:limit]:
            title = child_text(entry, ("title",))
            link = canonicalize_url(atom_link(entry))
            if not title or not link:
                continue
            published = parse_feed_datetime(child_text(entry, ("published", "updated")))
            if cutoff and published < cutoff:
                continue
            summary = child_text(entry, ("summary", "content"))
            articles.append(
                Article(
                    title=title,
                    url=link,
                    source=name,
                    published_at=published,
                    summary=summary,
                    kind=kind,
                    source_type="rss",
                    raw={"feed_url": url},
                    guid=stable_guid(link, name),
                )
            )
        return articles

    items: List[ElementTree.Element] = []
    for child in root.iter():
        if xml_local_name(child.tag) == "item":
            items.append(child)

    for item in items[:limit]:
        title = child_text(item, ("title",))
        link = canonicalize_url(child_text(item, ("link",)))
        if not title or not link:
            continue
        published = parse_feed_datetime(child_text(item, ("pubDate", "published", "updated", "date")))
        if cutoff and published < cutoff:
            continue
        summary = child_text(item, ("description", "summary", "content", "encoded"))
        articles.append(
            Article(
                title=title,
                url=link,
                source=name,
                published_at=published,
                summary=summary,
                kind=kind,
                source_type="rss",
                raw={"feed_url": url},
                guid=stable_guid(link, name),
            )
        )
    return articles


def fetch_hn(story_list: str, limit: int = 40) -> List[Article]:
    story_ids = http_get_json(f"{HN_BASE}/{story_list}.json")[:limit]
    articles: List[Article] = []

    for story_id in story_ids:
        try:
            item = http_get_json(f"{HN_BASE}/item/{story_id}.json")
        except Exception:
            continue
        if not item or item.get("type") != "story":
            continue

        title = item.get("title") or ""
        url = canonicalize_url(item.get("url") or f"https://news.ycombinator.com/item?id={story_id}")
        timestamp = item.get("time")
        if not timestamp:
            continue

        published_at = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        score = float(item.get("score") or 0)
        comments = int(item.get("descendants") or 0)
        summary = f"HN score {int(score)} · comments {comments}"

        articles.append(
            Article(
                title=title,
                url=url,
                source="Hacker News",
                published_at=published_at,
                summary=summary,
                score=score,
                comments=comments,
                kind="discussion" if "ycombinator.com/item?id=" in url else "news",
                source_type="hn",
                hn_id=int(story_id),
                hn_points=score,
                hn_comments=comments,
                raw=item,
                guid=hn_guid(story_id),
            )
        )

    return articles


def fetch_newsapi_everything(
    api_key: str,
    query: str,
    domains: List[str],
    hours: int,
    page_size: int = 50,
    language: Optional[str] = None,
) -> List[Article]:
    to_dt = now_utc()
    from_dt = to_dt - timedelta(hours=hours)

    params: Dict[str, Any] = {
        "q": query,
        "from": from_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "to": to_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "sortBy": "publishedAt",
        "pageSize": min(max(page_size, 1), 100),
        "domains": ",".join(domains),
        "apiKey": api_key,
    }
    if language:
        params["language"] = language

    data = http_get_json(f"{NEWSAPI_BASE}/everything", params=params)
    articles = data.get("articles") or []
    output: List[Article] = []

    for item in articles:
        url = item.get("url") or ""
        published_at = item.get("publishedAt")
        if not url or not published_at:
            continue

        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except Exception:
            published = now_utc()

        source = (item.get("source") or {}).get("name") or "NewsAPI"
        summary = item.get("description") or item.get("content") or ""
        url = canonicalize_url(url)

        output.append(
            Article(
                title=item.get("title") or "",
                url=url,
                source=source,
                published_at=published,
                summary=summary,
                lang=language,
                source_type="newsapi",
                raw=item,
                guid=stable_guid(url, source),
            )
        )

    return output


def fetch_newsapi_headlines(
    api_key: str,
    language: str | None = None,
    category: str | None = None,
    country: str | None = None,
    page_size: int = 50,
    kind: str = "news",
) -> List[Article]:
    params = {
        "apiKey": api_key,
        "pageSize": min(max(page_size, 1), 100),
    }
    if country:
        params["country"] = country
    if category:
        params["category"] = category

    data = http_get_json(f"{NEWSAPI_BASE}/top-headlines", params=params)
    output = []
    for item in data.get("articles") or []:
        url = item.get("url") or ""
        if not url:
            continue

        published_at = item.get("publishedAt")
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except Exception:
            published = now_utc()

        source = (item.get("source") or {}).get("name") or "NewsAPI"
        url = canonicalize_url(url)
        output.append(
            Article(
                title=item.get("title") or "",
                url=url,
                source=source,
                published_at=published,
                summary=item.get("description") or "",
                lang=language,
                kind=kind,
                source_type="newsapi",
                raw=item,
                guid=stable_guid(url, source),
            )
        )
    return output


def fetch_gdelt_doc(
    query: str,
    hours: int,
    maxrecords: int = 50,
    timeout: int = 30,
    retries: int = 2,
) -> List[Article]:
    to_dt = now_utc()
    from_dt = to_dt - timedelta(hours=hours)

    def gdelt_dt(dt: datetime) -> str:
        return dt.strftime("%Y%m%d%H%M%S")

    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": int(maxrecords),
        "sort": "HybridRel",
        "startdatetime": gdelt_dt(from_dt),
        "enddatetime": gdelt_dt(to_dt),
    }

    data = http_get_json(GDELT_DOC, params=params, timeout=timeout, retries=retries)
    output: List[Article] = []

    for item in data.get("articles") or []:
        url = item.get("url") or ""
        if not url:
            continue

        published = now_utc()
        seendate = item.get("seendate") or ""
        if len(seendate) == 14 and seendate.isdigit():
            try:
                published = datetime.strptime(seendate, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            except Exception:
                pass

        domain = item.get("domain")
        source = f"GDELT:{domain}" if domain else "GDELT"
        url = canonicalize_url(url)

        output.append(
            Article(
                title=item.get("title") or "",
                url=url,
                source=source,
                published_at=published,
                summary=item.get("excerpt") or "",
                lang=item.get("language"),
                kind="radar",
                source_type="gdelt",
                raw=item,
                guid=stable_guid(url, source),
            )
        )

    return output


def hn_query_terms(query: str) -> List[str]:
    terms = [part.strip().strip('"') for part in re.split(r"\s+OR\s+", query) if part.strip()]
    return terms or [query.strip().strip('"')]


def fetch_hn_algolia_hot(query: str, min_points: int = 100, limit: int = 20) -> List[Article]:
    by_url: Dict[str, Article] = {}
    per_term_limit = min(max(int(limit), 1), 100)
    for term in hn_query_terms(query):
        for article in fetch_hn_algolia_hot_term(term, min_points=min_points, limit=per_term_limit):
            article = mark_hn_hot_match(article, term)
            existing = by_url.get(article.url)
            if existing is None:
                by_url[article.url] = article
                continue
            by_url[article.url] = merge_hn_hot_matches(existing, article)
    output = list(by_url.values())
    output = [mark_hn_hot_query(article, query) for article in output]
    output.sort(key=lambda article: (article.score, article.comments), reverse=True)
    return output[:limit]


def mark_hn_hot_match(article: Article, term: str) -> Article:
    raw = dict(article.raw or {})
    terms = [str(item) for item in raw.get("hn_hot_matched_terms") or [] if str(item).strip()]
    clean = term.strip()
    if clean and clean not in terms:
        terms.append(clean)
    raw["hn_hot_matched_terms"] = terms
    return dataclasses.replace(article, raw=raw)


def mark_hn_hot_query(article: Article, query: str) -> Article:
    raw = dict(article.raw or {})
    raw["hn_hot_query"] = query
    return dataclasses.replace(article, raw=raw)


def merge_hn_hot_matches(existing: Article, incoming: Article) -> Article:
    existing_terms = [str(item) for item in (existing.raw or {}).get("hn_hot_matched_terms") or [] if str(item).strip()]
    incoming_terms = [str(item) for item in (incoming.raw or {}).get("hn_hot_matched_terms") or [] if str(item).strip()]
    merged_terms: List[str] = []
    for term in existing_terms + incoming_terms:
        if term not in merged_terms:
            merged_terms.append(term)
    chosen = incoming if (incoming.score, incoming.comments) > (existing.score, existing.comments) else existing
    raw = dict(chosen.raw or {})
    raw["hn_hot_matched_terms"] = merged_terms
    return dataclasses.replace(chosen, raw=raw)


def fetch_hn_algolia_hot_term(query: str, min_points: int = 100, limit: int = 20) -> List[Article]:
    params = {
        "query": query,
        "tags": "story",
        "numericFilters": f"points>{int(min_points)}",
        "hitsPerPage": min(max(int(limit), 1), 100),
    }
    data = http_get_json(HN_ALGOLIA_SEARCH, params=params, timeout=20, retries=2)
    output: List[Article] = []
    for item in data.get("hits") or []:
        object_id = item.get("objectID")
        title = item.get("title") or item.get("story_title") or ""
        url = item.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
        if not object_id or not title:
            continue
        try:
            created_at = datetime.fromisoformat(str(item.get("created_at", "")).replace("Z", "+00:00"))
        except Exception:
            created_at = now_utc()
        points = float(item.get("points") or 0)
        comments = int(item.get("num_comments") or 0)
        canonical = canonicalize_url(url)
        summary = f"HN historical hot post · {int(points)} points · {comments} comments"
        output.append(
            Article(
                title=f"[HN Hot] {title}",
                url=canonical,
                source="HN Hot",
                published_at=created_at,
                summary=summary,
                score=points,
                comments=comments,
                kind="hn_hot",
                source_type="hn_hot",
                hn_id=int(object_id),
                hn_points=points,
                hn_comments=comments,
                raw=item,
                guid=hn_guid(object_id),
            )
        )
    return output


def fetch_cisa_kev_catalog(limit: int = 50) -> List[Article]:
    data = http_get_json(CISA_KEV_JSON, timeout=30, retries=2)
    vulnerabilities = data.get("vulnerabilities") or []
    vulnerabilities = sorted(vulnerabilities, key=lambda item: str(item.get("dateAdded") or ""), reverse=True)
    output: List[Article] = []
    for item in vulnerabilities[: max(int(limit), 1)]:
        cve_id = item.get("cveID") or ""
        vendor = item.get("vendorProject") or ""
        product = item.get("product") or ""
        name = item.get("vulnerabilityName") or ""
        added = item.get("dateAdded") or ""
        due = item.get("dueDate") or ""
        title = f"{cve_id} {vendor} {product}: {name}".strip()
        summary_parts = [
            item.get("shortDescription") or "",
            f"Required action: {item.get('requiredAction')}" if item.get("requiredAction") else "",
            f"Date added: {added}" if added else "",
            f"Due date: {due}" if due else "",
        ]
        summary = " ".join(part for part in summary_parts if part)
        published = now_utc()
        if added:
            try:
                published = datetime.fromisoformat(added).replace(tzinfo=timezone.utc)
            except Exception:
                pass
        url = f"https://www.cisa.gov/known-exploited-vulnerabilities-catalog?search_api_fulltext={cve_id}"
        output.append(
            Article(
                title=title,
                url=canonicalize_url(url),
                source="CISA KEV",
                published_at=published,
                summary=summary,
                kind="news",
                source_type="cisa_kev",
                raw=item,
                guid=stable_guid(url, "CISA KEV"),
            )
        )
    return output
