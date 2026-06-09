from __future__ import annotations

import html
from urllib.parse import quote
from typing import Any, Dict, List

from app.helpers import now_utc, safe_text, stable_guid, to_rfc2822
from app.models import Article


def build_feedback_links(article: Article, feedback_url: str) -> str:
    guid = article.guid or stable_guid(article.url, article.source)
    base = feedback_url.rstrip("/")
    common = f"guid={quote(guid, safe='')}&source={quote(article.source, safe='')}&url={quote(article.url, safe='')}"
    links = [
        ("keep", "Keep"),
        ("save", "Save"),
        ("mute", "Mute"),
        ("mute-domain", "Mute source"),
    ]
    return " · ".join(
        f'<a href="{html.escape(base + "?action=" + action + "&" + common)}">{label}</a>'
        for action, label in links
    )


REASON_LABELS = {
    "topic_match": "matched configured topic",
    "interest_match": "matched personal interest",
    "excluded_term": "matched exclude term",
    "source_observe_only": "observe-only source",
    "low_interest_score": "below interest threshold",
    "unknown": "unknown",
}


def compact_list(items: List[str], limit: int = 5) -> str:
    values = [item for item in items if item][:limit]
    return ", ".join(values)


def build_item_explanation(detail: Dict[str, Any] | None) -> str:
    if not detail:
        return ""

    parts: List[str] = []
    reason_summary = str(detail.get("reason_summary") or "").strip()
    if reason_summary:
        parts.append(f"<p><b>Why:</b> {html.escape(reason_summary)}</p>")

    reason = str(detail.get("reason") or "unknown")
    bucket = str(detail.get("bucket") or "")
    prefix = "Why selected" if bucket == "news" else "Why radar"
    if not reason_summary:
        parts.append(f"<p><b>{prefix}:</b> {html.escape(REASON_LABELS.get(reason, reason))}</p>")

    matches = detail.get("matches") if isinstance(detail.get("matches"), dict) else {}
    matched = compact_list(list(matches.get("interest_keywords") or []) + list(matches.get("topic_terms") or []))
    excluded = compact_list(list(matches.get("exclude_terms") or []))
    if matched:
        parts.append(f"<p><b>Matched:</b> {html.escape(matched)}</p>")
    if excluded:
        parts.append(f"<p><b>Excluded:</b> {html.escape(excluded)}</p>")

    scores = detail.get("scores") if isinstance(detail.get("scores"), dict) else {}
    if "hotness_score" not in scores and isinstance(detail.get("hotness"), dict):
        scores = dict(detail.get("hotness") or {})
    hotness_scores = scores.get("weighted") if isinstance(scores.get("weighted"), dict) else {}
    if not hotness_scores and any(key in scores for key in ("hotness_score", "base_score", "bonus_score", "confidence_score")):
        hotness_scores = scores
    if hotness_scores and "hotness_score" in hotness_scores:
        bonus_parts = hotness_scores.get("bonus_parts") if isinstance(hotness_scores.get("bonus_parts"), dict) else {}
        bonus_text = ", ".join(f"{key} +{float(value):.2f}" for key, value in bonus_parts.items()) or "none"
        direct_reasons = hotness_scores.get("direct_reasons") if isinstance(hotness_scores.get("direct_reasons"), list) else []
        direct_text = ", ".join(str(item) for item in direct_reasons) or "none"
        threshold = "news: direct or hotness >= 38 and confidence >= 0.65; radar: hotness >= 20"
        score_text = (
            f"hotness {float(hotness_scores.get('hotness_score', 0.0)):.2f} = "
            f"base {float(hotness_scores.get('base_score', 0.0)):.2f} + "
            f"bonus {float(hotness_scores.get('bonus_score', 0.0)):.2f} - "
            f"noise {float(hotness_scores.get('noise_penalty', 0.0)):.2f}; "
            f"confidence {float(hotness_scores.get('confidence_score', 0.0)):.2f}; "
            f"bonus parts: {bonus_text}; direct: {direct_text}; threshold: {threshold}"
        )
        parts.append(f"<p><b>Score:</b> {html.escape(score_text)}</p>")

    weighted = scores.get("weighted") if isinstance(scores.get("weighted"), dict) else {}
    if weighted and "hotness_score" in weighted:
        weighted = {}
    if weighted:
        score_text = (
            f"fresh {float(weighted.get('freshness', 0.0)):.3f}, "
            f"source {float(weighted.get('source_quality', 0.0)):.3f}, "
            f"relevance {float(weighted.get('topic_relevance', 0.0)):.3f}, "
            f"engagement {float(weighted.get('engagement', 0.0)):.3f}"
        )
        parts.append(f"<p><b>Score:</b> {html.escape(score_text)}</p>")

    weights = detail.get("weights") if isinstance(detail.get("weights"), dict) else {}
    domain_delta = float(weights.get("domain_feedback_delta", 0.0)) if weights else 0.0
    if abs(domain_delta) > 0.0001:
        parts.append(f"<p><b>Feedback:</b> domain weight {domain_delta:+.2f}</p>")

    return "".join(parts)


def hn_discussion_link(article: Article) -> str:
    if article.hn_id is None:
        return ""
    return f"https://news.ycombinator.com/item?id={int(article.hn_id)}"


def build_rss(
    channel_title: str,
    channel_link: str,
    channel_desc: str,
    items: List[Article],
    include_feedback_links: bool = True,
    feedback_url: str | None = None,
    item_details: Dict[str, Dict[str, Any]] | None = None,
) -> str:
    lines: List[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<rss version="2.0">')
    lines.append("  <channel>")
    lines.append(f"    <title>{html.escape(channel_title)}</title>")
    lines.append(f"    <link>{html.escape(channel_link)}</link>")
    lines.append(f"    <description>{html.escape(channel_desc)}</description>")
    lines.append(f"    <lastBuildDate>{html.escape(to_rfc2822(now_utc()))}</lastBuildDate>")

    for article in items:
        title = html.escape(article.title or "(no title)")
        link = html.escape(article.url)
        guid = html.escape(article.guid or stable_guid(article.url, article.source))
        pub_date = html.escape(to_rfc2822(article.published_at))

        meta = f"{html.escape(article.source)} · priority {article.priority:.3f}"
        if article.score:
            meta += f" · HN {int(article.score)}"
        if article.comments:
            meta += f" · {article.comments} comments"

        description = f"<p><b>{meta}</b></p>"
        if article.summary.strip():
            description += f"<p>{safe_text(article.summary, 500)}</p>"
        discussion_url = hn_discussion_link(article)
        if discussion_url:
            description += f'<p><b>HN discussion:</b> <a href="{html.escape(discussion_url)}">{html.escape(discussion_url)}</a></p>'
        if item_details:
            detail = item_details.get(article.guid or stable_guid(article.url, article.source))
            description += build_item_explanation(detail)
        if include_feedback_links and feedback_url:
            description += f"<p>{build_feedback_links(article, feedback_url)}</p>"

        lines.append("    <item>")
        lines.append(f"      <title>{title}</title>")
        lines.append(f"      <link>{link}</link>")
        lines.append(f'      <guid isPermaLink="false">{guid}</guid>')
        lines.append(f"      <pubDate>{pub_date}</pubDate>")
        lines.append("      <description><![CDATA[")
        lines.append(description)
        lines.append("      ]]></description>")
        lines.append("    </item>")

    lines.append("  </channel>")
    lines.append("</rss>")
    return "\n".join(lines) + "\n"
