from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def canonicalize_url(url: str) -> str:
    normalized = url.strip().replace("http://", "https://")
    normalized = re.sub(r"#.*$", "", normalized)
    try:
        parsed = urlparse(normalized)
        query = ""
        host = parsed.netloc.lower()
        if host == "news.ycombinator.com" and parsed.path.rstrip("/") == "/item":
            params = dict(parse_qsl(parsed.query, keep_blank_values=False))
            if params.get("id"):
                query = urlencode({"id": params["id"]})
        parsed = parsed._replace(query=query)
        normalized = urlunparse(parsed)
    except Exception:
        pass
    if normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized


def safe_text(text: str, limit: int = 350) -> str:
    escaped = html.escape(text or "")
    if len(escaped) > limit:
        escaped = escaped[: limit - 1] + "…"
    return escaped


def keyword_hit(title: str, keywords: list[str] | tuple[str, ...]) -> float:
    normalized = (title or "").lower()
    hits = sum(1 for keyword in keywords if keyword.lower() in normalized)
    if hits <= 0:
        return 0.0
    return min(1.0, 0.25 + 0.15 * (hits - 1))


def stable_guid(url: str, source: str) -> str:
    return sha1_text(f"{source}|{canonicalize_url(url)}")


def hn_guid(hn_id: int | str) -> str:
    return sha1_text(f"hn|{int(hn_id)}")


def to_rfc2822(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)
