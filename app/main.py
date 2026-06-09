from __future__ import annotations

import argparse
import dataclasses
import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse, urlunparse

from app.daily import build_daily_index_feed, ensure_daily_archive
from app.db import (
    apply_rule_overlays,
    article_source_counts,
    classification_details_for_feed,
    connect_database,
    daily_entries,
    enabled_hn_hot_queries,
    feed_articles,
    finish_fetch_run,
    hn_hot_feed_guids,
    hn_hot_selection_details,
    init_database,
    load_feedback_events_from_db,
    published_daily_guids,
    replace_event_clusters,
    replace_classifications,
    replace_feed_items,
    replace_hn_hot_items,
    save_daily_issue,
    seed_sources_from_rules,
    start_fetch_run,
    upsert_articles,
)
from app.fetchers import fetch_cisa_kev_catalog, fetch_gdelt_doc, fetch_hn, fetch_hn_algolia_hot, fetch_newsapi_everything, fetch_newsapi_headlines, fetch_rss_feed, hn_query_terms
from app.feedback import apply_feedback_to_source_weights, build_feedback_preferences, load_feedback_events
from app.helpers import canonicalize_url, stable_guid
from app.hotness import annotate_hot_signal, annotate_ranked_articles, classify_hot_events, list_position_score, select_hot_feed_articles
from app.models import Article
from app.news_manager import all_profile_keywords
from app.pipeline import (
    article_source_weight,
    classify_with_reason,
    dedupe,
    dedupe_by_url,
    matched_terms,
    merge_dedupe_cross_source,
    rank_articles,
    score_breakdown,
)
from app.rss_writer import build_rss
from app.settings import (
    DEFAULT_ENV_FILE,
    DEFAULT_HOTNESS,
    DEFAULT_HOTNESS_FILE,
    DEFAULT_HN_HOT_QUERIES,
    DEFAULT_HN_HOT_QUERIES_FILE,
    DEFAULT_NOISE_FILE,
    DEFAULT_NOISE_RULES,
    DEFAULT_RULES_FILE,
    DEFAULT_SOURCES,
    DEFAULT_SOURCES_FILE,
    env_or_default,
    load_env_file,
    load_json_config,
    load_rules_config,
)
from app.state_store import load_daily_index, load_published_guids, save_daily_index, save_published_guids


OBSOLETE_OUTPUT_FILES = ("tech.xml", "world.xml", "daily-tech.xml", "daily-world.xml")


def chunks(items: List[str], size: int) -> List[List[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def source_family(source: str) -> str:
    if source.startswith("GDELT:"):
        return "GDELT"
    return source


def site_root_from_base_url(base_url: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    return urlunparse(parsed._replace(path="", params="", query="", fragment="")).rstrip("/")


def count_by_source(articles: List[Article]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for article in articles:
        source = source_family(article.source)
        counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items()))


def remove_obsolete_outputs(outdir: Path) -> None:
    for filename in OBSOLETE_OUTPUT_FILES:
        try:
            (outdir / filename).unlink()
        except FileNotFoundError:
            continue


def build_source_report(
    fetches: List[Dict[str, Any]],
    raw_articles: List[Article],
    normalized_count: int,
    merged_count: int,
    news: List[Article],
    radar: List[Article],
    feedback_preferences: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "fetches": fetches,
        "summary": {
            "raw_candidates": len(raw_articles),
            "after_source_guid_dedupe": normalized_count,
            "after_cross_source_merge": merged_count,
            "news_items": len(news),
            "radar_items": len(radar),
        },
        "raw_by_source": count_by_source(raw_articles),
        "news_by_source": count_by_source(news),
        "radar_by_source": count_by_source(radar),
        "feedback": feedback_preferences or {
            "event_count": 0,
            "action_counts": {},
            "source_adjustments": {},
            "domain_adjustments": {},
        },
    }


def fetch_with_report(fetches: List[Dict[str, Any]], source: str, source_type: str, fn: Any, **metadata: Any) -> List[Article]:
    started = time.monotonic()
    record: Dict[str, Any] = {
        "source": source,
        "type": source_type,
        "ok": False,
        "items": 0,
        **metadata,
    }
    try:
        articles = fn()
        record["ok"] = True
        record["items"] = len(articles)
        return articles
    except Exception as exc:
        record["error"] = str(exc)
        raise
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        fetches.append(record)


def compact_hn_hot_values(values: List[str], limit: int = 6) -> str:
    cleaned: List[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    if len(cleaned) <= limit:
        return ", ".join(cleaned)
    return ", ".join(cleaned[:limit]) + f" +{len(cleaned) - limit} more"


def hn_hot_reason_summary(article: Article, selections: List[Dict[str, Any]]) -> str:
    if not selections:
        return f"HN 历史热帖：{int(article.score)} points / {article.comments} comments，按总热度常驻。"

    groups: List[str] = []
    terms: List[str] = []
    expressions: List[str] = []
    ranks: List[str] = []
    for selection in selections:
        name = str(selection.get("query_name") or "").strip()
        if name:
            groups.append(name)
        matched = [str(term) for term in selection.get("matched_terms") or [] if str(term).strip()]
        if matched:
            terms.extend(matched)
        expression = str(selection.get("query") or "").strip()
        if expression:
            expressions.append(expression)
            if not matched:
                terms.extend(hn_query_terms(expression))
        rank = int(selection.get("rank") or 0)
        if name and rank:
            ranks.append(f"{name} #{rank}")

    group_text = compact_hn_hot_values(groups) or "unknown"
    term_text = compact_hn_hot_values(terms) or compact_hn_hot_values(expressions) or "unknown"
    rank_text = f"；查询内排名：{compact_hn_hot_values(ranks)}" if ranks else ""
    return (
        f"HN 历史热帖：{int(article.score)} points / {article.comments} comments；"
        f"入选查询组：{group_text}；匹配关键词/关键词组：{term_text}{rank_text}。"
    )


def article_domain(article: Article) -> str:
    domain = urlparse(article.url).netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def build_debug_report(
    articles: List[Article],
    bucket_reasons: Dict[str, Dict[str, str]],
    keywords: List[str],
    topic_terms: List[str],
    exclude_terms: List[str],
    base_source_weights: Dict[str, float],
    adjusted_source_weights: Dict[str, float],
    scoring_config: Dict[str, float | Dict[str, float]],
) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for article in articles:
        domain = article_domain(article)
        searchable_text = f"{article.title} {article.summary}"
        bucket_info = bucket_reasons.get(article.guid) or {"bucket": "radar", "reason": "unknown"}
        base_source_weight = float(base_source_weights.get(article.source, 0.60))
        domain_delta = float(adjusted_source_weights.get(f"domain:{domain}", 0.0)) if domain else 0.0
        items.append(
            {
                "title": article.title,
                "url": article.url,
                "guid": article.guid,
                "source": article.source,
                "domain": domain,
                "published_at": article.published_at.isoformat(),
                "kind": article.kind,
                "bucket": bucket_info["bucket"],
                "reason": bucket_info["reason"],
                "priority": round(article.priority, 4),
                "scores": score_breakdown(article, keywords, adjusted_source_weights, scoring_config),
                "matches": {
                    "interest_keywords": matched_terms(searchable_text, keywords),
                    "topic_terms": matched_terms(searchable_text, topic_terms),
                    "exclude_terms": matched_terms(searchable_text, exclude_terms),
                },
                "weights": {
                    "base_source_weight": round(base_source_weight, 4),
                    "adjusted_source_weight": round(article_source_weight(article, adjusted_source_weights), 4),
                    "domain_feedback_delta": round(domain_delta, 4),
                },
            }
        )

    return {
        "summary": {
            "items": len(items),
            "news_items": sum(1 for item in items if item["bucket"] == "news"),
            "radar_items": sum(1 for item in items if item["bucket"] == "radar"),
        },
        "items": items,
    }


def build_index_html() -> str:
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>My RSS Feeds</title></head>
<body>
<h1>My RSS Feeds</h1>
<ul>
  <li><a href="./news.xml">news.xml</a></li>
  <li><a href="./radar.xml">radar.xml</a></li>
  <li><a href="./hn-hot.xml">hn-hot.xml</a></li>
  <li><a href="./daily.xml">daily.xml</a></li>
  <li><a href="./source-report.json">source-report.json</a></li>
  <li><a href="./debug-report.json">debug-report.json</a></li>
</ul>
</body></html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build personal RSS feeds from HN + NewsAPI + GDELT")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="Local env file with secrets and runtime settings")
    parser.add_argument("--rules-file", default=DEFAULT_RULES_FILE, help="JSON rules file for keywords and feed settings")
    parser.add_argument("--outdir", help="Output directory")
    parser.add_argument("--hours", type=int, help="Lookback window in hours")
    parser.add_argument("--hn-limit", type=int, help="HN stories per list")
    parser.add_argument("--max-items", type=int, help="Max items per feed")
    parser.add_argument("--daily-max-items", type=int, help="Max items per daily archive issue")
    parser.add_argument("--newsapi-page-size", type=int, help="NewsAPI pageSize (<=100)")
    parser.add_argument("--gdelt-max", type=int, help="GDELT maxrecords")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(args.env_file)
    rules = load_rules_config(args.rules_file)
    sources_config = load_json_config(DEFAULT_SOURCES_FILE, DEFAULT_SOURCES)
    hotness_config = load_json_config(DEFAULT_HOTNESS_FILE, DEFAULT_HOTNESS)
    noise_config = load_json_config(DEFAULT_NOISE_FILE, DEFAULT_NOISE_RULES)
    hn_hot_config = load_json_config(DEFAULT_HN_HOT_QUERIES_FILE, DEFAULT_HN_HOT_QUERIES)
    rules["source_weights"] = {
        **dict(rules.get("source_weights") or {}),
        **dict(sources_config.get("source_weights") or {}),
    }
    rules["newsapi"] = {
        **dict(rules.get("newsapi") or {}),
        **dict(sources_config.get("newsapi") or {}),
    }
    rules["sources"] = {
        "rss_sources": list((sources_config.get("sources") or {}).get("rss_sources") or (rules.get("sources") or {}).get("rss_sources") or [])
    }
    feed_noise_terms = list((rules.get("feeds") or {}).get("exclude_terms", []))
    noise_terms = list(dict.fromkeys(feed_noise_terms + list(noise_config.get("exclude_terms") or [])))

    outdir = Path(args.outdir or env_or_default("OUTDIR", "dist"))
    hours = args.hours if args.hours is not None else env_or_default("FETCH_HOURS", 24, int)
    hn_limit = args.hn_limit if args.hn_limit is not None else env_or_default("HN_LIMIT", 60, int)
    max_items = args.max_items if args.max_items is not None else env_or_default("MAX_ITEMS", 120, int)
    daily_max_items = args.daily_max_items if args.daily_max_items is not None else env_or_default("DAILY_MAX_ITEMS", 25, int)
    newsapi_page_size = args.newsapi_page_size if args.newsapi_page_size is not None else env_or_default(
        "NEWSAPI_PAGE_SIZE",
        int(hotness_config.get("newsapi_top_headlines_limit", 100)),
        int,
    )
    gdelt_max = args.gdelt_max if args.gdelt_max is not None else env_or_default("GDELT_MAX", 0, int)
    gdelt_enabled = str(env_or_default("GDELT_HOT_ENABLED", "false")).lower() in {"1", "true", "yes"}
    state_dir = Path(env_or_default("STATE_DIR", "state"))
    database_path = Path(env_or_default("DATABASE_PATH", str(state_dir / "rssnews.db")))
    news_max_items = 0
    radar_max_items = env_or_default("RADAR_MAX_ITEMS", int(hotness_config.get("radar_max_items", 50)), int)
    hotness_config["radar_max_items"] = radar_max_items
    hn_hot_max_items = env_or_default("HN_HOT_MAX_ITEMS", 50, int)
    hn_hot_enabled = str(env_or_default("HN_HOT_ENABLED", "true")).lower() not in {"0", "false", "no"}
    cisa_kev_enabled = str(env_or_default("CISA_KEV_ENABLED", "true")).lower() not in {"0", "false", "no"}
    cisa_kev_limit = env_or_default("CISA_KEV_LIMIT", 50, int)

    outdir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    remove_obsolete_outputs(outdir)
    conn = connect_database(database_path)
    init_database(conn, state_dir, hn_hot_queries=list(hn_hot_config.get("queries") or []))
    rules = apply_rule_overlays(conn, rules)

    keywords = all_profile_keywords(rules)
    feeds_config = dict(rules["feeds"])
    topic_terms = list(feeds_config.get("topic_terms") or (list(feeds_config.get("tech_terms", [])) + list(feeds_config.get("world_terms", []))))
    exclude_terms = noise_terms
    primary_bucket = str(feeds_config.get("primary_bucket") or "news")
    source_weights = dict(rules["source_weights"])
    scoring_config = dict(rules["scoring"])
    newsapi_languages = list(rules["newsapi"]["top_headlines_languages"])
    newsapi_category = rules["newsapi"].get("top_headlines_category")
    newsapi_country = rules["newsapi"].get("top_headlines_country")
    gdelt_terms = list(rules["queries"]["gdelt_terms"])
    rss_sources = list(rules.get("sources", {}).get("rss_sources", []))

    seed_sources_from_rules(conn, rules)
    run_id = start_fetch_run(conn)

    base_url = os.environ.get("BASE_URL", "https://example.com").rstrip("/")
    feedback_url = f"{site_root_from_base_url(base_url)}/feedback"
    newsapi_key = os.environ.get("NEWSAPI_KEY", "").strip()
    today = date.today()
    feedback_preferences = build_feedback_preferences(load_feedback_events_from_db(conn) or load_feedback_events(state_dir))
    adjusted_source_weights = apply_feedback_to_source_weights(source_weights, feedback_preferences)

    all_articles: List[Article] = []
    fetches: List[Dict[str, Any]] = []
    hn_top_limit = int(hotness_config.get("hn_topstories_limit", hn_limit))
    hn_topstories = fetch_with_report(
        fetches,
        source="Hacker News",
        source_type="hn",
        fn=lambda: fetch_hn("topstories", limit=hn_top_limit),
        list="topstories",
    )
    all_articles.extend(
        annotate_ranked_articles(
            hn_topstories,
            source_family_name="hn",
            source_list="topstories",
            hot_signal_type="hn_topstories",
            hotness_config=hotness_config,
            limit_key="hn_topstories_limit",
            first_key="hn_topstories_base_score_first",
            last_key="hn_topstories_base_score_last",
        )
    )
    hn_best_limit = int(hotness_config.get("hn_beststories_limit", max(30, hn_limit // 2)))
    hn_beststories = fetch_with_report(
        fetches,
        source="Hacker News",
        source_type="hn",
        fn=lambda: fetch_hn("beststories", limit=hn_best_limit),
        list="beststories",
    )
    all_articles.extend(
        annotate_ranked_articles(
            hn_beststories,
            source_family_name="hn",
            source_list="beststories",
            hot_signal_type="hn_beststories",
            hotness_config=hotness_config,
            limit_key="hn_beststories_limit",
            first_key="hn_beststories_bonus_first",
            last_key="hn_beststories_bonus_last",
            as_bonus=True,
        )
    )

    if cisa_kev_enabled:
        try:
            cisa_articles = fetch_with_report(
                fetches,
                source="CISA KEV",
                source_type="cisa_kev",
                fn=lambda: fetch_cisa_kev_catalog(limit=cisa_kev_limit),
            )
            all_articles.extend(
                annotate_hot_signal(
                    article,
                    source_family_name="cisa_kev",
                    source_list="kev",
                    source_rank=rank,
                    hot_signal_type="security_hot_signal",
                    base_score=12.0,
                )
                for rank, article in enumerate(cisa_articles, start=1)
            )
        except Exception as exc:
            print(f"[WARN] CISA KEV failed: {exc}")
    else:
        fetches.append({"source": "CISA KEV", "type": "cisa_kev", "ok": False, "items": 0, "skipped": True, "error": "CISA_KEV_ENABLED=false"})

    if newsapi_key:
        newsapi_articles: List[Article] = []
        newsapi_limit = min(int(hotness_config.get("newsapi_top_headlines_limit", 100)), int(newsapi_page_size))
        for language in newsapi_languages:
            try:
                fetched = fetch_with_report(
                    fetches,
                    source=f"NewsAPI:{language}",
                    source_type="newsapi_headlines",
                    fn=lambda language=language: fetch_newsapi_headlines(
                        api_key=newsapi_key,
                        language=language,
                        category=newsapi_category,
                        country=newsapi_country,
                        page_size=newsapi_limit,
                        kind="news",
                    ),
                    language=language,
                    default_bucket="news",
                )
                newsapi_articles.extend(
                    annotate_ranked_articles(
                        fetched,
                        source_family_name="newsapi",
                        source_list="top-headlines",
                        hot_signal_type="newsapi_top_headlines",
                        hotness_config=hotness_config,
                        limit_key="newsapi_top_headlines_limit",
                        first_key="newsapi_base_score_first",
                        last_key="newsapi_base_score_last",
                    )
                )
            except Exception as exc:
                print(f"[WARN] NewsAPI {language} headlines failed: {exc}")
        all_articles.extend(newsapi_articles)
    else:
        print("[WARN] Missing NEWSAPI_KEY, skipping NewsAPI fetches")
        fetches.append({"source": "NewsAPI", "type": "newsapi", "ok": False, "items": 0, "error": "missing NEWSAPI_KEY"})

    for source_config in rss_sources:
        if not isinstance(source_config, dict) or source_config.get("enabled", True) is False:
            if isinstance(source_config, dict):
                fetches.append(
                    {
                        "source": str(source_config.get("name") or "RSS source"),
                        "type": "rss",
                        "ok": False,
                        "items": 0,
                        "skipped": True,
                        "error": str(source_config.get("disabled_reason") or "disabled"),
                    }
                )
            continue
        name = str(source_config.get("name") or "").strip()
        url = str(source_config.get("url") or "").strip()
        if not name or not url:
            continue
        try:
            max_age = source_config.get("max_age_hours")
            hot_signal = bool(source_config.get("hot_signal"))
            source_kind = "news" if hot_signal else "radar"
            rss_articles = fetch_with_report(
                fetches,
                source=name,
                source_type="rss",
                fn=lambda: fetch_rss_feed(
                    name=name,
                    url=url,
                    limit=int(source_config.get("limit", 30)),
                    max_age_hours=int(max_age) if max_age is not None else None,
                    kind=source_kind,
                ),
                hot_signal=hot_signal,
                verification_only=bool(source_config.get("verification_only")),
                hot_signal_type=str(source_config.get("hot_signal_type") or ""),
            )
            rss_base = float(source_config.get("base_score", 10 if hot_signal else 0))
            all_articles.extend(
                annotate_hot_signal(
                    article,
                    source_family_name="rss",
                    source_list=name,
                    source_rank=rank,
                    hot_signal_type=str(source_config.get("hot_signal_type") or ("rss_hot_signal" if hot_signal else "rss_verification")),
                    base_score=rss_base if hot_signal else 0.0,
                )
                for rank, article in enumerate(rss_articles, start=1)
            )
        except Exception as exc:
            print(f"[WARN] RSS source {name} failed: {exc}")

    if gdelt_enabled and gdelt_max > 0:
        gdelt_batches = chunks(gdelt_terms, 6) or [[]]
        gdelt_max_per_batch = max(10, gdelt_max // max(1, len(gdelt_batches)))
        for gdelt_batch in gdelt_batches:
            if not gdelt_batch:
                continue
            gdelt_query = "(" + " OR ".join(gdelt_batch) + ")"
            try:
                gdelt_articles = fetch_with_report(
                    fetches,
                    source="GDELT",
                    source_type="gdelt",
                    fn=lambda gdelt_query=gdelt_query: fetch_gdelt_doc(query=gdelt_query, hours=hours, maxrecords=gdelt_max_per_batch),
                    terms=gdelt_batch,
                )
                gdelt_articles = [
                    article
                    for article in gdelt_articles
                    if article.lang is None or article.lang.lower().startswith(("en", "zh"))
                ]
                all_articles.extend(gdelt_articles)
            except Exception as exc:
                print(f"[WARN] GDELT batch failed: {exc}")
    else:
        fetches.append({"source": "GDELT", "type": "gdelt", "ok": False, "items": 0, "skipped": True, "error": "GDELT keyword path disabled in v2"})

    hn_hot_articles: List[Article] = []
    if hn_hot_enabled:
        for query_config in enabled_hn_hot_queries(conn):
            try:
                items = fetch_with_report(
                    fetches,
                    source=f"HN Hot:{query_config['name']}",
                    source_type="hn_hot",
                    fn=lambda query_config=query_config: fetch_hn_algolia_hot(
                        query=str(query_config["query"]),
                        min_points=int(query_config["min_points"]),
                        limit=int(query_config["item_limit"]),
                    ),
                    query_name=str(query_config["name"]),
                )
                stored_hot = upsert_articles(conn, items)
                replace_hn_hot_items(conn, str(query_config["name"]), stored_hot)
                hn_hot_articles.extend(stored_hot)
            except Exception as exc:
                print(f"[WARN] HN hot query {query_config.get('name')} failed: {exc}")
    else:
        fetches.append({"source": "HN Hot", "type": "hn_hot", "ok": False, "items": 0, "skipped": True, "error": "HN_HOT_ENABLED=false"})

    normalized = [
        dataclasses.replace(
            article,
            url=canonicalize_url(article.url),
            guid=article.guid or stable_guid(article.url, article.source),
        )
        for article in all_articles
    ]

    normalized = dedupe(normalized)
    normalized_count = len(normalized)
    stored_candidates = upsert_articles(conn, normalized)
    stored_by_guid: Dict[str, Article] = {}
    for article in stored_candidates:
        stored_by_guid[article.guid] = article
    stored_articles = list(stored_by_guid.values())
    classified = classify_hot_events(stored_candidates, hotness_config, adjusted_source_weights, exclude_terms)
    merged_count = len(classified)
    replace_classifications(conn, run_id, classified)
    replace_event_clusters(conn, run_id, classified)
    selected = select_hot_feed_articles(classified)
    news = selected["news"]
    radar = selected["radar"]
    replace_feed_items(conn, "news", [article.guid for article in news], run_id)
    replace_feed_items(conn, "radar", [article.guid for article in radar], run_id)
    hot_guids = hn_hot_feed_guids(conn, hn_hot_max_items)
    replace_feed_items(conn, "hn-hot", hot_guids, run_id, persistent=True)

    bucket_reasons = {
        str(row["article_guid"]): {"bucket": str(row["bucket"]), "reason": str(row["reason_code"])}
        for row in classified
    }
    debug_report = build_debug_report(
        stored_articles,
        bucket_reasons,
        keywords,
        topic_terms,
        exclude_terms,
        source_weights,
        adjusted_source_weights,
        scoring_config,
    )
    for row in classified:
        guid = str(row["article_guid"])
        for item in debug_report["items"]:
            if item["guid"] == guid:
                item["profile"] = row["profile"]
                item["reason_summary"] = row["reason_summary"]
                item["importance_score"] = row["importance_score"]
                item["confidence_score"] = row["confidence_score"]
                item["hotness"] = row.get("scores", {})
                item["cluster_id"] = (row.get("cluster") or {}).get("cluster_id")
                break
    item_details = {str(item["guid"]): item for item in debug_report["items"]}

    link_root = base_url or "https://example.com"
    news_items = feed_articles(conn, "news", news_max_items)
    radar_items = feed_articles(conn, "radar", radar_max_items)
    hn_hot_items = feed_articles(conn, "hn-hot", hn_hot_max_items)
    news_details = classification_details_for_feed(conn, "news") or item_details
    radar_details = classification_details_for_feed(conn, "radar") or item_details
    hn_hot_selection_by_guid = hn_hot_selection_details(conn)
    hn_hot_details = {
        article.guid: {
            "reason_summary": hn_hot_reason_summary(article, hn_hot_selection_by_guid.get(article.guid, [])),
            "bucket": "hn-hot",
            "reason": "hn_hot",
        }
        for article in hn_hot_items
    }
    news_rss = build_rss(
        channel_title="High Signal News",
        channel_link=f"{link_root}/",
        channel_desc="Major events and DevOps/security news, ranked for low-noise daily reading.",
        items=news_items,
        feedback_url=feedback_url,
        item_details=news_details,
    )
    (outdir / "news.xml").write_text(news_rss, encoding="utf-8")

    (outdir / "radar.xml").write_text(
        build_rss(
            channel_title="My Radar",
            channel_link=f"{link_root}/",
            channel_desc="Early signals & monitoring feed (more noisy; use as radar).",
            items=radar_items,
            feedback_url=feedback_url,
            item_details=radar_details,
        ),
        encoding="utf-8",
    )

    (outdir / "hn-hot.xml").write_text(
        build_rss(
            channel_title="HN Historical Hot List",
            channel_link=f"{link_root}/hn-hot.xml",
            channel_desc="Persistent Hacker News historical hot posts by configured topics.",
            items=hn_hot_items,
            feedback_url=feedback_url,
            item_details=hn_hot_details,
        ),
        encoding="utf-8",
    )

    published_guids = published_daily_guids(conn, primary_bucket)
    daily_articles = [article for article in news_items if article.guid not in published_guids][:daily_max_items]
    if daily_articles:
        archive_relpath = f"archive/{today.isoformat()}/{primary_bucket}.xml"
        archive_path = outdir / archive_relpath
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_text(
            build_rss(
                channel_title=f"High Signal Daily Snapshot ({today.isoformat()})",
                channel_link=f"{base_url.rstrip('/')}/{archive_relpath}",
                channel_desc=f"Daily archived snapshot for {primary_bucket} on {today.isoformat()}.",
                items=daily_articles,
                feedback_url=feedback_url,
                item_details=news_details,
            ),
            encoding="utf-8",
        )
        save_daily_issue(
            conn,
            issue_date=today,
            bucket=primary_bucket,
            archive_path=archive_relpath,
            link=f"{base_url.rstrip('/')}/{archive_relpath}",
            guid=stable_guid(f"{today.isoformat()}|{primary_bucket}", "daily"),
            articles=daily_articles,
        )

    daily_rss = build_daily_index_feed(base_url=base_url, bucket=primary_bucket, entries=daily_entries(conn, primary_bucket))
    (outdir / "daily.xml").write_text(daily_rss, encoding="utf-8")

    source_report = build_source_report(fetches, all_articles, normalized_count, merged_count, news, radar, feedback_preferences)
    (outdir / "source-report.json").write_text(
        json.dumps(source_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (outdir / "debug-report.json").write_text(
        json.dumps(debug_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (outdir / "index.html").write_text(build_index_html(), encoding="utf-8")
    finish_fetch_run(conn, run_id, "ok", source_report)

    print("[OK] Wrote:")
    print(f" - {outdir / 'news.xml'} ({len(news_items)} items)")
    print(f" - {outdir / 'radar.xml'} ({len(radar_items)} items)")
    print(f" - {outdir / 'hn-hot.xml'} ({len(hn_hot_items)} items)")
    print(f" - {outdir / 'daily.xml'}")
    print(f" - {outdir / 'source-report.json'}")
    print(f" - {outdir / 'debug-report.json'}")
    return 0
