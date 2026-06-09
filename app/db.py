from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence
from urllib.parse import urlparse

from app.helpers import canonicalize_url, hn_guid, now_utc, stable_guid
from app.models import Article
from app.state_store import load_json_file

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

def utc_iso(dt: datetime | None = None) -> str:
    value = dt or now_utc()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def connect_database(path: Path | str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_database(conn: sqlite3.Connection, state_dir: Path | None = None, hn_hot_queries: Sequence[Dict[str, Any]] | None = None) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    ensure_v2_columns(conn)
    ensure_v2_indexes(conn)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (1, utc_iso()),
    )
    seed_hn_hot_queries(conn, hn_hot_queries or [])
    if state_dir is not None:
        migrate_legacy_json_state(conn, state_dir)
    conn.commit()


def ensure_v2_columns(conn: sqlite3.Connection) -> None:
    ensure_columns(
        conn,
        "articles",
        {
            "source_rank": "INTEGER",
            "source_list": "TEXT NOT NULL DEFAULT ''",
            "source_family": "TEXT NOT NULL DEFAULT ''",
            "base_score": "REAL NOT NULL DEFAULT 0",
            "bonus_score": "REAL NOT NULL DEFAULT 0",
            "hot_signal_type": "TEXT NOT NULL DEFAULT ''",
        },
    )
    ensure_columns(
        conn,
        "hn_hot_items",
        {
            "matched_terms_json": "TEXT NOT NULL DEFAULT '[]'",
        },
    )


def ensure_columns(conn: sqlite3.Connection, table: str, additions: Dict[str, str]) -> None:
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_v2_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_source_rank ON articles(source_family, source_list, source_rank)")


def article_domain(url: str) -> str:
    try:
        host = urlparse(canonicalize_url(url)).netloc.lower()
    except Exception:
        host = ""
    return host[4:] if host.startswith("www.") else host


def article_to_row(article: Article) -> Dict[str, Any]:
    canonical_url = canonicalize_url(article.url)
    source_type = article.source_type or infer_source_type(article.source)
    source_family = article.source_family or source_type
    guid = article.guid
    if not guid and article.hn_id is not None:
        guid = hn_guid(article.hn_id)
    return {
        "guid": guid or stable_guid(canonical_url, article.source),
        "canonical_url": canonical_url,
        "title": article.title or "(no title)",
        "url": article.url or canonical_url,
        "domain": article.domain or article_domain(canonical_url),
        "source_name": article.source,
        "source_type": source_type,
        "published_at": utc_iso(article.published_at),
        "discovered_at": utc_iso(article.discovered_at),
        "summary": article.summary or "",
        "content_text": article.content_text or "",
        "lang": article.lang,
        "hn_id": article.hn_id,
        "hn_points": float(article.score or article.hn_points or 0.0),
        "hn_comments": int(article.comments or article.hn_comments or 0),
        "source_rank": article.source_rank,
        "source_list": article.source_list,
        "source_family": source_family,
        "base_score": float(article.base_score or 0.0),
        "bonus_score": float(article.bonus_score or 0.0),
        "hot_signal_type": article.hot_signal_type,
        "raw_json": json.dumps(article.raw or {}, ensure_ascii=False, sort_keys=True),
        "updated_at": utc_iso(),
    }


def row_to_article(row: sqlite3.Row | Dict[str, Any]) -> Article:
    payload = dict(row)
    published_at = datetime.fromisoformat(str(payload["published_at"]))
    discovered_at = datetime.fromisoformat(str(payload.get("discovered_at") or payload["published_at"]))
    try:
        raw = json.loads(str(payload.get("raw_json") or "{}"))
    except Exception:
        raw = {}
    return Article(
        title=str(payload.get("title") or ""),
        url=str(payload.get("url") or payload.get("canonical_url") or ""),
        source=str(payload.get("source_name") or ""),
        published_at=published_at,
        summary=str(payload.get("summary") or ""),
        lang=payload.get("lang"),
        score=float(payload.get("hn_points") or 0.0),
        comments=int(payload.get("hn_comments") or 0),
        kind=str(payload.get("kind") or "news"),
        priority=float(payload.get("priority") or payload.get("importance_score") or 0.0),
        guid=str(payload.get("guid") or ""),
        source_type=str(payload.get("source_type") or ""),
        domain=str(payload.get("domain") or ""),
        discovered_at=discovered_at,
        content_text=str(payload.get("content_text") or ""),
        hn_id=payload.get("hn_id"),
        hn_points=float(payload.get("hn_points") or 0.0),
        hn_comments=int(payload.get("hn_comments") or 0),
        source_rank=payload.get("source_rank"),
        source_list=str(payload.get("source_list") or ""),
        source_family=str(payload.get("source_family") or ""),
        base_score=float(payload.get("base_score") or 0.0),
        bonus_score=float(payload.get("bonus_score") or 0.0),
        hot_signal_type=str(payload.get("hot_signal_type") or ""),
        raw=raw,
    )


def infer_source_type(source: str) -> str:
    if source == "Hacker News":
        return "hn"
    if source.startswith("NewsAPI"):
        return "newsapi"
    if source.startswith("GDELT"):
        return "gdelt"
    if source == "HN Hot":
        return "hn_hot"
    return "rss"


def upsert_source(
    conn: sqlite3.Connection,
    name: str,
    source_type: str,
    url: str = "",
    enabled: bool = True,
    observe_only: bool = False,
    trust_weight: float = 0.6,
    topic_hint: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO sources(name, type, url, enabled, observe_only, trust_weight, topic_hint, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
          type=excluded.type,
          url=excluded.url,
          enabled=excluded.enabled,
          observe_only=excluded.observe_only,
          trust_weight=excluded.trust_weight,
          topic_hint=excluded.topic_hint,
          updated_at=excluded.updated_at
        """,
        (name, source_type, url, int(enabled), int(observe_only), float(trust_weight), topic_hint, utc_iso()),
    )


def seed_sources_from_rules(conn: sqlite3.Connection, rules: Dict[str, Any]) -> None:
    weights = dict(rules.get("source_weights") or {})
    upsert_source(conn, "Hacker News", "hn", trust_weight=float(weights.get("Hacker News", 0.9)))
    upsert_source(conn, "NewsAPI", "newsapi", trust_weight=float(weights.get("NewsAPI", 0.65)))
    upsert_source(conn, "GDELT", "gdelt", trust_weight=float(weights.get("GDELT", 0.62)), observe_only=True)
    upsert_source(conn, "CISA KEV", "cisa_kev", trust_weight=float(weights.get("CISA KEV", 1.0)), topic_hint="devops_security")
    for source in rules.get("sources", {}).get("rss_sources", []):
        if not isinstance(source, dict):
            continue
        name = str(source.get("name") or "").strip()
        url = str(source.get("url") or "").strip()
        if not name:
            continue
        upsert_source(
            conn,
            name,
            "rss",
            url=url,
            enabled=source.get("enabled", True) is not False,
            observe_only=bool(source.get("observe_only")),
            trust_weight=float(weights.get(name, 0.6)),
            topic_hint=str(source.get("topic_hint") or ""),
        )


def upsert_article(conn: sqlite3.Connection, article: Article, source_type: str | None = None, raw: Dict[str, Any] | None = None) -> Article:
    typed = article
    if source_type or raw:
        typed = replace(
            article,
            source_type=source_type or article.source_type,
            raw=raw if raw is not None else article.raw,
            guid=article.guid or stable_guid(article.url, article.source),
            domain=article.domain or article_domain(article.url),
            discovered_at=article.discovered_at or now_utc(),
        )
    row = article_to_row(typed)
    existing = None
    if row["hn_id"] is not None:
        existing = conn.execute("SELECT * FROM articles WHERE hn_id = ?", (row["hn_id"],)).fetchone()
    if existing is None:
        existing = conn.execute("SELECT * FROM articles WHERE canonical_url = ?", (row["canonical_url"],)).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO articles(
              guid, canonical_url, title, url, domain, source_name, source_type, published_at,
              discovered_at, summary, content_text, lang, hn_id, hn_points, hn_comments,
              source_rank, source_list, source_family, base_score, bonus_score, hot_signal_type,
              raw_json, updated_at
            ) VALUES (
              :guid, :canonical_url, :title, :url, :domain, :source_name, :source_type, :published_at,
              :discovered_at, :summary, :content_text, :lang, :hn_id, :hn_points, :hn_comments,
              :source_rank, :source_list, :source_family, :base_score, :bonus_score, :hot_signal_type,
              :raw_json, :updated_at
            )
            """,
            row,
        )
    else:
        existing_hn_id = existing["hn_id"]
        if row["hn_id"] is not None and existing_hn_id is not None and int(row["hn_id"]) != int(existing_hn_id):
            raise RuntimeError(f"Refusing to merge different HN ids on canonical URL {row['canonical_url']}")
        conn.execute(
            """
            UPDATE articles SET
              canonical_url=:canonical_url,
              title=CASE WHEN length(:title) > length(title) THEN :title ELSE title END,
              url=:url,
              source_name=CASE WHEN :hn_points > hn_points THEN :source_name ELSE source_name END,
              source_type=CASE WHEN :hn_points > hn_points THEN :source_type ELSE source_type END,
              published_at=MIN(published_at, :published_at),
              summary=CASE WHEN length(:summary) > length(summary) THEN :summary ELSE summary END,
              content_text=CASE WHEN length(:content_text) > length(content_text) THEN :content_text ELSE content_text END,
              lang=COALESCE(lang, :lang),
              hn_id=COALESCE(hn_id, :hn_id),
              hn_points=MAX(hn_points, :hn_points),
              hn_comments=MAX(hn_comments, :hn_comments),
              source_rank=CASE WHEN :base_score > base_score THEN :source_rank ELSE source_rank END,
              source_list=CASE WHEN :base_score > base_score THEN :source_list ELSE source_list END,
              source_family=CASE WHEN :base_score > base_score THEN :source_family ELSE source_family END,
              base_score=MAX(base_score, :base_score),
              bonus_score=MAX(bonus_score, :bonus_score),
              hot_signal_type=CASE WHEN :hot_signal_type != '' THEN :hot_signal_type ELSE hot_signal_type END,
              raw_json=CASE WHEN :raw_json != '{}' THEN :raw_json ELSE raw_json END,
              updated_at=:updated_at
            WHERE guid = :existing_guid
            """,
            {**row, "existing_guid": existing["guid"]},
        )
    stored = conn.execute("SELECT * FROM articles WHERE guid = ?", (existing["guid"] if existing else row["guid"],)).fetchone()
    guid = str(stored["guid"]) if stored else row["guid"]
    conn.execute(
        """
        INSERT OR REPLACE INTO article_sources(
          article_guid, source_name, source_type, source_url, external_id, discovered_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guid,
            row["source_name"],
            row["source_type"],
            row["url"],
            str(row["hn_id"] or row["canonical_url"]),
            row["discovered_at"],
            row["raw_json"],
        ),
    )
    return replace(typed, guid=guid, domain=row["domain"], source_type=row["source_type"])


def upsert_articles(conn: sqlite3.Connection, articles: Iterable[Article]) -> List[Article]:
    stored = [upsert_article(conn, article) for article in articles]
    conn.commit()
    return stored


def start_fetch_run(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "INSERT INTO fetch_runs(started_at, status, summary_json) VALUES (?, ?, ?)",
        (utc_iso(), "running", "{}"),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_fetch_run(conn: sqlite3.Connection, run_id: int, status: str, summary: Dict[str, Any]) -> None:
    conn.execute(
        "UPDATE fetch_runs SET finished_at = ?, status = ?, summary_json = ? WHERE id = ?",
        (utc_iso(), status, json.dumps(summary, ensure_ascii=False, sort_keys=True), run_id),
    )
    conn.commit()


def replace_classifications(conn: sqlite3.Connection, run_id: int, rows: Sequence[Dict[str, Any]]) -> None:
    for row in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO classifications(
              article_guid, run_id, profile, bucket, importance_score, relevance_score, confidence_score,
              reason_code, reason_summary, matched_terms_json, score_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["article_guid"],
                run_id,
                row["profile"],
                row["bucket"],
                float(row.get("importance_score", 0.0)),
                float(row.get("relevance_score", 0.0)),
                float(row.get("confidence_score", 0.0)),
                row.get("reason_code", ""),
                row.get("reason_summary", ""),
                json.dumps(row.get("matched_terms", []), ensure_ascii=False, sort_keys=True),
                json.dumps(row.get("scores", {}), ensure_ascii=False, sort_keys=True),
                utc_iso(),
            ),
        )
    conn.commit()


def replace_feed_items(conn: sqlite3.Connection, feed_name: str, article_guids: Sequence[str], run_id: int, persistent: bool = False) -> None:
    if persistent:
        conn.execute("DELETE FROM feed_items WHERE feed_name = ?", (feed_name,))
    else:
        conn.execute("DELETE FROM feed_items WHERE feed_name = ? AND persistent = 0", (feed_name,))
    for rank, guid in enumerate(article_guids, start=1):
        conn.execute(
            """
            INSERT OR REPLACE INTO feed_items(feed_name, article_guid, rank, added_at, persistent, run_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (feed_name, guid, rank, utc_iso(), int(persistent), run_id),
        )
    conn.commit()


def feed_articles(conn: sqlite3.Connection, feed_name: str, limit: int) -> List[Article]:
    sql_limit = int(limit)
    if sql_limit <= 0:
        sql_limit = -1
    rows = conn.execute(
        """
        SELECT a.*, c.importance_score AS priority, c.reason_summary
        FROM feed_items f
        JOIN articles a ON a.guid = f.article_guid
        LEFT JOIN classifications c ON c.article_guid = a.guid
          AND c.run_id = f.run_id
          AND c.bucket = CASE WHEN f.feed_name = 'news' THEN 'news' ELSE c.bucket END
        WHERE f.feed_name = ?
        GROUP BY a.guid
        ORDER BY f.rank ASC, a.published_at DESC
        LIMIT ?
        """,
        (feed_name, sql_limit),
    ).fetchall()
    return [row_to_article(row) for row in rows]


def replace_event_clusters(conn: sqlite3.Connection, run_id: int, rows: Sequence[Dict[str, Any]]) -> None:
    conn.execute("DELETE FROM event_cluster_items WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM event_clusters WHERE run_id = ?", (run_id,))
    for row in rows:
        article = row["article"]
        cluster = dict(row.get("cluster") or {})
        items = list(cluster.get("items") or [article])
        conn.execute(
            """
            INSERT OR REPLACE INTO event_clusters(
              cluster_id, run_id, canonical_title, representative_guid, representative_url,
              first_seen_at, last_seen_at, source_count, independent_source_count,
              hotness_score, confidence_score, bucket, reason_code, score_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(cluster.get("cluster_id") or row["article_guid"]),
                run_id,
                str(cluster.get("canonical_title") or article.title),
                str(row["article_guid"]),
                article.url,
                utc_iso(cluster.get("first_seen_at") or article.published_at),
                utc_iso(cluster.get("last_seen_at") or article.published_at),
                int(cluster.get("source_count") or len(items)),
                int(cluster.get("independent_source_count") or 1),
                float(row.get("importance_score") or 0.0),
                float(row.get("confidence_score") or 0.0),
                str(row.get("bucket") or ""),
                str(row.get("reason_code") or ""),
                json.dumps(row.get("scores", {}), ensure_ascii=False, sort_keys=True),
                utc_iso(),
            ),
        )
        for item in items:
            conn.execute(
                """
                INSERT OR REPLACE INTO event_cluster_items(
                  cluster_id, run_id, article_guid, source_name, source_family, list_name,
                  rank_in_source, match_type, similarity_score, base_score, bonus_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(cluster.get("cluster_id") or row["article_guid"]),
                    run_id,
                    item.guid,
                    item.source,
                    item.source_family or item.source_type,
                    item.source_list,
                    item.source_rank,
                    str(cluster.get("match_types", {}).get(item.guid, "")) if isinstance(cluster.get("match_types"), dict) else "",
                    float(cluster.get("similarities", {}).get(item.guid, 0.0)) if isinstance(cluster.get("similarities"), dict) else 0.0,
                    float(item.base_score or 0.0),
                    float(item.bonus_score or 0.0),
                ),
            )
    conn.commit()


def classification_details_for_feed(conn: sqlite3.Connection, feed_name: str) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT f.article_guid, c.*
        FROM feed_items f
        LEFT JOIN classifications c ON c.article_guid = f.article_guid AND c.run_id = f.run_id
        WHERE f.feed_name = ?
        """,
        (feed_name,),
    ).fetchall()
    details: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not row["profile"]:
            continue
        details[str(row["article_guid"])] = {
            "bucket": row["bucket"],
            "reason": row["reason_code"],
            "reason_summary": row["reason_summary"],
            "profile": row["profile"],
            "matches": {
                "interest_keywords": json.loads(row["matched_terms_json"] or "[]"),
                "topic_terms": [],
                "exclude_terms": [],
            },
            "scores": {"weighted": json.loads(row["score_json"] or "{}")},
            "weights": {},
        }
    return details


def append_feedback_event(conn: sqlite3.Connection, event: Dict[str, Any]) -> None:
    article_guid = str(event.get("guid") or event.get("article_guid") or "")
    action = str(event.get("action") or "")
    created_at = str(event.get("created_at") or utc_iso())
    conn.execute(
        """
        INSERT INTO feedback_events(action, article_guid, source, url, profile, remote_addr, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action,
            article_guid,
            str(event.get("source") or ""),
            str(event.get("url") or ""),
            str(event.get("profile") or ""),
            str(event.get("remote_addr") or ""),
            created_at,
        ),
    )
    if action == "save" and article_guid and conn.execute("SELECT 1 FROM articles WHERE guid = ?", (article_guid,)).fetchone():
        conn.execute(
            "INSERT OR REPLACE INTO saved_articles(article_guid, note, created_at) VALUES (?, ?, ?)",
            (article_guid, str(event.get("note") or ""), created_at),
        )
    conn.commit()


def load_feedback_events_from_db(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT action, article_guid AS guid, source, url, profile, remote_addr, created_at
        FROM feedback_events
        ORDER BY id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def save_daily_issue(
    conn: sqlite3.Connection,
    issue_date: date,
    bucket: str,
    archive_path: str,
    link: str,
    guid: str,
    articles: Sequence[Article],
) -> bool:
    issue_key = issue_date.isoformat()
    exists = conn.execute(
        "SELECT 1 FROM daily_issues WHERE issue_date = ? AND bucket = ?",
        (issue_key, bucket),
    ).fetchone()
    if exists:
        return False
    conn.execute(
        """
        INSERT INTO daily_issues(issue_date, bucket, archive_path, link, guid, item_count, top_titles_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            issue_key,
            bucket,
            archive_path,
            link,
            guid,
            len(articles),
            json.dumps([article.title for article in articles[:5]], ensure_ascii=False),
            utc_iso(),
        ),
    )
    for rank, article in enumerate(articles, start=1):
        conn.execute(
            "INSERT OR REPLACE INTO daily_issue_items(issue_date, bucket, article_guid, rank) VALUES (?, ?, ?, ?)",
            (issue_key, bucket, article.guid, rank),
        )
    conn.commit()
    return True


def daily_entries(conn: sqlite3.Connection, bucket: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM daily_issues WHERE bucket = ? ORDER BY issue_date DESC",
        (bucket,),
    ).fetchall()
    return [
        {
            "bucket": row["bucket"],
            "date": row["issue_date"],
            "archive_path": row["archive_path"],
            "link": row["link"],
            "guid": row["guid"],
            "item_count": row["item_count"],
            "top_titles": json.loads(row["top_titles_json"] or "[]"),
        }
        for row in rows
    ]


def published_daily_guids(conn: sqlite3.Connection, bucket: str) -> set[str]:
    rows = conn.execute(
        "SELECT article_guid FROM daily_issue_items WHERE bucket = ?",
        (bucket,),
    ).fetchall()
    return {str(row["article_guid"]) for row in rows}


def seed_hn_hot_queries(conn: sqlite3.Connection, queries: Sequence[Dict[str, Any]]) -> None:
    for query in queries:
        name = str(query.get("name") or "").strip()
        expression = str(query.get("query") or "").strip()
        if not name or not expression:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO hn_hot_queries(name, query, min_points, item_limit, enabled, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                expression,
                int(query.get("min_points", 100)),
                int(query.get("item_limit", 20)),
                int(bool(query.get("enabled", True))),
                utc_iso(),
            ),
        )


def enabled_hn_hot_queries(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM hn_hot_queries WHERE enabled = 1 ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def replace_hn_hot_items(conn: sqlite3.Connection, query_name: str, items: Sequence[Article]) -> None:
    conn.execute("DELETE FROM hn_hot_items WHERE query_name = ?", (query_name,))
    for rank, article in enumerate(items, start=1):
        matched_terms = article.raw.get("hn_hot_matched_terms") if isinstance(article.raw, dict) else []
        if not isinstance(matched_terms, list):
            matched_terms = []
        matched_terms = [str(term).strip() for term in matched_terms if str(term).strip()]
        conn.execute(
            """
            INSERT OR REPLACE INTO hn_hot_items(
              query_name, article_guid, rank, points, comments, matched_terms_json, fetched_at, persistent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                query_name,
                article.guid,
                rank,
                float(article.score),
                int(article.comments),
                json.dumps(matched_terms, ensure_ascii=False),
                utc_iso(),
            ),
        )
    conn.commit()


def hn_hot_selection_details(conn: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT
          h.article_guid, h.query_name, h.rank, h.points, h.comments, h.matched_terms_json,
          q.query, q.min_points, q.item_limit
        FROM hn_hot_items h
        LEFT JOIN hn_hot_queries q ON q.name = h.query_name
        ORDER BY h.article_guid, h.rank, h.query_name
        """
    ).fetchall()
    details: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        try:
            matched_terms = json.loads(str(row["matched_terms_json"] or "[]"))
        except Exception:
            matched_terms = []
        if not isinstance(matched_terms, list):
            matched_terms = []
        guid = str(row["article_guid"])
        details.setdefault(guid, []).append(
            {
                "query_name": str(row["query_name"] or ""),
                "query": str(row["query"] or ""),
                "rank": int(row["rank"] or 0),
                "points": float(row["points"] or 0.0),
                "comments": int(row["comments"] or 0),
                "matched_terms": [str(term) for term in matched_terms if str(term).strip()],
                "min_points": int(row["min_points"] or 0),
                "item_limit": int(row["item_limit"] or 0),
            }
        )
    return details


def hn_hot_feed_guids(conn: sqlite3.Connection, limit: int) -> List[str]:
    rows = conn.execute(
        """
        SELECT article_guid
        FROM hn_hot_items
        GROUP BY article_guid
        ORDER BY MAX(points) DESC, MAX(comments) DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [str(row["article_guid"]) for row in rows]


def merge_unique_terms(existing: Sequence[Any], additions: Sequence[Any]) -> List[str]:
    seen: set[str] = set()
    merged: List[str] = []
    for value in list(existing) + list(additions):
        term = str(value or "").strip()
        key = term.lower()
        if not term or key in seen:
            continue
        seen.add(key)
        merged.append(term)
    return merged


def apply_rule_overlays(conn: sqlite3.Connection, rules: Dict[str, Any]) -> Dict[str, Any]:
    merged = json.loads(json.dumps(rules))
    rows = conn.execute(
        "SELECT profile, term_type, term FROM rule_terms WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    feeds = merged.setdefault("feeds", {})
    profiles = merged.setdefault("topic_profiles", {})
    queries = merged.setdefault("queries", {})
    for row in rows:
        profile = str(row["profile"])
        term_type = str(row["term_type"])
        term = str(row["term"]).strip()
        if not term:
            continue
        if profile == "feeds" and term_type == "exclude":
            feeds["exclude_terms"] = merge_unique_terms(feeds.get("exclude_terms", []), [term])
        elif profile == "feeds" and term_type == "watched_entity":
            feeds["watched_entities"] = merge_unique_terms(feeds.get("watched_entities", []), [term])
        elif profile == "queries" and term_type in {"newsapi", "gdelt"}:
            key = "newsapi_terms" if term_type == "newsapi" else "gdelt_terms"
            queries[key] = merge_unique_terms(queries.get(key, []), [term])
        elif term_type in {"must_track", "watch"}:
            bucket = profiles.setdefault(profile, {"must_track": [], "watch": []})
            bucket[term_type] = merge_unique_terms(bucket.get(term_type, []), [term])
    for row in conn.execute("SELECT key, value_json FROM rule_settings").fetchall():
        key = str(row["key"])
        try:
            value = json.loads(str(row["value_json"]))
        except Exception:
            continue
        if key == "feeds.hn_news" and isinstance(value, dict):
            current = dict(feeds.get("hn_news") or {})
            current.update(value)
            feeds["hn_news"] = current
    return merged


def rule_terms(conn: sqlite3.Connection, include_disabled: bool = True) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM rule_terms ORDER BY profile, term_type, term"
    params: tuple[Any, ...] = ()
    if not include_disabled:
        sql = "SELECT * FROM rule_terms WHERE enabled = 1 ORDER BY profile, term_type, term"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def upsert_rule_term(
    conn: sqlite3.Connection,
    profile: str,
    term_type: str,
    term: str,
    enabled: bool = True,
    notes: str = "",
    source: str = "admin",
) -> None:
    now = utc_iso()
    conn.execute(
        """
        INSERT INTO rule_terms(profile, term_type, term, enabled, notes, source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(profile, term_type, term) DO UPDATE SET
          enabled=excluded.enabled,
          notes=excluded.notes,
          source=excluded.source,
          updated_at=excluded.updated_at
        """,
        (profile, term_type, term, int(enabled), notes, source, now, now),
    )
    conn.commit()


def set_rule_term_enabled(conn: sqlite3.Connection, term_id: int, enabled: bool) -> bool:
    cursor = conn.execute(
        "UPDATE rule_terms SET enabled = ?, updated_at = ? WHERE id = ?",
        (int(enabled), utc_iso(), int(term_id)),
    )
    conn.commit()
    return cursor.rowcount > 0


def admin_rows(conn: sqlite3.Connection, table: str, limit: int = 100) -> List[Dict[str, Any]]:
    allowed = {
        "articles": "SELECT * FROM articles ORDER BY published_at DESC LIMIT ?",
        "feedback_events": "SELECT * FROM feedback_events ORDER BY id DESC LIMIT ?",
        "saved_articles": "SELECT s.*, a.title, a.url, a.source_name FROM saved_articles s JOIN articles a ON a.guid = s.article_guid ORDER BY s.created_at DESC LIMIT ?",
        "fetch_runs": "SELECT * FROM fetch_runs ORDER BY id DESC LIMIT ?",
        "hn_hot": "SELECT h.*, a.title, a.url FROM hn_hot_items h JOIN articles a ON a.guid = h.article_guid ORDER BY h.query_name, h.rank LIMIT ?",
        "sources": "SELECT * FROM sources ORDER BY name LIMIT ?",
        "rule_terms": "SELECT * FROM rule_terms ORDER BY profile, term_type, term LIMIT ?",
    }
    sql = allowed[table]
    return [dict(row) for row in conn.execute(sql, (int(limit),)).fetchall()]


def article_source_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT article_guid, COUNT(DISTINCT source_name) AS source_count FROM article_sources GROUP BY article_guid"
    ).fetchall()
    return {str(row["article_guid"]): int(row["source_count"]) for row in rows}


def article_detail(conn: sqlite3.Connection, guid: str) -> Dict[str, Any] | None:
    article = conn.execute("SELECT * FROM articles WHERE guid = ?", (guid,)).fetchone()
    if not article:
        return None
    classifications = [dict(row) for row in conn.execute("SELECT * FROM classifications WHERE article_guid = ? ORDER BY run_id DESC", (guid,)).fetchall()]
    sources = [dict(row) for row in conn.execute("SELECT * FROM article_sources WHERE article_guid = ?", (guid,)).fetchall()]
    feedback = [dict(row) for row in conn.execute("SELECT * FROM feedback_events WHERE article_guid = ? ORDER BY id DESC", (guid,)).fetchall()]
    return {
        "article": dict(article),
        "classifications": classifications,
        "sources": sources,
        "feedback": feedback,
    }


def migrate_legacy_json_state(conn: sqlite3.Connection, state_dir: Path) -> None:
    marker = state_dir / ".db-migrated-v1"
    if marker.exists():
        return

    feedback = load_json_file(state_dir / "feedback.json", [])
    if isinstance(feedback, list):
        for event in feedback:
            if isinstance(event, dict):
                exists = conn.execute(
                    """
                    SELECT 1 FROM feedback_events
                    WHERE action = ? AND article_guid = ? AND created_at = ?
                    """,
                    (
                        str(event.get("action") or ""),
                        str(event.get("guid") or ""),
                        str(event.get("created_at") or ""),
                    ),
                ).fetchone()
                if not exists:
                    append_feedback_event(conn, event)

    daily = load_json_file(state_dir / "daily-index.json", [])
    if isinstance(daily, list):
        for entry in daily:
            if not isinstance(entry, dict):
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO daily_issues(
                  issue_date, bucket, archive_path, link, guid, item_count, top_titles_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(entry.get("date") or ""),
                    str(entry.get("bucket") or "news"),
                    str(entry.get("archive_path") or ""),
                    str(entry.get("link") or ""),
                    str(entry.get("guid") or stable_guid(str(entry.get("date") or ""), "daily")),
                    int(entry.get("item_count") or 0),
                    json.dumps(entry.get("top_titles") or [], ensure_ascii=False),
                    utc_iso(),
                ),
            )

    published = load_json_file(state_dir / "published-guids.json", [])
    if isinstance(published, list):
        for index, guid in enumerate(published, start=1):
            if not conn.execute("SELECT 1 FROM articles WHERE guid = ?", (str(guid),)).fetchone():
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO feed_items(feed_name, article_guid, rank, added_at, persistent, run_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("legacy-published", str(guid), index, utc_iso(), 1, 0),
            )

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(utc_iso() + "\n", encoding="utf-8")
