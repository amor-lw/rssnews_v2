from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from app.helpers import canonicalize_url
from app.state_store import load_json_file


SOURCE_DELTA_LIMITS = (-0.25, 0.20)
DOMAIN_DELTA_LIMITS = (-0.35, 0.20)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def domain_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(canonicalize_url(url))
    except Exception:
        return ""
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def load_feedback_events(state_dir: Path) -> List[Dict[str, Any]]:
    payload = load_json_file(state_dir / "feedback.json", [])
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def build_feedback_preferences(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    source_deltas: Dict[str, float] = {}
    domain_deltas: Dict[str, float] = {}
    action_counts: Counter[str] = Counter()

    for event in events:
        action = str(event.get("action") or "").strip()
        source = str(event.get("source") or "").strip()
        domain = domain_from_url(str(event.get("url") or ""))
        action_counts[action] += 1

        if action == "keep":
            if source:
                source_deltas[source] = source_deltas.get(source, 0.0) + 0.05
            if domain:
                domain_deltas[domain] = domain_deltas.get(domain, 0.0) + 0.05
        elif action == "mute":
            if source:
                source_deltas[source] = source_deltas.get(source, 0.0) - 0.05
            if domain:
                domain_deltas[domain] = domain_deltas.get(domain, 0.0) - 0.08
        elif action == "mute-domain":
            if domain:
                domain_deltas[domain] = domain_deltas.get(domain, 0.0) - 0.20

    source_adjustments = {
        source: round(clamp(delta, *SOURCE_DELTA_LIMITS), 3)
        for source, delta in sorted(source_deltas.items())
        if abs(delta) > 0.0001
    }
    domain_adjustments = {
        domain: round(clamp(delta, *DOMAIN_DELTA_LIMITS), 3)
        for domain, delta in sorted(domain_deltas.items())
        if abs(delta) > 0.0001
    }

    return {
        "event_count": len(events),
        "action_counts": dict(sorted(action_counts.items())),
        "source_adjustments": source_adjustments,
        "domain_adjustments": domain_adjustments,
    }


def apply_feedback_to_source_weights(base_weights: Dict[str, float], preferences: Dict[str, Any]) -> Dict[str, float]:
    adjusted = dict(base_weights)
    source_adjustments = preferences.get("source_adjustments") or {}
    domain_adjustments = preferences.get("domain_adjustments") or {}

    if isinstance(source_adjustments, dict):
        for source, delta in source_adjustments.items():
            try:
                adjusted[str(source)] = round(clamp(float(adjusted.get(str(source), 0.60)) + float(delta), 0.0, 1.0), 3)
            except (TypeError, ValueError):
                continue

    if isinstance(domain_adjustments, dict):
        for domain, delta in domain_adjustments.items():
            try:
                adjusted[f"domain:{domain}"] = round(clamp(float(delta), *DOMAIN_DELTA_LIMITS), 3)
            except (TypeError, ValueError):
                continue

    return adjusted
