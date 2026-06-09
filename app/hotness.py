from __future__ import annotations

import dataclasses
import re
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from app.helpers import canonicalize_url, now_utc, sha1_text
from app.models import Article
from app.pipeline import term_hit


STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "amid",
    "among",
    "and",
    "are",
    "from",
    "have",
    "into",
    "over",
    "said",
    "says",
    "that",
    "the",
    "their",
    "this",
    "with",
    "will",
    "your",
}


def source_family(article: Article) -> str:
    if article.source_family:
        return article.source_family
    if article.source_type == "hn" or article.source == "Hacker News":
        return "hn"
    if article.source_type == "newsapi":
        return "newsapi"
    if article.source_type == "cisa_kev" or article.source == "CISA KEV":
        return "cisa_kev"
    if article.source_type == "hn_hot":
        return "hn_hot"
    return "rss"


def list_position_score(rank: int, limit: int, first_score: float, last_score: float) -> float:
    if limit <= 1:
        return round(float(first_score), 2)
    bounded_rank = min(max(int(rank), 1), int(limit))
    span = float(first_score) - float(last_score)
    score = float(first_score) - (bounded_rank - 1) * (span / (int(limit) - 1))
    return round(max(float(last_score), score), 2)


def hn_quality_score(article: Article, hotness_config: Dict[str, Any]) -> float:
    point_threshold = max(float(hotness_config.get("hn_direct_points", 800)), 1.0)
    comment_threshold = max(float(hotness_config.get("hn_direct_comments", 300)), 1.0)
    points = min(max(float(article.score or article.hn_points or 0.0), 0.0) / point_threshold, 1.0)
    comments = min(max(float(article.comments or article.hn_comments or 0), 0.0) / comment_threshold, 1.0)
    return round(min(1.0, 0.72 * points + 0.28 * comments), 4)


def annotate_hot_signal(
    article: Article,
    *,
    source_family_name: str,
    source_list: str,
    source_rank: int | None,
    hot_signal_type: str,
    base_score: float = 0.0,
    bonus_score: float = 0.0,
) -> Article:
    hotness_raw = dict(article.raw or {})
    hotness_raw["hotness"] = {
        **dict(hotness_raw.get("hotness") or {}),
        "source_family": source_family_name,
        "source_list": source_list,
        "source_rank": source_rank,
        "hot_signal_type": hot_signal_type,
        "base_score": round(float(base_score), 4),
        "bonus_score": round(float(bonus_score), 4),
    }
    return dataclasses.replace(
        article,
        source_family=source_family_name,
        source_list=source_list,
        source_rank=source_rank,
        hot_signal_type=hot_signal_type,
        base_score=round(float(base_score), 4),
        bonus_score=round(float(bonus_score), 4),
        raw=hotness_raw,
    )


def annotate_ranked_articles(
    articles: Sequence[Article],
    *,
    source_family_name: str,
    source_list: str,
    hot_signal_type: str,
    hotness_config: Dict[str, Any],
    limit_key: str,
    first_key: str,
    last_key: str,
    as_bonus: bool = False,
) -> List[Article]:
    limit = int(hotness_config.get(limit_key, len(articles) or 1))
    first = float(hotness_config.get(first_key, 0.0))
    last = float(hotness_config.get(last_key, 0.0))
    output: List[Article] = []
    for rank, article in enumerate(articles[:limit], start=1):
        score = list_position_score(rank, limit, first, last)
        output.append(
            annotate_hot_signal(
                article,
                source_family_name=source_family_name,
                source_list=source_list,
                source_rank=rank,
                hot_signal_type=hot_signal_type,
                base_score=0.0 if as_bonus else score,
                bonus_score=score if as_bonus else 0.0,
            )
        )
    return output


def normalize_title(title: str) -> str:
    normalized = re.sub(r"https?://\S+", " ", title.lower())
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def title_tokens(title: str) -> set[str]:
    return {
        token
        for token in normalize_title(title).split()
        if len(token) > 3 and token not in STOPWORDS
    }


def title_similarity(left: Article, right: Article) -> float:
    left_title = normalize_title(left.title)
    right_title = normalize_title(right.title)
    if not left_title or not right_title:
        return 0.0
    sequence_ratio = SequenceMatcher(None, left_title, right_title).ratio()
    left_tokens = title_tokens(left.title)
    right_tokens = title_tokens(right.title)
    token_ratio = 0.0
    if left_tokens and right_tokens:
        token_ratio = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    return round(max(sequence_ratio, token_ratio), 4)


def event_match(left: Article, right: Article, hotness_config: Dict[str, Any]) -> Tuple[str, float]:
    if canonicalize_url(left.url) == canonicalize_url(right.url):
        return "strong:url", 1.0

    age_delta_hours = abs((left.published_at - right.published_at).total_seconds()) / 3600.0
    if age_delta_hours > float(hotness_config.get("match_time_window_hours", 48)):
        return "", 0.0

    similarity = title_similarity(left, right)
    left_tokens = title_tokens(left.title)
    right_tokens = title_tokens(right.title)
    common_tokens = left_tokens & right_tokens
    same_domain = bool(left.domain and right.domain and left.domain == right.domain)

    if same_domain and similarity >= 0.62:
        return "medium:same_domain_title", similarity
    if len(common_tokens) >= 3 and similarity >= 0.58:
        return "medium:shared_entities", similarity
    if similarity >= 0.78 and len(common_tokens) >= 2:
        return "medium:title", similarity
    if similarity >= 0.64 and len(common_tokens) >= 1:
        return "weak:title", similarity
    return "", similarity


def noise_hit(article: Article, noise_terms: Iterable[str]) -> List[str]:
    text = f"{article.title} {article.summary} {article.source}"
    return [term for term in noise_terms if term_hit(text, str(term))]


def cluster_articles(articles: Sequence[Article], hotness_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    parent = list(range(len(articles)))
    match_types: Dict[tuple[int, int], str] = {}
    similarities: Dict[tuple[int, int], float] = {}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for left_index in range(len(articles)):
        for right_index in range(left_index + 1, len(articles)):
            match_type, similarity = event_match(articles[left_index], articles[right_index], hotness_config)
            if match_type.startswith(("strong", "medium")):
                union(left_index, right_index)
            if match_type:
                key = (left_index, right_index)
                match_types[key] = match_type
                similarities[key] = similarity

    grouped: Dict[int, List[tuple[int, Article]]] = {}
    for index, article in enumerate(articles):
        grouped.setdefault(find(index), []).append((index, article))

    clusters: List[Dict[str, Any]] = []
    for indexed_items in grouped.values():
        items = [article for _index, article in indexed_items]
        title = max((item.title for item in items), key=len, default="")
        cluster_id = sha1_text("|".join(sorted(canonicalize_url(item.url) for item in items)) or title)
        item_match_types: Dict[str, str] = {}
        item_similarities: Dict[str, float] = {}
        for left_index, left_article in indexed_items:
            for right_index, right_article in indexed_items:
                if left_index >= right_index:
                    continue
                key = (left_index, right_index)
                if key in match_types:
                    item_match_types[left_article.guid] = match_types[key]
                    item_match_types[right_article.guid] = match_types[key]
                    item_similarities[left_article.guid] = similarities[key]
                    item_similarities[right_article.guid] = similarities[key]
        clusters.append(
            {
                "cluster_id": cluster_id,
                "canonical_title": title,
                "items": items,
                "first_seen_at": min((item.published_at for item in items), default=now_utc()),
                "last_seen_at": max((item.published_at for item in items), default=now_utc()),
                "match_types": item_match_types,
                "similarities": item_similarities,
            }
        )
    return clusters


def cluster_families(items: Sequence[Article]) -> set[str]:
    return {source_family(item) for item in items}


def cluster_has(items: Sequence[Article], family: str, source_list: str | None = None) -> bool:
    for item in items:
        if source_family(item) != family:
            continue
        if source_list is None or item.source_list == source_list:
            return True
    return False


def has_hn_topstories_signal(items: Sequence[Article], hotness_config: Dict[str, Any]) -> bool:
    limit = int(hotness_config.get("hn_topstories_limit", 100))
    for item in items:
        if source_family(item) != "hn" or item.source_list != "topstories":
            continue
        if item.source_rank is None or int(item.source_rank) <= limit:
            return True
    return False


def best_hn_article(items: Sequence[Article]) -> Article | None:
    hn_items = [item for item in items if source_family(item) == "hn"]
    if not hn_items:
        return None
    return max(hn_items, key=lambda item: (item.score, item.comments, item.base_score + item.bonus_score))


def score_cluster(
    cluster: Dict[str, Any],
    hotness_config: Dict[str, Any],
    source_weights: Dict[str, float],
    noise_terms: Sequence[str],
) -> Dict[str, Any]:
    items: List[Article] = list(cluster["items"])
    base_by_family_list: Dict[str, float] = {}
    best_bonus_by_family_list: Dict[str, float] = {}
    for item in items:
        key = f"{source_family(item)}:{item.source_list}"
        base_by_family_list[key] = max(base_by_family_list.get(key, 0.0), float(item.base_score or 0.0))
        best_bonus_by_family_list[key] = max(best_bonus_by_family_list.get(key, 0.0), float(item.bonus_score or 0.0))

    families = cluster_families(items)
    base_score = round(sum(base_by_family_list.values()), 4)
    bonus_parts: Dict[str, float] = {}
    hn_topstories_signal = has_hn_topstories_signal(items, hotness_config)
    if hn_topstories_signal and "newsapi" in families:
        bonus_parts["newsapi_hn_match"] = float(hotness_config.get("match_bonus_newsapi_hn", 18))
    if hn_topstories_signal and "rss" in families:
        bonus_parts["rss_hn_match"] = float(hotness_config.get("match_bonus_rss_hn", 10))
    if hn_topstories_signal and "cisa_kev" in families:
        bonus_parts["security_match"] = float(hotness_config.get("security_match_bonus", 12))
    hn_best_bonus = max(
        (value for key, value in best_bonus_by_family_list.items() if key == "hn:beststories"),
        default=0.0,
    )
    if hn_best_bonus:
        bonus_parts["hn_beststories"] = hn_best_bonus

    all_noise = sorted({term for item in items for term in noise_hit(item, noise_terms)})
    noise_penalty = 15.0 if all_noise else 0.0
    bonus_score = round(sum(bonus_parts.values()), 4)
    hotness_score = round(max(0.0, base_score + bonus_score - noise_penalty), 4)

    strong_matches = sum(1 for value in cluster.get("match_types", {}).values() if str(value).startswith("strong"))
    medium_matches = sum(1 for value in cluster.get("match_types", {}).values() if str(value).startswith("medium"))
    best_source_weight = max((float(source_weights.get(item.source, 0.7)) for item in items), default=0.7)
    consensus = min(1.0, 0.18 * max(0, len(families) - 1))
    match_confidence = min(0.35, 0.16 * strong_matches + 0.08 * medium_matches)
    rank_confidence = min(0.20, base_score / 150.0)
    confidence = round(min(1.0, 0.24 + consensus + match_confidence + rank_confidence + 0.20 * best_source_weight), 4)

    direct_reasons: List[str] = []
    hn_item = best_hn_article(items)
    if hn_item:
        quality = hn_quality_score(hn_item, hotness_config)
        if (
            hn_item.score >= float(hotness_config.get("hn_direct_points", 800))
            or hn_item.comments >= int(hotness_config.get("hn_direct_comments", 300))
            or quality >= float(hotness_config.get("hn_direct_quality_score", 0.75))
        ):
            direct_reasons.append("hn_direct_quality")

    representative = max(
        items,
        key=lambda item: (
            float(item.base_score or 0.0) + float(item.bonus_score or 0.0),
            item.score,
            item.comments,
            item.published_at.timestamp(),
        ),
    )
    cluster["source_count"] = len({item.source for item in items})
    cluster["independent_source_count"] = len(families)
    cluster["representative"] = representative

    return {
        "cluster": cluster,
        "article": representative,
        "base_score": base_score,
        "bonus_score": bonus_score,
        "bonus_parts": bonus_parts,
        "hotness_score": hotness_score,
        "confidence_score": confidence,
        "families": sorted(families),
        "noise_terms": all_noise,
        "noise_penalty": noise_penalty,
        "direct_reasons": direct_reasons,
    }


def reason_summary(scored: Dict[str, Any]) -> str:
    article: Article = scored["article"]
    parts: List[str] = []
    for item in scored["cluster"]["items"]:
        family = source_family(item)
        if family == "newsapi" and item.source_rank:
            parts.append(f"NewsAPI #{item.source_rank}")
        elif family == "hn" and item.source_list == "topstories" and item.source_rank:
            parts.append(f"HN topstories #{item.source_rank}")
        elif family == "hn" and item.source_list == "beststories" and item.source_rank:
            parts.append(f"HN beststories #{item.source_rank}")
        elif family in {"rss", "cisa_kev"}:
            parts.append(item.source)
    unique_parts = []
    seen = set()
    for part in parts:
        if part not in seen:
            seen.add(part)
            unique_parts.append(part)
    basis = " + ".join(unique_parts[:5]) or article.source
    direct = ""
    if "hn_direct_quality" in scored["direct_reasons"]:
        direct = "，HN 热度/优质度直通"
    return (
        f"{basis}{direct}；hotness {scored['hotness_score']:.2f}，"
        f"confidence {scored['confidence_score']:.2f}。"
    )


def classify_hot_events(
    articles: Sequence[Article],
    hotness_config: Dict[str, Any],
    source_weights: Dict[str, float],
    noise_terms: Sequence[str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    radar_candidates: List[Dict[str, Any]] = []
    for cluster in cluster_articles(articles, hotness_config):
        scored = score_cluster(cluster, hotness_config, source_weights, noise_terms)
        hotness_score = float(scored["hotness_score"])
        confidence_score = float(scored["confidence_score"])
        direct = bool(scored["direct_reasons"])
        has_noise = bool(scored["noise_terms"])
        bucket = ""
        reason_code = "below_threshold"

        if has_noise:
            bucket = ""
            reason_code = "noise_excluded"
        elif direct:
            bucket = "news"
            reason_code = "+".join(scored["direct_reasons"])
        elif hotness_score >= float(hotness_config.get("news_min_score", 38)) and confidence_score >= float(hotness_config.get("news_min_confidence", 0.65)):
            bucket = "news"
            reason_code = "score_threshold"
        elif hotness_score >= float(hotness_config.get("radar_min_score", 20)):
            bucket = "radar"
            reason_code = "radar_threshold"

        if not bucket:
            continue

        article = dataclasses.replace(scored["article"], priority=hotness_score)
        row = {
            "article": article,
            "article_guid": article.guid,
            "profile": "hotness",
            "bucket": bucket,
            "importance_score": hotness_score,
            "relevance_score": float(scored["base_score"]),
            "confidence_score": confidence_score,
            "reason_code": reason_code,
            "reason_summary": reason_summary(scored),
            "matched_terms": scored["noise_terms"],
            "scores": {
                "hotness_score": hotness_score,
                "confidence_score": confidence_score,
                "base_score": scored["base_score"],
                "bonus_score": scored["bonus_score"],
                "bonus_parts": scored["bonus_parts"],
                "noise_penalty": scored["noise_penalty"],
                "families": scored["families"],
                "direct_reasons": scored["direct_reasons"],
                "cluster_size": len(scored["cluster"]["items"]),
            },
            "cluster": scored["cluster"],
        }
        if bucket == "news":
            rows.append(row)
        else:
            radar_candidates.append(row)

    rows.sort(key=lambda row: (row["importance_score"], row["confidence_score"]), reverse=True)
    radar_candidates.sort(key=lambda row: (row["importance_score"], row["confidence_score"]), reverse=True)
    radar_limit = int(hotness_config.get("radar_max_items", 50))
    return rows + radar_candidates[:radar_limit]


def select_hot_feed_articles(classified: Sequence[Dict[str, Any]]) -> Dict[str, List[Article]]:
    news_rows = [row for row in classified if row["bucket"] == "news"]
    radar_rows = [row for row in classified if row["bucket"] == "radar"]
    news_rows.sort(key=lambda row: (row["importance_score"], row["confidence_score"]), reverse=True)
    radar_rows.sort(key=lambda row: (row["importance_score"], row["confidence_score"]), reverse=True)
    return {
        "news": [dataclasses.replace(row["article"], priority=float(row["importance_score"])) for row in news_rows],
        "radar": [dataclasses.replace(row["article"], priority=float(row["importance_score"])) for row in radar_rows],
    }
