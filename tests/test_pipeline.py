from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.db import (
    admin_rows,
    append_feedback_event,
    apply_rule_overlays,
    article_source_counts,
    connect_database,
    enabled_hn_hot_queries,
    hn_hot_feed_guids,
    hn_hot_selection_details,
    init_database,
    replace_classifications,
    replace_feed_items,
    replace_hn_hot_items,
    upsert_rule_term,
    upsert_articles,
)
from app.daily import build_daily_index_feed, ensure_daily_archive
from app.fetchers import fetch_hn_algolia_hot, hn_query_terms, fetch_newsapi_headlines, fetch_rss_feed
from app.feedback import apply_feedback_to_source_weights, build_feedback_preferences
from app.feedback_server import append_feedback
from app.helpers import canonicalize_url, now_utc
from app.helpers import hn_guid
from app.hotness import annotate_hot_signal, classify_hot_events, list_position_score, select_hot_feed_articles
from app.main import build_debug_report, build_source_report, hn_hot_reason_summary, remove_obsolete_outputs, site_root_from_base_url
from app.models import Article
from app.news_manager import classify_articles_v1, select_feed_articles, story_fingerprint
from app.pipeline import classify, classify_with_reason, dedupe_by_url, rank_articles, term_hit, term_match_score
from app.rss_writer import build_rss
from app.settings import DEFAULT_RULES, load_rules_config
from app.state_store import load_daily_index, load_published_guids, save_daily_index, save_published_guids


SCORING_CONFIG = {
    "weights": {
        "freshness": 0.35,
        "source_quality": 0.25,
        "topic_relevance": 0.25,
        "engagement": 0.15,
    },
    "freshness_half_life_hours": 18,
    "feed_min_topic_score": 0.18,
    "feed_min_interest_score": 0.12,
}

SOURCE_WEIGHTS = {
    "Hacker News": 0.85,
    "Reuters": 0.95,
    "NewsAPI": 0.65,
}

KEYWORDS = ["OpenAI", "semiconductor", "conflict", "cybersecurity"]
TOPIC_TERMS = ["openai", "ai", "semiconductor", "cybersecurity", "cloud", "conflict", "war", "sanction", "tariff"]
EXCLUDE_TERMS = ["sports"]

HOTNESS_CONFIG = {
    "newsapi_top_headlines_limit": 100,
    "newsapi_base_score_first": 30,
    "newsapi_base_score_last": 3,
    "hn_topstories_limit": 100,
    "hn_topstories_base_score_first": 30,
    "hn_topstories_base_score_last": 3,
    "hn_beststories_limit": 50,
    "hn_beststories_bonus_first": 12,
    "hn_beststories_bonus_last": 2,
    "hn_direct_points": 800,
    "hn_direct_comments": 300,
    "hn_direct_quality_score": 0.75,
    "match_bonus_newsapi_hn": 18,
    "match_bonus_rss_hn": 10,
    "security_match_bonus": 12,
    "news_min_score": 38,
    "news_min_confidence": 0.65,
    "radar_min_score": 20,
    "radar_max_items": 2,
    "match_time_window_hours": 48,
}


class PipelineTests(unittest.TestCase):
    def test_hotness_rank_score_decreases_from_first_to_last(self) -> None:
        self.assertEqual(list_position_score(1, 100, 30, 3), 30)
        self.assertEqual(list_position_score(100, 100, 30, 3), 3)
        self.assertGreater(list_position_score(5, 100, 30, 3), list_position_score(50, 100, 30, 3))

    def test_newsapi_top_five_no_longer_enters_news_directly(self) -> None:
        article = annotate_hot_signal(
            Article(
                title="Major cloud provider outage affects customers",
                url="https://example.com/outage",
                source="NewsAPI Source",
                published_at=now_utc(),
                source_type="newsapi",
            ),
            source_family_name="newsapi",
            source_list="top-headlines",
            source_rank=4,
            hot_signal_type="newsapi_top_headlines",
            base_score=list_position_score(4, 100, 30, 3),
        )

        rows = classify_hot_events([article], HOTNESS_CONFIG, SOURCE_WEIGHTS, [])
        self.assertEqual(rows[0]["bucket"], "radar")
        self.assertEqual(rows[0]["reason_code"], "radar_threshold")
        self.assertGreater(rows[0]["scores"]["hotness_score"], 0)

    def test_hn_high_quality_enters_news_directly(self) -> None:
        article = annotate_hot_signal(
            Article(
                title="Show HN: A useful database debugger",
                url="https://example.com/hn",
                source="Hacker News",
                published_at=now_utc(),
                source_type="hn",
                score=850,
                comments=120,
            ),
            source_family_name="hn",
            source_list="topstories",
            source_rank=20,
            hot_signal_type="hn_topstories",
            base_score=list_position_score(20, 100, 30, 3),
        )

        rows = classify_hot_events([article], HOTNESS_CONFIG, SOURCE_WEIGHTS, [])
        self.assertEqual(rows[0]["bucket"], "news")
        self.assertEqual(rows[0]["reason_code"], "hn_direct_quality")

    def test_cross_source_match_adds_bonus(self) -> None:
        published = now_utc()
        newsapi = annotate_hot_signal(
            Article(
                title="OpenAI announces new model for developers",
                url="https://news.example.com/openai-model",
                source="NewsAPI Source",
                published_at=published,
                source_type="newsapi",
            ),
            source_family_name="newsapi",
            source_list="top-headlines",
            source_rank=20,
            hot_signal_type="newsapi_top_headlines",
            base_score=20,
        )
        hn = annotate_hot_signal(
            Article(
                title="OpenAI announces new model for developers",
                url="https://news.ycombinator.com/item?id=1",
                source="Hacker News",
                published_at=published,
                source_type="hn",
                score=120,
                comments=30,
            ),
            source_family_name="hn",
            source_list="topstories",
            source_rank=15,
            hot_signal_type="hn_topstories",
            base_score=22,
        )

        rows = classify_hot_events([newsapi, hn], HOTNESS_CONFIG, SOURCE_WEIGHTS, [])
        self.assertEqual(rows[0]["bucket"], "news")
        self.assertIn("newsapi_hn_match", rows[0]["scores"]["bonus_parts"])

    def test_other_sources_only_cross_validate_against_hn_topstories(self) -> None:
        published = now_utc()
        rss = annotate_hot_signal(
            Article(
                title="OpenAI files confidential S-1 with the SEC",
                url="https://openai.com/s1",
                source="OpenAI News",
                published_at=published,
                source_type="rss",
            ),
            source_family_name="rss",
            source_list="OpenAI News",
            source_rank=1,
            hot_signal_type="rss_verification",
            base_score=20,
        )
        hn_best = annotate_hot_signal(
            Article(
                title="OpenAI files confidential S-1 with the SEC",
                url="https://news.ycombinator.com/item?id=123",
                source="Hacker News",
                published_at=published,
                source_type="hn",
                score=100,
                comments=10,
            ),
            source_family_name="hn",
            source_list="beststories",
            source_rank=1,
            hot_signal_type="hn_beststories",
            bonus_score=12,
        )

        rows = classify_hot_events([rss, hn_best], HOTNESS_CONFIG, SOURCE_WEIGHTS, [])
        self.assertEqual(rows[0]["scores"]["bonus_parts"], {"hn_beststories": 12.0})
        self.assertNotIn("rss_hn_match", rows[0]["scores"]["bonus_parts"])

    def test_radar_is_limited_but_news_is_not(self) -> None:
        articles = []
        titles = [
            "Database startup raises funding after launch",
            "Chip factory delays expansion in Arizona",
            "Cloud vendor previews storage migration tool",
            "Security team publishes incident review",
        ]
        for index, title in enumerate(titles):
            articles.append(
                annotate_hot_signal(
                    Article(
                        title=title,
                        url=f"https://example.com/radar-{index}",
                        source="NewsAPI Source",
                        published_at=now_utc(),
                        source_type="newsapi",
                    ),
                    source_family_name="newsapi",
                    source_list="top-headlines",
                    source_rank=30 + index,
                    hot_signal_type="newsapi_top_headlines",
                    base_score=22 - index,
                )
            )

        rows = classify_hot_events(articles, HOTNESS_CONFIG, SOURCE_WEIGHTS, [])
        selected = select_hot_feed_articles(rows)
        self.assertEqual(len(selected["radar"]), 2)

    def test_canonicalize_url_removes_query_fragment_and_trailing_slash(self) -> None:
        url = "http://example.com/path/?utm_source=x&id=1#section"
        self.assertEqual(canonicalize_url(url), "https://example.com/path")

    def test_canonicalize_url_preserves_hacker_news_item_id(self) -> None:
        first = canonicalize_url("https://news.ycombinator.com/item?id=21924298&utm_source=x")
        second = canonicalize_url("https://news.ycombinator.com/item?id=48449187")

        self.assertEqual(first, "https://news.ycombinator.com/item?id=21924298")
        self.assertEqual(second, "https://news.ycombinator.com/item?id=48449187")
        self.assertNotEqual(first, second)

    def test_hn_guid_is_based_on_hn_id_not_source_or_queryless_url(self) -> None:
        self.assertEqual(hn_guid(21924298), hn_guid("21924298"))
        self.assertNotEqual(hn_guid(21924298), hn_guid(48449187))

    def test_hn_articles_with_different_ids_do_not_merge_in_database(self) -> None:
        with TemporaryDirectory() as tmpdir:
            conn = connect_database(Path(tmpdir) / "rssnews.db")
            init_database(conn)
            upsert_articles(
                conn,
                [
                    Article(
                        title="Ask HN: Full-on machine learning for 2020",
                        url="https://news.ycombinator.com/item?id=21924298",
                        source="HN Hot",
                        published_at=now_utc(),
                        source_type="hn_hot",
                        hn_id=21924298,
                        guid=hn_guid(21924298),
                    ),
                    Article(
                        title="Ask HN: What are tools you have made for yourself since the advent of AI?",
                        url="https://news.ycombinator.com/item?id=48449187",
                        source="Hacker News",
                        published_at=now_utc(),
                        source_type="hn",
                        hn_id=48449187,
                        guid=hn_guid(48449187),
                    ),
                ],
            )

            count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            urls = {row[0] for row in conn.execute("SELECT canonical_url FROM articles").fetchall()}
            self.assertEqual(count, 2)
            self.assertEqual(
                urls,
                {
                    "https://news.ycombinator.com/item?id=21924298",
                    "https://news.ycombinator.com/item?id=48449187",
                },
            )

    def test_reposted_external_url_with_different_hn_ids_merges_without_crashing(self) -> None:
        with TemporaryDirectory() as tmpdir:
            conn = connect_database(Path(tmpdir) / "rssnews.db")
            init_database(conn)
            stored = upsert_articles(
                conn,
                [
                    Article(
                        title="Building Zork Part 2",
                        url="https://www.dpolakovic.space/blogs/zork-part2",
                        source="Hacker News",
                        published_at=now_utc() - timedelta(days=10),
                        source_type="hn",
                        hn_id=1001,
                        score=120,
                        comments=40,
                        guid=hn_guid(1001),
                    ),
                    Article(
                        title="Building Zork Part 2",
                        url="https://www.dpolakovic.space/blogs/zork-part2",
                        source="Hacker News",
                        published_at=now_utc(),
                        source_type="hn",
                        hn_id=2002,
                        score=240,
                        comments=80,
                        guid=hn_guid(2002),
                    ),
                ],
            )

            count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            row = conn.execute("SELECT hn_id, hn_points, hn_comments FROM articles").fetchone()
            source_ids = {row[0] for row in conn.execute("SELECT external_id FROM article_sources").fetchall()}
            self.assertEqual(count, 1)
            self.assertEqual(stored[0].guid, stored[1].guid)
            self.assertEqual(row["hn_id"], 2002)
            self.assertEqual(row["hn_points"], 240)
            self.assertEqual(row["hn_comments"], 80)
            self.assertEqual(source_ids, {"1001", "2002"})

    def test_dedupe_by_url_keeps_more_informative_article(self) -> None:
        older = Article(
            title="A",
            url="https://example.com/story?a=1",
            source="NewsAPI",
            published_at=now_utc(),
            summary="short",
            score=0.0,
        )
        better = Article(
            title="A",
            url="https://example.com/story?b=2",
            source="Hacker News",
            published_at=now_utc(),
            summary="longer summary",
            score=12.0,
        )

        deduped = dedupe_by_url([older, better])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].source, "Hacker News")

    def test_rank_articles_prefers_fresh_relevant_story(self) -> None:
        stale = Article(
            title="Generic market update",
            url="https://example.com/old",
            source="Reuters",
            published_at=now_utc() - timedelta(hours=72),
            summary="No special topic match.",
        )
        fresh = Article(
            title="OpenAI announces new semiconductor effort",
            url="https://example.com/new",
            source="NewsAPI",
            published_at=now_utc() - timedelta(hours=1),
            summary="AI and chip news.",
        )

        ranked = rank_articles([stale, fresh], KEYWORDS, SOURCE_WEIGHTS, SCORING_CONFIG)
        self.assertEqual(ranked[0].url, "https://example.com/new")

    def test_feedback_preferences_adjust_source_and_domain_weights(self) -> None:
        preferences = build_feedback_preferences(
            [
                {"action": "keep", "source": "Hacker News", "url": "https://example.com/a?utm=x"},
                {"action": "mute-domain", "source": "Hacker News", "url": "https://noisy.example/post"},
                {"action": "mute", "source": "Reuters", "url": "https://wire.example/story"},
            ]
        )
        adjusted = apply_feedback_to_source_weights(SOURCE_WEIGHTS, preferences)

        self.assertGreater(adjusted["Hacker News"], SOURCE_WEIGHTS["Hacker News"])
        self.assertLess(adjusted["Reuters"], SOURCE_WEIGHTS["Reuters"])
        self.assertEqual(adjusted["domain:noisy.example"], -0.2)
        self.assertEqual(preferences["event_count"], 3)

    def test_domain_feedback_can_lower_article_rank(self) -> None:
        muted = Article(
            title="OpenAI security update",
            url="https://noisy.example/story",
            source="Reuters",
            published_at=now_utc(),
            summary="OpenAI cybersecurity signal.",
        )
        neutral = Article(
            title="OpenAI security update",
            url="https://trusted.example/story",
            source="Reuters",
            published_at=now_utc(),
            summary="OpenAI cybersecurity signal.",
        )

        weights = dict(SOURCE_WEIGHTS)
        weights["domain:noisy.example"] = -0.35
        ranked = rank_articles([muted, neutral], KEYWORDS, weights, SCORING_CONFIG)
        self.assertEqual(ranked[0].url, "https://trusted.example/story")

    def test_classify_routes_low_confidence_story_to_radar(self) -> None:
        article = Article(
            title="Local feature update",
            url="https://example.com/local",
            source="Reuters",
            published_at=now_utc(),
            summary="A generic story with no configured topic match.",
        )

        bucket = classify(article, KEYWORDS, TOPIC_TERMS, EXCLUDE_TERMS, SCORING_CONFIG)
        self.assertEqual(bucket, "radar")

    def test_classify_routes_relevant_story_to_unified_news_feed(self) -> None:
        article = Article(
            title="New tariff conflict impacts regional trade",
            url="https://example.com/world",
            source="Reuters",
            published_at=now_utc(),
            summary="Sanction pressure continues.",
        )

        bucket = classify(article, KEYWORDS, TOPIC_TERMS, EXCLUDE_TERMS, SCORING_CONFIG)
        self.assertEqual(bucket, "news")

    def test_classify_routes_excluded_story_to_radar(self) -> None:
        article = Article(
            title="Sports celebrity invests in AI startup",
            url="https://example.com/sports",
            source="Reuters",
            published_at=now_utc(),
            summary="OpenAI and semiconductor terms appear, but the configured exclusion wins.",
        )

        bucket = classify(article, KEYWORDS, TOPIC_TERMS, EXCLUDE_TERMS, SCORING_CONFIG)
        self.assertEqual(bucket, "radar")

    def test_classify_with_reason_explains_radar_bucket(self) -> None:
        article = Article(
            title="Sports celebrity invests in AI startup",
            url="https://example.com/sports",
            source="Reuters",
            published_at=now_utc(),
            summary="OpenAI and cybersecurity terms appear, but sports is excluded.",
        )

        bucket, reason = classify_with_reason(article, KEYWORDS, TOPIC_TERMS, EXCLUDE_TERMS, SCORING_CONFIG)
        self.assertEqual(bucket, "radar")
        self.assertEqual(reason, "excluded_term")

    def test_classify_keeps_radar_articles_out_of_news(self) -> None:
        article = Article(
            title="OpenAI reports a cloud outage",
            url="https://example.com/radar",
            source="GDELT:example.com",
            published_at=now_utc(),
            summary="AI and cloud outage terms match configured topics.",
            kind="radar",
        )

        bucket = classify(article, KEYWORDS, TOPIC_TERMS, EXCLUDE_TERMS, SCORING_CONFIG)
        self.assertEqual(bucket, "radar")

    def test_daily_archive_is_created_once_per_day(self) -> None:
        article = Article(
            title="OpenAI ships new inference stack",
            url="https://example.com/openai",
            source="Hacker News",
            published_at=now_utc(),
            summary="A useful summary.",
            guid="guid-openai",
        )

        with TemporaryDirectory() as tmpdir:
            outdir = Path(tmpdir) / "dist"
            state_dir = Path(tmpdir) / "state"
            issue_date = now_utc().date()

            daily_index, published_guids = ensure_daily_archive(
                outdir=outdir,
                base_url="https://example.com/rss",
                issue_date=issue_date,
                bucket="news",
                articles=[article],
                max_items=10,
                published_guids=set(),
                daily_index=[],
            )
            daily_index, published_guids = ensure_daily_archive(
                outdir=outdir,
                base_url="https://example.com/rss",
                issue_date=issue_date,
                bucket="news",
                articles=[article],
                max_items=10,
                published_guids=published_guids,
                daily_index=daily_index,
            )

            self.assertEqual(len(daily_index), 1)
            self.assertIn(article.guid, published_guids)
            self.assertTrue((outdir / f"archive/{issue_date.isoformat()}/news.xml").exists())

    def test_daily_archive_allows_zero_selected_articles(self) -> None:
        with TemporaryDirectory() as tmpdir:
            outdir = Path(tmpdir) / "dist"
            issue_date = now_utc().date()

            daily_index, published_guids = ensure_daily_archive(
                outdir=outdir,
                base_url="https://example.com/rss",
                issue_date=issue_date,
                bucket="news",
                articles=[],
                max_items=10,
                published_guids=set(),
                daily_index=[],
            )

            self.assertEqual(daily_index, [])
            self.assertEqual(published_guids, set())
            self.assertFalse((outdir / f"archive/{issue_date.isoformat()}/news.xml").exists())

    def test_daily_archive_skips_already_published_guids_on_later_days(self) -> None:
        article = Article(
            title="OpenAI ships new inference stack",
            url="https://example.com/openai",
            source="Hacker News",
            published_at=now_utc(),
            summary="A useful summary.",
            guid="guid-openai",
        )
        newer = Article(
            title="New tariff conflict impacts regional trade",
            url="https://example.com/world",
            source="Reuters",
            published_at=now_utc(),
            summary="World summary.",
            guid="guid-world",
        )

        with TemporaryDirectory() as tmpdir:
            outdir = Path(tmpdir) / "dist"
            first_day = now_utc().date()
            second_day = first_day + timedelta(days=1)

            daily_index, published_guids = ensure_daily_archive(
                outdir=outdir,
                base_url="https://example.com/rss",
                issue_date=first_day,
                bucket="news",
                articles=[article],
                max_items=10,
                published_guids=set(),
                daily_index=[],
            )
            daily_index, published_guids = ensure_daily_archive(
                outdir=outdir,
                base_url="https://example.com/rss",
                issue_date=second_day,
                bucket="news",
                articles=[article, newer],
                max_items=10,
                published_guids=published_guids,
                daily_index=daily_index,
            )

            self.assertEqual(len(daily_index), 2)
            second_entry = daily_index[1]
            self.assertEqual(second_entry["item_count"], 1)
            self.assertEqual(second_entry["top_titles"], [newer.title])

    def test_state_store_round_trip(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "state"
            save_published_guids(state_dir, {"a", "b"})
            save_daily_index(state_dir, [{"bucket": "news", "date": "2026-04-26"}])

            self.assertEqual(load_published_guids(state_dir), {"a", "b"})
            self.assertEqual(load_daily_index(state_dir), [{"bucket": "news", "date": "2026-04-26"}])

    def test_daily_index_feed_contains_archive_link(self) -> None:
        rss = build_daily_index_feed(
            base_url="https://example.com/rss",
            bucket="news",
            entries=[
                {
                    "bucket": "news",
                    "date": "2026-04-26",
                    "archive_path": "archive/2026-04-26/news.xml",
                    "link": "https://example.com/rss/archive/2026-04-26/news.xml",
                    "guid": "daily-guid",
                    "item_count": 3,
                    "top_titles": ["A", "B"],
                }
            ],
        )

        self.assertIn("daily.xml", rss)
        self.assertIn("archive/2026-04-26/news.xml", rss)

    def test_default_rules_do_not_embed_business_keywords(self) -> None:
        self.assertEqual(DEFAULT_RULES["interest_keywords"], [])
        self.assertEqual(DEFAULT_RULES["feeds"]["topic_terms"], [])
        self.assertEqual(DEFAULT_RULES["feeds"]["exclude_terms"], [])
        self.assertEqual(DEFAULT_RULES["queries"]["gdelt_terms"], [])
        self.assertEqual(DEFAULT_RULES["queries"]["newsapi_terms"], [])
        self.assertEqual(DEFAULT_RULES["sources"]["rss_sources"], [])

    def test_rules_file_is_the_business_keyword_source(self) -> None:
        rules = load_rules_config("config/feed_rules.json")
        self.assertIn("OpenAI", rules["interest_keywords"])
        self.assertIn("OpenAI", rules["feeds"]["topic_terms"])
        self.assertIn("sports", rules["feeds"]["exclude_terms"])
        self.assertIn("ransomware", rules["queries"]["gdelt_terms"])
        self.assertIn("OpenAI", rules["queries"]["newsapi_terms"])
        source_names = {source["name"] for source in rules["sources"]["rss_sources"]}
        self.assertIn("Harvard Business Review", source_names)
        self.assertIn("CISA Cybersecurity Advisories", source_names)
        self.assertIn("OpenAI News", source_names)

    def test_fetch_rss_feed_parses_standard_rss_items(self) -> None:
        rss = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Leadership lessons for AI founders</title>
      <link>https://hbr.org/example?utm_source=x</link>
      <pubDate>Wed, 06 May 2026 10:00:00 GMT</pubDate>
      <description>Useful management advice.</description>
    </item>
  </channel>
</rss>
"""
        with patch("app.fetchers.http_get_text", return_value=rss):
            articles = fetch_rss_feed("Harvard Business Review", "https://example.com/feed.xml", kind="radar")

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].source, "Harvard Business Review")
        self.assertEqual(articles[0].url, "https://hbr.org/example")
        self.assertIn("AI founders", articles[0].title)
        self.assertEqual(articles[0].kind, "radar")

    def test_newsapi_headline_fallback_can_be_marked_as_radar(self) -> None:
        payload = {
            "articles": [
                {
                    "title": "AI market update",
                    "url": "https://example.com/newsapi",
                    "publishedAt": "2026-05-06T10:00:00Z",
                    "description": "Generic headline fallback item.",
                    "source": {"name": "NewsAPI Source"},
                }
            ]
        }
        with patch("app.fetchers.http_get_json", return_value=payload):
            articles = fetch_newsapi_headlines("key", "en", page_size=1, kind="radar")

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].kind, "radar")

    def test_source_report_counts_raw_and_bucketed_sources(self) -> None:
        raw = [
            Article(title="A", url="https://example.com/a", source="Hacker News", published_at=now_utc()),
            Article(title="B", url="https://example.com/b", source="GDELT:example.com", published_at=now_utc()),
            Article(title="C", url="https://example.com/c", source="Harvard Business Review", published_at=now_utc()),
        ]
        report = build_source_report(
            fetches=[{"source": "Hacker News", "type": "hn", "ok": True, "items": 1}],
            raw_articles=raw,
            normalized_count=3,
            merged_count=2,
            news=[raw[0]],
            radar=[raw[1], raw[2]],
        )

        self.assertEqual(report["summary"]["raw_candidates"], 3)
        self.assertEqual(report["summary"]["after_cross_source_merge"], 2)
        self.assertEqual(report["raw_by_source"]["GDELT"], 1)
        self.assertEqual(report["news_by_source"]["Hacker News"], 1)
        self.assertEqual(report["radar_by_source"]["Harvard Business Review"], 1)

    def test_debug_report_explains_article_scores_and_matches(self) -> None:
        article = Article(
            title="OpenAI security update",
            url="https://noisy.example/story",
            source="Reuters",
            published_at=now_utc(),
            summary="OpenAI cybersecurity signal.",
            guid="guid-debug",
            priority=0.7,
        )
        weights = dict(SOURCE_WEIGHTS)
        weights["domain:noisy.example"] = -0.2
        report = build_debug_report(
            articles=[article],
            bucket_reasons={"guid-debug": {"bucket": "news", "reason": "topic_match"}},
            keywords=KEYWORDS,
            topic_terms=TOPIC_TERMS,
            exclude_terms=EXCLUDE_TERMS,
            base_source_weights=SOURCE_WEIGHTS,
            adjusted_source_weights=weights,
            scoring_config=SCORING_CONFIG,
        )

        self.assertEqual(report["summary"]["news_items"], 1)
        item = report["items"][0]
        self.assertEqual(item["bucket"], "news")
        self.assertEqual(item["reason"], "topic_match")
        self.assertEqual(item["domain"], "noisy.example")
        self.assertIn("OpenAI", item["matches"]["interest_keywords"])
        self.assertIn("cybersecurity", item["matches"]["topic_terms"])
        self.assertEqual(item["weights"]["domain_feedback_delta"], -0.2)
        self.assertIn("weighted", item["scores"])

    def test_rss_items_include_feedback_links(self) -> None:
        article = Article(
            title="OpenAI ships new inference stack",
            url="https://example.com/openai",
            source="Hacker News",
            published_at=now_utc(),
            summary="A useful summary.",
            guid="guid-openai",
        )

        rss = build_rss("Title", "https://reader.example/rss", "Desc", [article], feedback_url="https://reader.example/feedback")

        self.assertIn("https://reader.example/feedback?action=keep", rss)
        self.assertIn("action=mute", rss)
        self.assertIn("action=mute-domain", rss)
        self.assertIn("guid=guid-openai", rss)

    def test_rss_items_include_compact_selection_explanation(self) -> None:
        article = Article(
            title="OpenAI ships new inference stack",
            url="https://example.com/openai",
            source="Hacker News",
            published_at=now_utc(),
            summary="A useful summary.",
            guid="guid-openai",
            priority=0.7,
        )
        details = {
            "guid-openai": {
                "bucket": "news",
                "reason": "topic_match",
                "matches": {
                    "interest_keywords": ["OpenAI"],
                    "topic_terms": ["AI"],
                    "exclude_terms": [],
                },
                "scores": {
                    "weighted": {
                        "freshness": 0.1,
                        "source_quality": 0.2,
                        "topic_relevance": 0.3,
                        "engagement": 0.0,
                    }
                },
                "weights": {"domain_feedback_delta": -0.2},
            }
        }

        rss = build_rss(
            "Title",
            "https://reader.example/rss",
            "Desc",
            [article],
            feedback_url="https://reader.example/feedback",
            item_details=details,
        )

        self.assertIn("Why selected", rss)
        self.assertIn("matched configured topic", rss)
        self.assertIn("Matched:</b> OpenAI, AI", rss)
        self.assertIn("domain weight -0.20", rss)

    def test_rss_items_include_hotness_scores_and_hn_discussion_link(self) -> None:
        article = Article(
            title="Ask HN: useful tool",
            url="https://news.ycombinator.com/item?id=123",
            source="Hacker News",
            published_at=now_utc(),
            summary="HN score 900 · comments 320",
            guid=hn_guid(123),
            hn_id=123,
            score=900,
            comments=320,
            priority=42,
        )
        details = {
            article.guid: {
                "bucket": "news",
                "reason": "hn_direct_quality",
                "reason_summary": "HN 热度/优质度直通。",
                "scores": {
                    "weighted": {
                        "hotness_score": 42,
                        "base_score": 30,
                        "bonus_score": 12,
                        "noise_penalty": 0,
                        "confidence_score": 0.8,
                        "bonus_parts": {"hn_beststories": 12},
                        "direct_reasons": ["hn_direct_quality"],
                    }
                },
            }
        }

        rss = build_rss("Title", "https://reader.example/rss", "Desc", [article], item_details=details)

        self.assertIn("HN discussion:", rss)
        self.assertIn("https://news.ycombinator.com/item?id=123", rss)
        self.assertIn("hotness 42.00 = base 30.00 + bonus 12.00 - noise 0.00", rss)
        self.assertIn("threshold: news: direct or hotness &gt;= 38 and confidence &gt;= 0.65", rss)

    def test_append_feedback_persists_events(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "state"
            append_feedback(state_dir, {"action": "keep", "guid": "a"})
            append_feedback(state_dir, {"action": "mute", "guid": "b"})

            payload = load_daily_index(state_dir)
            self.assertEqual(payload, [])
            raw = (state_dir / "feedback.json").read_text(encoding="utf-8")
            self.assertIn('"guid": "a"', raw)
            self.assertIn('"action": "mute"', raw)

    def test_site_root_from_base_url_removes_rss_path(self) -> None:
        self.assertEqual(site_root_from_base_url("http://example.com/rss"), "http://example.com")
        self.assertEqual(site_root_from_base_url("http://example.com/rss/"), "http://example.com")

    def test_remove_obsolete_outputs_keeps_current_feeds(self) -> None:
        with TemporaryDirectory() as tmpdir:
            outdir = Path(tmpdir)
            for filename in ("tech.xml", "world.xml", "daily-tech.xml", "daily-world.xml", "news.xml"):
                (outdir / filename).write_text("x", encoding="utf-8")

            remove_obsolete_outputs(outdir)

            self.assertFalse((outdir / "tech.xml").exists())
            self.assertFalse((outdir / "world.xml").exists())
            self.assertFalse((outdir / "daily-tech.xml").exists())
            self.assertFalse((outdir / "daily-world.xml").exists())
            self.assertTrue((outdir / "news.xml").exists())

    def test_database_init_and_feedback_event_round_trip(self) -> None:
        with TemporaryDirectory() as tmpdir:
            conn = connect_database(Path(tmpdir) / "rssnews.db")
            init_database(
                conn,
                Path(tmpdir),
                hn_hot_queries=[
                    {
                        "name": "ai-learning",
                        "query": "AI learning",
                        "min_points": 100,
                        "item_limit": 20,
                        "enabled": True,
                    }
                ],
            )
            article = Article(
                title="Critical Kubernetes CVE exploited",
                url="https://example.com/cve",
                source="CISA KEV",
                source_type="cisa_kev",
                published_at=now_utc(),
                summary="Remote code execution is actively exploited.",
                guid="guid-cve",
            )
            upsert_articles(conn, [article])
            append_feedback_event(conn, {"action": "save", "guid": "guid-cve", "created_at": now_utc().isoformat()})

            saved = admin_rows(conn, "saved_articles")
            self.assertEqual(saved[0]["article_guid"], "guid-cve")

    def test_database_init_migrates_existing_v1_tables_before_v2_indexes(self) -> None:
        with TemporaryDirectory() as tmpdir:
            conn = connect_database(Path(tmpdir) / "rssnews.db")
            conn.execute(
                """
                CREATE TABLE articles (
                  guid TEXT PRIMARY KEY,
                  canonical_url TEXT NOT NULL UNIQUE,
                  title TEXT NOT NULL,
                  url TEXT NOT NULL,
                  domain TEXT NOT NULL DEFAULT '',
                  source_name TEXT NOT NULL,
                  source_type TEXT NOT NULL DEFAULT '',
                  published_at TEXT NOT NULL,
                  discovered_at TEXT NOT NULL,
                  summary TEXT NOT NULL DEFAULT '',
                  content_text TEXT NOT NULL DEFAULT '',
                  lang TEXT,
                  hn_id INTEGER,
                  hn_points REAL NOT NULL DEFAULT 0,
                  hn_comments INTEGER NOT NULL DEFAULT 0,
                  raw_json TEXT NOT NULL DEFAULT '{}',
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE hn_hot_items (
                  query_name TEXT NOT NULL,
                  article_guid TEXT NOT NULL,
                  rank INTEGER NOT NULL DEFAULT 0,
                  points REAL NOT NULL DEFAULT 0,
                  comments INTEGER NOT NULL DEFAULT 0,
                  fetched_at TEXT NOT NULL,
                  persistent INTEGER NOT NULL DEFAULT 1,
                  PRIMARY KEY (query_name, article_guid)
                )
                """
            )

            init_database(conn, Path(tmpdir))

            article_cols = {row["name"] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
            hn_hot_cols = {row["name"] for row in conn.execute("PRAGMA table_info(hn_hot_items)").fetchall()}
            indexes = {row["name"] for row in conn.execute("PRAGMA index_list(articles)").fetchall()}
            self.assertIn("source_family", article_cols)
            self.assertIn("source_rank", article_cols)
            self.assertIn("matched_terms_json", hn_hot_cols)
            self.assertIn("idx_articles_source_rank", indexes)

    def test_database_migrates_legacy_feedback_json_idempotently(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            (state_dir / "feedback.json").write_text(
                json.dumps([{"action": "keep", "guid": "legacy-guid", "source": "HN", "url": "https://example.com", "created_at": "2026-01-01T00:00:00+00:00"}]),
                encoding="utf-8",
            )
            conn = connect_database(state_dir / "rssnews.db")
            init_database(conn, state_dir)
            init_database(conn, state_dir)

            rows = admin_rows(conn, "feedback_events")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["article_guid"], "legacy-guid")

    def test_database_ingest_merges_sources_by_canonical_url(self) -> None:
        with TemporaryDirectory() as tmpdir:
            conn = connect_database(Path(tmpdir) / "rssnews.db")
            init_database(conn, Path(tmpdir))
            first = Article(title="A", url="https://example.com/story?utm=x", source="Reuters", published_at=now_utc(), guid="guid-a")
            second = Article(title="A longer title", url="https://example.com/story", source="GDELT:example.com", published_at=now_utc(), guid="guid-b")

            stored = upsert_articles(conn, [first, second])
            counts = article_source_counts(conn)

            self.assertEqual(stored[0].guid, stored[1].guid)
            self.assertEqual(counts[stored[0].guid], 2)

    def test_database_rule_overlay_extends_runtime_rules(self) -> None:
        with TemporaryDirectory() as tmpdir:
            conn = connect_database(Path(tmpdir) / "rssnews.db")
            init_database(conn, Path(tmpdir))
            rules = {
                "feeds": {"exclude_terms": [], "watched_entities": []},
                "topic_profiles": {"tech_industry_major": {"must_track": [], "watch": []}},
                "queries": {"newsapi_terms": [], "gdelt_terms": []},
            }

            upsert_rule_term(conn, "tech_industry_major", "must_track", "Anthropic")
            upsert_rule_term(conn, "feeds", "watched_entity", "GitHub")
            upsert_rule_term(conn, "feeds", "exclude", "NFL")
            merged = apply_rule_overlays(conn, rules)

            self.assertIn("Anthropic", merged["topic_profiles"]["tech_industry_major"]["must_track"])
            self.assertIn("GitHub", merged["feeds"]["watched_entities"])
            self.assertIn("NFL", merged["feeds"]["exclude_terms"])

    def test_v1_classification_promotes_major_news_without_personal_keyword(self) -> None:
        rules = {
            "interest_keywords": ["OpenAI"],
            "feeds": {"exclude_terms": [], "active_profiles": ["major_events", "devops_security", "personal_interest"], "news_profiles": ["major_events", "devops_security"]},
            "topic_profiles": {
                "major_news": {"must_track": ["war", "sanction"], "watch": ["central bank"]},
                "devops_security": {"must_track": ["CVE", "RCE"], "watch": ["Kubernetes"]},
            },
        }
        article = Article(
            title="Central bank emergency decision shakes markets",
            url="https://reuters.com/story",
            source="Reuters",
            published_at=now_utc(),
            summary="No personal keyword appears here.",
            guid="guid-major",
        )
        ranked = rank_articles([article], ["war", "central bank"], {"Reuters": 0.95}, SCORING_CONFIG)
        rows = classify_articles_v1(ranked, rules, {"Reuters": 0.95}, SCORING_CONFIG, {"guid-major": 1})

        self.assertEqual(rows[0]["profile"], "major_events")
        self.assertEqual(rows[0]["bucket"], "news")
        self.assertIn("重大新闻", rows[0]["reason_summary"])

    def test_v1_classification_promotes_devops_security(self) -> None:
        rules = {
            "interest_keywords": [],
            "feeds": {"exclude_terms": [], "active_profiles": ["major_events", "devops_security", "personal_interest"], "news_profiles": ["major_events", "devops_security"]},
            "topic_profiles": {
                "major_news": {"must_track": ["war"], "watch": []},
                "devops_security": {"must_track": ["CVE", "remote code execution"], "watch": ["Kubernetes"]},
            },
        }
        article = Article(
            title="CVE-2026-0001 Kubernetes remote code execution exploited",
            url="https://cisa.gov/cve",
            source="CISA KEV",
            source_type="cisa_kev",
            published_at=now_utc(),
            summary="Critical vulnerability is actively exploited.",
            guid="guid-sec",
        )
        ranked = rank_articles([article], ["CVE", "remote code execution"], {"CISA KEV": 1.0}, SCORING_CONFIG)
        rows = classify_articles_v1(ranked, rules, {"CISA KEV": 1.0}, SCORING_CONFIG, {"guid-sec": 1})

        self.assertEqual(rows[0]["profile"], "devops_security")
        self.assertEqual(rows[0]["bucket"], "news")
        self.assertIn("DevOps 安全", rows[0]["reason_summary"])

    def test_v1_classification_promotes_devops_platform_compromise(self) -> None:
        rules = {
            "interest_keywords": [],
            "feeds": {"exclude_terms": [], "active_profiles": ["major_events", "devops_security", "personal_interest"], "news_profiles": ["major_events", "devops_security"]},
            "topic_profiles": {
                "major_news": {"must_track": ["war"], "watch": []},
                "devops_security": {
                    "must_track": ["GitHub compromised", "packages compromised", "npm packages compromised"],
                    "watch": ["GitHub Actions"],
                },
            },
        }
        articles = [
            Article(
                title="GitHub Compromised",
                url="https://news.ycombinator.com/item?id=1",
                source="Hacker News",
                source_type="hn",
                published_at=now_utc(),
                summary="HN score 95 · comments 26",
                score=95,
                comments=26,
                guid="guid-github-compromised",
            ),
            Article(
                title="Mini Shai-Hulud Strikes Again: 314 npm Packages Compromised",
                url="https://safedep.io/mini-shai-hulud",
                source="Hacker News",
                source_type="hn",
                published_at=now_utc(),
                summary="HN score 369 · comments 280",
                score=369,
                comments=280,
                guid="guid-npm-compromised",
            ),
        ]
        ranked = rank_articles(
            articles,
            ["GitHub compromised", "packages compromised", "npm packages compromised"],
            {"Hacker News": 0.85},
            SCORING_CONFIG,
        )
        rows = classify_articles_v1(ranked, rules, {"Hacker News": 0.85}, SCORING_CONFIG, {article.guid: 1 for article in articles})

        by_guid = {str(row["article_guid"]): row for row in rows}

        self.assertEqual(by_guid["guid-github-compromised"]["profile"], "devops_security")
        self.assertEqual(by_guid["guid-github-compromised"]["bucket"], "news")
        self.assertIn("GitHub compromised", by_guid["guid-github-compromised"]["matched_terms"])
        self.assertEqual(by_guid["guid-npm-compromised"]["profile"], "devops_security")
        self.assertEqual(by_guid["guid-npm-compromised"]["bucket"], "news")
        self.assertIn("packages compromised", by_guid["guid-npm-compromised"]["matched_terms"])

    def test_v1_classification_promotes_high_heat_hn_without_keyword_match(self) -> None:
        rules = {
            "interest_keywords": [],
            "feeds": {
                "exclude_terms": [],
                "active_profiles": ["major_events", "devops_security", "tech_industry_major", "personal_interest"],
                "news_profiles": ["major_events", "devops_security", "tech_industry_major"],
                "hn_news": {"direct_points": 800, "direct_comments": 300, "entity_points": 250, "entity_comments": 80},
                "watched_entities": ["Anthropic", "GitHub"],
            },
            "topic_profiles": {
                "major_news": {"must_track": ["war"], "watch": []},
                "devops_security": {"must_track": ["remote code execution"], "watch": []},
                "tech_industry_major": {"must_track": ["OpenAI"], "watch": []},
            },
        }
        article = Article(
            title="I’ve joined Anthropic",
            url="https://twitter.com/karpathy/status/2056753169888334312",
            source="Hacker News",
            source_type="hn",
            published_at=now_utc(),
            summary="HN score 1253 · comments 517",
            score=1253,
            comments=517,
            guid="guid-karpathy",
        )
        ranked = rank_articles([article], ["war"], {"Hacker News": 0.85}, SCORING_CONFIG)
        rows = classify_articles_v1(ranked, rules, {"Hacker News": 0.85}, SCORING_CONFIG, {"guid-karpathy": 1})

        self.assertEqual(rows[0]["profile"], "tech_industry_major")
        self.assertEqual(rows[0]["bucket"], "news")
        self.assertEqual(rows[0]["reason_code"], "hn_direct_news")
        self.assertIn("HN 高热度", rows[0]["reason_summary"])

    def test_short_security_acronyms_do_not_match_inside_words(self) -> None:
        text = "A number of Americans in Congo had exposure to suspected Ebola cases, sources said."

        self.assertFalse(term_hit(text, "RCE"))
        self.assertEqual(term_match_score(text, ["RCE"]), 0.0)
        self.assertTrue(term_hit("CISA warns of RCE exploited in the wild", "RCE"))

    def test_v1_classification_does_not_promote_ebola_story_as_rce(self) -> None:
        rules = {
            "interest_keywords": [],
            "feeds": {"exclude_terms": [], "active_profiles": ["major_events", "devops_security", "personal_interest"], "news_profiles": ["major_events", "devops_security"]},
            "topic_profiles": {
                "major_news": {"must_track": ["war"], "watch": []},
                "devops_security": {"must_track": ["RCE"], "watch": []},
            },
        }
        article = Article(
            title="In Ebola outbreak, a number of Americans in the Congo believed to have had exposure to suspected cases",
            url="https://www.statnews.com/2026/05/17/ebola-outbreak-congo-americans-exposure-suspected-cases",
            source="STAT",
            published_at=now_utc(),
            summary="A number of Americans who are in the Congo are believed to have had exposure to suspected cases, sources have told STAT.",
            guid="guid-ebola",
        )
        ranked = rank_articles([article], ["RCE"], {"STAT": 0.65}, SCORING_CONFIG)
        rows = classify_articles_v1(ranked, rules, {"STAT": 0.65}, SCORING_CONFIG, {"guid-ebola": 1})

        self.assertNotEqual(rows[0]["profile"], "devops_security")
        self.assertNotEqual(rows[0]["bucket"], "news")
        self.assertNotIn("RCE", rows[0]["matched_terms"])

    def test_v1_classification_demotes_consumer_data_breach_settlement(self) -> None:
        rules = {
            "interest_keywords": [],
            "feeds": {"exclude_terms": [], "active_profiles": ["major_events", "devops_security", "personal_interest"], "news_profiles": ["major_events", "devops_security"]},
            "topic_profiles": {
                "major_news": {"must_track": ["war"], "watch": []},
                "devops_security": {"must_track": ["data breach"], "watch": []},
            },
        }
        article = Article(
            title="Fidelity data breach settlement offers up to $5,000: How to file a claim",
            url="https://example.com/fidelity-data-breach-settlement",
            source="GDELT:wcnc.com",
            source_type="gdelt",
            published_at=now_utc(),
            summary="Consumers may be eligible for compensation after a class action settlement.",
            guid="guid-breach-settlement",
        )
        ranked = rank_articles([article], ["data breach"], {"GDELT:wcnc.com": 0.55}, SCORING_CONFIG)
        rows = classify_articles_v1(ranked, rules, {"GDELT:wcnc.com": 0.55}, SCORING_CONFIG, {"guid-breach-settlement": 1})

        self.assertEqual(rows[0]["profile"], "devops_security")
        self.assertEqual(rows[0]["bucket"], "radar")
        self.assertEqual(rows[0]["reason_code"], "consumer_breach_followup")
        self.assertIn("降噪", rows[0]["reason_summary"])

    def test_v1_major_events_source_signal_reason_does_not_claim_keyword_hit(self) -> None:
        rules = {
            "interest_keywords": [],
            "feeds": {"exclude_terms": [], "active_profiles": ["major_events"], "news_profiles": ["major_events"]},
            "topic_profiles": {
                "major_news": {"must_track": ["war"], "watch": []},
            },
        }
        article = Article(
            title="Popular open source project releases a new version",
            url="https://example.com/project",
            source="Hacker News",
            published_at=now_utc(),
            summary="HN score 400.",
            score=400,
            comments=100,
            guid="guid-source-signal",
        )
        ranked = rank_articles([article], ["war"], {"Hacker News": 0.85}, SCORING_CONFIG)
        rows = classify_articles_v1(ranked, rules, {"Hacker News": 0.85}, SCORING_CONFIG, {"guid-source-signal": 1})

        self.assertEqual(rows[0]["reason_code"], "source_signal")
        self.assertIn("未直接命中重大事件关键词", rows[0]["reason_summary"])
        self.assertNotIn("命中 重大事件信号", rows[0]["reason_summary"])

    def test_v1_sports_story_is_excluded_before_wire_source_boost(self) -> None:
        rules = {
            "interest_keywords": [],
            "feeds": {"exclude_terms": ["sports", "NFL", "Pittsburgh Steelers"], "active_profiles": ["major_events", "devops_security"], "news_profiles": ["major_events", "devops_security"]},
            "topic_profiles": {
                "major_news": {"must_track": ["war"], "watch": []},
                "devops_security": {"must_track": ["RCE"], "watch": []},
            },
        }
        article = Article(
            title="Aaron Rodgers agrees to a 1-year deal to return to the Pittsburgh Steelers, AP sources say",
            url="https://apnews.com/article/aaron-rodgers-pittsburgh-steelers-return",
            source="Associated Press",
            published_at=now_utc(),
            summary="The four-time NFL MVP agreed to a one-year deal.",
            guid="guid-sports",
        )
        ranked = rank_articles([article], ["war"], {"Associated Press": 0.95}, SCORING_CONFIG)
        rows = classify_articles_v1(ranked, rules, {"Associated Press": 0.95}, SCORING_CONFIG, {"guid-sports": 1})

        self.assertEqual(rows[0]["profile"], "noise")
        self.assertEqual(rows[0]["reason_code"], "excluded_term")
        self.assertEqual(rows[0]["bucket"], "radar")

    def test_feed_selection_limits_news_and_radar(self) -> None:
        articles = [
            Article(title=f"A{i}", url=f"https://example.com/{i}", source="Reuters", published_at=now_utc(), guid=f"g{i}")
            for i in range(5)
        ]
        classified = [
            {"article": article, "bucket": "news" if index < 3 else "radar", "profile": "major_events", "importance_score": 1 - index * 0.1, "confidence_score": 0.5}
            for index, article in enumerate(articles)
        ]
        selected = select_feed_articles(classified, news_limit=2, radar_limit=1)

        self.assertEqual(len(selected["news"]), 2)
        self.assertEqual(len(selected["radar"]), 1)

    def test_feed_selection_dedupes_syndicated_same_title_stories(self) -> None:
        title = "Fidelity data breach settlement offers up to $5,000: How to file a claim"
        first = Article(title=title, url="https://www.wcnc.com/article/news/fidelity/507-same", source="GDELT:wcnc.com", published_at=now_utc(), guid="g1")
        second = Article(title=title, url="https://www.wnep.com/article/news/fidelity/507-same", source="GDELT:wnep.com", published_at=now_utc(), guid="g2")
        classified = [
            {"article": first, "bucket": "news", "profile": "devops_security", "importance_score": 0.46, "confidence_score": 0.53},
            {"article": second, "bucket": "news", "profile": "devops_security", "importance_score": 0.45, "confidence_score": 0.52},
        ]

        selected = select_feed_articles(classified, news_limit=10, radar_limit=10)

        self.assertEqual(len(selected["news"]), 1)
        self.assertEqual(story_fingerprint(first), story_fingerprint(second))

    def test_hn_hot_query_results_can_be_persisted_as_standalone_feed(self) -> None:
        with TemporaryDirectory() as tmpdir:
            conn = connect_database(Path(tmpdir) / "rssnews.db")
            init_database(
                conn,
                Path(tmpdir),
                hn_hot_queries=[
                    {
                        "name": "ai-learning",
                        "query": "AI learning",
                        "min_points": 100,
                        "item_limit": 20,
                        "enabled": True,
                    }
                ],
            )
            hot = Article(
                title="[HN Hot] How to learn AI",
                url="https://news.ycombinator.com/item?id=1",
                source="HN Hot",
                source_type="hn_hot",
                published_at=now_utc() - timedelta(days=500),
                summary="HN historical hot post.",
                score=500,
                comments=120,
                guid="guid-hot",
            )
            stored = upsert_articles(conn, [hot])
            replace_hn_hot_items(conn, "ai-learning", stored)
            guids = hn_hot_feed_guids(conn, 10)

            self.assertEqual(guids, ["guid-hot"])
            details = hn_hot_selection_details(conn)
            self.assertEqual(details["guid-hot"][0]["query_name"], "ai-learning")
            self.assertEqual(details["guid-hot"][0]["matched_terms"], [])

    def test_hn_hot_reason_summary_includes_query_group_and_matched_terms(self) -> None:
        article = Article(
            title="[HN Hot] How to learn AI",
            url="https://news.ycombinator.com/item?id=1",
            source="HN Hot",
            source_type="hn_hot",
            published_at=now_utc() - timedelta(days=500),
            summary="HN historical hot post.",
            score=500,
            comments=120,
            guid="guid-hot",
        )
        summary = hn_hot_reason_summary(
            article,
            [
                {
                    "query_name": "ai-learning",
                    "query": '"AI learning" OR "learn AI"',
                    "rank": 2,
                    "points": 500,
                    "comments": 120,
                    "matched_terms": ["learn AI"],
                }
            ],
        )

        self.assertIn("入选查询组：ai-learning", summary)
        self.assertIn("匹配关键词/关键词组：learn AI", summary)
        self.assertIn("查询内排名：ai-learning #2", summary)

    def test_hn_hot_queries_are_seeded_from_config(self) -> None:
        with TemporaryDirectory() as tmpdir:
            conn = connect_database(Path(tmpdir) / "rssnews.db")
            init_database(
                conn,
                Path(tmpdir),
                hn_hot_queries=[
                    {
                        "name": "custom-topic",
                        "query": "Postgres OR SQLite",
                        "min_points": 123,
                        "item_limit": 7,
                        "enabled": True,
                    }
                ],
            )
            rows = enabled_hn_hot_queries(conn)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "custom-topic")
            self.assertEqual(rows[0]["query"], "Postgres OR SQLite")
            self.assertEqual(rows[0]["min_points"], 123)
            self.assertEqual(rows[0]["item_limit"], 7)

    def test_fetch_hn_algolia_hot_sorts_by_total_heat(self) -> None:
        payload = {
            "hits": [
                {"objectID": "0", "title": "Too Low", "url": "https://example.com/too-low", "created_at": "2020-01-01T00:00:00Z", "points": 50, "num_comments": 100},
                {"objectID": "1", "title": "Low", "url": "https://example.com/low", "created_at": "2020-01-01T00:00:00Z", "points": 200, "num_comments": 10},
                {"objectID": "2", "title": "High", "url": "https://example.com/high", "created_at": "2019-01-01T00:00:00Z", "points": 500, "num_comments": 20},
            ]
        }
        with patch("app.fetchers.http_get_json", return_value=payload):
            items = fetch_hn_algolia_hot("AI learning", min_points=100, limit=2)

        self.assertEqual(items[0].title, "[HN Hot] High")
        self.assertEqual(items[0].source_type, "hn_hot")
        self.assertEqual(items[0].raw["hn_hot_matched_terms"], ["AI learning"])
        self.assertEqual(items[0].raw["hn_hot_query"], "AI learning")
        self.assertNotIn("[HN Hot] Too Low", {item.title for item in items})

    def test_fetch_hn_algolia_hot_merges_matched_terms_for_same_story(self) -> None:
        def fake_get_json(url: str, params: dict | None = None, **kwargs: object) -> dict:
            term = str((params or {}).get("query") or "")
            return {
                "hits": [
                    {
                        "objectID": "1",
                        "title": "How to learn AI",
                        "url": "https://example.com/ai",
                        "created_at": "2020-01-01T00:00:00Z",
                        "points": 500,
                        "num_comments": 20,
                    }
                ]
                if term in {"AI learning", "learn AI"}
                else []
            }

        with patch("app.fetchers.http_get_json", side_effect=fake_get_json):
            items = fetch_hn_algolia_hot('"AI learning" OR "learn AI"', min_points=100, limit=2)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].raw["hn_hot_matched_terms"], ["AI learning", "learn AI"])

    def test_hn_hot_query_terms_split_or_expression(self) -> None:
        self.assertEqual(hn_query_terms('"AI learning" OR "learn AI"'), ["AI learning", "learn AI"])


if __name__ == "__main__":
    unittest.main()
