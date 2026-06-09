from __future__ import annotations

import dataclasses
import math
import re
from typing import Any, Dict, Iterable, List, Tuple

from app.feedback import domain_from_url
from app.helpers import canonicalize_url, keyword_hit, now_utc, stable_guid
from app.models import Article
from app.settings import HN_SCORE_CAP


def source_weight(source: str, weights: Dict[str, float]) -> float:
    return float(weights.get(source, 0.60))


def article_source_weight(article: Article, weights: Dict[str, float]) -> float:
    base = source_weight(article.source, weights)
    domain = domain_from_url(article.url)
    domain_delta = float(weights.get(f"domain:{domain}", 0.0)) if domain else 0.0
    return max(0.0, min(1.0, base + domain_delta))


def term_hit(text: str, term: str) -> bool:
    candidate = str(term or "").strip()
    if not candidate:
        return False
    normalized = text or ""
    pattern = rf"(?<![A-Za-z0-9]){re.escape(candidate)}(?![A-Za-z0-9])"
    return re.search(pattern, normalized, flags=re.IGNORECASE) is not None


def term_match_score(text: str, terms: Iterable[str]) -> float:
    hits = sum(1 for term in terms if term_hit(text, str(term)))
    if hits <= 0:
        return 0.0
    return min(1.0, 0.25 + 0.15 * (hits - 1))


def matched_terms(text: str, terms: Iterable[str]) -> List[str]:
    matches: List[str] = []
    for term in terms:
        candidate = str(term)
        if term_hit(text, candidate):
            matches.append(candidate)
    return matches


def article_interest_score(article: Article, keywords: Iterable[str]) -> float:
    title_score = term_match_score(article.title, keywords)
    summary_score = term_match_score(article.summary, keywords)
    return min(1.0, 0.75 * title_score + 0.25 * summary_score)


def article_topic_score(article: Article, terms: Iterable[str]) -> float:
    title_score = term_match_score(article.title, terms)
    summary_score = term_match_score(article.summary, terms)
    return min(1.0, 0.7 * title_score + 0.3 * summary_score)


def freshness_score(article: Article, half_life_hours: float) -> float:
    age_hours = max(0.0, (now_utc() - article.published_at).total_seconds() / 3600.0)
    half_life = max(1.0, half_life_hours)
    return math.pow(0.5, age_hours / half_life)


def engagement_score(article: Article) -> float:
    hn_points = min(max(article.score, 0.0), HN_SCORE_CAP) / HN_SCORE_CAP
    comment_norm = min(max(article.comments, 0), 200) / 200.0
    return min(1.0, 0.8 * hn_points + 0.2 * comment_norm)


def compute_priority(
    article: Article,
    keywords: Iterable[str],
    weights: Dict[str, float],
    scoring_config: Dict[str, float | Dict[str, float]],
) -> float:
    weight_map = scoring_config["weights"]
    return (
        float(weight_map["freshness"]) * freshness_score(article, float(scoring_config["freshness_half_life_hours"]))
        + float(weight_map["source_quality"]) * article_source_weight(article, weights)
        + float(weight_map["topic_relevance"]) * article_interest_score(article, keywords)
        + float(weight_map["engagement"]) * engagement_score(article)
    )


def score_breakdown(
    article: Article,
    keywords: Iterable[str],
    weights: Dict[str, float],
    scoring_config: Dict[str, float | Dict[str, float]],
) -> Dict[str, Any]:
    weight_map = scoring_config["weights"]
    freshness = freshness_score(article, float(scoring_config["freshness_half_life_hours"]))
    source_quality = article_source_weight(article, weights)
    topic_relevance = article_interest_score(article, keywords)
    engagement = engagement_score(article)
    raw_scores = {
        "freshness": round(freshness, 4),
        "source_quality": round(source_quality, 4),
        "topic_relevance": round(topic_relevance, 4),
        "engagement": round(engagement, 4),
    }
    weighted_scores = {
        key: round(float(weight_map[key]) * value, 4)
        for key, value in {
            "freshness": freshness,
            "source_quality": source_quality,
            "topic_relevance": topic_relevance,
            "engagement": engagement,
        }.items()
    }
    return {
        "priority": round(sum(weighted_scores.values()), 4),
        "raw": raw_scores,
        "weighted": weighted_scores,
    }


def dedupe(articles: List[Article]) -> List[Article]:
    seen: set[str] = set()
    output: List[Article] = []
    for article in articles:
        key = stable_guid(article.url, article.source)
        if key in seen:
            continue
        seen.add(key)
        output.append(article)
    return output


def dedupe_by_url(articles: List[Article]) -> List[Article]:
    seen: Dict[str, Article] = {}
    for article in articles:
        canonical_url = canonicalize_url(article.url)
        existing = seen.get(canonical_url)
        if existing is None or (article.score, len(article.summary)) > (existing.score, len(existing.summary)):
            seen[canonical_url] = article
    return list(seen.values())


def merge_dedupe_cross_source(articles: List[Article]) -> List[Article]:
    by_url: Dict[str, Article] = {}
    for article in articles:
        canonical_url = canonicalize_url(article.url)
        existing = by_url.get(canonical_url)
        if existing is None:
            by_url[canonical_url] = article
            continue

        score_article = (article.priority, article.score, len(article.summary), article.published_at.timestamp())
        score_existing = (existing.priority, existing.score, len(existing.summary), existing.published_at.timestamp())
        if score_article > score_existing:
            by_url[canonical_url] = article
    return list(by_url.values())


def classify(
    article: Article,
    keywords: Iterable[str],
    topic_terms: Iterable[str],
    exclude_terms: Iterable[str],
    scoring_config: Dict[str, float | Dict[str, float]],
) -> str:
    bucket, _reason = classify_with_reason(article, keywords, topic_terms, exclude_terms, scoring_config)
    return bucket


def classify_with_reason(
    article: Article,
    keywords: Iterable[str],
    topic_terms: Iterable[str],
    exclude_terms: Iterable[str],
    scoring_config: Dict[str, float | Dict[str, float]],
) -> Tuple[str, str]:
    searchable_text = f"{article.title} {article.summary}"
    if term_match_score(searchable_text, exclude_terms) > 0:
        return "radar", "excluded_term"

    if article.kind == "radar":
        return "radar", "source_observe_only"

    topic_score = article_topic_score(article, topic_terms)
    interest_score = article_interest_score(article, keywords)
    min_topic_score = float(scoring_config["feed_min_topic_score"])
    min_interest_score = float(scoring_config["feed_min_interest_score"])

    if topic_score >= min_topic_score:
        return "news", "topic_match"

    if interest_score < min_interest_score:
        return "radar", "low_interest_score"

    return "news", "interest_match"


def rank_articles(
    articles: List[Article],
    keywords: Iterable[str],
    source_weights_map: Dict[str, float],
    scoring_config: Dict[str, float | Dict[str, float]],
) -> List[Article]:
    ranked = [
        dataclasses.replace(article, priority=compute_priority(article, keywords, source_weights_map, scoring_config))
        for article in articles
    ]
    ranked.sort(key=lambda item: (item.priority, item.published_at), reverse=True)
    return ranked
