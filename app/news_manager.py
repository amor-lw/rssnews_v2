from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Dict, Iterable, List, Sequence

from app.feedback import domain_from_url
from app.models import Article
from app.pipeline import (
    article_source_weight,
    engagement_score,
    freshness_score,
    matched_terms,
    rank_articles,
    score_breakdown,
    term_hit,
    term_match_score,
)


DEFAULT_NEWS_PROFILES = ("major_events", "devops_security", "tech_industry_major")
PROFILE_ALIASES = {
    "major_news": "major_events",
}


def normalize_profile_name(name: str) -> str:
    return PROFILE_ALIASES.get(name, name)


def profile_terms(rules: Dict[str, Any], profile: str) -> Dict[str, List[str]]:
    profiles = dict(rules.get("topic_profiles") or {})
    source_name = "major_news" if profile == "major_events" and "major_news" in profiles else profile
    config = dict(profiles.get(source_name) or {})
    must_track = [str(term) for term in config.get("must_track", []) if str(term).strip()]
    watch = [str(term) for term in config.get("watch", []) if str(term).strip()]
    if profile == "personal_interest":
        already = {term.lower() for item in profiles.values() for group in ("must_track", "watch") for term in item.get(group, [])}
        personal = [str(term) for term in rules.get("interest_keywords", []) if str(term).lower() not in already]
        must_track = []
        watch = personal
    return {"must_track": must_track, "watch": watch}


def active_profiles(rules: Dict[str, Any]) -> List[str]:
    configured = list((rules.get("feeds") or {}).get("active_profiles") or [])
    if configured:
        return [normalize_profile_name(str(profile)) for profile in configured]
    return ["major_events", "devops_security", "tech_industry_major", "personal_interest"]


def all_profile_keywords(rules: Dict[str, Any]) -> List[str]:
    seen: set[str] = set()
    output: List[str] = []
    for profile in active_profiles(rules):
        terms = profile_terms(rules, profile)
        for term in terms["must_track"] + terms["watch"]:
            key = term.lower()
            if key not in seen:
                seen.add(key)
                output.append(term)
    if not output:
        output = list(rules.get("interest_keywords") or [])
    return output


def rce_context_hit(text: str) -> bool:
    if not term_hit(text, "RCE"):
        return False
    context_terms = (
        "CVE",
        "exploit",
        "exploited",
        "actively exploited",
        "vulnerability",
        "critical vulnerability",
        "patch",
        "advisory",
        "security advisory",
        "PoC",
        "CISA",
        "remote code execution",
    )
    return any(term_hit(text, term) for term in context_terms)


def severe_devops_hit(text: str) -> bool:
    severe_terms = (
        "cve",
        "zero-day",
        "0-day",
        "remote code execution",
        "actively exploited",
        "known exploited",
        "privilege escalation",
        "supply chain attack",
        "software supply chain",
        "github compromised",
        "packages compromised",
        "package compromised",
        "npm packages compromised",
        "malicious package",
        "npm malware",
        "credential theft",
        "credentials compromised",
        "token theft",
        "tokens compromised",
        "secrets leaked",
        "ransomware",
        "data breach",
        "critical vulnerability",
    )
    return rce_context_hit(text) or any(term_hit(text, term) for term in severe_terms)


def hn_heat_config(rules: Dict[str, Any]) -> Dict[str, int]:
    configured = dict((rules.get("feeds") or {}).get("hn_news") or {})
    return {
        "direct_points": int(configured.get("direct_points", 800)),
        "direct_comments": int(configured.get("direct_comments", 300)),
        "entity_points": int(configured.get("entity_points", 250)),
        "entity_comments": int(configured.get("entity_comments", 80)),
    }


def hn_article(article: Article) -> bool:
    return article.source_type == "hn" or article.source == "Hacker News"


def hn_direct_news_hit(article: Article, rules: Dict[str, Any]) -> bool:
    if not hn_article(article):
        return False
    config = hn_heat_config(rules)
    return article.score >= config["direct_points"] or article.comments >= config["direct_comments"]


def hn_entity_news_matches(article: Article, rules: Dict[str, Any]) -> List[str]:
    if not hn_article(article):
        return []
    config = hn_heat_config(rules)
    if article.score < config["entity_points"] and article.comments < config["entity_comments"]:
        return []
    entities = [str(term) for term in (rules.get("feeds") or {}).get("watched_entities", []) if str(term).strip()]
    return matched_terms(f"{article.title} {article.summary}", entities)


def consumer_data_breach_followup(text: str, matched: Sequence[str]) -> bool:
    if "data breach" not in {term.lower() for term in matched}:
        return False
    followup_terms = (
        "settlement",
        "compensation",
        "file a claim",
        "claim form",
        "class action",
        "lawsuit",
        "you could be owed",
        "how to claim",
        "your share",
    )
    return any(term_hit(text, term) for term in followup_terms)


def source_type_bonus(article: Article) -> float:
    if article.source in {"Reuters", "Associated Press", "Financial Times", "The Economist", "Bloomberg"}:
        return 0.12
    if article.source_type == "gdelt" or article.source.startswith("GDELT:"):
        return -0.06
    if article.source_type == "hn_hot":
        return 0.20
    if article.source_type == "cisa_kev" or article.source == "CISA KEV":
        return 0.18
    return 0.0


def build_reason_summary(
    article: Article,
    profile: str,
    bucket: str,
    matched: Sequence[str],
    source_count: int,
    importance: float,
    reason_code: str,
) -> str:
    display_profile = {
        "major_events": "重大新闻",
        "devops_security": "DevOps 安全",
        "tech_industry_major": "技术行业重大新闻",
        "personal_interest": "个人兴趣",
        "hn_hot": "HN 历史热帖",
    }.get(profile, profile)
    if profile == "hn_hot":
        return f"HN 历史热帖：{int(article.score)} points / {article.comments} comments，匹配历史热度榜。"
    if reason_code == "excluded_term":
        return "降噪：命中排除词，保留在后台或 radar 观察。"
    if reason_code == "consumer_breach_followup":
        return f"降噪：数据泄露后续理赔/法律文章，来自 {article.source}，不作为 DevOps 主新闻。"
    if reason_code == "hn_direct_news":
        return f"HN 高热度：{int(article.score)} points / {article.comments} comments，未命中排除词，直接进入 news。"
    if reason_code == "hn_entity_news":
        return f"技术行业重大新闻：HN {int(article.score)} points / {article.comments} comments，命中 {', '.join(matched[:3]) or '关注实体'}。"
    if profile == "devops_security" and severe_devops_hit(f"{article.title} {article.summary}"):
        return f"DevOps 安全：疑似严重安全事件，来自 {article.source}，命中 {', '.join(matched[:3]) or '安全高危信号'}。"
    if profile == "major_events":
        cross_source = "，多个来源同时出现" if source_count > 1 else ""
        if not matched:
            if bucket == "news":
                return f"重大新闻：来自 {article.source}{cross_source}，未直接命中关键词，但来源/多来源信号较强。"
            return f"观察信号：来自 {article.source}{cross_source}，未直接命中重大事件关键词，先放入 radar。"
        return f"重大新闻：来自 {article.source}{cross_source}，命中 {', '.join(matched[:3])}。"
    if bucket == "news":
        return f"{display_profile}：相关性较高，来自 {article.source}，综合分 {importance:.2f}。"
    return f"{display_profile}：命中 {', '.join(matched[:3]) or '观察信号'}，但置信度较低，先放入 radar。"


def classify_articles_v1(
    articles: List[Article],
    rules: Dict[str, Any],
    source_weights: Dict[str, float],
    scoring_config: Dict[str, Any],
    source_counts: Dict[str, int] | None = None,
) -> List[Dict[str, Any]]:
    source_counts = source_counts or {}
    exclude_terms = list((rules.get("feeds") or {}).get("exclude_terms", []))
    results: List[Dict[str, Any]] = []
    profiles = active_profiles(rules)
    news_profiles = set((rules.get("feeds") or {}).get("news_profiles") or DEFAULT_NEWS_PROFILES)

    for article in articles:
        text = f"{article.title} {article.summary}"
        if term_match_score(text, exclude_terms) > 0:
            results.append(
                {
                    "article": article,
                    "article_guid": article.guid,
                    "profile": "noise",
                    "bucket": "radar",
                    "importance_score": 0.0,
                    "relevance_score": 0.0,
                    "confidence_score": 0.0,
                    "reason_code": "excluded_term",
                    "reason_summary": build_reason_summary(article, "noise", "radar", [], source_counts.get(article.guid, 1), 0.0, "excluded_term"),
                    "matched_terms": matched_terms(text, exclude_terms),
                    "scores": {},
                }
            )
            continue

        best: Dict[str, Any] | None = None
        for profile in profiles:
            terms = profile_terms(rules, profile)
            must_matches = matched_terms(text, terms["must_track"])
            watch_matches = matched_terms(text, terms["watch"])
            relevance = min(1.0, 0.55 * term_match_score(text, terms["must_track"]) + 0.35 * term_match_score(text, terms["watch"]))

            if profile == "devops_security" and severe_devops_hit(text):
                relevance = max(relevance, 0.7)
            if profile == "major_events" and article.source in {"Reuters", "Associated Press", "Financial Times"}:
                relevance = max(relevance, 0.35 if not (must_matches or watch_matches) else relevance)

            base_priority = article.priority or score_breakdown(article, all_profile_keywords(rules), source_weights, scoring_config)["priority"]
            cross_source = min(0.16, 0.04 * max(0, source_counts.get(article.guid, 1) - 1))
            importance = min(
                1.0,
                0.40 * base_priority
                + 0.35 * relevance
                + 0.10 * article_source_weight(article, source_weights)
                + cross_source
                + source_type_bonus(article),
            )
            confidence = min(1.0, 0.55 * relevance + 0.25 * article_source_weight(article, source_weights) + cross_source)
            matched = must_matches + [term for term in watch_matches if term not in must_matches]
            reason_code = "profile_match" if matched else "source_signal"
            if profile == "devops_security" and consumer_data_breach_followup(text, matched):
                relevance = min(relevance, 0.2)
                importance = min(importance, 0.34)
                confidence = min(confidence, 0.35)
                reason_code = "consumer_breach_followup"
            candidate = {
                "article": article,
                "article_guid": article.guid,
                "profile": profile,
                "bucket": "radar",
                "importance_score": round(importance, 4),
                "relevance_score": round(relevance, 4),
                "confidence_score": round(confidence, 4),
                "reason_code": reason_code,
                "matched_terms": matched,
                "scores": {
                    "importance": round(importance, 4),
                    "relevance": round(relevance, 4),
                    "confidence": round(confidence, 4),
                    "source_count": source_counts.get(article.guid, 1),
                },
            }
            if best is None or (candidate["importance_score"], candidate["confidence_score"]) > (best["importance_score"], best["confidence_score"]):
                best = candidate

        if best is None:
            continue

        profile = str(best["profile"])
        hn_entity_matches = hn_entity_news_matches(article, rules)
        if hn_direct_news_hit(article, rules):
            best["profile"] = "tech_industry_major"
            best["bucket"] = "news"
            best["importance_score"] = max(float(best["importance_score"]), 0.62)
            best["confidence_score"] = max(float(best["confidence_score"]), 0.75)
            best["relevance_score"] = max(float(best["relevance_score"]), 0.65)
            best["reason_code"] = "hn_direct_news"
            best["matched_terms"] = hn_entity_matches
        elif hn_entity_matches:
            best["profile"] = "tech_industry_major"
            best["bucket"] = "news"
            best["importance_score"] = max(float(best["importance_score"]), 0.50)
            best["confidence_score"] = max(float(best["confidence_score"]), 0.62)
            best["relevance_score"] = max(float(best["relevance_score"]), 0.55)
            best["reason_code"] = "hn_entity_news"
            best["matched_terms"] = hn_entity_matches

        best["scores"]["importance"] = round(float(best["importance_score"]), 4)
        best["scores"]["relevance"] = round(float(best["relevance_score"]), 4)
        best["scores"]["confidence"] = round(float(best["confidence_score"]), 4)

        profile = str(best["profile"])
        news_threshold = 0.46 if profile == "major_events" else 0.42
        if profile == "tech_industry_major":
            news_threshold = 0.50
        if profile == "personal_interest":
            news_threshold = 0.75
        if best["bucket"] == "news":
            pass
        elif profile in news_profiles and best["importance_score"] >= news_threshold:
            best["bucket"] = "news"
        elif profile == "devops_security" and severe_devops_hit(text) and best["importance_score"] >= 0.36:
            best["bucket"] = "news"
        else:
            best["bucket"] = "radar"

        best["reason_summary"] = build_reason_summary(
            article,
            profile,
            str(best["bucket"]),
            list(best["matched_terms"]),
            source_counts.get(article.guid, 1),
            float(best["importance_score"]),
            str(best["reason_code"]),
        )
        results.append(best)

    return results


def select_feed_articles(
    classified: Sequence[Dict[str, Any]],
    news_limit: int,
    radar_limit: int,
) -> Dict[str, List[Article]]:
    news_rows = [row for row in classified if row["bucket"] == "news"]
    radar_rows = [row for row in classified if row["bucket"] == "radar" and row["profile"] != "noise"]
    news_rows.sort(key=lambda row: (row["importance_score"], row["confidence_score"]), reverse=True)
    radar_rows.sort(key=lambda row: (row["importance_score"], row["confidence_score"]), reverse=True)

    def materialize(rows: Sequence[Dict[str, Any]], limit: int) -> List[Article]:
        deduped = dedupe_story_rows(rows)
        return [
            replace(row["article"], priority=float(row["importance_score"]))
            for row in deduped[:limit]
        ]

    return {
        "news": materialize(news_rows, news_limit),
        "radar": materialize(radar_rows, radar_limit),
    }


def story_fingerprint(article: Article) -> str:
    normalized_title = re.sub(r"[^a-z0-9]+", " ", article.title.lower()).strip()
    normalized_title = re.sub(r"\s+", " ", normalized_title)
    if len(normalized_title) >= 28:
        return f"title:{normalized_title}"
    return f"url:{article.url}"


def dedupe_story_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_fingerprint: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        article = row["article"]
        key = story_fingerprint(article)
        existing = by_fingerprint.get(key)
        if existing is None:
            by_fingerprint[key] = row
            continue
        current_score = (
            float(row.get("importance_score", 0.0)),
            float(row.get("confidence_score", 0.0)),
            article.score,
            len(article.summary),
        )
        existing_article = existing["article"]
        existing_score = (
            float(existing.get("importance_score", 0.0)),
            float(existing.get("confidence_score", 0.0)),
            existing_article.score,
            len(existing_article.summary),
        )
        if current_score > existing_score:
            by_fingerprint[key] = row
    output = list(by_fingerprint.values())
    output.sort(key=lambda row: (row["importance_score"], row["confidence_score"]), reverse=True)
    return output


def source_count_by_guid(article_sources: Dict[str, int] | Iterable[Article]) -> Dict[str, int]:
    if isinstance(article_sources, dict):
        return article_sources
    counts: Dict[str, set[str]] = {}
    for article in article_sources:
        if not article.guid:
            continue
        counts.setdefault(article.guid, set()).add(article.source)
    return {guid: len(sources) for guid, sources in counts.items()}
