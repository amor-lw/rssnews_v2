from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

DEFAULT_ENV_FILE = ".env"
DEFAULT_RULES_FILE = "config/feed_rules.json"
DEFAULT_SOURCES_FILE = "config/sources.json"
DEFAULT_HOTNESS_FILE = "config/hotness.json"
DEFAULT_NOISE_FILE = "config/noise_rules.json"
DEFAULT_HN_HOT_QUERIES_FILE = "config/hn_hot_queries.json"
HN_SCORE_CAP = 500

DEFAULT_HOTNESS = {
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
    "same_domain_duplicate_bonus": 0,
    "max_bonus_per_source_family": 20,
    "news_min_score": 38,
    "news_min_confidence": 0.65,
    "radar_min_score": 20,
    "radar_max_items": 50,
    "match_time_window_hours": 48,
}

DEFAULT_SOURCES = {
    "source_weights": {},
    "newsapi": {
        "top_headlines_languages": ["en"],
        "top_headlines_category": None,
        "top_headlines_country": "us",
    },
    "sources": {
        "rss_sources": [],
    },
}

DEFAULT_NOISE_RULES = {
    "exclude_terms": [],
}

DEFAULT_HN_HOT_QUERIES = {
    "notes": [],
    "queries": [],
}

DEFAULT_RULES = {
    "interest_keywords": [],
    "source_weights": {},
    "newsapi": {
        "domains": [],
        "top_headlines_languages": [],
        "top_headlines_category": None,
    },
    "feeds": {
        "primary_bucket": "news",
        "active_profiles": [],
        "news_profiles": ["major_events", "devops_security", "tech_industry_major"],
        "topic_terms": [],
        "exclude_terms": [],
        "tech_terms": [],
        "world_terms": [],
        "watched_entities": [],
        "hn_news": {
            "direct_points": 800,
            "direct_comments": 300,
            "entity_points": 250,
            "entity_comments": 80,
        },
    },
    "topic_profiles": {},
    "queries": {
        "gdelt_terms": [],
        "newsapi_terms": [],
    },
    "sources": {
        "rss_sources": [],
    },
    "scoring": {
        "weights": {
            "freshness": 0.35,
            "source_quality": 0.25,
            "topic_relevance": 0.25,
            "engagement": 0.15
        },
        "freshness_half_life_hours": 18,
        "feed_min_topic_score": 0.18,
        "feed_min_interest_score": 0.12
    }
}


def load_env_file(path: str) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = deep_merge(base[key], value)
        else:
            merged[key] = value
    return merged


def load_rules_config(path: str) -> Dict[str, Any]:
    rules_path = Path(path)
    if not rules_path.exists():
        return DEFAULT_RULES
    loaded = json.loads(rules_path.read_text(encoding="utf-8"))
    return deep_merge(DEFAULT_RULES, loaded)


def load_json_config(path: str, defaults: Dict[str, Any]) -> Dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return defaults
    loaded = json.loads(config_path.read_text(encoding="utf-8"))
    return deep_merge(defaults, loaded)


def env_or_default(name: str, default: Any, cast: Any = str) -> Any:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return cast(raw)
