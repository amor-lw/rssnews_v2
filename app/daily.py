from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

from app.helpers import stable_guid
from app.models import Article
from app.rss_writer import build_rss


def issue_date_string(issue_date: date) -> str:
    return issue_date.isoformat()


def archive_relative_path(issue_date: date, bucket: str) -> str:
    return f"archive/{issue_date_string(issue_date)}/{bucket}.xml"


def archive_absolute_path(outdir: Path, issue_date: date, bucket: str) -> Path:
    return outdir / archive_relative_path(issue_date, bucket)


def find_daily_entry(entries: Sequence[Dict[str, Any]], issue_date: date, bucket: str) -> Dict[str, Any] | None:
    target_date = issue_date_string(issue_date)
    for entry in entries:
        if entry.get("date") == target_date and entry.get("bucket") == bucket:
            return entry
    return None


def select_daily_articles(articles: Sequence[Article], published_guids: Set[str], max_items: int) -> List[Article]:
    selected: List[Article] = []
    for article in articles:
        if article.guid in published_guids:
            continue
        selected.append(article)
        if len(selected) >= max_items:
            break
    return selected


def issue_timestamp(issue_date: date) -> datetime:
    return datetime.combine(issue_date, time(hour=8, minute=0), tzinfo=timezone.utc)


def create_daily_index_entry(base_url: str, issue_date: date, bucket: str, articles: Sequence[Article]) -> Dict[str, Any]:
    relpath = archive_relative_path(issue_date, bucket)
    top_titles = [article.title for article in articles[:5]]
    return {
        "bucket": bucket,
        "date": issue_date_string(issue_date),
        "archive_path": relpath,
        "link": f"{base_url.rstrip('/')}/{relpath}",
        "guid": stable_guid(f"{issue_date_string(issue_date)}|{bucket}", "daily"),
        "item_count": len(articles),
        "top_titles": top_titles,
    }


def build_daily_archive_feed(base_url: str, issue_date: date, bucket: str, articles: Sequence[Article]) -> str:
    return build_rss(
        channel_title=f"My {bucket.title()} Daily Snapshot ({issue_date_string(issue_date)})",
        channel_link=f"{base_url.rstrip('/')}/{archive_relative_path(issue_date, bucket)}",
        channel_desc=f"Daily archived snapshot for {bucket} on {issue_date_string(issue_date)}.",
        items=list(articles),
    )


def write_daily_archive(outdir: Path, base_url: str, issue_date: date, bucket: str, articles: Sequence[Article]) -> Dict[str, Any]:
    archive_path = archive_absolute_path(outdir, issue_date, bucket)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(build_daily_archive_feed(base_url, issue_date, bucket, articles), encoding="utf-8")
    return create_daily_index_entry(base_url, issue_date, bucket, articles)


def build_daily_summary_article(entry: Dict[str, Any]) -> Article:
    issue_date = date.fromisoformat(str(entry["date"]))
    top_titles = list(entry.get("top_titles", []))
    summary = "\n".join(f"- {title}" for title in top_titles)
    return Article(
        title=f"{entry['bucket'].title()} Daily {entry['date']} ({entry['item_count']} items)",
        url=str(entry["link"]),
        source="Daily Archive",
        published_at=issue_timestamp(issue_date),
        summary=summary,
        guid=str(entry["guid"]),
        kind="daily",
        priority=float(entry.get("item_count", 0)),
    )


def build_daily_index_feed(base_url: str, bucket: str, entries: Iterable[Dict[str, Any]]) -> str:
    ordered_entries = sorted(
        [entry for entry in entries if entry.get("bucket") == bucket],
        key=lambda item: str(item.get("date", "")),
        reverse=True,
    )
    items = [build_daily_summary_article(entry) for entry in ordered_entries]
    feed_name = "daily.xml" if bucket == "news" else f"daily-{bucket}.xml"
    return build_rss(
        channel_title=f"My {bucket.title()} Daily Archive",
        channel_link=f"{base_url.rstrip('/')}/{feed_name}",
        channel_desc=f"Daily archived snapshots for {bucket}, newest first.",
        items=items,
        include_feedback_links=False,
    )


def ensure_daily_archive(
    outdir: Path,
    base_url: str,
    issue_date: date,
    bucket: str,
    articles: Sequence[Article],
    max_items: int,
    published_guids: Set[str],
    daily_index: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Set[str]]:
    existing_entry = find_daily_entry(daily_index, issue_date, bucket)
    if existing_entry is not None:
        return daily_index, published_guids

    selected = select_daily_articles(articles, published_guids, max_items)
    if not selected:
        return daily_index, published_guids

    entry = write_daily_archive(outdir, base_url, issue_date, bucket, selected)
    next_index = list(daily_index)
    next_index.append(entry)

    next_guids = set(published_guids)
    next_guids.update(article.guid for article in selected)
    return next_index, next_guids
