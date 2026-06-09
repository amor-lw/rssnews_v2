from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Set


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_published_guids(state_dir: Path) -> Set[str]:
    payload = load_json_file(state_dir / "published-guids.json", [])
    if not isinstance(payload, list):
        return set()
    return {str(item) for item in payload if item}


def save_published_guids(state_dir: Path, guids: Set[str]) -> None:
    save_json_file(state_dir / "published-guids.json", sorted(guids))


def load_daily_index(state_dir: Path) -> List[Dict[str, Any]]:
    payload = load_json_file(state_dir / "daily-index.json", [])
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def save_daily_index(state_dir: Path, entries: List[Dict[str, Any]]) -> None:
    save_json_file(state_dir / "daily-index.json", entries)
